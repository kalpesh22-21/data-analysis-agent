"""Unit tests for InMemorySessionStore, including D45 CAS exactly-once resume (Layer 1)."""

from __future__ import annotations

import pytest

from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import PauseCheckpoint, TrailEntry, TurnMessage
from data_agent.runtime.session.store import AlreadyConsumedError, CASMismatchError


@pytest.fixture
def store() -> InMemorySessionStore:
    return InMemorySessionStore()


async def test_create_session_is_idempotent(store: InMemorySessionStore) -> None:
    doc1 = await store.create_session("sess-1")
    doc2 = await store.create_session("sess-1")
    assert doc1.session_id == doc2.session_id == "sess-1"
    assert doc1.created_at == doc2.created_at


async def test_get_or_create_creates_when_absent(store: InMemorySessionStore) -> None:
    doc = await store.get_or_create_session("new-session")
    assert doc.session_id == "new-session"
    assert doc.tool_trail == []


async def test_append_trail_entry_and_load_trail(store: InMemorySessionStore) -> None:
    entry = TrailEntry(
        turn_index=0,
        tool_call_id="call_1",
        tool_name="runQuery",
        args={"sql": "SELECT 1"},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )
    await store.append_trail_entry("sess-1", entry)
    trail = await store.load_trail("sess-1")
    assert trail == [entry]


async def test_append_message_bumps_last_activity(store: InMemorySessionStore) -> None:
    doc0 = await store.create_session("sess-1")
    initial_activity = doc0.last_activity
    await store.append_message(
        "sess-1", TurnMessage(turn_index=0, role="user", content="hi", ts="2026-07-01T00:00:00+00:00")
    )
    doc = await store.get_or_create_session("sess-1")
    assert len(doc.messages) == 1
    assert doc.last_activity >= initial_activity


async def test_write_full_result_returns_ref(store: InMemorySessionStore) -> None:
    ref = await store.write_full_result("sess-1", "uuid-123", {"columns": ["a"], "rows": [[1]]})
    assert ref == "result::uuid-123"


async def test_write_pause_checkpoint(store: InMemorySessionStore) -> None:
    checkpoint = PauseCheckpoint(
        reason="askUser",
        pending_question={"question": "Which dept?", "options": None},
        awaiting="user_answer",
        consumed=False,
    )
    await store.write_pause_checkpoint("sess-1", checkpoint)
    doc = await store.get_or_create_session("sess-1")
    assert doc.pause_checkpoint == checkpoint


# ---------------------------------------------------------------------------
# D45 — CAS exactly-once resume
# ---------------------------------------------------------------------------


async def test_resume_checkpoint_exactly_once(store: InMemorySessionStore) -> None:
    checkpoint = PauseCheckpoint(
        reason="askUser",
        pending_question={"question": "Which dept?", "options": None},
        awaiting="user_answer",
        consumed=False,
    )
    await store.write_pause_checkpoint("sess-1", checkpoint)

    doc, cas = await store.get_session_with_cas("sess-1")
    assert doc.pause_checkpoint is not None
    assert not doc.pause_checkpoint.consumed

    resumed = await store.resume_checkpoint("sess-1", cas, "Sales")
    assert resumed.pause_checkpoint.consumed is True
    assert resumed.messages[-1].role == "user"
    assert resumed.messages[-1].content == "Sales"


async def test_concurrent_resume_second_caller_fails(store: InMemorySessionStore) -> None:
    """Two callers race on the SAME cas: the first wins, the second is rejected."""
    checkpoint = PauseCheckpoint(
        reason="askUser",
        pending_question={"question": "Which dept?", "options": None},
        awaiting="user_answer",
        consumed=False,
    )
    await store.write_pause_checkpoint("sess-1", checkpoint)

    # Both "concurrent" callers read the same cas before either resumes.
    _, cas_a = await store.get_session_with_cas("sess-1")
    _, cas_b = await store.get_session_with_cas("sess-1")
    assert cas_a == cas_b

    winner = await store.resume_checkpoint("sess-1", cas_a, "Sales")
    assert winner.pause_checkpoint.consumed is True

    # The loser's checkpoint is already consumed -> AlreadyConsumedError.
    with pytest.raises(AlreadyConsumedError):
        await store.resume_checkpoint("sess-1", cas_b, "Engineering")


async def test_cas_mismatch_from_unrelated_write(store: InMemorySessionStore) -> None:
    """A CAS token that goes stale due to an unrelated write (not a resume) -> CASMismatchError."""
    checkpoint = PauseCheckpoint(
        reason="budget_cap",
        pending_question={"question": "Continue?", "options": ["continue", "stop"]},
        awaiting="user_answer",
        consumed=False,
        budget_window_count=1,
    )
    await store.write_pause_checkpoint("sess-1", checkpoint)
    _, stale_cas = await store.get_session_with_cas("sess-1")

    # An unrelated mutating write advances the version without touching the checkpoint.
    await store.bump_last_activity("sess-1")

    with pytest.raises(CASMismatchError):
        await store.resume_checkpoint("sess-1", stale_cas, "continue")


async def test_resume_without_pending_checkpoint_raises(store: InMemorySessionStore) -> None:
    _, cas = await store.get_session_with_cas("sess-1")
    with pytest.raises(AlreadyConsumedError):
        await store.resume_checkpoint("sess-1", cas, "anything")
