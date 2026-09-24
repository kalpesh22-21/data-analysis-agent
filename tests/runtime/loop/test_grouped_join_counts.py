"""A grouped lookup must not make employee-row counts require unique departments."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data_agent.runtime.loop.measurement import cardinality_probes, validate_join_cardinality

LOOKUP = "SELECT department_code, avg(annual_salary) AS dept_avg FROM hr.employee GROUP BY department_code"
SOURCE = "SELECT employee_code, department_code, annual_salary FROM hr.employee WHERE employee_status='A'"


def query(measure="count(*), countIf(e.annual_salary > d.dept_avg)", lookup=LOOKUP):
    return f"SELECT {measure} FROM ({SOURCE}) e JOIN ({lookup}) d ON e.department_code=d.department_code"


@pytest.mark.parametrize("cte", [False, True])
@pytest.mark.parametrize(
    "measure", ["count(*)", "count(1)", "countIf(e.annual_salary > d.dept_avg)"]
)
async def test_employee_row_counts_preserve_many_to_one_grain(cte, measure):
    sql = query(measure)
    if cte:
        sql = f"WITH averages AS ({LOOKUP}) " + sql.replace(f"({LOOKUP}) d", "averages d")
    dispatcher = AsyncMock()
    assert await validate_join_cardinality(sql, dispatcher, object()) is None
    dispatcher.dispatch.assert_not_called()


def test_renamed_group_key_and_using_are_supported():
    renamed = query()
    renamed = renamed.replace("SELECT department_code, avg", "SELECT department_code AS dept, avg")
    renamed = renamed.replace("d.department_code", "d.dept")
    assert cardinality_probes(renamed) == []
    assert (
        cardinality_probes(
            query().replace("ON e.department_code=d.department_code", "USING (department_code)")
        )
        == []
    )


@pytest.mark.parametrize(
    "lookup",
    [
        "SELECT department_code, annual_salary AS dept_avg FROM hr.employee",
        "SELECT department_code, employee_status, avg(annual_salary) AS dept_avg FROM hr.employee GROUP BY department_code, employee_status",
        LOOKUP + " WITH ROLLUP",
        LOOKUP + " WITH TOTALS",
        "SELECT department_code, avg(annual_salary) AS dept_avg, arrayJoin([1,2]) AS n FROM hr.employee GROUP BY department_code",
        "SELECT lower(department_code) AS department_code, avg(annual_salary) AS dept_avg FROM hr.employee GROUP BY department_code",
    ],
)
async def test_nonunique_or_unproven_lookups_still_block(lookup):
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = SimpleNamespace(status="ok", result_full={"rows": [[7, 3]]})
    assert await validate_join_cardinality(query(lookup=lookup), dispatcher, object())
    dispatcher.dispatch.assert_awaited()


async def test_summing_lookup_measure_still_checks_employee_side():
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = SimpleNamespace(status="ok", result_full={"rows": [[7, 3]]})
    error = await validate_join_cardinality(
        query("count(*), sum(d.dept_avg)"), dispatcher, object()
    )
    assert error and "duplicate" in error
    assert "e.department_code" in dispatcher.dispatch.call_args.args[1]["sql"]


async def test_payroll_leave_fanout_still_blocks():
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = SimpleNamespace(status="ok", result_full={"rows": [[6, 3]]})
    sql = "SELECT count(*), sum(p.amount) FROM hr.payroll p JOIN hr.accrual_events a ON p.employee_code=a.employee_code"
    assert await validate_join_cardinality(sql, dispatcher, object())


@pytest.mark.parametrize(
    "condition",
    [
        "NOT (e.department_code=d.department_code)",
        "e.department_code!=d.department_code",
    ],
)
def test_nonmandatory_equality_does_not_prove_a_lookup(condition):
    sql = query().replace("e.department_code=d.department_code", condition)
    try:
        probes = cardinality_probes(sql)
    except ValueError:
        return
    assert probes


def test_multiple_joins_do_not_inherit_two_relation_proof():
    sql = query() + " JOIN hr.accrual_events a ON a.employee_code=e.employee_code"
    assert cardinality_probes(sql)


def test_scoped_cte_resolution_does_not_use_shadowed_grouping():
    sql = f"WITH averages AS ({LOOKUP}) SELECT count(*) FROM ({SOURCE}) e JOIN (WITH averages AS (SELECT department_code, annual_salary AS dept_avg FROM hr.employee) SELECT * FROM averages) d ON e.department_code=d.department_code"
    assert cardinality_probes(sql)


@pytest.mark.parametrize("side", ["RIGHT", "FULL", "LEFT"])
def test_outer_join_count_star_keeps_unmatched_row_protection(side):
    with pytest.raises(ValueError, match="Outer-join count"):
        cardinality_probes(query().replace(" JOIN ", f" {side} JOIN "))


def test_complete_composite_group_key_preserves_counts():
    lookup = "SELECT department_code, employee_status, avg(annual_salary) AS dept_avg FROM hr.employee GROUP BY department_code, employee_status"
    sql = query(lookup=lookup).replace(
        "SELECT employee_code, department_code, annual_salary",
        "SELECT employee_code, department_code, employee_status, annual_salary",
    )
    sql += " AND e.employee_status=d.employee_status"
    assert cardinality_probes(sql) == []


def test_unsafe_aggregate_inside_grouped_lookup_still_gets_checked():
    lookup = "SELECT p.department_code, sum(p.amount) AS dept_avg FROM hr.payroll p JOIN hr.accrual_events a ON p.employee_code=a.employee_code GROUP BY p.department_code"
    assert cardinality_probes(query(lookup=lookup))
