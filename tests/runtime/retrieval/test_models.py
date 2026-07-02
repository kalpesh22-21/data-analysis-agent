"""Layer-1 tests for retrieval/models.py value objects."""

from __future__ import annotations

from data_agent.runtime.retrieval.models import (
    KnowledgeHit,
    RetrievedContext,
    ThinCard,
    UserMemoryItem,
)


def test_empty_is_empty() -> None:
    ctx = RetrievedContext.empty()
    assert ctx.is_empty() is True
    assert ctx.reranked is False
    assert ctx.thin_cards == []


def test_nonempty_variants_are_not_empty() -> None:
    assert not RetrievedContext(
        thin_cards=[ThinCard(id="a", intent="i", slots_summary="s", score=1.0)]
    ).is_empty()
    assert not RetrievedContext(
        knowledge_hits=[KnowledgeHit(id="k", text="t", score=1.0)]
    ).is_empty()
    assert not RetrievedContext(
        user_memory=[UserMemoryItem(kind="pref", text="x")]
    ).is_empty()
