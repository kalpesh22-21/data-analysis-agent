"""REGRESSION GUARD: the frozen D48 hard key and the shared `canonical_ast_norm`.

PriorArtIndex Slice 1 added a SECOND, looser key and MOVED the §11.2 normalization recipe
out of `learning/generalize/canonical.py` into `runtime/blueprint/structural_key.py`. Two
untouched contracts must survive that refactor byte-for-byte:

  1. `compute_canonical_key` (Contract C §3, FROZEN) — its digests are persisted in the
     Couchbase corpus bucket as `canonical_key` and are the S4->S6 dedup identity. If a
     digest changes, every stored artifact becomes unreachable, every candidate looks
     new, and the loop re-proposes the entire corpus. The GOLDEN digests below are pinned
     literals: a change makes this file fail rather than silently re-key production.

  2. `canonical_ast_norm`'s public behavior for existing learning callers, now that the
     module DELEGATES to the runtime implementation. The delegation must be a pure move —
     same output for single templates, same composite join, same exception on garbage.

Neither of these is exercised by the new slice's own tests, which assert the NEW key.
"""

from __future__ import annotations

import pytest
import sqlglot.errors

from data_agent.learning.candidate.generalization import NodeTemplate
from data_agent.learning.dedup.canonical_key import compute_canonical_key
from data_agent.learning.generalize.canonical import canonical_ast_norm, canonical_ast_norm_one
from data_agent.runtime.blueprint.structural_key import (
    blueprint_canonical_ast_norm,
)
from data_agent.runtime.blueprint.structural_key import (
    canonical_ast_norm_one as runtime_canonical_ast_norm_one,
)

# --- 1. the frozen D48 hard key ------------------------------------------------

# GOLDEN digests, pinned as literals. VERIFIED equal against the PRE-SLICE tree (a
# detached worktree at the commit before the PriorArtIndex changes), so they are a real
# regression guard rather than a snapshot of whatever the code happens to do now.
# DO NOT regenerate these to make a test pass — a mismatch means the key changed, which
# invalidates every `canonical_key` already persisted in the corpus bucket.
_GOLDEN: list[tuple[str, tuple, str]] = [
    (
        "empty-everything",
        ({}, [], {"columns": [], "verifiable": False}, ""),
        "sha256:a953eda020c4a3b491b297f37aff5a8b13dc8ffc11d3c7bf0d91270c5050d152",
    ),
    (
        "realistic-s4-shape",
        (
            {"overtime": "gross_earnings"},
            ["active_employee"],
            {"columns": ["department"], "verifiable": True},
            "SELECT sum(gross_pay) AS total_earnings FROM payroll.payroll_fact "
            "WHERE department = {department: }",
        ),
        "sha256:d1e02f395f4ed7c311ba85c1c6b83ba1380ddeb16cc9802aa8a64385a053b5b3",
    ),
    (
        "dedup-and-sort-normalization",
        (
            {"a": "b", "c": "d"},
            ["r2", "r1", "r1"],
            {"columns": ["b", "a"], "verifiable": True},
            "SELECT 1",
        ),
        "sha256:c9ba45b8a4ab49c65803eff193180969134177076292d6ad58364d1e6bb54326",
    ),
    (
        "non-ascii-ensure-ascii-false",
        ({}, [], {"columns": ["Département"], "verifiable": False}, "SELECT 1"),
        "sha256:2074898c9b765f7f509e96c0cf356569fd659d2ea3e723605bfc28c4630b12e0",
    ),
]


@pytest.mark.parametrize(("label", "args", "expected"), _GOLDEN, ids=[g[0] for g in _GOLDEN])
def test_compute_canonical_key_digests_are_unchanged(label, args, expected) -> None:
    assert compute_canonical_key(*args) == expected


def test_the_frozen_key_still_normalizes_uses_rules_as_a_set() -> None:
    """QA-Q5: `uses_rules` is deduplicated and sorted, so listing a rule twice or in a
    different order cannot mint a different key."""
    base = ({}, ["a", "b"], {"columns": [], "verifiable": False}, "SELECT 1")
    assert compute_canonical_key(*base) == compute_canonical_key(
        {}, ["b", "a", "a"], {"columns": [], "verifiable": False}, "SELECT 1"
    )


def test_the_frozen_key_still_normalizes_grain_column_order() -> None:
    """QA-Q6: `result_grain.columns` order-independence."""
    assert compute_canonical_key(
        {}, [], {"columns": ["a", "b"], "verifiable": True}, "SELECT 1"
    ) == compute_canonical_key({}, [], {"columns": ["b", "a"], "verifiable": True}, "SELECT 1")


def test_the_frozen_key_is_still_case_sensitive_on_grain() -> None:
    """The two keys deliberately DIVERGE here and must keep diverging.

    The new `structural_key` case-FOLDS the grain (canon capitalizes its labels); the
    frozen D48 key does NOT, and must not start doing so — that would silently re-key
    every persisted artifact. This test is the tripwire against someone "harmonizing" the
    two grain normalizations.
    """
    assert compute_canonical_key(
        {}, [], {"columns": ["Department"], "verifiable": True}, "SELECT 1"
    ) != compute_canonical_key({}, [], {"columns": ["department"], "verifiable": True}, "SELECT 1")


