"""Column denials remain observable and cannot be evaded by SQL rewording."""

import json
from dataclasses import replace

import pytest

from data_agent.runtime.dispatch.denial_targets import denied_columns, repeated_target_denial
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from tests.runtime.dispatch.test_invalid_column_reference import MESSAGE, SQL, TABLE
from tests.runtime.loop.test_repository_regressions import entry
from tests.runtime.observability.test_tool_span_wiring_e2e import _tracer_with_memory_exporter
from tests.runtime.test_harness_improvements import (
    CATALOG,
    CREDS,
    batch,
    build,
    call,
    discovery,
    finish,
    query,
    run,
)


def denied(sql=SQL, code="INVALID_COLUMN_REFERENCE", detail=MESSAGE, tool="runQuery"):
    return replace(
        entry("d", tool, status="denied"),
        args={"sql": sql},
        error_code=code,
        denial_detail=detail,
        model_response={"scope_hash": "scope"},
    )


def test_reworded_same_column_is_bounded_but_corrected_query_is_allowed():
    second = f"SELECT field_id AS Changed FROM {TABLE} WHERE field_id IS NOT NULL"
    third = f"SELECT count(field_id) FROM {TABLE}"
    trail = [denied(), denied(second)]
    assert (
        repeated_target_denial("runQuery", {"sql": third}, trail, 0, "scope")
        == "INVALID_COLUMN_REFERENCE"
    )
    assert (
        repeated_target_denial(
            "runQuery", {"sql": third.replace("field_id", "field_label")}, trail, 0, "scope"
        )
        is None
    )
    assert repeated_target_denial("runQuery", {"sql": third}, trail, 1, "scope") is None
    assert repeated_target_denial("runQuery", {"sql": third}, trail, 0, "new-scope") is None
    assert repeated_target_denial("explainQuery", {"sql": third}, trail, 0, "scope") is None
    assert (
        repeated_target_denial(
            "runQuery", {"sql": third}, [*trail, replace(trail[0], status="ok")], 0, "scope"
        )
        is None
    )


def test_different_error_categories_do_not_combine():
    scope = f"This query needs access to columns outside your permitted scope: {TABLE}.field_id. You do not have access."
    trail = [denied(), denied(code="COLUMN_SCOPE_VIOLATION", detail=scope)]
    assert repeated_target_denial("runQuery", {"sql": SQL}, trail, 0, "scope") is None


def test_sample_rows_scope_denial_is_bounded_by_table_and_column():
    detail = f"This table has columns outside your permitted scope: {TABLE}.field_id. sampleRows cannot partially project columns."
    args = {"database": TABLE.split(".")[0], "table": TABLE.split(".")[1]}
    receipt = replace(
        denied(code="COLUMN_SCOPE_VIOLATION", detail=detail, tool="sampleRows"), args=args
    )
    assert (
        repeated_target_denial("sampleRows", args, [receipt, receipt], 0, "scope")
        == "COLUMN_SCOPE_VIOLATION"
    )
    assert (
        repeated_target_denial(
            "sampleRows", {**args, "table": "other"}, [receipt, receipt], 0, "scope"
        )
        is None
    )


def test_only_api_identifiers_present_in_call_are_telemetry_targets():
    assert denied_columns("INVALID_COLUMN_REFERENCE", MESSAGE, {"sql": SQL}) == {
        (TABLE, "field_id")
    }
    assert not denied_columns(
        "INVALID_COLUMN_REFERENCE", MESSAGE.replace("field_id", "secret_value"), {"sql": SQL}
    )
    assert not denied_columns("INTERNAL_ERROR", MESSAGE, {"sql": SQL})
    assert not denied_columns("INVALID_COLUMN_REFERENCE", MESSAGE, {"sql": "SELECT 'field_id'"})


@pytest.mark.parametrize("code", ["INVALID_COLUMN_REFERENCE", "COLUMN_SCOPE_VIOLATION"])
async def test_real_span_carries_column_event_without_error_prose_or_literal(code):
    tracer, exporter = _tracer_with_memory_exporter()
    message = (
        MESSAGE
        if code == "INVALID_COLUMN_REFERENCE"
        else f"This query needs access to columns outside your permitted scope: {TABLE}.field_id. You do not have access."
    )
    dispatcher = ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [MCPToolError(code, message + " PRIVATE_PROSE")]}),
        CATALOG,
        tracer=tracer,
    )
    await dispatcher.dispatch(
        "runQuery",
        {"sql": SQL + " WHERE p.field_id = 'PRIVATE_LITERAL'"},
        CREDS,
        tool_call_id="denied-call",
    )
    events = [
        event
        for span in exporter.get_finished_spans()
        for event in span.events
        if event.name == "tool.column_denial"
    ]
    assert len(events) == 1
    attrs = dict(events[0].attributes)
    assert list(attrs["columns"]) == [TABLE + ".field_id"]
    assert attrs["tool_call_id"] == "denied-call"
    assert "PRIVATE" not in json.dumps(attrs)
    assert CREDS.jwt not in json.dumps(attrs)


async def test_loop_blocks_third_reworded_denial_and_allows_corrected_read():
    queries = [
        SQL,
        f"SELECT field_id AS renamed FROM {TABLE}",
        f"SELECT field_id FROM {TABLE} LIMIT 1",
    ]
    steps = [
        discovery(),
        *[batch(call("runQuery", f"bad{i}", sql=sql)) for i, sql in enumerate(queries)],
        query(),
        batch(finish()),
    ]
    loop, store, model, mcp, events = build(
        steps,
        rows=[
            MCPToolError("INVALID_COLUMN_REFERENCE", MESSAGE),
            MCPToolError("INVALID_COLUMN_REFERENCE", MESSAGE),
            {"columns": ["Department", "n"], "rows": [["Sales", 120]], "row_count": 1},
        ],
    )
    await run(loop)
    trail = await store.load_trail(CREDS.session_id)
    blocked = next(e for e in trail if e.tool_call_id == "bad2")
    assert blocked.error_code == "SQL_REPAIR_EXHAUSTED"
    assert len(mcp.calls) == 3  # Two denied calls and the corrected read.
    assert any(event == "loop_semantic_denial_exhausted" for event, _ in events)
