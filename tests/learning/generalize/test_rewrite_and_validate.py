"""Unit coverage for the AST rewrite (slot/inline/rule roles) and the read-only /
dict-family static checks (D52). These exercise code paths the frozen fixture does
not (role=rule predicate drop; dict-family + star rejection)."""

from __future__ import annotations

import pytest

from data_agent.learning.generalize.builder import generalize_blueprint
from data_agent.learning.generalize.rewrite import RewriteError, rewrite_sql_to_template
from data_agent.learning.generalize.validate import check_read_only_select
from data_agent.runtime.blueprint.template import bind_template

from .helpers import CATALOG

_SQL = (
    "SELECT sum(gross_pay) AS t FROM payroll.payroll_fact "
    "WHERE department = '0420' AND record_type = 'EARNING'"
)


def test_slot_becomes_brace_placeholder_inline_literal_kept():
    params = [
        {"locator": {"table": "payroll.payroll_fact", "column": "department", "value": "0420"},
         "role": "slot", "slot": {"name": "department"}},
        {"locator": {"table": "payroll.payroll_fact", "column": "record_type", "value": "EARNING"},
         "role": "inline"},
    ]
    template = rewrite_sql_to_template(_SQL, params, strict=True)
    assert "department = {department}" in template
    assert "record_type = 'EARNING'" in template  # inline literal preserved


def test_rule_role_keeps_the_predicate_in_the_template():
    """WAS `test_rule_role_drops_predicate_from_template`, asserting the opposite.

    The drop rested on "the rule re-applies at runtime". It does not: a learned
    `uses_rules` entry is a bare catalog id string, `parse_rule` classifies every one as
    STATIC, and `executor._expand_rules` skips static rules — so the deleted filter was
    re-applied by nothing. The predicate stays in the template and the rule id is
    recorded beside it (`test_rule_id_lands_in_uses_rules`, unchanged below): kept AND
    annotated, instead of removed and annotated. See
    `test_rewrite_rule_role_keeps.py` for the full argument and the shape matrix."""
    params = [
        {"locator": {"table": "payroll.payroll_fact", "column": "department", "value": "0420"},
         "role": "slot", "slot": {"name": "department"}},
        {"locator": {"table": "payroll.payroll_fact", "column": "record_type", "value": "EARNING"},
         "role": "rule", "rule_id": "rule.earning_record_type"},
    ]
    template = rewrite_sql_to_template(_SQL, params, strict=True)
    assert "{department}" in template          # the slot still substitutes
    assert "record_type = 'EARNING'" in template  # the rule predicate is KEPT verbatim


def test_rule_id_lands_in_uses_rules():
    payload = {
        "kind": "single",
        "intent": "x",
        "source_tool_call_refs": ["tc1"],
        "parameterization": [
            {"locator": {"table": "payroll.payroll_fact", "column": "department", "value": "0420"},
             "role": "slot", "slot": {"name": "department", "binds_to": "payroll.payroll_fact.department"}},
            {"locator": {"table": "payroll.payroll_fact", "column": "record_type", "value": "EARNING"},
             "role": "rule", "rule_id": "rule.earning_record_type"},
        ],
        "result_signature": {"grain": {"columns": [], "verifiable": False}},
    }
    gen = generalize_blueprint(payload, {"tc1": _SQL}, CATALOG)
    assert gen.uses_rules == ("rule.earning_record_type",)


def test_missing_slot_literal_raises_in_strict_mode():
    params = [
        {"locator": {"table": "payroll.payroll_fact", "column": "department", "value": "NOPE"},
         "role": "slot", "slot": {"name": "department"}},
    ]
    try:
        rewrite_sql_to_template(_SQL, params, strict=True)
    except RewriteError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected RewriteError for an unlocatable slot literal")


