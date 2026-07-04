"""Golden: S4 reproduces the frozen `s4_enriched_blueprint.json` generalization
block — including the BYTE-EXACT `canonical_ast_norm` (the S6 hash input, D48).

The recipe is pinned (§11.2): a drift here silently mints a different `canonical_key`
and degrades a D48 `increment` into a spurious `insert` — so this asserts the exact
string for single AND composite (the `\n`-joined per-node composite rule).

The single fixture's `sql_template` is BRACE authoring form (`{department}`) — the same
shape the runtime binder expects — and its `uses_rules == []`, derived from the S3
plan which marks `record_type` as role=INLINE (`rule_id: null`) with NO role=rule
param. This test pins every derivable field byte-for-byte AND the contract-correct
`uses_rules == []` for the single case.
"""

from __future__ import annotations

from data_agent.learning.generalize.builder import generalize_blueprint

from .helpers import (
    CATALOG,
    composite_sql_by_ref,
    load_expected,
    load_plan,
    single_sql_by_ref,
)

_DERIVABLE = ("sql_template", "uses", "node_templates", "result_grain",
              "static_validation", "canonical_ast_norm")


def test_single_canonical_ast_norm_byte_exact():
    plan = load_plan()["single"]
    expected = load_expected()["single"]["generalization"]
    got = generalize_blueprint(plan, single_sql_by_ref(), CATALOG).to_doc()
    # The strictest pin: the exact S6 hash input.
    assert got["canonical_ast_norm"] == expected["canonical_ast_norm"]
    assert got["sql_template"] == expected["sql_template"]


def test_single_generalization_matches_fixture_except_uses_rules():
    plan = load_plan()["single"]
    expected = load_expected()["single"]["generalization"]
    got = generalize_blueprint(plan, single_sql_by_ref(), CATALOG).to_doc()
    for field in _DERIVABLE:
        assert got[field] == expected[field], field
    # Contract-correct derivation (documented fixture inconsistency, see module docstring).
    assert got["uses_rules"] == []


def test_composite_canonical_ast_norm_byte_exact():
    plan = load_plan()["composite"]
    expected = load_expected()["composite"]["generalization"]
    got = generalize_blueprint(plan, composite_sql_by_ref(), CATALOG).to_doc()
    assert got["canonical_ast_norm"] == expected["canonical_ast_norm"]
    # Composite `canonical_ast_norm` is the per-node normalized templates joined by
    # a single newline in ascending order (§11.2 composite rule).
    assert "\n" in got["canonical_ast_norm"]
    assert got["node_templates"] == expected["node_templates"]


def test_composite_generalization_matches_fixture_fully():
    plan = load_plan()["composite"]
    expected = load_expected()["composite"]["generalization"]
    got = generalize_blueprint(plan, composite_sql_by_ref(), CATALOG).to_doc()
    # The composite fixture is internally consistent — assert the WHOLE block.
    assert got == expected
