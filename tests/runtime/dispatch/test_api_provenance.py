"""Strict API contract tests use a raw client, outside scripted-fixture adaptation."""

import pytest

from data_agent.runtime.dispatch.sql_diagnostics import decode_diagnostic
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
from tests.runtime.dispatch.test_tool_dispatcher import _credentials


class RawAPI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call_tool(self, name, args, **kwargs):
        self.calls.append((name, args))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def response(columns=None):
    return {
        "columns": ["Total Pay"],
        "rows": [[123]],
        "row_count": 1,
        "provenance": {
            "version": 1,
            "columns": columns
            if columns is not None
            else [["hr.payroll", "GrossPay"], ["hr.payroll", "PayDate"]],
        },
    }


async def unavailable_catalog(_):
    raise AssertionError("Runtime must not fetch its catalog to rederive API provenance")


async def test_success_uses_api_receipt_despite_unavailable_agent_catalog(monkeypatch):
    import data_agent.sqlparse

    monkeypatch.setattr(
        data_agent.sqlparse,
        "extract_column_provenance",
        lambda *a, **k: pytest.fail("local reparse"),
    )
    client = RawAPI([response()])
    dispatcher = ToolDispatcher(client, unavailable_catalog)
    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT sum(GrossPay) FROM hr.payroll"}, _credentials()
    )
    assert result.status == "ok"
    assert result.provenance == frozenset({("hr.payroll", "GrossPay"), ("hr.payroll", "PayDate")})
    assert "provenance" not in result.result_full
    assert (
        await dispatcher.capture_sql_provenance(
            "SELECT sum(GrossPay) FROM hr.payroll", _credentials()
        )
        == result.provenance
    )
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"rows": [[123]]},
        {**response(), "provenance": {"version": 2, "columns": []}},
        {**response(), "provenance": {"version": 1, "columns": [None]}},
    ],
)
async def test_missing_receipt_becomes_actionable_failure_not_success_withheld(payload):
    result = await ToolDispatcher(RawAPI([payload]), unavailable_catalog).dispatch(
        "runQuery", {"sql": "SELECT 1"}, _credentials()
    )
    assert result.status != "ok" and result.error_code == "API_PROVENANCE_INVALID"
    assert result.result_full is None and result.result_preview is None
    assert "API" in result.denial_detail and "123" not in result.denial_detail
    assert result.retryable is False


async def test_api_scope_mismatch_is_explicit_failure():
    result = await ToolDispatcher(RawAPI([response()]), unavailable_catalog).dispatch(
        "runQuery", {"sql": "SELECT 1"}, _credentials(frozenset({"hr.payroll.GrossPay"}))
    )
    assert result.error_code == "API_PROVENANCE_INVALID"
    assert result.result_full is None


async def test_unexecuted_table_validates_at_api_and_preserves_error():
    client = RawAPI(
        [MCPToolError("CLICKHOUSE_QUERY_ERROR", "Code: 47. UNKNOWN_IDENTIFIER: column MissingPay")]
    )
    dispatcher = ToolDispatcher(client, unavailable_catalog)
    with pytest.raises(MCPToolError) as error:
        await dispatcher.capture_sql_provenance("SELECT MissingPay FROM hr.payroll", _credentials())
    assert error.value.code == "CLICKHOUSE_QUERY_ERROR"
    assert "MissingPay" in error.value.message
    assert client.calls[0][0] == "explainQuery"


async def test_real_sql_error_survives_with_repair_hint_and_no_credentials():
    credentials = _credentials()
    error = "Code: 43. ILLEGAL_TYPE_OF_ARGUMENT: sum cannot accept String " + credentials.jwt
    result = await ToolDispatcher(
        RawAPI([MCPToolError("CLICKHOUSE_QUERY_ERROR", error)]), unavailable_catalog
    ).dispatch("runQuery", {"sql": "SELECT sum(GrossPay) FROM hr.payroll"}, credentials)
    diagnostic = decode_diagnostic(result.denial_detail)
    assert "sum cannot accept String" in diagnostic["api_message"]
    assert credentials.jwt not in result.denial_detail
    assert diagnostic["engine_code"] == "ILLEGAL_TYPE_OF_ARGUMENT"
    assert result.result_full is None


async def test_unknown_internal_error_is_not_mistaken_for_a_recognized_sql_error():
    result = await ToolDispatcher(
        RawAPI([MCPToolError("UNKNOWN_INTERNAL_CODE", "private service internals")]),
        unavailable_catalog,
    ).dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())
    assert result.status != "ok"
    assert result.denial_detail is None
    assert "private service internals" not in result.user_message


async def test_receipt_cache_is_bound_to_credentials():
    from dataclasses import replace

    client = RawAPI([response(), response()])
    dispatcher = ToolDispatcher(client, unavailable_catalog)
    credentials = _credentials()
    sql = "SELECT sum(GrossPay) FROM hr.payroll"
    await dispatcher.dispatch("runQuery", {"sql": sql}, credentials)
    await dispatcher.capture_sql_provenance(sql, replace(credentials, jwt="new-token"))
    assert [name for name, _args in client.calls] == ["runQuery", "explainQuery"]