def _numbers_horizon(value: str = "5", **overrides):
    locator = {
        "kind": "function_argument",
        "function": "numbers",
        "argument_index": 0,
        "occurrence": 0,
        "context": "table_source",
        "value": value,
    }
    locator.update(overrides)
    return {
        "locator": locator,
        "role": "slot",
        "slot": {"name": "forecast_months"},
    }


def test_numbers_table_source_horizon_becomes_slot():
    sql = "SELECT addMonths(today(), number) FROM numbers(5)"
    template = rewrite_sql_to_template(sql, [_numbers_horizon()], strict=True)
    assert "numbers({forecast_months})" in template


def test_interval_magnitude_becomes_relative_window_slot():
    sql = "SELECT today() - INTERVAL 5 YEAR AS cutoff"
    parameterization = [{
        "locator": {
            "kind": "interval_argument",
            "unit": "YEAR",
            "occurrence": 0,
            "context": "interval",
            "value": "5",
        },
        "role": "slot",
        "slot": {"name": "historical_years"},
    }]
    template = rewrite_sql_to_template(sql, parameterization, strict=True)
    assert "INTERVAL {historical_years} YEAR" in template


def test_interval_occurrence_does_not_slide_past_an_unsupported_site():
    sql = "SELECT today() - INTERVAL (1 + 1) YEAR, today() - INTERVAL 5 YEAR"
    parameterization = [{
        "locator": {
            "kind": "interval_argument", "unit": "YEAR", "occurrence": 0,
            "context": "interval", "value": "5",
        },
        "role": "slot", "slot": {"name": "historical_years"},
    }]
    with pytest.raises(RewriteError, match="not found"):
        rewrite_sql_to_template(sql, parameterization, strict=True)


def test_in_list_becomes_one_typed_list_slot_without_double_parentheses():
    sql = "SELECT department FROM payroll.payroll_fact WHERE department IN ('Sales','Finance')"
    parameterization = [{
        "locator": {
            "kind": "in_list", "table": "payroll.payroll_fact",
            "column": "department", "occurrence": 0,
            "context": "in_predicate", "value": "Sales,Finance",
        },
        "role": "slot", "slot": {"name": "departments"},
    }]

    template = rewrite_sql_to_template(sql, parameterization, strict=True)

    assert "department IN {departments}" in template
    assert "IN ({departments})" not in template
    assert "department IN ('Legal', 'HR')" in bind_template(
        template, {"departments": ["Legal", "HR"]}
    )


def test_in_list_occurrence_does_not_slide_to_a_later_matching_value():
    sql = (
        "SELECT department FROM payroll.payroll_fact "
        "WHERE department IN (lower('Sales')) OR department IN ('Sales','Finance')"
    )
    parameterization = [{
        "locator": {
            "kind": "in_list", "table": "payroll.payroll_fact",
            "column": "department", "occurrence": 0,
            "context": "in_predicate", "value": "Sales,Finance",
        },
        "role": "slot", "slot": {"name": "departments"},
    }]

    with pytest.raises(RewriteError, match="not found"):
        rewrite_sql_to_template(sql, parameterization, strict=True)


def test_limit_argument_becomes_a_positive_integer_slot():
    sql = "SELECT employee_code FROM dbpcm_warehouse.employee ORDER BY employee_code LIMIT 5"
    parameterization = [{
        "locator": {
            "kind": "limit_argument", "occurrence": 0,
            "context": "limit", "value": "5",
        },
        "role": "slot", "slot": {"name": "result_count"},
    }]

    template = rewrite_sql_to_template(sql, parameterization, strict=True)

    assert template.endswith("LIMIT {result_count}")
    assert bind_template(template, {"result_count": 3}).endswith("LIMIT 3")


def test_limit_occurrence_does_not_slide_past_an_expression_limit():
    sql = (
        "WITH unsupported AS (SELECT 1 LIMIT 2 + 3), "
        "supported AS (SELECT 2 LIMIT 5) SELECT * FROM supported"
    )
    parameterization = [{
        "locator": {
            "kind": "limit_argument", "occurrence": 0,
            "context": "limit", "value": "5",
        },
        "role": "slot", "slot": {"name": "result_count"},
    }]

    with pytest.raises(RewriteError, match="not found"):
        rewrite_sql_to_template(sql, parameterization, strict=True)


