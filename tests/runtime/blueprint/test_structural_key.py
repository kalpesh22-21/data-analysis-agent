"""structural_key — the LOOSE cross-authoring-path blueprint identity (PriorArtIndex).

The point of the key is that a HAND-AUTHORED MCP-canon blueprint and a LEARNING-extracted
candidate describing the same query hash to the SAME digest, which the frozen D48
canonical key cannot do (it hashes `resolves`/`uses_rules`, which canon largely omits).
If `test_canon_shape_and_learning_shape_agree` ever fails, the key is worthless — the
whole slice exists to make that assertion true.

The two authoring paths differ in exactly one hashed input's SHAPE (`result_grain`:
canon writes a bare list, learning writes the D56 `{columns, verifiable}` stamp), so the
grain normalization carries the entire cross-tier burden and is tested three ways:
list/dict equivalence, column-order independence, and `verifiable` being ignored.
"""

from __future__ import annotations

import pytest

from data_agent.learning.candidate.generalization import NodeTemplate, ResultGrainStamp
from data_agent.learning.generalize.canonical import canonical_ast_norm
from data_agent.runtime.blueprint.structural_key import (
    blueprint_canonical_ast_norm,
    normalize_structural_grain,
    structural_ast_norm,
    structural_ast_norm_one,
    structural_key,
    structural_key_from_templates,
)

# A canon-authored template, verbatim from
# clickhouse-api/app/corpus/data/blueprints/bp-active-headcount-by-department.yaml.
_CANON_TEMPLATE = """SELECT department_name AS department, COUNT(DISTINCT employee_code) AS headcount
FROM dbpcm_warehouse.employee
WHERE employee_status = 'A' AND department_name = {department}
GROUP BY department_name
"""


# --- grain normalization ----------------------------------------------------


def test_bare_list_and_columns_dict_grain_produce_the_same_key():
    """Canon writes `result_grain: [month]`; the learning side writes
    `{"columns": ["month"], "verifiable": true}`. Same grain ⇒ same key, or the two
    tiers can never match."""
    norm = "SELECT 1"
    assert structural_key(["month"], norm) == structural_key(
        {"columns": ["month"], "verifiable": True}, norm
    )


def test_column_order_does_not_change_the_key():
    """`canonical_json` preserves LIST order, so an unsorted grain would mint a
    different key for the same set of grain columns (the QA-Q6 rule)."""
    norm = "SELECT 1"
    assert structural_key(["type_code", "register_type"], norm) == structural_key(
        ["register_type", "type_code"], norm
    )


def test_verifiable_is_ignored():
    """`verifiable` has no canon equivalent — hashing it would guarantee a cross-tier
    miss for every blueprint."""
    norm = "SELECT 1"
    assert structural_key({"columns": ["month"], "verifiable": True}, norm) == structural_key(
        {"columns": ["month"], "verifiable": False}, norm
    )


def test_case_differences_must_not_split_the_key_canon_capitalizes_grain_labels():
    """DO NOT "fix" the case fold in `normalize_structural_grain` back to
    case-sensitive. This is the MAJORITY case in the real corpus, not a nicety:

    6 of the 10 MCP-canon blueprints declare `result_grain: [Department]` (capital D)
    while their SQL selects `AS department`. `canonical_ast_norm` runs
    `normalize_identifiers`, so the AST half of the key already agrees across the two
    authoring paths — if the grain half did not fold, the structural key would fail on
    exactly the department-grouped blueprints the learning loop is most likely to
    re-derive, which is the single case the PriorArtIndex exists to catch.

    Grain entries are OUTPUT-COLUMN display labels for a `SELECT ... AS x`, not
    case-distinguishing identifiers, so folding loses no meaning.
    """
    norm = "SELECT 1"
    canon = structural_key(["Department"], norm)
    learning = structural_key({"columns": ["department"], "verifiable": True}, norm)
    assert canon == learning


def test_case_fold_applies_to_the_full_canon_blueprint_shape():
    """The same regression at the real call sites' input shapes: a canon bare-list
    `Department` grain over a real canon template vs. the learning D56 stamp with the
    lowercase alias the SQL actually selects."""
    canon = structural_key_from_templates(["Department"], _CANON_TEMPLATE)
    learning = structural_key(
        ResultGrainStamp(columns=("department",), verifiable=True).to_doc(),
        canonical_ast_norm(_CANON_TEMPLATE),
    )
    assert canon
    assert canon == learning


@pytest.mark.parametrize(
    ("grain", "expected"),
    [
        (None, []),
        ([], []),
        ({}, []),
        ({"verifiable": True}, []),
        (["b", "a"], ["a", "b"]),
        ({"columns": ["b", "a"], "verifiable": False}, ["a", "b"]),
        ("month", []),  # a scalar is not a grain — never a one-element list
        (["Department"], ["department"]),  # canon capitalizes; the AST does not
        (["Department", "MONTH"], ["department", "month"]),
        ([" Department "], ["department"]),  # defensive strip
        # Fold-then-sort, not sort-then-fold: ASCII sort puts every capital before
        # every lowercase, so the two orders differ and only one is stable.
        (["month", "Department"], ["department", "month"]),
    ],
)
def test_grain_normalization_shapes(grain, expected):
    assert normalize_structural_grain(grain) == expected


