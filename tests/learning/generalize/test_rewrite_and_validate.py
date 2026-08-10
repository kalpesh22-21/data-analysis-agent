"""Unit coverage for the AST rewrite (slot/inline/rule roles) and the read-only /
dict-family static checks (D52). These exercise code paths the frozen fixture does
not (role=rule predicate drop; dict-family + star rejection)."""

from __future__ import annotations

import pytest

from data_agent.learning.generalize.builder import generalize_blueprint
from data_agent.learning.generalize.rewrite import RewriteError, rewrite_sql_to_template
from data_agent.learning.generalize.validate import check_read_only_select

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


def test_rule_role_drops_predicate_from_template():
    params = [
        {"locator": {"table": "payroll.payroll_fact", "column": "department", "value": "0420"},
         "role": "slot", "slot": {"name": "department"}},
        {"locator": {"table": "payroll.payroll_fact", "column": "record_type", "value": "EARNING"},
         "role": "rule", "rule_id": "rule.earning_record_type"},
    ]
    template = rewrite_sql_to_template(_SQL, params, strict=True)
    assert "{department}" in template
    assert "record_type" not in template  # the rule predicate is dropped from the template


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
