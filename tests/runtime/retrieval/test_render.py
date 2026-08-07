"""Layer-1 tests for retrieval/render.py — determinism + single user-role block."""

from __future__ import annotations

from data_agent.runtime.retrieval.models import (
    KnowledgeHit,
    RetrievedContext,
    ThinCard,
    UserMemoryItem,
)
from data_agent.runtime.retrieval.render import _USER_CONTEXT_PREFIX, render_retrieved_context


def _ctx() -> RetrievedContext:
    return RetrievedContext(
        thin_cards=[ThinCard(id="bp-1", intent="sales overtime", slots_summary="dept, period", score=0.9)],
        knowledge_hits=[KnowledgeHit(id="kn-1", text="OT is 1.5x pay", score=0.8, title="Overtime")],
        user_memory=[UserMemoryItem(kind="entity_default", text="'my team' -> dept=0420")],
        reranked=True,
    )


def test_empty_context_renders_none() -> None:
    assert render_retrieved_context(RetrievedContext.empty()) is None


def test_renders_single_user_message() -> None:
    # NON-system so the base prompt stays the sole `role: "system"` message, with
    # the prior-context prefix marking it as retrieved reference material.
    msg = render_retrieved_context(_ctx())
    assert msg is not None
    assert msg["role"] == "user"
    assert isinstance(msg["content"], str)
    assert msg["content"].startswith(_USER_CONTEXT_PREFIX)


def test_render_is_deterministic() -> None:
    # Same block -> byte-identical string (design §6 resume determinism).
    assert render_retrieved_context(_ctx()) == render_retrieved_context(_ctx())


def test_render_includes_all_sections() -> None:
    content = render_retrieved_context(_ctx())["content"]  # type: ignore[index]
    assert "bp-1" in content
    assert "sales overtime" in content
    assert "OT is 1.5x pay" in content
    assert "my team" in content


def test_render_never_leaks_question_scope_or_jwt() -> None:
    # The renderer's input carries none of these; assert the output cannot
    # accidentally contain a scope/jwt-shaped token (redaction by construction).
    content = render_retrieved_context(_ctx())["content"]  # type: ignore[index]
    assert "eyJ" not in content  # no JWT header prefix
    assert "column_scope" not in content
