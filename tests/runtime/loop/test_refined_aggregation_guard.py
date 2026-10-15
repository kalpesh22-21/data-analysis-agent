"""Safety and recovery regressions using synthetic catalog relations only."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data_agent.runtime.dispatch.denial_mapping import classify_denial
from data_agent.runtime.dispatch.sql_diagnostics import (
    aggregation_signature,
    decode_diagnostic,
    repeated_sql_failure,
)
from data_agent.runtime.loop.measurement import cardinality_probes, validate_join_cardinality
from tests.runtime.loop.test_repository_regressions import entry

SQL = "SELECT sum(e.salary) AS total FROM hr.employee e JOIN hr.payroll p ON e.id=p.employee_id"


@pytest.mark.parametrize(
    "rows", [[], [[None, None]], [[True, 1]], [[-1, 0]], [[1, 2]], [[1]], [[1, 1], [1, 1]]]
)
async def test_invalid_probe_results_are_not_duplicate_verdicts(rows):
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = SimpleNamespace(status="ok", result_full={"rows": rows})
    issue = await validate_join_cardinality(SQL, dispatcher, None)
    assert issue.code == "AGGREGATION_CHECK_UNAVAILABLE"
    assert not issue.retryable


@pytest.mark.parametrize(
    "code,retryable,expected",
    [
        ("EXECUTION_SERVICE_UNAVAILABLE", False, "AGGREGATION_CHECK_UNAVAILABLE"),
        ("COLUMN_SCOPE_VIOLATION", False, "AGGREGATION_CHECK_UNAVAILABLE"),
        ("SQL_UNSUPPORTED_CONSTRUCT", True, "AGGREGATION_CHECK_FAILED"),
    ],
)
async def test_failed_probes_preserve_recovery_policy(code, retryable, expected):
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = SimpleNamespace(
        status="error",
        result_full=None,
        error_code=code,
        retryable=retryable,
        denial_detail=None,
        user_message="private backend text",
    )
    issue = await validate_join_cardinality(SQL, dispatcher, None)
    assert issue.code == expected and issue.retryable == retryable
    assert "private backend text" not in issue.denial_detail
    assert "not proof of duplicate" in issue
    assert decode_diagnostic(issue.denial_detail)["cause"]["code"] == code


async def test_duplicate_keys_and_success_have_distinct_outcomes():
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = SimpleNamespace(status="ok", result_full={"rows": [[10, 3]]})
    issue = await validate_join_cardinality(SQL, dispatcher, None)
    assert issue.code == "AGGREGATION_DUPLICATE_KEYS" and issue.retryable
    assert "p.employee_id" in issue
    assert "ClickHouse SQL" in issue and "does not prove" in issue
    dispatcher.dispatch.return_value.result_full = {"rows": [[3, 3]]}
    assert await validate_join_cardinality(SQL, dispatcher, None) is None
    assert dispatcher.dispatch.await_count == 2  # No cached data-dependent proof.


async def test_grouped_sum_lookup_needs_no_data_probe():
    sql = "SELECT sum(e.salary) FROM hr.employee e JOIN (SELECT employee_id FROM hr.payroll GROUP BY employee_id) p ON e.id=p.employee_id"
    dispatcher = AsyncMock()
    assert await validate_join_cardinality(sql, dispatcher, None) is None
    dispatcher.dispatch.assert_not_called()


@pytest.mark.parametrize("suffix", [", month", " WITH ROLLUP"])
def test_partial_group_key_and_rollup_still_require_probe(suffix):
    sql = (
        "SELECT sum(e.salary) FROM hr.employee e JOIN (SELECT employee_id FROM hr.payroll GROUP BY employee_id"
        + suffix
        + ") p ON e.id=p.employee_id"
    )
    assert cardinality_probes(sql)


def test_semijoin_still_requires_no_probe():
    assert (
        cardinality_probes(
            "SELECT sum(e.salary) FROM hr.employee e WHERE e.id IN (SELECT employee_id FROM hr.payroll)"
        )
        == []
    )


@pytest.mark.parametrize("dtype,allowed", [("Nullable(Int64)", True), ("Int64", False)])
def test_outer_direct_count_uses_verified_nullable_type(dtype, allowed):
    sql = "SELECT count(p.employee_id) FROM hr.employee e LEFT JOIN hr.payroll p ON e.id=p.employee_id"
    schema = {"hr.employee": {"id": "Int64"}, "hr.payroll": {"employee_id": dtype}}
    if allowed:
        cardinality_probes(sql, schema=schema)
    else:
        with pytest.raises(ValueError, match="Outer-join count"):
            cardinality_probes(sql, schema=schema)


def test_coalesce_nullable_count_and_count_star_are_not_whitelisted():
    schema = {"hr.employee": {"id": "Int64"}, "hr.payroll": {"employee_id": "Nullable(Int64)"}}
    for measure in ["count(coalesce(p.employee_id, 0))", "count(*)"]:
        with pytest.raises(ValueError, match="Outer-join count"):
            cardinality_probes(
                f"SELECT {measure} FROM hr.employee e LEFT JOIN hr.payroll p ON e.id=p.employee_id",
                schema=schema,
            )


def test_cosmetic_guard_rewrites_stop_but_substantive_repairs_can_proceed():
    renamed = (
        SQL.replace("e.", "staff.")
        .replace("employee e", "employee staff")
        .replace("AS total", "AS value")
    )
    assert aggregation_signature(SQL) == aggregation_signature(renamed)
    trail = [
        replace(
            entry(str(n), "runQuery", status="error"),
            args={"sql": query},
            error_code="AGGREGATION_DUPLICATE_KEYS",
        )
        for n, query in enumerate([SQL, renamed])
    ]
    assert repeated_sql_failure(renamed + " ORDER BY value", trail, 0)
    assert not repeated_sql_failure(SQL + " WHERE p.employee_id=1", trail, 0)
    assert not repeated_sql_failure(SQL.replace("sum(e.salary)", "max(e.salary)"), trail, 0)
    assert not repeated_sql_failure(SQL, trail, 1)
    assert not repeated_sql_failure(SQL, trail, 0, scope_hash="different")


def test_unavailable_guard_is_not_retried_as_sql_repair():
    failure = replace(
        entry("x", "runQuery", status="error"),
        args={"sql": SQL},
        error_code="AGGREGATION_CHECK_UNAVAILABLE",
    )
    assert repeated_sql_failure(SQL, [failure], 0)
    assert not classify_denial(failure.error_code).retryable


async def test_internal_exception_is_logged_without_private_message(caplog):
    dispatcher = AsyncMock()
    dispatcher.dispatch.side_effect = RuntimeError("private token")
    issue = await validate_join_cardinality(SQL, dispatcher, None)
    assert issue.code == "AGGREGATION_CHECK_UNAVAILABLE"
    assert not issue.retryable
    assert "private token" not in caplog.text + issue.denial_detail
    assert "Aggregation verification" in caplog.text


@pytest.mark.parametrize("tool", ["runQuery", "runBlueprint"])
def test_unavailable_receipt_can_close_blocked_intent_without_extra_query(tool):
    from data_agent.runtime.composite.analysis_state import (
        classify_block_evidence,
        validate_completion_evidence,
    )

    failed = replace(entry("f", tool, status="error"), error_code="AGGREGATION_CHECK_UNAVAILABLE")
    assert classify_block_evidence("f", [failed], 0) == "EXECUTION_FAILED"
    assert validate_completion_evidence("f", [failed], 0)
    for code in [
        "AGGREGATION_UNVERIFIED",
        "AGGREGATION_DUPLICATE_KEYS",
        "AGGREGATION_CHECK_FAILED",
    ]:
        assert classify_block_evidence("f", [replace(failed, error_code=code)], 0) is None


async def test_live_loop_blocks_cosmetic_retry_without_third_probe():
    from tests.runtime.test_harness_improvements import batch, build, call, discovery, run

    sql = "SELECT sum(e.Salary) AS Total FROM hr.employee e JOIN hr.payments p ON e.Id=p.EmployeeId"
    renamed = (
        sql.replace("e.", "staff.")
        .replace("employee e", "employee staff")
        .replace("AS Total", "AS Sum")
    )
    probe = {"columns": ["row_count", "distinct_count"], "rows": [[4, 2]], "row_count": 1}
    loop, store, _, mcp, _ = build(
        [
            discovery(),
            batch(call("runQuery", "a", sql=sql)),
            batch(call("runQuery", "b", sql=renamed)),
            batch(call("runQuery", "c", sql=renamed + " ORDER BY Sum")),
            batch(
                call(
                    "finalizeAnswer",
                    "done",
                    answer="I could not verify a safe salary total from these results.",
                    tables=[],
                    capability_refs=[],
                    evidence=["c"],
                )
            ),
        ],
        rows=[probe, probe],
    )
    await run(loop, "Total employee salary for employees with payments")
    doc = await store.get_or_create_session("harness-test")
    outcomes = {e.tool_call_id: e for e in doc.tool_trail}
    assert outcomes["a"].error_code == "AGGREGATION_DUPLICATE_KEYS"
    assert outcomes["b"].error_code == "AGGREGATION_DUPLICATE_KEYS"
    assert outcomes["c"].error_code == "SQL_REPAIR_EXHAUSTED"
    assert len([c for c in mcp.calls if c.tool_name == "runQuery"]) == 2
    assert decode_diagnostic(outcomes["a"].denial_detail)["code"] == "AGGREGATION_DUPLICATE_KEYS"
