"""Regression coverage for the difficult SQL probe harness faults."""

import json
from dataclasses import replace

import pytest

from data_agent.runtime.composite.analysis_state import (
    classify_block_evidence,
    validate_completion_evidence,
)
from data_agent.runtime.context.budget import render_entry
from data_agent.runtime.dispatch.sql_diagnostics import (
    decode_diagnostic,
    repeated_sql_failure,
    sql_diagnostic,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import _tool_trail_entry_to_canonical
from data_agent.runtime.loop.measurement import cardinality_probes
from data_agent.runtime.loop.turn_accumulators import TurnAccumulators
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from tests.runtime.loop.test_repository_regressions import entry
from tests.runtime.test_harness_improvements import CATALOG, CREDS, batch, build, call


async def test_engine_diagnostic_survives_persistence_and_canonical_replay_without_raw_text():
    raw = "UNKNOWN_FUNCTION: Function with name 'COUNTIF' does not exist. secret-row-value bearer secret"
    dispatcher = ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [MCPToolError("CLICKHOUSE_QUERY_ERROR", raw)]}), CATALOG
    )
    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT COUNTIF(Salary IS NULL) FROM hr.employee"}, CREDS
    )
    receipt = replace(
        entry("failed", "runQuery", status="denied", provenance=None),
        error_code=result.error_code,
        denial_detail=result.denial_detail,
    )
    receipt = type(receipt).from_doc(receipt.to_doc())
    rendered = render_entry(receipt, 5)
    assert rendered["sql_diagnostic"]["suggested_function"] == "countIf"
    assert "secret" not in json.dumps(rendered)
    canonical = _tool_trail_entry_to_canonical(rendered)
    text = json.dumps(canonical)
    assert "sql_diagnostic" in text and "failed" in text
    assert "secret" not in text
    assert "countIf" not in rendered["user_message"]


def test_unknown_error_text_is_not_forwarded():
    assert sql_diagnostic("secret arbitrary error", "SELECT 1")["engine_code"] == "QUERY_ERROR"
    assert "secret" not in str(sql_diagnostic("UNKNOWN_FUNCTION secret", "SELECT 1"))
    assert decode_diagnostic("sql_diagnostic:invalid") is None


def test_retry_detection_is_current_turn_and_literal_preserving():
    a = replace(
        entry("a", "runQuery", status="denied"),
        args={"sql": "SELECT Salary FROM hr.employee WHERE Department='Sales'"},
        error_code="CLICKHOUSE_QUERY_ERROR",
    )
    b = replace(a, tool_call_id="b")
    assert repeated_sql_failure(a.args["sql"] + " -- comment", [a, b], 0)
    assert not repeated_sql_failure(a.args["sql"].replace("Sales", "Engineering"), [a, b], 0)
    assert not repeated_sql_failure(a.args["sql"], [a, b], 1)
    assert not repeated_sql_failure(a.args["sql"], [a], 0)
    assert not repeated_sql_failure(
        a.args["sql"], [a, replace(b, error_code="DISALLOWED_KEYWORD")], 0
    )


def test_only_exhausted_execution_supports_execution_failed_not_completion():
    failed = replace(entry("f", "runQuery", status="error"), error_code="SQL_REPAIR_EXHAUSTED")
    assert classify_block_evidence("f", [failed], 0) == "EXECUTION_FAILED"
    assert validate_completion_evidence("f", [failed], 0)
    assert (
        classify_block_evidence("f", [replace(failed, error_code="CLICKHOUSE_QUERY_ERROR")], 0)
        is None
    )


def test_window_join_back_checks_derived_salary_level_uniqueness():
    sql = "WITH ranked AS (SELECT Department, Salary, dense_rank() OVER (PARTITION BY Department ORDER BY Salary DESC) r FROM hr.employee), levels AS (SELECT Department, Salary, r FROM ranked WHERE r <= 2) SELECT e.Id, l.r FROM hr.employee e JOIN levels l ON e.Department=l.Department AND e.Salary=l.Salary"
    with pytest.raises(ValueError, match="Rank-level joins"):
        cardinality_probes(sql)
    probes = cardinality_probes(
        sql.replace(
            "SELECT Department, Salary, r FROM ranked",
            "SELECT DISTINCT Department, Salary, r FROM ranked",
        )
    )
    assert len(probes) == 1
    assert "FROM levels AS l" in probes[0] and "l.Salary" in probes[0]
    assert (
        cardinality_probes(
            "SELECT Id FROM (SELECT Id, dense_rank() OVER (ORDER BY Salary DESC) r FROM hr.employee) WHERE r <= 2"
        )
        == []
    )


