"""Layer-1 tests for the blueprint scope pre-filter (design §2) — USES ⊄ scope."""

from __future__ import annotations

from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.scope_filter import (
    filter_blueprints_by_scope,
    is_blueprint_in_scope,
)


def _bp(id: str, uses: set[str] | None) -> Candidate:
    return Candidate(
        id=id, kind="blueprint", text=id, uses=frozenset(uses) if uses is not None else None
    )


def test_subset_kept_superset_dropped() -> None:
    scope = frozenset({"w.t.a", "w.t.b"})
    assert is_blueprint_in_scope(_bp("ok", {"w.t.a"}), scope) is True
    assert is_blueprint_in_scope(_bp("ok2", {"w.t.a", "w.t.b"}), scope) is True
    assert is_blueprint_in_scope(_bp("no", {"w.t.a", "w.t.c"}), scope) is False


def test_empty_scope_is_allow_all() -> None:
    assert is_blueprint_in_scope(_bp("x", {"w.t.a", "w.t.z"}), frozenset()) is True


def test_none_uses_is_fail_closed() -> None:
    # An undetermined USES set is dropped under every scope, including allow-all.
    assert is_blueprint_in_scope(_bp("undet", None), frozenset()) is False
    assert is_blueprint_in_scope(_bp("undet", None), frozenset({"w.t.a"})) is False


def test_filter_is_order_preserving() -> None:
    scope = frozenset({"w.t.a"})
    cands = [_bp("a", {"w.t.a"}), _bp("b", {"w.t.b"}), _bp("c", {"w.t.a"})]
    assert [c.id for c in filter_blueprints_by_scope(cands, scope)] == ["a", "c"]
