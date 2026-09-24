"""Regressions for the live SQL guard audit, including unsafe counterexamples."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data_agent.runtime.loop.measurement import cardinality_probes, validate_join_cardinality

JOIN = "FROM hr.employee e JOIN hr.department d ON e.department_code=d.department_code"


@pytest.mark.parametrize("clause", ["HAVING sum(e.salary)>500000", "ORDER BY sum(e.salary) DESC"])
async def test_nonprojection_aggregates_block_fanout(clause):
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = SimpleNamespace(status="ok", result_full={"rows": [[6, 2]]})
    sql = f"SELECT e.department_code FROM hr.employee e JOIN hr.payroll p ON e.id=p.id GROUP BY e.department_code {clause}"
    assert "duplicate" in await validate_join_cardinality(sql, dispatcher, object())
    assert "p.id" in dispatcher.dispatch.call_args.args[1]["sql"]


@pytest.mark.parametrize(
    "measure",
    [
        "count(*)",
        "count(d.department_code)",
        "sumIf(e.salary,d.department_code='D01')",
        "sum(CASE WHEN d.department_code='D01' THEN e.salary ELSE 0 END)",
    ],
)
def test_measure_and_predicate_grains(measure):
    probes = cardinality_probes(f"SELECT {measure} {JOIN}")
    assert len(probes) == 1
    assert "FROM hr.department AS d" in probes[0]


@pytest.mark.parametrize(
    "condition",
    ["NOT(e.department_code=d.department_code)", "e.department_code!=d.department_code"],
)
def test_nonmandatory_equality_is_never_a_key(condition):
    with pytest.raises(ValueError, match="cardinality cannot"):
        cardinality_probes(
            f"SELECT sum(e.salary) FROM hr.employee e JOIN hr.department d ON {condition}"
        )


def test_or_filter_does_not_remove_mandatory_key():
    assert cardinality_probes(f"SELECT sum(e.salary) {JOIN} AND (d.name='A' OR d.name='B')")
    with pytest.raises(ValueError, match="Disjunctive"):
        cardinality_probes(f"SELECT sum(e.salary) {JOIN} OR e.name=d.name")


def test_filtered_probe_keeps_only_local_conjuncts():
    (probe,) = cardinality_probes(
        f"SELECT sum(e.salary) {JOIN} WHERE d.name='A' AND e.salary>0 AND e.name=d.name"
    )
    assert "d.name = 'A'" in probe
    assert "e.salary" not in probe and "e.name" not in probe


def test_nested_cte_binding_survives_probe():
    (probe,) = cardinality_probes(
        "SELECT * FROM (WITH dept AS (SELECT department_code FROM hr.department) SELECT sum(e.salary) FROM hr.employee e JOIN dept d ON e.department_code=d.department_code)"
    )
    assert "WITH dept AS" in probe


def test_cte_shadowing_preserves_outer_dependency_binding():
    (probe,) = cardinality_probes(
        "WITH original AS (SELECT department_code FROM hr.department), dept AS (SELECT * FROM original) SELECT * FROM (WITH original AS (SELECT department_code FROM hr.employee) SELECT sum(e.salary) FROM hr.employee e JOIN dept d ON e.department_code=d.department_code)"
    )
    assert probe.startswith("WITH original AS (SELECT department_code FROM hr.department)")
    assert "FROM (WITH original AS (SELECT department_code FROM hr.employee)" in probe


@pytest.mark.parametrize("literal,allowed", [("'D01'", True), ("''", False), ("0", False)])
def test_outer_count_requires_filter_excluding_defaults(literal, allowed):
    sql = f"SELECT count(d.department_code) {JOIN.replace(' JOIN ', ' LEFT JOIN ')} WHERE d.department_code={literal}"
    if allowed:
        assert cardinality_probes(sql, schema={"hr.department": {"department_code": "String"}})
    else:
        with pytest.raises(ValueError, match="Outer-join count"):
            cardinality_probes(sql)


def test_transformed_key_probe_checks_transformed_not_raw_values():
    (probe,) = cardinality_probes(
        "SELECT sum(e.salary) FROM hr.employee e JOIN hr.department d ON lower(e.department_code)=lower(d.department_code)"
    )
    assert "uniqExact((lower(d.department_code)))" in probe


def test_any_join_only_preserves_left_measure():
    assert (
        cardinality_probes(f"SELECT sum(e.salary) {JOIN.replace(' JOIN ', ' ANY INNER JOIN ')}")
        == []
    )
    assert cardinality_probes(f"SELECT sum(d.budget) {JOIN.replace(' JOIN ', ' ANY INNER JOIN ')}")


def test_unqualified_measure_needs_complete_unambiguous_schema():
    schema = {"hr.employee": {"salary": "Int64"}, "hr.department": {"budget": "Int64"}}
    assert cardinality_probes(f"SELECT sum(salary) {JOIN}", schema=schema)
    schema["hr.department"]["salary"] = "Int64"
    with pytest.raises(ValueError, match="Qualify"):
        cardinality_probes(f"SELECT sum(salary) {JOIN}", schema=schema)


@pytest.mark.parametrize("dtype", ["Enum8('D01'=1)", "Date", "Unknown"])
def test_unknown_or_nonzero_default_types_do_not_get_outer_exception(dtype):
    sql = f"SELECT count(d.department_code) {JOIN.replace(' JOIN ', ' LEFT JOIN ')} WHERE d.department_code='D01'"
    with pytest.raises(ValueError, match="Outer-join count"):
        cardinality_probes(sql, schema={"hr.department": {"department_code": dtype}})


def test_nondeterministic_filter_is_not_pushed_into_probe():
    (probe,) = cardinality_probes(f"SELECT sum(e.salary) {JOIN} WHERE d.budget>rand()")
    assert "rand" not in probe.lower()


def test_nondeterministic_key_cannot_certify_uniqueness():
    with pytest.raises(ValueError, match="cardinality cannot"):
        cardinality_probes(
            "SELECT sum(e.salary) FROM hr.employee e JOIN hr.department d ON e.salary=d.budget+rand()"
        )


async def test_transformed_key_collisions_still_reject():
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = SimpleNamespace(status="ok", result_full={"rows": [[2, 1]]})
    sql = "SELECT sum(e.salary) FROM hr.employee e JOIN hr.department d ON lower(e.department_code)=lower(d.department_code)"
    assert "duplicate" in await validate_join_cardinality(sql, dispatcher, object())
