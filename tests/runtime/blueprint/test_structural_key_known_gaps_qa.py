"""KNOWN cross-authoring-path DIVERGENCES of the `structural_key` (PriorArtIndex Slice 1).

Every test here asserts that two descriptions of the SAME query currently mint DIFFERENT
structural keys. That is a cross-tier prior-art MISS: the learning loop will re-propose a
blueprint the MCP canon already carries. All are ACCEPTED for Slice 1 — the key closes
the `resolves`/`uses_rules` gap that the frozen D48 key cannot, and these residuals are
the next tranche.

The point of writing them down as tests is that slice 2 discovers them from the SUITE
rather than from production. Each test is named `..._is_a_known_miss` and asserts the
CURRENT divergent behavior, so tightening the normalizer makes the test FAIL loudly and
forces a deliberate update rather than a silent semantic change.

Gaps (a)-(d) were identified by the builder's own audit and REMAIN open; the
grain-qualification one CONFIRMS the builder's claim and also remains open. QA
additionally found aggregate-function casing and SQL comments, which were NOT on the
builder's list — both have since been FIXED in `structural_ast_norm_one`, and their tests
now assert the agreement plus the boundary of the fix (camelCase names still preserved,
and neither fold leaked into the frozen recipe).

The remaining gaps are deliberately NOT being closed: an LLM prior-art judge lands in
slice 3 and carries recall, so the exact key becomes a cost-saver rather than the matcher.
Positional slot renaming in particular would close gap (c) at the price of a debatable
semantic change (two blueprints binding different columns in the same position would
collide).

MEASURED CROSS-TIER RATE: 8/10, not 10/10. The builder's original "10/10" twin was
CIRCULAR — it was constructed by applying the very transformation the fold inverts
(lowercasing the aggregates and the grain), so it could only ever match. Running the REAL
S4 generalizer over the canon templates gives 8/10. The two genuine misses are gap (f)
below, a rewriter gap, NOT a key gap — the key is doing the right thing with the templates
it is handed.
"""

from __future__ import annotations

from data_agent.runtime.blueprint.structural_key import (
    normalize_structural_grain,
    structural_key,
    structural_key_from_templates,
)

_GRAIN = ["Department"]


def _keys(sql_a: str, sql_b: str) -> tuple[str, str]:
    a = structural_key_from_templates(_GRAIN, sql_a)
    b = structural_key_from_templates(_GRAIN, sql_b)
    assert a and b, "both templates must normalize; this is a divergence test, not fail-soft"
    return a, b


# --- (a) role=rule predicates -------------------------------------------------


def test_a_rule_predicate_dropped_by_s4_is_a_known_miss() -> None:
    """GAP (a). `learning/generalize/rewrite.py:142` DROPS `role=rule` predicates from
    the generated `sql_template`, while a hand-authored canon blueprint INLINES the same
    predicate (e.g. the `active_employee` rule as `employee_status = 'A'`).

    So canon's template and the learning template for one query differ by a whole WHERE
    conjunct and mint different keys. This is the highest-likelihood miss of the four:
    `uses_rules` is exactly the concept the canon tier expresses inline, and 4 of the 10
    canon blueprints carry an inlined rule predicate.
    """
    canon_inlines_the_rule = (
        "SELECT department_name AS department, COUNT(DISTINCT employee_code) AS headcount "
        "FROM dbpcm_warehouse.employee "
        "WHERE employee_status = 'A' AND department_name = {department} "
        "GROUP BY department_name"
    )
    learning_drops_the_rule = (
        "SELECT department_name AS department, COUNT(DISTINCT employee_code) AS headcount "
        "FROM dbpcm_warehouse.employee "
        "WHERE department_name = {department} "
        "GROUP BY department_name"
    )
    a, b = _keys(canon_inlines_the_rule, learning_drops_the_rule)
    assert a != b


# --- (b) table aliases / column qualification ---------------------------------


def test_table_alias_and_column_qualification_is_a_known_miss() -> None:
    """GAP (b). `normalize_identifiers` case-folds identifiers but does NOT resolve
    aliases or strip/add qualification — that needs `qualify`, which requires a schema
    and chokes on slot placeholders (the recipe deliberately avoids it).

    So `db.employee AS e ... e.department_name` and the unaliased single-table spelling
    of the identical query mint different keys.
    """
    aliased = (
        "SELECT e.department_name AS department FROM dbpcm_warehouse.employee AS e "
        "WHERE e.employee_status = 'A'"
    )
    unaliased = (
        "SELECT department_name AS department FROM dbpcm_warehouse.employee "
        "WHERE employee_status = 'A'"
    )
    a, b = _keys(aliased, unaliased)
    assert a != b


def test_a_different_alias_letter_alone_is_a_known_miss() -> None:
    """The narrower form of (b): the SAME query, aliased `e` vs `emp`. Nothing about the
    query differs, yet the keys do."""
    a, b = _keys(
        "SELECT e.d AS department FROM db.employee AS e WHERE e.s = 1",
        "SELECT emp.d AS department FROM db.employee AS emp WHERE emp.s = 1",
    )
    assert a != b