def test_function_argument_occurrence_is_structural_and_exact():
    sql = "SELECT * FROM numbers(3) a CROSS JOIN numbers(5) b"
    template = rewrite_sql_to_template(
        sql, [_numbers_horizon(occurrence=1)], strict=True
    )
    assert "numbers(3)" in template
    assert "numbers({forecast_months})" in template


@pytest.mark.parametrize(
    "sql, locator",
    [
        ("SELECT avg(5)", _numbers_horizon()),
        ("SELECT number FROM numbers(6)", _numbers_horizon()),
        ("SELECT number FROM numbers(5)", _numbers_horizon(function="range")),
        ("SELECT number FROM numbers(5)", _numbers_horizon(context="expression")),
    ],
)
def test_function_argument_locator_never_falls_back_to_an_unrelated_literal(sql, locator):
    with pytest.raises(RewriteError, match="not found"):
        rewrite_sql_to_template(sql, [locator], strict=True)


def test_numbers_horizon_generalizes_to_an_executable_blueprint_template():
    sql = (
        "WITH hires AS (SELECT employee_code FROM dbpcm_warehouse.employee "
        "WHERE employee_status != 'Not Hired'), future AS ("
        "SELECT addMonths(today(), number + 1) AS month FROM numbers(5)) "
        "SELECT month, count(employee_code) AS forecast_hires FROM future CROSS JOIN hires "
        "GROUP BY month"
    )
    payload = {
        "kind": "single",
        "intent": "forecast hires for the next N months",
        "source_tool_call_refs": ["tc1"],
        "parameterization": [
            _numbers_horizon(),
            {
                "locator": {
                    "table": "dbpcm_warehouse.employee",
                    "column": "employee_status",
                    "value": "Not Hired",
                },
                "role": "inline",
                "why": "defines the population",
            },
        ],
        "composes": [],
        "result_signature": None,
    }
    catalog = {
        "dbpcm_warehouse.employee": {
            "employee_code": "String",
            "employee_status": "String",
        }
    }
    generalized = generalize_blueprint(payload, {"tc1": sql}, catalog)
    assert "numbers({forecast_months})" in generalized.sql_template
    assert generalized.static_validation.outcome == "ok"


@pytest.mark.parametrize("name", [["department"], 7, {"n": "department"}, 1.5])
def test_non_string_slot_name_raises_rewrite_error_not_type_error(name):
    """A TRUTHY non-string `slot.name` passed the `if not name` guard and then died on
    the `"{" + name + ": }"` render with a `TypeError` — past the `except RewriteError`
    that is the only exception this module's callers catch. The module contract is that
    an un-rewritable plan raises `RewriteError`, so the type check belongs in the same
    guard as the emptiness one. (The builder's `_plan_params_ok` gates this field too;
    this keeps the contract true for every other caller.)"""
    params = [
        {"locator": {"table": "payroll.payroll_fact", "column": "department", "value": "0420"},
         "role": "slot", "slot": {"name": name}},
    ]
    with pytest.raises(RewriteError, match="slot.name"):
        rewrite_sql_to_template(_SQL, params, strict=True)


def test_read_only_rejects_star_and_dict_family():
    assert check_read_only_select("SELECT gross_pay FROM payroll.payroll_fact") is True
    assert check_read_only_select("SELECT * FROM payroll.payroll_fact") is False
    assert (
        check_read_only_select("SELECT dictGet('d', 'c', k) FROM payroll.payroll_fact")
        is False
    )
    # COUNT(*) is a read-only aggregate, not a projection star.
    assert check_read_only_select("SELECT count(*) FROM payroll.payroll_fact") is True
