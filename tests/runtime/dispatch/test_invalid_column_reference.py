"""clickhouse-api's missing physical column error stays actionable through replay."""

import json
from dataclasses import replace

import pytest

from data_agent.runtime.dispatch.sql_diagnostics import (
    decode_diagnostic,
    encode_column_reference_diagnostic,
    repeated_sql_failure,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.mcp.real_client import _parse_tool_error_text
from tests.runtime.dispatch.test_tool_dispatcher import CATALOG, _credentials
from tests.runtime.loop.test_repository_regressions import entry
from tests.runtime.test_harness_improvements import Judge, batch, build, call, discovery, query, run

TABLE = "dbpcm_warehouse.personnel_action_form_changes"
SQL = f"SELECT p.field_id FROM {TABLE} p"
MESSAGE = (
    f"Column 'field_id' is not present in the physical catalog for {TABLE}. "
    "Check getTableSchema and use an available column; if semantic rules reference "
    "this column, report a catalog/schema mismatch. Restructuring the SQL will not "
    "add the missing column."
)


@pytest.mark.parametrize("tool", ["runQuery", "explainQuery"])
async def test_api_error_is_repairable_and_contains_no_result_rows(tool):
    code, message = _parse_tool_error_text(
        f"Error executing tool {tool}: [INVALID_COLUMN_REFERENCE] {MESSAGE}"
    )
    dispatcher = ToolDispatcher(
        FakeMCPClient(scripted={tool: [MCPToolError(code, message)]}), CATALOG
    )
    result = await dispatcher.dispatch(tool, {"sql": SQL}, _credentials())
    assert result.status == "denied"
    assert result.error_code == "INVALID_COLUMN_REFERENCE"
    assert result.retryable is True
    assert result.result_preview is None and result.result_full is None
    assert result.provenance is None
    diagnostic = decode_diagnostic(result.denial_detail)
    assert diagnostic["column"] == "field_id"
    assert diagnostic["tables"] == [TABLE]
    assert "getTableSchema" in result.user_message
    assert "catalog/schema mismatch" in result.user_message


async def test_missing_column_feedback_reaches_model_and_corrected_query_can_ship():
    judge = Judge()
    loop, _, model, _, _ = build(
        [
            discovery(),
            batch(call("runQuery", "bad", sql=SQL)),
            query(),
            batch(
                call(
                    "finalizeAnswer",
                    "answer",
                    answer="Sales has 120 employees.",
                    tables=[{"result_id": "q"}],
                    evidence=["q"],
                    capability_refs=[],
                )
            ),
        ],
        judge,
        rows=[
            MCPToolError("INVALID_COLUMN_REFERENCE", MESSAGE),
            {"columns": ["Department", "n"], "rows": [["Sales", 120]], "row_count": 1},
        ],
    )
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert len(out.answer_tables) == 1
    feedback = [
        json.loads(m["content"])
        for request in model.calls
        for m in request.messages
        if m.get("role") == "tool" and "INVALID_COLUMN_REFERENCE" in m.get("content", "")
    ]
    assert feedback
    assert feedback[0]["sql_diagnostic"]["column"] == "field_id"
    assert feedback[0]["result_preview"] is None
    assert not any(
        "result withheld: provenance" in json.dumps(request.messages) for request in model.calls
    )


def test_identical_failures_are_bounded_but_corrected_sql_is_allowed():
    trail = [
        replace(
            entry(str(i), "runQuery", status="denied", provenance=None),
            args={"sql": SQL},
            error_code="INVALID_COLUMN_REFERENCE",
            denial_detail=encode_column_reference_diagnostic(MESSAGE, SQL),
        )
        for i in range(2)
    ]
    assert repeated_sql_failure(SQL, trail, trail[0].turn_index)
    assert not repeated_sql_failure(
        SQL.replace("field_id", "field_label"), trail, trail[0].turn_index
    )
    assert not repeated_sql_failure(SQL, trail, trail[0].turn_index + 1)


@pytest.mark.parametrize(
    "message", ["secret raw backend text", MESSAGE.replace("field_id", "secret_column")]
)
def test_unrecognized_or_unrequested_diagnostic_details_do_not_leak(message):
    diagnostic = decode_diagnostic(encode_column_reference_diagnostic(message, SQL))
    assert "column" not in diagnostic
    assert "tables" not in diagnostic
    assert "secret" not in json.dumps(diagnostic)


def test_valid_column_diagnostic_does_not_copy_raw_suffix_or_unrequested_tables():
    message = MESSAGE.replace(TABLE, TABLE + ", private.secret_table") + " secret raw backend text"
    diagnostic = decode_diagnostic(encode_column_reference_diagnostic(message, SQL))
    assert diagnostic["column"] == "field_id"
    assert diagnostic["tables"] == [TABLE]
    assert "secret" not in json.dumps(diagnostic)
