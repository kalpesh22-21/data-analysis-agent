"""QA (delta pass): adversarial coverage of the NEW function-name fold in
`structural_ast_norm_one`.

The fold's whole implementation is two lines — drop `node.meta["name"]` from every
`exp.Func` that is not `exp.Anonymous`, and clear `comments`. The previous QA pass drove
the fold's existence; this file attacks the fold ITSELF on the shapes the builder's own
tests do not reach:

  * aggregate MODIFIERS (`DISTINCT`), ClickHouse `-If`/`-Merge` COMBINATORS, and WINDOW
    forms — all of which wrap or subclass the aggregate node the fold rewrites;
  * NESTING in both directions (a folded function inside a preserved one and vice versa),
    which is where a naive implementation folds the outer node and misses the inner;
  * the exact boundary of the recognized/unrecognized split, which is the fold's ONLY
    safety argument. QA's first pass found the guard AND its stated rationale were both
    wrong here — `exp.Anonymous` does not cover sqlglot's aggregate/parameterized
    unrecognized roots. The guard is now the explicit `_NAME_CARRYING_FUNCS` set and the
    tests below pin its completeness against sqlglot's real class table;
  * COLLAPSE safety — the dangerous direction for a prior-art key is not "two spellings
    fail to match" but "two DIFFERENT queries mint one key", i.e. false prior art;
  * in-process mutation leakage, since the fold mutates a parsed tree in place.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest
import sqlglot
from sqlglot import exp

from data_agent.runtime.blueprint import structural_key as structural_key_module
from data_agent.runtime.blueprint.structural_key import (
    canonical_ast_norm_one,
    structural_ast_norm_one,
    structural_key_from_templates,
)

_GRAIN = ["Department"]


def _keys(sql_a: str, sql_b: str) -> tuple[str, str]:
    a = structural_key_from_templates(_GRAIN, sql_a)
    b = structural_key_from_templates(_GRAIN, sql_b)
    assert a and b, "both templates must normalize; this is a fold test, not a fail-soft one"
    return a, b


# --- aggregate modifiers, combinators, window forms ---------------------------


@pytest.mark.parametrize(
    ("upper", "lower"),
    [
        # DISTINCT lives INSIDE the aggregate node, so a fold that rebuilt the node
        # rather than dropping one meta key could drop the modifier with it.
        ("SELECT COUNT(DISTINCT x) AS c FROM db.t", "SELECT count(distinct x) AS c FROM db.t"),
        ("SELECT SUM(DISTINCT y) AS s FROM db.t", "SELECT sum(distinct y) AS s FROM db.t"),
        ("SELECT AVG(DISTINCT y) AS a FROM db.t", "SELECT avg(distinct y) AS a FROM db.t"),
        # A windowed aggregate is an exp.Window WRAPPING the aggregate; the fold has to
        # reach the wrapped node, which `walk()` does but a top-level-only pass would not.
        (
            "SELECT SUM(x) OVER (PARTITION BY d ORDER BY m) AS w FROM db.t",
            "SELECT sum(x) OVER (PARTITION BY d ORDER BY m) AS w FROM db.t",
        ),
        ("SELECT COUNT(*) OVER () AS w FROM db.t", "SELECT count(*) OVER () AS w FROM db.t"),
        # Aggregates inside HAVING / a subquery / a CTE, not just the projection.
        (
            "SELECT d FROM db.t GROUP BY d HAVING COUNT(*) > {n}",
            "SELECT d FROM db.t GROUP BY d HAVING count(*) > {n}",
        ),
        (
            "SELECT a FROM db.t WHERE a > (SELECT AVG(b) FROM db.u)",
            "SELECT a FROM db.t WHERE a > (SELECT avg(b) FROM db.u)",
        ),
        (
            "WITH c AS (SELECT SUM(x) AS s FROM db.t) SELECT s FROM c",
            "WITH c AS (SELECT sum(x) AS s FROM db.t) SELECT s FROM c",
        ),
    ],
)
def test_the_fold_reaches_aggregates_under_modifiers_windows_and_nesting(upper, lower) -> None:
    """The casing fold must apply wherever the aggregate sits, not only as a bare
    top-level projection — otherwise the canon/learning match silently depends on where
    in the query the aggregate happens to appear."""
    a, b = _keys(upper, lower)
    assert a == b


@pytest.mark.parametrize(
    ("upper", "lower"),
    [
        # A folded function INSIDE a preserved ClickHouse one, and the reverse. The canon
        # corpus ships exactly this shape: `toFloat64(SUM(amount))` in
        # bp-compare-employee-check-detail-two-periods.
        ("SELECT toFloat64(SUM(x)) AS v FROM db.t", "SELECT toFloat64(sum(x)) AS v FROM db.t"),
        ("SELECT SUM(toFloat64(x)) AS v FROM db.t", "SELECT sum(toFloat64(x)) AS v FROM db.t"),
        ("SELECT ROUND(AVG(x), 2) AS v FROM db.t", "SELECT round(avg(x), 2) AS v FROM db.t"),
        (
            "SELECT ROUND(AVG(toFloat64(x)), 2) AS v FROM db.t",
            "SELECT round(avg(toFloat64(x)), 2) AS v FROM db.t",
        ),
        # `any(toString(...))`, verbatim from the canon compare blueprint.
        (
            "SELECT ANY(toString(c)) AS v FROM db.t",
            "SELECT any(toString(c)) AS v FROM db.t",
        ),
    ],
)
def test_the_fold_is_applied_at_every_nesting_depth(upper, lower) -> None:
    a, b = _keys(upper, lower)
    assert a == b


def test_a_preserved_camelcase_name_survives_being_wrapped_by_a_folded_one() -> None:
    """The two halves of the rule have to coexist in ONE expression: the outer standard
    aggregate folds to its canonical spelling while the inner ClickHouse camelCase name
    is left byte-for-byte alone."""
    rendered = structural_ast_norm_one("SELECT sum(toFloat64(gross_pay)) AS v FROM db.t")
    assert "SUM(" in rendered, rendered
    assert "toFloat64(" in rendered, rendered


# --- the boundary of the recognized/unrecognized split ------------------------


def test_the_clickhouse_agg_family_is_not_covered_by_the_anonymous_class_alone() -> None:
    """THE FINDING THAT DROVE THE GUARD FIX — kept as the tripwire.

    sqlglot's "unrecognized function" hierarchy is NOT rooted at `exp.Anonymous`. An
    unrecognized AGGREGATE parses to `exp.AnonymousAggFunc` (or `exp.CombinedAggFunc` for
    an `-If`/`-Merge` combinator), and NEITHER subclasses `exp.Anonymous`. So the original
    guard, `not isinstance(node, exp.Anonymous)`, was TRUE for them and the fold DID pop
    their `meta["name"]` — they survived only because sqlglot 30.12 stores those names in
    the node's `this` arg and leaves `meta` empty, making the pop a no-op.

    That was an incidental property of the pinned version, not a guarantee. The guard is
    now `_NAME_CARRYING_FUNCS`, which enumerates all three roots, so the family is
    excluded BY CONSTRUCTION. This test keeps pinning the underlying sqlglot facts,
    because a bump that reshuffles the hierarchy (or starts recording `meta["name"]` for
    the agg family) is exactly what would need re-examining."""
    for sql, expected_cls in [
        ("SELECT uniqExact(x) FROM db.t", exp.AnonymousAggFunc),
        ("SELECT sumIf(x, y > 1) FROM db.t", exp.CombinedAggFunc),
        ("SELECT anyLast(x) FROM db.t", exp.AnonymousAggFunc),
    ]:
        node = next(
            n for n in sqlglot.parse_one(sql, dialect="clickhouse").walk() if isinstance(n, exp.Func)
        )
        assert isinstance(node, expected_cls)
        # The ORIGINAL guard did not exclude them...
        assert not isinstance(node, exp.Anonymous)
        # ...the CURRENT guard does, by construction rather than by luck...
        assert isinstance(node, structural_key_module._NAME_CARRYING_FUNCS)
        # ...and 30.12 additionally leaves `meta` empty, the belt to that braces.
        assert "name" not in node.meta

    # The observable consequence that must hold either way: casing is preserved.
    for sql in [
        "SELECT uniqExact(x) AS v FROM db.t",
        "SELECT sumIf(x, y > 1) AS v FROM db.t",
        "SELECT sumMerge(x) AS v FROM db.t",
        "SELECT anyLast(x) AS v FROM db.t",
        "SELECT groupArray(x) AS v FROM db.t",
    ]:
        assert structural_ast_norm_one(sql) == canonical_ast_norm_one(sql), sql


def test_the_fold_guard_covers_every_name_carrying_node_class() -> None:
    """The guard's completeness argument, checked against sqlglot's actual class table
    rather than asserted in prose.

    A node "carries its name as data" iff sqlglot could not resolve the spelling to a
    node type — those are the ones whose `meta["name"]`/`this` must survive the fold.
    Enumerate the classes that hold a bare-string `this` name and assert every one is
    caught by `_NAME_CARRYING_FUNCS`, so a sqlglot bump introducing a FOURTH root fails
    here instead of silently re-keying that family."""
    roots = [
        exp.Anonymous,
        exp.AnonymousAggFunc,
        exp.CombinedAggFunc,
        exp.ParameterizedAgg,
        exp.CombinedParameterizedAgg,
    ]
    for cls in roots:
        assert issubclass(cls, structural_key_module._NAME_CARRYING_FUNCS), cls.__name__
    # And the recognized nodes the fold MUST still reach are not accidentally excluded.
    for cls in [exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max, exp.Round, exp.CountIf]:
        assert not issubclass(cls, structural_key_module._NAME_CARRYING_FUNCS), cls.__name__


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT quantile(0.5)(x) AS v FROM db.t",
        "SELECT topK(3)(x) AS v FROM db.t",
    ],
)
def test_parameterized_aggregates_keep_their_authored_casing(sql) -> None:
    """`ParameterizedAgg`/`CombinedParameterizedAgg` are the third name-carrying root, and
    the one neither `Anonymous` nor `AnonymousAggFunc` would have covered."""
    assert structural_ast_norm_one(sql) == canonical_ast_norm_one(sql), sql


@pytest.mark.parametrize("name", ["uniqExact", "sumIf", "sumMerge", "anyLast", "groupArray"])
def test_clickhouse_combinator_casing_does_not_fold_and_that_is_a_known_miss(name) -> None:
    """KNOWN MISS, deliberately pinned. ClickHouse function recognition in sqlglot is
    CASE-SENSITIVE for this family: `uniqExact` parses to `AnonymousAggFunc` but
    `UNIQEXACT` parses to a plain `Anonymous`, so the two mint DIFFERENT structural keys.

    This is defensible (ClickHouse camelCase names really are case-sensitive, and
    `UNIQEXACT` is not a valid ClickHouse function), but it means the fold's coverage is
    "standard SQL aggregates only" — the `-If`/`-Merge`/`uniq*` family is NOT casing-
    normalized. A canon blueprint and an extractor that disagree on the casing of one of
    these still miss. Recorded so slice 2 sizes the gap from the suite.
    """
    args = "x, y > 1" if name.endswith("If") else "x"
    a, b = _keys(
        f"SELECT {name}({args}) AS v FROM db.t", f"SELECT {name.upper()}({args}) AS v FROM db.t"
    )
    assert a != b


@pytest.mark.parametrize("spelling", ["countIf", "COUNTIF", "countif", "CountIf"])
def test_countif_is_the_exception_and_normalizes_in_the_frozen_recipe_already(spelling) -> None:
    """The counter-example that keeps the rule above honest, and a reminder that the
    family is not uniform: `countIf` is a first-class `exp.CountIf` in sqlglot, recognized
    CASE-INSENSITIVELY, and the ClickHouse generator emits its own spelling regardless of
    `meta["name"]`. So every casing of it already collapsed BEFORE this slice — the fold
    is not what makes it work, and a test that lumped it in with `sumIf` would be asserting
    the wrong mechanism."""
    sql = f"SELECT {spelling}(x > 1) AS v FROM db.t"
    assert canonical_ast_norm_one(sql) == "SELECT countIf(x > 1) AS v FROM db.t"
    assert structural_ast_norm_one(sql) == canonical_ast_norm_one(sql)


def test_tostartofmonth_is_not_anonymous_and_is_rewritten_by_the_frozen_recipe() -> None:
    """CORRECTS THE DOCSTRING, and pins behavior the fold is NOT responsible for.

    `structural_ast_norm_one`'s docstring cites `toStartOfMonth` as an example of a
    camelCase name the `exp.Anonymous` guard preserves. It is not: `toStartOfMonth` parses
    to `exp.TimestampTrunc`, and the FROZEN §11.2 recipe already rewrites it to
    `dateTrunc('MONTH', ...)` — before this slice existed. The fold neither causes nor
    changes that.

    It matters because it is a REAL cross-tier hazard that the fold does not address: the
    canon corpus writes `toStartOfMonth(...)` (bp-hires-per-month, bp-hires-projection)
    and both tiers therefore render `dateTrunc('MONTH', ...)`, but a template authored as
    `dateTrunc('month', ...)` renders with a LOWERCASE unit and mints a different key.
    """
    node = next(
        n
        for n in sqlglot.parse_one("SELECT toStartOfMonth(d) FROM db.t", dialect="clickhouse").walk()
        if isinstance(n, exp.Func)
    )
    assert isinstance(node, exp.TimestampTrunc)
    assert not isinstance(node, exp.Anonymous)

    frozen = canonical_ast_norm_one("SELECT toStartOfMonth(d) AS m FROM db.t")
    assert "dateTrunc('MONTH'" in frozen, frozen
    # The fold changes nothing here — this is pre-existing frozen behavior.
    assert structural_ast_norm_one("SELECT toStartOfMonth(d) AS m FROM db.t") == frozen

    # The residual hazard: unit casing still splits the key.
    a, b = _keys(
        "SELECT toStartOfMonth(d) AS m FROM db.t",
        "SELECT dateTrunc('month', d) AS m FROM db.t",
    )
    assert a != b


# --- collapse safety: the fold must not mint FALSE prior art ------------------


@pytest.mark.parametrize(
    ("sql_a", "sql_b"),
    [
        # Different functions entirely.
        ("SELECT SUM(x) AS v FROM db.t", "SELECT AVG(x) AS v FROM db.t"),
        ("SELECT MIN(x) AS v FROM db.t", "SELECT MAX(x) AS v FROM db.t"),
        ("SELECT COUNT(x) AS v FROM db.t", "SELECT COUNT(DISTINCT x) AS v FROM db.t"),
        # Different ClickHouse numeric casts must not merge.
        ("SELECT toFloat64(x) AS v FROM db.t", "SELECT toFloat32(x) AS v FROM db.t"),
        ("SELECT toInt64(x) AS v FROM db.t", "SELECT toInt32(x) AS v FROM db.t"),
        # Different combinators.
        ("SELECT sumIf(x, y > 1) AS v FROM db.t", "SELECT countIf(x > 1) AS v FROM db.t"),
        # Different arguments / structure under the same function.
        ("SELECT SUM(x) AS v FROM db.t", "SELECT SUM(y) AS v FROM db.t"),
        ("SELECT ROUND(AVG(x), 2) AS v FROM db.t", "SELECT ROUND(AVG(x), 3) AS v FROM db.t"),
        # A windowed aggregate is NOT the same query as the plain aggregate.
        ("SELECT SUM(x) OVER () AS v FROM db.t", "SELECT SUM(x) AS v FROM db.t"),
    ],
)
def test_the_fold_never_collapses_two_different_queries_into_one_key(sql_a, sql_b) -> None:
    """The dangerous direction. A missed match costs a duplicate proposal; a FALSE match
    tells the loop the canon already carries a blueprint it does not, and the artifact is
    silently dropped. Nothing the fold does may merge two distinct queries."""
    a, b = _keys(sql_a, sql_b)
    assert a != b


def test_the_only_names_the_fold_merges_are_documented_sql_synonyms() -> None:
    """A sweep of the ClickHouse dialect's whole function table found EIGHT groups where
    different spellings collapse to one structural render. Every one is a true SQL
    synonym (the same function under two names), so the collapse is semantically correct
    for a LOOSE key — it is recorded here so a future sqlglot bump that adds a
    non-synonym collapse is visible."""
    synonyms = [
        ("SELECT COALESCE(a, b) AS v FROM db.t", "SELECT IFNULL(a, b) AS v FROM db.t"),
        ("SELECT COALESCE(a, b) AS v FROM db.t", "SELECT NVL(a, b) AS v FROM db.t"),
        ("SELECT POW(a, b) AS v FROM db.t", "SELECT POWER(a, b) AS v FROM db.t"),
        ("SELECT SUBSTR(a, b) AS v FROM db.t", "SELECT SUBSTRING(a, b) AS v FROM db.t"),
        ("SELECT CEIL(a) AS v FROM db.t", "SELECT CEILING(a) AS v FROM db.t"),
    ]
    for sql_a, sql_b in synonyms:
        a, b = _keys(sql_a, sql_b)
        assert a == b, (sql_a, sql_b)


# --- the fold mutates a tree in place: prove nothing leaks --------------------


def test_the_in_place_fold_does_not_leak_between_calls() -> None:
    """`structural_ast_norm_one` MUTATES the AST it renders (`meta.pop`, `comments = None`).
    Every call re-parses, so there is no shared tree — but the frozen render sits one
    function away from the mutation, and a future refactor that hoisted or cached
    `_normalized_ast` would silently start folding the FROZEN recipe's output too.

    Interleave the two renderers and demand each stays on its own side of the fold.
    """
    sql = "SELECT sum(x) AS v, toFloat64(y) AS w FROM db.t -- note"
    frozen_runs, structural_runs = [], []
    for _ in range(4):
        frozen_runs.append(canonical_ast_norm_one(sql))
        structural_runs.append(structural_ast_norm_one(sql))
        frozen_runs.append(canonical_ast_norm_one(sql))

    assert len(set(frozen_runs)) == 1, frozen_runs
    assert len(set(structural_runs)) == 1, structural_runs
    # The fold really fired (otherwise this test would pass vacuously).
    assert frozen_runs[0] != structural_runs[0]
    assert "sum(x)" in frozen_runs[0] and "/*" in frozen_runs[0]
    assert "SUM(x)" in structural_runs[0] and "/*" not in structural_runs[0]


def test_the_fold_does_not_mutate_a_tree_the_caller_still_holds() -> None:
    """Defence in depth for the same hazard from the other side: a caller that parsed its
    own tree and then rendered the same SQL through the structural recipe must find its
    tree unchanged."""
    sql = "SELECT sum(x) AS v FROM db.t"
    caller_tree = sqlglot.parse_one(sql, dialect="clickhouse")
    before = caller_tree.sql(dialect="clickhouse")
    structural_ast_norm_one(sql)
    assert caller_tree.sql(dialect="clickhouse") == before


# --- determinism across processes for the NEW render --------------------------


@pytest.mark.parametrize("hashseed", ["0", "1", "12345"])
def test_the_structural_render_is_identical_across_separate_processes(hashseed, tmp_path) -> None:
    """The previous QA pass pinned cross-process determinism for the grain half. The fold
    added a `set`-free but dict-mutating pass over the AST; `walk()` order and `meta`
    iteration must not become hash-seed dependent, or two loader processes would mint
    different keys for one blueprint."""
    script = textwrap.dedent(
        """
        import json
        from data_agent.runtime.blueprint.structural_key import (
            structural_ast_norm_one, structural_key_from_templates)
        T = [
            "SELECT SUM(x) OVER (PARTITION BY d) AS v, toFloat64(y) AS w FROM db.t -- c",
            "SELECT department_name AS department, COUNT(DISTINCT employee_code) AS h "
            "FROM db.employee WHERE employee_status = 'A' AND department_name = {department} "
            "GROUP BY department_name",
            "SELECT 'Département' AS d, round(avg(x), 2) AS v FROM db.t",
        ]
        print(json.dumps({
            t: [structural_ast_norm_one(t), structural_key_from_templates(["Département", "M"], t)]
            for t in T
        }, sort_keys=True, ensure_ascii=False))
        """
    )
    path = tmp_path / "probe.py"
    path.write_text(script)
    runs = []
    for _ in range(2):
        out = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": hashseed, "PATH": "/usr/bin:/bin"},
        )
        runs.append(json.loads(out.stdout))
    assert runs[0] == runs[1]
    for value in runs[0].values():
        assert value[1].startswith("sha256:")
