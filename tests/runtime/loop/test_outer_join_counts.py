"""Unmatched ClickHouse join defaults must not become phantom entities."""

from unittest.mock import AsyncMock

import pytest

from data_agent.runtime.loop.measurement import cardinality_probes, validate_join_cardinality


@pytest.mark.parametrize(
    "measure",
    [
        "count(e.employee_code)",
        "COUNT(DISTINCT e.employee_code)",
        "countDistinct(e.employee_code)",
        "uniqExact(e.employee_code)",
        "uniq(e.employee_code)",
        "uniqCombined64(e.employee_code)",
        "count(*)",
        "count(1)",
        "countDistinct(employee_code)",
        "count(coalesce(e.employee_code, ''))",
    ],
)
def test_unmatched_employee_counts_require_rewrite(measure):
    sql = f"SELECT d.department_code, {measure} FROM hr.department d LEFT JOIN hr.employee e ON d.department_code=e.department_code GROUP BY d.department_code"
    with pytest.raises(ValueError, match="Outer-join count"):
        cardinality_probes(sql)


@pytest.mark.parametrize(
    "join,measure",
    [
        ("RIGHT JOIN", "countDistinct(d.department_code)"),
        ("FULL JOIN", "countDistinct(e.employee_code)"),
        ("FULL JOIN", "countDistinct(d.department_code)"),
    ],
)
def test_other_outer_join_directions(join, measure):
    with pytest.raises(ValueError, match="Outer-join count"):
        cardinality_probes(
            f"SELECT {measure} FROM hr.department d {join} hr.employee e ON d.department_code=e.department_code"
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(DISTINCT d.department_code) FROM hr.department d LEFT JOIN hr.employee e ON d.department_code=e.department_code",
        "SELECT COUNT(DISTINCT e.employee_code) FROM hr.department d JOIN hr.employee e ON d.department_code=e.department_code",
        "SELECT d.department_code, coalesce(e.n, 0) FROM hr.department d LEFT JOIN (SELECT department_code, uniqExact(employee_code) n FROM hr.employee GROUP BY department_code) e ON d.department_code=e.department_code",
        "WITH counts AS (SELECT department_code, uniqExact(employee_code) n FROM hr.employee GROUP BY department_code) SELECT d.department_code, coalesce(e.n, 0) FROM hr.department d LEFT JOIN counts e ON d.department_code=e.department_code",
    ],
)
def test_preserved_side_inner_join_and_preaggregation_remain_valid(sql):
    cardinality_probes(sql)


async def test_rejection_precedes_any_warehouse_dispatch():
    dispatcher = AsyncMock()
    error = await validate_join_cardinality(
        "SELECT countDistinct(e.employee_code) FROM hr.department d LEFT JOIN hr.employee e ON d.department_code=e.department_code",
        dispatcher,
        object(),
    )
    assert error and "Aggregate the counted source before" in error
    dispatcher.dispatch.assert_not_called()
