"""Unit tests for dispatch/tool_dispatcher.py (Layer 1 — FakeMCPClient, no infra).

Includes the D5 injection-integrity invariant: the JWT/session_id reach the
FakeMCPClient transport boundary but never appear in the ToolResult.
"""

from __future__ import annotations

import dataclasses
import json

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_E = "dbpcm_warehouse.employee"

CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

SECRET_JWT = "eyJhbGciOi.super-secret-jwt-body.sig"
SESSION_ID = "sess-integrity-test"


def _credentials(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=scope)


def _result_to_scannable_json(result: ToolResult) -> str:
    payload = dataclasses.asdict(result)
    # frozenset isn't JSON-serializable; render provenance as a sorted list first.
    if payload.get("provenance") is not None:
        payload["provenance"] = sorted(payload["provenance"])
    return json.dumps(payload, default=str)


async def test_ok_result_never_carries_credentials() -> None:
    mcp_client = FakeMCPClient(
        scripted={
            "getTableSchema": [
                {
                    "database": "dbpcm_warehouse",
                    "table": "employee",
                    "columns": [{"name": "EmployeeCode", "type": "String", "comment": ""}],
                }
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )

    # Transport boundary DID receive the credentials.
    assert len(mcp_client.calls) == 1
    assert mcp_client.calls[0].jwt == SECRET_JWT
    assert mcp_client.calls[0].session_id == SESSION_ID

    # ToolResult NEVER carries them.
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
    assert SESSION_ID not in blob
    assert result.status == "ok"


async def test_denied_result_never_carries_credentials() -> None:
    mcp_client = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "denied")]}
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT Amount FROM payroll"}, _credentials()
    )

    assert result.status == "denied"
    assert result.error_code == "COLUMN_SCOPE_VIOLATION"
    assert result.retryable is False
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
    assert SESSION_ID not in blob