# --- (c) slot naming ----------------------------------------------------------


def test_slot_naming_is_a_known_miss() -> None:
    """GAP (c). A `{slot}` renders into the canonical string as its NAME
    (`{department: }`), so the same bind site named `{department}` by canon and `{dept}`
    by the S4 generalizer mints different keys.

    Positional slot erasure (hashing `{?}` for every slot) would close this, at the cost
    of colliding two blueprints that bind different columns in the same position.
    """
    a, b = _keys(
        "SELECT a FROM db.t WHERE a = {department}",
        "SELECT a FROM db.t WHERE a = {dept}",
    )
    assert a != b


# --- (d) commutative operand order --------------------------------------------


def test_commutative_and_operand_order_is_a_known_miss() -> None:
    """GAP (d). `sqlglot.optimizer.normalize` canonicalizes boolean STRUCTURE (to CNF)
    but does NOT sort commutative operands, so `A AND B` and `B AND A` render
    differently and mint different keys."""
    a, b = _keys(
        "SELECT a FROM db.t WHERE x = 1 AND y = 2",
        "SELECT a FROM db.t WHERE y = 2 AND x = 1",
    )
    assert a != b


def test_commutative_or_operand_order_is_a_known_miss() -> None:
    a, b = _keys(
        "SELECT a FROM db.t WHERE x = 1 OR y = 2",
        "SELECT a FROM db.t WHERE y = 2 OR x = 1",
    )
    assert a != b


def test_predicate_side_order_is_a_known_miss() -> None:
    """The same class one level down: `x = 1` vs `1 = x` is the same predicate."""
    a, b = _keys("SELECT a FROM db.t WHERE x = 1", "SELECT a FROM db.t WHERE 1 = x")
    assert a != b


# --- (f) slots the S4 rewriter never parameterizes -----------------------------


def test_a_slot_outside_a_comparison_predicate_is_a_known_miss() -> None:
    """GAP (f) — the rewriter gap behind the real 8/10 cross-tier rate. DEFERRED TO
    SLICE 3; do not "fix" it here.

    `learning/generalize/rewrite.py::_find_literal` only searches `_COMPARISONS`
    (`EQ`/`NEQ`/`GT`/`GTE`/`LT`/`LTE`/`Like`/`ILike`/`In`). A literal that a canon author
    parameterized but that does NOT sit in a comparison predicate is therefore never
    turned into a `{slot}` — it stays INLINE in S4's template. The canon template has a
    placeholder where the learning template has a number, so the ASTs differ and the keys
    miss.

    Two real shapes in the canon corpus hit this: `INTERVAL {window_months} MONTH` and
    `COUNT(...) / {window_months}`. This is a rewriter limitation, not a key limitation —
    the structural key correctly reports that two structurally different templates are
    different. Closing it means teaching `_find_literal` about non-comparison literal
    positions, which is slice-3 work with its own correctness surface.
    """
    canon_parameterizes_it = (
        "SELECT COUNT(*) / {window_months} AS rate FROM db.t "
        "WHERE d > now() - INTERVAL {window_months} MONTH"
    )
    s4_leaves_it_inline = (
        "SELECT COUNT(*) / 6 AS rate FROM db.t WHERE d > now() - INTERVAL 6 MONTH"
    )
    a, b = _keys(canon_parameterizes_it, s4_leaves_it_inline)
    assert a != b


# --- grain qualification (CONFIRMING the builder's claim) ----------------------


def test_grain_column_qualification_is_a_known_miss() -> None:
    """CONFIRMS the builder's claim. `normalize_structural_grain` lowercases and sorts
    but does NOT strip table qualification, so a grain declared `["e.department"]` and
    one declared `["department"]` are different grains and mint different keys.

    The AST half of the digest already normalizes NOTHING about qualification either
    (gap b), so the two halves fail together rather than compensating.

    Grain entries are OUTPUT-COLUMN labels (`SELECT ... AS department`), which are never
    legitimately qualified — so stripping everything up to the last `.` would be safe
    and is the obvious slice-2 fix.
    """
    assert normalize_structural_grain(["e.department"]) == ["e.department"]
    assert normalize_structural_grain(["department"]) == ["department"]
    assert structural_key(["e.department"], "SELECT 1") != structural_key(
        ["department"], "SELECT 1"
    )


# --- FIXED: misses QA found that the structural render now closes ---------------


def test_aggregate_function_name_casing_now_agrees() -> None:
    """FIXED — this was the highest-likelihood miss QA found.

    `normalize_identifiers` folds IDENTIFIERS and never touches FUNCTION names, so
    `SUM(p.amount)` and `sum(p.amount)` used to mint different keys for the same query.
    That was not hypothetical: every one of the 10 MCP-canon blueprints writes its
    aggregates UPPERCASE while the frozen S4 fixture writes `sum(gross_pay)` lowercase,
    so the two tiers sat systematically on opposite sides of a fold that never happened —
    a miss on essentially every aggregate blueprint.

    `structural_ast_norm_one` now drops the parser's recorded spelling from every
    function node sqlglot RECOGNIZED, so the generator renders that function's canonical
    name. See `test_clickhouse_camelcase_function_names_are_still_preserved` for the
    other half of the rule."""
    a, b = _keys(
        "SELECT d AS department, SUM(amount) AS total FROM db.payroll GROUP BY d",
        "SELECT d AS department, sum(amount) AS total FROM db.payroll GROUP BY d",
    )
    assert a == b


