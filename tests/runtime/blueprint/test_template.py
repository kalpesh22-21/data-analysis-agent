"""Layer-1: F1 sqlglot-AST typed-literal slot binding (runblueprint-design §3.3).

The injection matrix (the `D77-concept-never-in-sql` sibling): a hostile slot
value is always ClickHouse-escaped into an AST literal and can NEVER break out of
the literal; unknown slot / missing binding / extra binding / a template that does
not parse all fail CLOSED.
"""

from __future__ import annotations

import pytest
import sqlglot

from data_agent.runtime.blueprint.template import (
    TemplateBindError,
    assert_read_only_select,
    bind_template,
    contains_star,
    parse_template,
    referenced_slots,
)

# Every adversarial value must round-trip as a single string literal — the
# re-parsed AST has exactly one string literal whose value is the ORIGINAL text
# (proving no break-out), and the value never appears un-escaped in the SQL.
_HOSTILE = [
    "Warehouse'; DROP TABLE employee; --",
    "O'Brien",
    "back\\slash",
    "new\nline\tand\ttabs",
    "unicode é中文 \U0001f600",
    "'; SELECT * FROM secrets --",
    "a' OR '1'='1",
    "quote''doubled",
]


@pytest.mark.parametrize("value", _HOSTILE)
def test_hostile_slot_value_is_escaped_into_a_single_literal(value: str) -> None:
    sql = bind_template("SELECT x FROM t WHERE c = {slot}", {"slot": value})
    # Re-parse the RESULT — the bound value must be exactly one string literal
    # equal to the original text (so it never escaped the literal into SQL syntax).
    tree = sqlglot.parse_one(sql, dialect="clickhouse")
    literals = [
        node for node in tree.walk() if isinstance(node, sqlglot.exp.Literal) and node.args.get("is_string")
    ]
    assert len(literals) == 1
    assert literals[0].this == value
    # The whole result is still a SINGLE Select with the value contained ENTIRELY
    # inside that one string literal — the hostile text never became SQL syntax
    # (any "SELECT"/"DROP TABLE" text lives inside the escaped literal, not as a
    # statement). `parse_one` returning one Select node proves no break-out.
    assert isinstance(tree, sqlglot.exp.Select)


def test_typed_number_and_bool_literals() -> None:
    assert bind_template("SELECT x FROM t WHERE n = {n}", {"n": 42}) == (
        "SELECT x FROM t WHERE n = 42"
    )
    out = bind_template("SELECT x FROM t WHERE flag = {f}", {"f": True})
    assert "TRUE" in out.upper()


def test_list_binds_as_in_tuple_escaped() -> None:
    sql = bind_template("SELECT x FROM t WHERE code IN {codes}", {"codes": ["OT", "P'T"]})
    assert sql == "SELECT x FROM t WHERE code IN ('OT', 'P''T')"


def test_same_slot_referenced_twice_binds_both_sites() -> None:
    sql = bind_template(
        "SELECT x FROM t WHERE a = {d} OR b = {d}", {"d": "Ware"}
    )
    assert sql.count("'Ware'") == 2


def test_missing_binding_fails_closed() -> None:
    with pytest.raises(TemplateBindError):
        bind_template("SELECT x FROM t WHERE c = {slot}", {})


def test_extra_binding_fails_closed() -> None:
    with pytest.raises(TemplateBindError):
        bind_template("SELECT x FROM t WHERE c = {a}", {"a": "x", "b": "y"})


def test_empty_list_binding_fails_closed() -> None:
    with pytest.raises(TemplateBindError):
        bind_template("SELECT x FROM t WHERE c IN {codes}", {"codes": []})


def test_unbindable_value_type_fails_closed() -> None:
    with pytest.raises(TemplateBindError):
        bind_template("SELECT x FROM t WHERE c = {c}", {"c": {"nested": "dict"}})


def test_unparseable_template_fails_closed() -> None:
    with pytest.raises(TemplateBindError):
        bind_template("SELECT SELECT FROM WHERE {c}", {"c": "x"})


def test_referenced_slots_and_parse_helper() -> None:
    assert referenced_slots("SELECT {a}, {b} FROM t WHERE c = {a}") == {"a", "b"}
    tree = parse_template("SELECT x FROM t WHERE c = {slot}")
    assert isinstance(tree, sqlglot.exp.Expression)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_float_fails_closed(value: float) -> None:
    # FIX 4b: inf/-inf/nan render as bare identifiers under ClickHouse — never a
    # safe numeric literal. Fail-closed.
    with pytest.raises(TemplateBindError):
        bind_template("SELECT x FROM t WHERE n = {n}", {"n": value})


# -- statement-kind + star guards (FIX 1a / FIX 2) --------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE payroll",  # multi-statement Block
        "DROP TABLE payroll",
        "INSERT INTO t VALUES (1)",
        "ALTER TABLE t ADD COLUMN c Int",
    ],
)
def test_assert_read_only_select_rejects_non_select(sql: str) -> None:
    tree = parse_template(sql)
    with pytest.raises(TemplateBindError):
        assert_read_only_select(tree)


def test_assert_read_only_select_accepts_plain_and_union_selects() -> None:
    assert_read_only_select(parse_template("SELECT a FROM t"))
    assert_read_only_select(parse_template("SELECT a FROM t UNION ALL SELECT b FROM u"))


def test_contains_star_detects_top_level_and_subquery() -> None:
    assert contains_star(parse_template("SELECT * FROM t"))
    assert contains_star(parse_template("SELECT a FROM (SELECT * FROM t) x"))
    assert not contains_star(parse_template("SELECT a, b FROM t"))