async def test_runtime_fallback_is_persisted_with_data_free_provenance():
    loop, store, *_ = build([])
    text = "I could not verify every requested part from the available evidence."
    result = await loop._finish(
        session_id=CREDS.session_id,
        turn_index=0,
        status="done",
        exit_label="answer_with_text",
        assistant_text=text,
        tool_calls_made=0,
        accum=TurnAccumulators(),
        provenance=None,
    )
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].content == result.assistant_text == text
    assert doc.messages[-1].provenance == frozenset()


async def test_third_failed_sql_is_not_dispatched():
    sql = "SELECT Salary FROM hr.employee"
    loop, store, model, mcp, *_ = build(
        [batch(call("searchBlueprints", "s", query="salary"))]
        + [batch(call("runQuery", f"q{i}", sql=sql)) for i in range(3)]
        + [
            batch(
                call(
                    "finalizeAnswer",
                    "f",
                    answer="I could not complete this calculation.",
                    tables=[],
                    capability_refs=[],
                    evidence=[],
                )
            )
        ],
        rows=[MCPToolError("CLICKHOUSE_QUERY_ERROR", "SYNTAX_ERROR") for _ in range(3)],
    )
    await loop.run(session_id=CREDS.session_id, credentials=CREDS, user_message="Show salary")
    trail = await store.load_trail(CREDS.session_id)
    assert next(e for e in trail if e.tool_call_id == "q2").error_code == "SQL_REPAIR_EXHAUSTED"


def test_retry_exhaustion_does_not_cross_scope_or_different_engine_errors():
    from data_agent.runtime.dispatch.sql_diagnostics import encode_diagnostic

    a = replace(
        entry("a", "runQuery", status="denied"),
        args={"sql": "SELECT Salary FROM hr.employee"},
        error_code="CLICKHOUSE_QUERY_ERROR",
        denial_detail=encode_diagnostic("UNKNOWN_IDENTIFIER", ""),
        model_response={"scope_hash": "old"},
    )
    b = replace(a, tool_call_id="b")
    assert not repeated_sql_failure(a.args["sql"], [a, b], 0, scope_hash="new")
    assert repeated_sql_failure(a.args["sql"], [a, b], 0, scope_hash="old")
    b = replace(b, denial_detail=encode_diagnostic("SYNTAX_ERROR", ""))
    assert not repeated_sql_failure(a.args["sql"], [a, b], 0, scope_hash="old")


async def test_exhaustion_receipt_closes_only_failed_intent_with_explicit_binding():
    steps = [
        batch(
            call("updateAnalysisState", "declare", intents=[{"description": "Salary calculation"}])
        ),
        batch(call("searchBlueprints", "s", query="salary")),
    ]
    steps += [
        batch(
            call("runQuery", f"q{i}", sql="SELECT Salary FROM hr.employee", serves_intents=["i1"])
        )
        for i in range(3)
    ]
    steps += [
        batch(
            call(
                "updateAnalysisState",
                "close",
                intents=[{"intent_id": "i1", "status": "blocked", "result_id": "q2"}],
            )
        ),
        batch(
            call(
                "finalizeAnswer",
                "f",
                answer="I could not complete the salary calculation.",
                tables=[],
                capability_refs=[],
                evidence=[],
            )
        ),
    ]
    loop, store, *_ = build(
        steps, rows=[MCPToolError("CLICKHOUSE_QUERY_ERROR", "SYNTAX_ERROR") for _ in range(2)]
    )
    result = await loop.run(
        session_id=CREDS.session_id, credentials=CREDS, user_message="Calculate salary"
    )
    doc = await store.get_or_create_session(CREDS.session_id)
    assert result.status == "done"
    intent = doc.analysis_state.intents[0]
    assert (intent.status, intent.reason_code, intent.evidence_tool_call_id) == (
        "blocked",
        "EXECUTION_FAILED",
        "q2",
    )


async def test_scope_filtered_history_keeps_runtime_fallback_but_not_unsupported_prose():
    from data_agent.runtime.session.models import TurnMessage
    from data_agent.runtime.session_history import project_history

    loop, store, *_ = build([])
    await store.append_message(
        CREDS.session_id, TurnMessage(turn_index=0, role="user", content="Salary?", ts="t")
    )
    text = "I could not verify every requested part from the available evidence."
    await loop._finish(
        session_id=CREDS.session_id,
        turn_index=0,
        status="done",
        exit_label="answer_with_text",
        assistant_text=text,
        tool_calls_made=0,
        accum=TurnAccumulators(),
        provenance=None,
    )
    doc = await store.get_or_create_session(CREDS.session_id)
    scope = frozenset({"hr.employee.Id"})
    assert project_history(doc.messages, [], scope, None)["turns"][0]["answer"] == text
    doc.messages[-1] = replace(doc.messages[-1], content="Salary is 999999.", provenance=None)
    assert project_history(doc.messages, [], scope, None)["turns"][0]["answer"] is None