def test_count_distinct_casing_now_agrees() -> None:
    """The same fix on the exact spelling `bp-active-headcount-by-department` uses."""
    a, b = _keys(
        "SELECT COUNT(DISTINCT employee_code) AS headcount FROM db.employee",
        "SELECT count(DISTINCT employee_code) AS headcount FROM db.employee",
    )
    assert a == b


def test_every_standard_aggregate_in_either_corpus_folds() -> None:
    """The aggregates actually present across the MCP canon and the S4 fixture."""
    for fn in ["SUM", "COUNT", "AVG", "MIN", "MAX", "ROUND"]:
        a, b = _keys(
            f"SELECT {fn}(a) AS v FROM db.t",
            f"SELECT {fn.lower()}(a) AS v FROM db.t",
        )
        assert a == b, fn


def test_clickhouse_camelcase_function_names_are_still_preserved() -> None:
    """The other half of the fold rule, and the reason it is not a blanket
    `normalize_functions="upper"`: ClickHouse camelCase function names are
    case-SENSITIVE and canonical as authored. `toFloat64` must NOT collapse into
    `tofloat64`.

    The fold's exclusion set is `_NAME_CARRYING_FUNCS`, which enumerates ALL THREE roots
    sqlglot uses for an unrecognized name — `Anonymous` (scalars), `AnonymousAggFunc`
    (aggregates, incl. `CombinedAggFunc`) and `ParameterizedAgg`. `exp.Anonymous` alone
    would NOT have covered the aggregate family; see
    `test_the_fold_guard_covers_every_name_carrying_node_class` in the fold QA suite."""
    a, b = _keys(
        "SELECT toFloat64(a) AS v FROM db.t",
        "SELECT tofloat64(a) AS v FROM db.t",
    )
    assert a != b


def test_a_sql_comment_no_longer_changes_the_key() -> None:
    """FIXED. Comments were PRESERVED through the recipe (a `--` line comment was even
    re-rendered as `/* ... */`), so an explanatory comment in a hand-authored canon
    template made that blueprint unmatchable against the learning tier. No canon template
    carries one today, but the YAMLs are hand-authored and commented elsewhere in the
    same files. `structural_ast_norm_one` now clears `comments` on every node."""
    a, b = _keys(
        "SELECT a FROM db.t",
        "-- headcount of active employees\nSELECT a FROM db.t",
    )
    assert a == b
    # Block and trailing comments too, not just leading line comments.
    a, b = _keys("SELECT a FROM db.t", "SELECT a /* the col */ FROM db.t -- trailing")
    assert a == b


def test_the_frozen_recipe_did_not_take_either_fold() -> None:
    """SAFETY: neither fix may leak into `canonical_ast_norm_one`. Its output is an input
    to the FROZEN D48 key whose digests are already persisted in the corpus bucket, so a
    change there orphans every stored artifact. The two renders MUST differ here."""
    from data_agent.runtime.blueprint.structural_key import (
        canonical_ast_norm_one,
        structural_ast_norm_one,
    )

    assert canonical_ast_norm_one("SELECT sum(a) FROM db.t") != canonical_ast_norm_one(
        "SELECT SUM(a) FROM db.t"
    )
    assert "sum(a)" in canonical_ast_norm_one("SELECT sum(a) FROM db.t")
    assert "SUM(a)" in structural_ast_norm_one("SELECT sum(a) FROM db.t")
    assert "/*" in canonical_ast_norm_one("-- c\nSELECT a FROM db.t")
    assert "/*" not in structural_ast_norm_one("-- c\nSELECT a FROM db.t")


def test_select_star_vs_explicit_projection_is_a_known_miss() -> None:
    """Expected and arguably correct (the recipe is schema-free so it cannot expand
    `*`), pinned for completeness of the divergence inventory."""
    a, b = _keys("SELECT * FROM db.t", "SELECT a, b FROM db.t")
    assert a != b


# --- what DOES normalize (the guardrail on the other side) --------------------


def test_whitespace_and_identifier_casing_do_normalize() -> None:
    """The complement of the misses above: these SHOULD collapse, and if one ever stops
    collapsing the key has regressed rather than merely stayed loose."""
    # Trailing newline / internal whitespace.
    a, b = _keys("SELECT a FROM db.t", "SELECT   a\n  FROM   db.t\n")
    assert a == b
    # Identifier case (this is what `normalize_identifiers` is for).
    a, b = _keys("SELECT a FROM db.t WHERE Col = 1", "SELECT A FROM DB.T WHERE col = 1")
    assert a == b
    # Keyword case.
    a, b = _keys("SELECT a FROM db.t WHERE x = 1", "select a from db.t where x = 1")
    assert a == b
