"""QA adversarial Layer-1 tests for retrieval/scope_filter.py (design §2).

Additive to `test_scope_filter.py`. Focus: the set-subset semantics are pure
EXACT-STRING matching (D70 exact-case precedent, D80(b) allow-all), with no
key-format normalisation. These tests pin the load-bearing consequences:
  - a `db.table.column` key and a `table.column` key are DIFFERENT tokens;
  - case differences are NOT folded (fail-closed on case skew);
  - an empty `uses` frozenset is trivially in-scope (subset of everything);
  - `uses=None` is the ONLY undetermined case, dropped even under allow-all.
"""

from __future__ import annotations

from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.scope_filter import (
    filter_blueprints_by_scope,
    is_blueprint_in_scope,
)


def _bp(uses: set[str] | None) -> Candidate:
    return Candidate(
        id="bp",
        kind="blueprint",
        text="bp",
        uses=frozenset(uses) if uses is not None else None,
    )


def test_key_format_is_exact_string_no_normalisation() -> None:
    # A fully-qualified db.table.column USES key is NOT satisfied by a scope
    # that only lists table.column (or vice-versa) — no format coercion.
    scope_short = frozenset({"table.column"})
    scope_full = frozenset({"db.table.column"})
    assert is_blueprint_in_scope(_bp({"db.table.column"}), scope_short) is False
    assert is_blueprint_in_scope(_bp({"table.column"}), scope_full) is False
    # Exact match on either format is kept.
    assert is_blueprint_in_scope(_bp({"db.table.column"}), scope_full) is True
    assert is_blueprint_in_scope(_bp({"table.column"}), scope_short) is True


def test_case_sensitivity_is_fail_closed() -> None:
    # D70 exact-case precedent: a case-skewed USES token is out of scope.
    scope = frozenset({"db.Table.Column"})
    assert is_blueprint_in_scope(_bp({"db.table.column"}), scope) is False
    assert is_blueprint_in_scope(_bp({"db.Table.Column"}), scope) is True


def test_empty_uses_frozenset_is_trivially_in_scope() -> None:
    # An empty USES set is a subset of every scope (including a narrow one) —
    # distinct from uses=None (undetermined → dropped). A blueprint that reads
    # nothing scope-sensitive is always allowed.
    assert is_blueprint_in_scope(_bp(set()), frozenset({"db.t.c"})) is True
    assert is_blueprint_in_scope(_bp(set()), frozenset()) is True


def test_none_uses_dropped_even_under_allow_all_is_distinct_from_empty() -> None:
    # The fail-closed uses=None check runs BEFORE the allow-all shortcut, so an
    # undetermined blueprint is dropped even when scope is empty — whereas an
    # empty-uses blueprint under the same empty scope is kept.
    assert is_blueprint_in_scope(_bp(None), frozenset()) is False
    assert is_blueprint_in_scope(_bp(set()), frozenset()) is True


def test_superset_scope_keeps_strict_subset_blueprint() -> None:
    scope = frozenset({"db.t.a", "db.t.b", "db.t.c"})
    assert is_blueprint_in_scope(_bp({"db.t.a", "db.t.b"}), scope) is True


def test_whitespace_in_keys_is_significant() -> None:
    # No trimming — a trailing space makes the token distinct (fail-closed).
    assert is_blueprint_in_scope(_bp({"db.t.c "}), frozenset({"db.t.c"})) is False


def test_filter_preserves_order_and_drops_mixed_batch() -> None:
    scope = frozenset({"db.t.a"})
    batch = [
        _bp_id("keep-empty", set()),
        _bp_id("drop-none", None),
        _bp_id("keep-subset", {"db.t.a"}),
        _bp_id("drop-super", {"db.t.a", "db.t.b"}),
        _bp_id("drop-case", {"db.t.A"}),
    ]
    kept = [c.id for c in filter_blueprints_by_scope(batch, scope)]
    assert kept == ["keep-empty", "keep-subset"]


def _bp_id(id: str, uses: set[str] | None) -> Candidate:
    return Candidate(
        id=id,
        kind="blueprint",
        text=id,
        uses=frozenset(uses) if uses is not None else None,
    )