def test_the_frozen_key_still_hashes_resolves_and_uses_rules() -> None:
    """The precise reason a second, looser key was needed: these two members make the
    D48 key unable to match a canon blueprint (which carries neither). Pinned so the
    motivation stays true and testable."""
    grain = {"columns": ["department"], "verifiable": True}
    norm = "SELECT 1"
    canon_like = compute_canonical_key({}, [], grain, norm)
    learning_like = compute_canonical_key({"x": "y"}, ["rule"], grain, norm)
    assert canon_like != learning_like


# --- 2. canonical_ast_norm's public behavior after the delegation --------------


_SINGLE = (
    "SELECT e.department_name AS department, SUM(p.amount) AS earnings "
    "FROM dbpcm_warehouse.payroll AS p "
    "JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code "
    "WHERE p.register_type = 'EARN' AND e.department_name = {department} "
    "GROUP BY e.department_name"
)

# The exact rendered output of the pre-slice recipe, pinned as a literal and VERIFIED
# byte-equal against the pre-slice worktree. This is the string S4 hands S6 and that ends
# up inside a persisted `canonical_key`; it must not move when the implementation
# relocates.
_SINGLE_EXPECTED = (
    "SELECT e.department_name AS department, SUM(p.amount) AS earnings "
    "FROM dbpcm_warehouse.payroll AS p "
    "JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code "
    "WHERE p.register_type = 'EARN' AND e.department_name = {department: } "
    "GROUP BY e.department_name"
)


def test_canonical_ast_norm_single_output_is_byte_identical_to_the_pinned_string() -> None:
    assert canonical_ast_norm(_SINGLE) == _SINGLE_EXPECTED
    assert canonical_ast_norm_one(_SINGLE) == _SINGLE_EXPECTED


def test_the_learning_adapter_is_the_same_function_as_the_runtime_recipe() -> None:
    """A DUPLICATED implementation is the one failure mode that defeats the whole key.
    Assert identity of behavior on both entry points, not just equality on one input."""
    assert canonical_ast_norm_one is runtime_canonical_ast_norm_one
    for template in [
        _SINGLE,
        "SELECT 1",
        "select a from db.t where X = {s}",
        "SELECT toStartOfMonth(d) AS m FROM db.t GROUP BY m",
    ]:
        assert canonical_ast_norm(template) == blueprint_canonical_ast_norm(template)


def test_slot_braces_still_render_as_the_clickhouse_placeholder_token() -> None:
    """`{slot}` -> `:slot` -> ClickHouse `{slot: }`. This round-trip is what makes the
    stored brace authoring form and the runtime `:slot` intermediate hash equal."""
    assert canonical_ast_norm("SELECT a FROM db.t WHERE a = {department}").endswith(
        "WHERE a = {department: }"
    )


def test_composite_join_order_and_separator_are_unchanged() -> None:
    """The composite rule: per-node normalized templates in ASCENDING `order`, joined by
    a SINGLE newline. Nodes are supplied out of order to prove the sort still runs."""
    node_a = "SELECT AVG(annual_salary) AS company_avg FROM dbpcm_warehouse.employee"
    node_b = "SELECT department_name AS department FROM dbpcm_warehouse.employee"
    out = canonical_ast_norm(
        None,
        [NodeTemplate(order=1, sql_template=node_b), NodeTemplate(order=0, sql_template=node_a)],
    )
    assert out == f"{canonical_ast_norm_one(node_a)}\n{canonical_ast_norm_one(node_b)}"
    assert out.count("\n") == 1


def test_composite_with_a_single_node_has_no_trailing_newline() -> None:
    node = "SELECT a FROM db.t"
    assert canonical_ast_norm(None, [NodeTemplate(order=0, sql_template=node)]) == (
        canonical_ast_norm_one(node)
    )


def test_an_empty_composite_is_the_empty_string_not_a_raise() -> None:
    """The D52 fail-soft input to `structural_key`: no nodes ⇒ no canonical string ⇒ the
    caller mints no key."""
    assert canonical_ast_norm(None, []) == ""


def test_canonical_ast_norm_still_raises_on_an_unparseable_template() -> None:
    """The learning entry point is STRICT (only the seeder wrapper is fail-soft). A
    silent "" here would let S4 emit a keyless candidate instead of surfacing a genuine
    rewrite bug."""
    with pytest.raises(sqlglot.errors.ParseError):
        canonical_ast_norm("SELECT FROM WHERE ((")


def test_the_two_keys_read_the_same_canonical_ast_norm_string() -> None:
    """End-to-end coherence: the SAME `canonical_ast_norm` string feeds both keys, so a
    change to the recipe moves both together rather than desynchronizing them."""
    from data_agent.runtime.blueprint.structural_key import structural_key

    norm = canonical_ast_norm(_SINGLE)
    grain = {"columns": ["department"], "verifiable": True}
    hard = compute_canonical_key({}, [], grain, norm)
    loose = structural_key(grain, norm)
    assert hard.startswith("sha256:") and loose.startswith("sha256:")
    # Different inputs ⇒ different digests; they are two keys, not one under two names.
    assert hard != loose