def test_a_different_grain_changes_the_key():
    """Sanity: the grain is actually hashed, not silently dropped."""
    norm = "SELECT 1"
    assert structural_key(["month"], norm) != structural_key(["department"], norm)


# --- the cross-tier agreement (the point of the slice) ----------------------


def test_canon_shape_and_learning_shape_agree():
    """THE load-bearing assertion. Two descriptions of ONE blueprint:

      * canon    — a bare-list grain + a `{slot}` `sql_template`, no `canonical_ast_norm`
                   (derived here by the seeder helper, exactly as `dag_properties` does);
      * learning — the D56 `{columns, verifiable}` grain + the S4-computed
                   `canonical_ast_norm` string.

    They must hash to the SAME structural key.
    """
    canon = structural_key_from_templates(["Department"], _CANON_TEMPLATE)

    s4_norm = canonical_ast_norm(_CANON_TEMPLATE)
    learning = structural_key(
        ResultGrainStamp(columns=("Department",), verifiable=True).to_doc(), s4_norm
    )

    assert canon
    assert canon == learning


def test_composite_canon_and_learning_agree():
    """The composite rule (per-node templates joined in ascending `order`) must also
    agree across the two paths — canon authors `composes[*].sql_template`, the learning
    side carries `NodeTemplate`s."""
    node_a = "SELECT AVG(annual_salary) AS company_avg FROM dbpcm_warehouse.employee"
    node_b = (
        "SELECT department_name AS department, AVG(annual_salary) AS dept_avg "
        "FROM dbpcm_warehouse.employee GROUP BY department_name "
        "HAVING AVG(annual_salary) > {company_avg}"
    )

    canon = structural_key_from_templates(
        ["Department"], None, [(1, node_b), (0, node_a)]  # deliberately out of order
    )
    learning = structural_key(
        ResultGrainStamp(columns=("Department",)).to_doc(),
        canonical_ast_norm(
            None,
            [NodeTemplate(order=0, sql_template=node_a), NodeTemplate(order=1, sql_template=node_b)],
        ),
    )

    assert canon
    assert canon == learning


def test_shared_normalizer_is_one_implementation():
    """The learning adapter must DELEGATE to the runtime recipe, not re-implement it —
    a second copy is the failure mode that would silently break cross-tier matching."""
    assert canonical_ast_norm(_CANON_TEMPLATE) == blueprint_canonical_ast_norm(_CANON_TEMPLATE)


def test_the_two_renders_share_the_composite_join_rule():
    """The frozen and structural renders differ ONLY in their per-template post-passes.
    The composite rule itself (ascending `order`, single-newline join, template-less nodes
    skipped) is one shared implementation, so the two can never disagree about DAG shape
    while agreeing about SQL."""
    nodes = [(1, "SELECT b FROM db.t"), (0, "SELECT a FROM db.t")]
    assert blueprint_canonical_ast_norm(None, nodes).count("\n") == 1
    assert structural_ast_norm(None, nodes).count("\n") == 1
    assert structural_ast_norm(None, nodes) == "\n".join(
        [structural_ast_norm_one("SELECT a FROM db.t"), structural_ast_norm_one("SELECT b FROM db.t")]
    )


def test_structural_key_from_templates_uses_the_structural_render_not_the_frozen_one():
    """The safe entry point must route through `structural_ast_norm`. If it ever reverted
    to `blueprint_canonical_ast_norm`, every canon/learning pair differing only in
    aggregate casing would silently stop matching — and nothing else would fail."""
    lower = "SELECT sum(a) AS v FROM db.t"
    assert structural_key_from_templates(["g"], lower) == structural_key(
        ["g"], structural_ast_norm_one(lower)
    )
    assert structural_key_from_templates(["g"], lower) != structural_key(
        ["g"], canonical_ast_norm(lower)
    )


# --- shape + fail-soft ------------------------------------------------------


def test_key_is_sha256_prefixed_and_deterministic():
    key = structural_key(["month"], "SELECT 1")
    assert key.startswith("sha256:")
    assert len(key) == len("sha256:") + 64
    assert key == structural_key(["month"], "SELECT 1")


def test_blank_canonical_ast_norm_yields_no_key():
    """An AST-less key would degenerate to a hash of the grain alone and collide every
    unparseable blueprint of that grain into one bogus prior-art match."""
    assert structural_key(["month"], "") == ""
    assert structural_key(["month"], "   ") == ""
    assert structural_key(["month"], None) == ""


def test_unparseable_template_fails_soft():
    """The seeder helper must never raise — a missing key costs a dedup match; a raise
    would brick corpus loading."""
    assert structural_key_from_templates(["month"], "SELECT FROM WHERE ((") == ""


def test_canonical_ast_norm_is_hashed_opaquely():
    """The string is a hash INPUT and is never re-parsed: two semantically-identical but
    textually-different norms are DIFFERENT keys (the D48 freeze rationale)."""
    assert structural_key(["month"], "SELECT 1") != structural_key(["month"], "SELECT  1")