def test_window_grain_extension_does_not_reject_unrelated_nonaggregate_joins():
    assert (
        cardinality_probes(
            "SELECT e.Id FROM hr.employee e JOIN hr.payments p ON e.Id=p.EmployeeId OR e.Id=1"
        )
        == []
    )


async def test_window_join_duplicate_probe_blocks_execution_and_reports_probe_errors():
    from data_agent.runtime.loop.measurement import validate_join_cardinality

    sql = "SELECT e.Id, r.r FROM hr.employee e JOIN (SELECT Id, Salary, dense_rank() OVER (ORDER BY Salary DESC) r FROM hr.employee) r ON e.Salary=r.Salary AND e.Id=r.Id"
    duplicate = ToolDispatcher(
        FakeMCPClient(
            scripted={
                "runQuery": [
                    {"columns": ["row_count", "distinct_count"], "rows": [[3, 2]], "row_count": 1}
                ]
            }
        ),
        CATALOG,
    )
    assert await validate_join_cardinality(sql, duplicate, CREDS)
    broken = ToolDispatcher(
        FakeMCPClient(
            scripted={
                "runQuery": [
                    MCPToolError("CLICKHOUSE_QUERY_ERROR", "UNKNOWN_IDENTIFIER secret-value")
                ]
            }
        ),
        CATALOG,
    )
    detail = await validate_join_cardinality(sql, broken, CREDS)
    assert "UNKNOWN_IDENTIFIER" in detail and "secret-value" not in detail
    assert "not proof of duplicate" in detail


async def test_data_free_denial_does_not_taint_successful_answer_provenance():
    from data_agent.runtime.session.models import ResultPreview

    loop, store, *_ = build([])
    success = entry("ok", "runQuery", provenance=frozenset({("hr.employee", "Salary")}))
    failure = replace(
        entry("bad", "runQuery", status="denied", provenance=None),
        error_code="CLICKHOUSE_QUERY_ERROR",
    )
    await store.append_trail_entry(CREDS.session_id, success)
    await store.append_trail_entry(CREDS.session_id, failure)
    assert await loop._compute_turn_provenance_union(CREDS.session_id, 0) == success.provenance
    # An error that did return partial data remains fail-closed.
    await store.append_trail_entry(
        CREDS.session_id,
        replace(
            failure,
            tool_call_id="partial",
            result_preview=ResultPreview(
                columns=["secret"], preview_rows=[[123]], row_count=1, truncated=False
            ),
        ),
    )
    assert await loop._compute_turn_provenance_union(CREDS.session_id, 0) is None


def test_cte_order_alias_provenance_does_not_depend_on_ast_visit_order():
    from data_agent.sqlparse import ProvenanceExtractionError, extract_column_provenance

    schema = {"hr.employee": {"Id": "String", "Department": "String", "Salary": "Int64"}}
    sql = "WITH ranked AS (SELECT Id, Department, Salary, dense_rank() OVER (PARTITION BY Department ORDER BY Salary DESC) r FROM hr.employee) SELECT Id, Department, Salary, r FROM ranked WHERE r<=2 ORDER BY Department, r, Salary DESC, Id"
    assert extract_column_provenance(sql, schema) == frozenset(
        {("hr.employee", "Id"), ("hr.employee", "Department"), ("hr.employee", "Salary")}
    )
    # A matching ORDER alias does not excuse an unresolved defining expression.
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(
            "SELECT Missing AS Department FROM hr.employee ORDER BY Department", schema
        )


def test_ranked_employees_can_join_department_directory_on_department():
    sql = "WITH ranked AS (SELECT Id, Department, Salary, dense_rank() OVER (PARTITION BY Department ORDER BY Salary DESC) r FROM hr.employee) SELECT e.Id, d.Name FROM ranked e JOIN hr.department d ON e.Department=d.Code WHERE e.r<=2"
    assert cardinality_probes(sql) == []
    assert (
        cardinality_probes(
            "SELECT e.Id, dense_rank() OVER (PARTITION BY e.Department ORDER BY e.Salary DESC) r FROM hr.employee e JOIN hr.department d ON e.Department=d.Code"
        )
        == []
    )


async def test_preflight_parse_error_reports_safe_location_not_an_aggregation_claim():
    from data_agent.runtime.loop.measurement import validate_join_cardinality

    dispatcher = ToolDispatcher(FakeMCPClient(), CATALOG)
    detail = await validate_join_cardinality(
        "SELECT SalaryFROM hr.employee WHERE Id='private-value'", dispatcher, CREDS
    )
    assert "SQL syntax" in detail and "No join-cardinality verdict" in detail
    assert "private-value" not in detail
