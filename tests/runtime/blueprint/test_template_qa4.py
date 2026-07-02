"""QA4 Layer-1: F1 slot-binding injection matrix BEYOND the shipped set (§3.3).

Adversarial extensions of `test_template.py`: recursive-placeholder values, values
that carry other slots' `{token}` / `:placeholder` syntax, values that are valid SQL
fragments, 1MB values, heterogeneous/hostile IN lists, zero-slot templates paired
with non-empty bindings (and vice-versa), a slot named the same as a CTE alias, and
DETERMINISM (same inputs → byte-identical SQL). Every case must either bind the value
as a single typed literal (never re-substituted, never break out) or fail CLOSED.

ADD-only; does not modify the reviewer-owned `test_template.py`.
"""

from __future__ import annotations

import pytest
import sqlglot

from data_agent.runtime.blueprint.template import (
    TemplateBindError,
    bind_template,
)


def _string_literals(sql: str) -> list[str]:
    tree = sqlglot.parse_one(sql, dialect="clickhouse")
    return [
        node.this
        for node in tree.walk()
        if isinstance(node, sqlglot.exp.Literal) and node.args.get("is_string")
    ]


def test_value_containing_another_slots_token_is_not_recursively_substituted() -> None:
    # `{b}` inside the value of `a` must stay the LITERAL text "{b}", never expand
    # to b's binding — the substitution runs on the TEMPLATE, never on values.
    sql = bind_template(
        "SELECT x FROM t WHERE a = {a} AND b = {b}", {"a": "{b}", "b": "HELLO"}
    )
    lits = _string_literals(sql)
    assert "{b}" in lits  # a's value survives verbatim as a literal
    assert "HELLO" in lits
    # "HELLO" appears exactly once (only as b's binding, not folded into a).
    assert sql.count("HELLO") == 1


def test_value_with_colon_placeholder_syntax_stays_a_literal() -> None:
    sql = bind_template("SELECT x FROM t WHERE a = {a}", {"a": ":b OR 1=1"})
    assert _string_literals(sql) == [":b OR 1=1"]


def test_valid_sql_fragment_value_is_contained_in_one_literal() -> None:
    sql = bind_template("SELECT x FROM t WHERE a = {a}", {"a": "1 OR 1=1"})
    tree = sqlglot.parse_one(sql, dialect="clickhouse")
    assert isinstance(tree, sqlglot.exp.Select)
    assert _string_literals(sql) == ["1 OR 1=1"]  # the fragment never became syntax


def test_one_megabyte_value_binds_and_is_deterministic() -> None:
    big = "A'" * 500_000  # ~1MB, embedded single-quotes to stress escaping
    o1 = bind_template("SELECT x FROM t WHERE a = {a}", {"a": big})
    o2 = bind_template("SELECT x FROM t WHERE a = {a}", {"a": big})
    assert o1 == o2  # determinism at scale
    assert _string_literals(o1) == [big]  # exactly one literal == the original text


def test_hostile_and_heterogeneous_in_list_each_element_escaped() -> None:
    codes = ["OT", "P'T", "x'); DROP TABLE t; --"]
    sql = bind_template("SELECT x FROM t WHERE c IN {codes}", {"codes": codes})
    # Every element round-trips as its own string literal, escaped, none breaking out.
    assert _string_literals(sql) == codes
    tree = sqlglot.parse_one(sql, dialect="clickhouse")
    assert isinstance(tree, sqlglot.exp.Select)


def test_zero_slot_template_with_non_empty_bindings_fails_closed() -> None:
    # A template that references NO slots but is handed bindings → extra-binding
    # fail-closed (never silently ignore a value the template does not use).
    with pytest.raises(TemplateBindError):
        bind_template("SELECT 1", {"x": "y"})


def test_template_with_slots_but_empty_bindings_fails_closed() -> None:
    with pytest.raises(TemplateBindError):
        bind_template("SELECT x FROM t WHERE a = {a}", {})


def test_slot_named_same_as_cte_alias_binds_only_the_brace_site() -> None:
    # CTE alias `dept` (no braces) must be untouched; only `{dept}` is a bind site.
    sql = bind_template(
        "WITH dept AS (SELECT 1 AS n) SELECT n FROM dept WHERE n = {dept}",
        {"dept": 5},
    )
    assert "WITH dept AS" in sql  # the CTE name survived
    assert sql.rstrip().endswith("= 5")  # the {dept} slot bound as a number literal


def test_duplicate_slot_reference_both_sites_bound_identically() -> None:
    sql = bind_template(
        "SELECT x FROM t WHERE a = {d} OR b = {d} OR c = {d}", {"d": "V'x"}
    )
    assert _string_literals(sql) == ["V'x", "V'x", "V'x"]  # same value at every site
    assert sql.count("'V''x'") == 3  # every site escaped identically


def test_boolean_value_binds_as_boolean_not_string() -> None:
    sql = bind_template("SELECT x FROM t WHERE f = {f}", {"f": False})
    assert _string_literals(sql) == []  # no string literal — it is a boolean
    assert "FALSE" in sql.upper()


def test_real_seed_template_binding_is_deterministic() -> None:
    tmpl = (
        "SELECT e.Department AS department, SUM(p.Amount) AS overtime_pay "
        "FROM dbpcm_warehouse.payroll AS p "
        "JOIN dbpcm_warehouse.employee AS e ON e.EmployeeCode = p.EmployeeCode "
        "WHERE p.RegisterType = 'EARN' AND e.Department = {department} "
        "AND p.PayPeriodEndDate = {pay_period} GROUP BY e.Department"
    )
    b = {"department": "Ware'house", "pay_period": "2026-05-31"}
    assert bind_template(tmpl, b) == bind_template(tmpl, b)
