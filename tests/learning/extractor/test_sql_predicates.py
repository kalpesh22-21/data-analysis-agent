"""S3-no-drop-totality — the literal-predicate enumerator (`sql_predicates`),
the sharp edge of the D97 totality check (matrix row 6; task item 4).

Every literal predicate the extractor must account for has to be ENUMERATED here,
because `_validate_totality` declines a candidate only for predicates this module
returns. A class it silently omits ⇒ an un-planned predicate slips through to a
VALID candidate = the D56 wrong-answer class (a silent dropped filter).

Design §4.1 scopes the totality set to: WHERE equality/`IN` literal predicates
AND constant JOIN-on predicates.

Scope (reconciled §4.1 ↔ D97): EVERY literal COMPARISON predicate —
`=`,`!=`,`<`,`<=`,`>`,`>=`,`IN`,`BETWEEN`,`LIKE`/`ILIKE` — found ANYWHERE (WHERE,
JOIN-`ON`, HAVING, nested sub-SELECTs). The `region='NA'` JOIN-on and the
`col = fn('literal')` value-side-fn regression guards (formerly strict-xfail
findings, now FIXED) live at the bottom.
"""

from __future__ import annotations

from data_agent.learning.extractor.sql_predicates import literal_predicates


def _cols(sql: str) -> set[str]:
    preds = literal_predicates(sql)
    assert preds is not None, f"expected parseable SQL: {sql!r}"
    return {p.column.lower() for p in preds}


# --- classes the enumerator MUST cover (design §4.1 in-scope) ---------------


def test_where_equality_is_enumerated():
    assert _cols("SELECT sum(x) FROM t WHERE region = 'NA'") == {"region"}


def test_where_function_on_column_is_enumerated():
    # toYear(pay_period) = 2025 → the underlying column pay_period is seen through.
    assert _cols("SELECT sum(x) FROM t WHERE toYear(pay_period) = 2025") == {"pay_period"}


def test_reversed_literal_equality_is_enumerated():
    # literal on the LEFT: 'NA' = region.
    assert _cols("SELECT x FROM t WHERE 'NA' = region") == {"region"}


def test_where_in_list_is_enumerated():
    preds = literal_predicates("SELECT x FROM t WHERE status IN ('a','b','c')")
    assert preds is not None
    assert [p.column for p in preds] == ["status"]
    assert preds[0].value == "a,b,c"


def test_and_or_nesting_enumerates_every_literal():
    # a=1 AND (b=2 OR c=3) — all three literal predicates enumerated.
    assert _cols("SELECT x FROM t WHERE a = '1' AND (b = '2' OR c = '3')") == {"a", "b", "c"}


def test_subquery_internal_predicate_is_enumerated():
    # Over-enumeration is the SAFE direction: a predicate inside a WHERE subquery
    # is reached by the whole-AST walk (conservative-decline class).
    assert "flag" in _cols("SELECT x FROM t WHERE id IN (SELECT id FROM u WHERE u.flag = 'Y')")


def test_multiple_where_predicates_all_enumerated():
    assert _cols(
        "SELECT sum(gross_pay) FROM payroll.payroll_fact "
        "WHERE department = '0420' AND toYear(pay_period) = 2025 "
        "AND record_type = 'EARNING' AND region = 'NA'"
    ) == {"department", "pay_period", "record_type", "region"}


# --- transport behavior -----------------------------------------------------


def test_unparseable_sql_returns_none_not_crash():
    assert literal_predicates("SELCT (( bad from") is None


def test_no_where_clause_returns_empty_list():
    assert literal_predicates("SELECT 1") == []


def test_deterministic_order():
    sql = "SELECT x FROM t WHERE a = '1' AND b = '2'"
    assert literal_predicates(sql) == literal_predicates(sql)


# --- NEWLY IN-SCOPE classes (reconciled §4.1 — the fix bundle) --------------
# Totality was expanded to cover HAVING + BETWEEN + all literal comparison
# operators across WHERE / JOIN-ON / HAVING. Each literal is now enumerated, so an
# omitting plan declines (see test_validation.py).


def test_having_literal_is_now_enumerated():
    preds = literal_predicates("SELECT dept, sum(x) s FROM t GROUP BY dept HAVING sum(pay) > 5")
    assert preds is not None
    assert ("pay", "5") in {(p.column, p.value) for p in preds}


def test_between_is_now_enumerated():
    preds = literal_predicates("SELECT x FROM t WHERE pay BETWEEN 100 AND 200")
    assert preds is not None
    assert [(p.column, p.value) for p in preds] == [("pay", "100,200")]


def test_not_equal_literal_is_enumerated():
    assert _cols("SELECT x FROM t WHERE status <> 'inactive'") == {"status"}


def test_greater_than_literal_is_enumerated():
    assert _cols("SELECT x FROM t WHERE age > 21") == {"age"}


def test_like_literal_is_enumerated():
    preds = literal_predicates("SELECT x FROM t WHERE name LIKE 'A%'")
    assert preds is not None
    assert [(p.column, p.value) for p in preds] == [("name", "A%")]


# --- LOW-1: per-predicate + table-qualifier enumeration ---------------------


def test_or_of_two_values_enumerates_each_predicate():
    # region='NA' OR region='EU' → TWO distinct predicates (one per value).
    preds = literal_predicates("SELECT x FROM t WHERE region = 'NA' OR region = 'EU'")
    assert preds is not None
    assert sorted(p.value for p in preds) == ["EU", "NA"]
    assert all(p.column == "region" for p in preds)


def test_qualified_tables_are_distinguished_by_table():
    # Same column name on two JOINed tables → distinct `table` qualifiers, so the
    # totality check can require a plan per (table, column, value).
    preds = literal_predicates(
        "SELECT x FROM a JOIN b ON a.k = b.k WHERE a.region = 'NA' AND b.region = 'EU'"
    )
    assert preds is not None
    by_table = {p.table: (p.column, p.value) for p in preds}
    assert by_table == {"a": ("region", "NA"), "b": ("region", "EU")}


# =====================================================================
# REGRESSION GUARDS — formerly strict-xfail silent-drop FINDINGS, now FIXED.
# The fix enumerates JOIN-ON literals + value-side-fn literals; these lock that in
# so a regression to a WHERE-only / column-side-only walk fails loudly.
# =====================================================================


def test_constant_join_on_predicate_is_enumerated():
    cols = _cols(
        "SELECT sum(pay) FROM db.fact f "
        "JOIN db.dim d ON f.dk = d.dk AND d.region = 'NA' "
        "WHERE f.dept = '0420'"
    )
    assert "region" in cols  # was MISSING (WHERE-only walk); now enumerated


def test_value_side_function_literal_is_enumerated():
    cols = _cols("SELECT x FROM t WHERE dept = '0420' AND pay_period = toDate('2025-01-01')")
    assert "pay_period" in cols  # was MISSING (column-side-only); now enumerated