async def test_dispatch_captures_provenance_on_success() -> None:
    raw_result = {
        "columns": ["EmployeeCode", "Department"],
        "rows": [["E1", "Sales"]],
        "row_count": 1,
        "truncated": False,
    }
    mcp_client = FakeMCPClient(scripted={"runQuery": [dict(raw_result)]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    result = await dispatcher.dispatch(
        "runQuery",
        {"sql": "SELECT EmployeeCode, Department FROM employee"},
        _credentials(),
    )
    assert result.status == "ok"
    assert result.provenance == frozenset({(_E, "EmployeeCode"), (_E, "Department")})
    assert result.result_preview is not None
    assert result.result_preview.row_count == 1
    assert result.result_full == raw_result


async def test_preview_truncates_to_preview_row_count() -> None:
    rows = [[f"E{i}"] for i in range(50)]
    mcp_client = FakeMCPClient(
        scripted={
            "sampleRows": [
                {"columns": ["EmployeeCode"], "rows": rows, "row_count": 50, "truncated": False}
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG, preview_row_count=5)
    result = await dispatcher.dispatch(
        "sampleRows", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )
    assert result.result_preview.truncated is True
    assert len(result.result_preview.preview_rows) == 5


async def test_wide_get_table_schema_is_size_capped_with_a_marker() -> None:
    """Part 2 fix: a wide getTableSchema (100+ columns) must NOT be stored as one
    unbounded ~30k-token preview cell — it is size-capped to `max_tool_result_tokens`
    with a clear marker, and the stored preview stays a VALID, parseable dict the
    model can still read columns from."""
    wide_schema = {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "columns": [
            {"name": f"Column_{i}", "type": "String", "comment": "some descriptive comment"}
            for i in range(400)
        ],
    }
    mcp_client = FakeMCPClient(scripted={"getTableSchema": [wide_schema]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG, max_tool_result_tokens=500)

    result = await dispatcher.dispatch(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )

    assert result.status == "ok"
    assert result.result_preview is not None
    assert result.result_preview.truncated is True
    # The stored cell is the capped dict — still valid + parseable.
    capped = result.result_preview.preview_rows[0][0]
    assert isinstance(capped, dict)
    assert capped["database"] == "dbpcm_warehouse"
    assert capped["table"] == "employee"
    # Head preserved: leading columns kept, far fewer than the full 400.
    assert 0 < len(capped["columns"]) < 400
    assert capped["columns"][0]["name"] == "Column_0"
    # Marker names how many of how many were omitted.
    assert "_truncated" in capped
    assert "of 400 columns omitted" in capped["_truncated"]
    # Actually bounded (JSON estimate under a small multiple of the cap).
    assert len(json.dumps(capped)) // 4 <= 500 * 2

    # The FULL, un-capped result is still returned on result_full for the caller
    # (the preview cap bounds only the model-facing stored preview).
    assert len(result.result_full["columns"]) == 400


def test_dispatch_estimator_matches_budget_estimator() -> None:
    """Parity guard (NIT 3): dispatch's per-result cap estimator and the trail
    budget walk's estimator must measure token cost IDENTICALLY. They cannot share
    an import (dispatch/__init__ eagerly imports tool_dispatcher and budget.py
    transitively imports dispatch → cycle), so this pins the two copies together —
    a future tokenizer swap must update both."""
    from data_agent.runtime.context.budget import _estimate_tokens as budget_estimate
    from data_agent.runtime.dispatch.tool_dispatcher import _estimate_tokens as dispatch_estimate

    for text in ["", "a", "SELECT * FROM t", '{"columns": [{"name": "x"}]}' * 100, "x" * 30_000]:
        assert dispatch_estimate(text) == budget_estimate(text)


async def test_small_get_table_schema_is_unchanged_no_marker() -> None:
    """A normal-sized getTableSchema is byte-identical to before the cap existed:
    stored whole, truncated=False, no marker."""
    schema = {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "columns": [{"name": "EmployeeCode", "type": "String", "comment": ""}],
    }
    mcp_client = FakeMCPClient(scripted={"getTableSchema": [schema]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    result = await dispatcher.dispatch(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )
    assert result.result_preview.truncated is False
    stored = result.result_preview.preview_rows[0][0]
    assert stored == schema
    assert "_truncated" not in stored


async def test_observer_is_called_at_each_stage() -> None:
    events: list[tuple[str, dict]] = []

    def observer(event: str, payload: dict) -> None:
        events.append((event, payload))

    mcp_client = FakeMCPClient(scripted={"listDatabases": [[{"name": "dbpcm_warehouse"}]]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG, observer=observer)
    await dispatcher.dispatch("listDatabases", {}, _credentials())

    event_names = [name for name, _ in events]
    assert event_names == ["tool_dispatch_start", "tool_dispatch_ok"]


async def test_default_observer_is_noop_by_default() -> None:
    """The default observer must not raise or require configuration (Pass-A seam)."""
    mcp_client = FakeMCPClient(scripted={"listDatabases": [[{"name": "dbpcm_warehouse"}]]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    result = await dispatcher.dispatch("listDatabases", {}, _credentials())
    assert result.status == "ok"


async def test_column_scope_denial_surfaces_the_named_column() -> None:
    """For COLUMN_SCOPE_VIOLATION SPECIFICALLY, the denied ToolResult.user_message
    carries the MCP's author-controlled, column-naming detail (exc.message) so the
    model sees WHICH column it lacks and can self-correct on the live turn.
    Column names are catalog metadata (not PII / cell values, D25)."""
    scope_message = (
        "This query needs access to columns outside your permitted scope: "
        "employee.EmployeeStatus. You do not have access to those columns — "
        "remove them from the query, or ask the user to grant access."
    )
    mcp_client = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", scope_message)]}
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT EmployeeStatus FROM employee"}, _credentials()
    )

    assert result.status == "denied"
    assert result.error_code == "COLUMN_SCOPE_VIOLATION"
    assert result.retryable is False
    # The specific out-of-scope column NAME reaches the model.
    assert "employee.EmployeeStatus" in result.user_message
    assert result.user_message == scope_message
    # Still never leaks credentials.
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
    assert SESSION_ID not in blob


async def test_non_scope_denial_stays_generic_canned_message() -> None:
    """Regression guard for B4/D25: a NON-scope MCP error must NOT surface its raw
    exc.message — the model only ever sees the generic canned denial string. Here
    the raw message contains backend detail that must be suppressed."""
    raw_backend_detail = "Code: 47. DB::Exception: Unknown column secret_internal_col"
    mcp_client = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("CLICKHOUSE_QUERY_ERROR", raw_backend_detail)]}
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT bad FROM employee"}, _credentials()
    )

    assert result.status == "denied"
    assert result.error_code == "CLICKHOUSE_QUERY_ERROR"
    # Generic canned message only — the raw backend text must NOT leak.
    assert result.user_message == "That query didn't run correctly. Let me fix it and try again."
    assert "DB::Exception" not in result.user_message
    assert "secret_internal_col" not in result.user_message


async def test_raw_transport_exception_stays_generic_canned_message() -> None:
    """Regression guard for B4/D25: a RAW (non-MCPToolError) transport exception
    must never surface str(exc) — the model only sees the generic canned message."""
    raw_transport_detail = "ConnectionRefusedError: [Errno 61] to 10.0.0.5:8123"

    class _BoomClient(FakeMCPClient):
        async def call_tool(self, tool_name, args, *, jwt, session_id):
            raise RuntimeError(raw_transport_detail)

    dispatcher = ToolDispatcher(_BoomClient(), CATALOG)

    result = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())

    assert result.status == "error"
    assert result.error_code == "INTERNAL_TRANSPORT_ERROR"
    assert result.user_message == (
        "Something went wrong reaching the data warehouse. Please try again."
    )
    assert "ConnectionRefusedError" not in result.user_message
    assert "10.0.0.5" not in result.user_message


# --- D75 Wave 1b: catalog PROVIDER seam -------------------------------------


async def test_catalog_provider_resolved_per_dispatch() -> None:
    """A ToolDispatcher given an async catalog PROVIDER (not a fixed handle)
    resolves it with THIS turn's credentials before capturing provenance, so a
    runQuery over the resolved catalog yields DETERMINED provenance."""
    calls: list[RuntimeCredentials] = []

    async def _provider(credentials: RuntimeCredentials) -> CatalogHandle:
        calls.append(credentials)
        return CATALOG

    mcp_client = FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["EmployeeCode"], "rows": [["E1"]], "row_count": 1, "truncated": False}
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp_client, _provider)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": f"SELECT EmployeeCode FROM {_E}"}, _credentials()
    )

    # The provider was awaited with the turn's credentials (D5: used only to
    # resolve the handle, never surfaced on the result).
    assert len(calls) == 1
    assert calls[0].jwt == SECRET_JWT
    # Provenance was captured against the resolved catalog (determined, non-None).
    assert result.status == "ok"
    assert result.provenance == frozenset({(_E, "EmployeeCode")})
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
