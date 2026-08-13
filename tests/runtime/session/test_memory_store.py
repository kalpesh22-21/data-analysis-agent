"""Unit tests for InMemorySessionStore, including D45 CAS exactly-once resume (Layer 1)."""

from __future__ import annotations

import json

import pytest

from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import (
    AnalysisState,
    PauseCheckpoint,
    SessionDoc,
    TrackedIntent,
    TrailEntry,
    TurnMessage,
    live_analysis_state,
)
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


# --- apply_analysis_state (Release 1, 03 §B) --------------------------------


async def test_apply_analysis_state_is_latest_wins_on_one_field(
    store: InMemorySessionStore,
) -> None:
    """Never append-only trail entries: N rounds would put N copies inside the
    pinned region of `fit_request_to_budget` and reproduce unbounded context
    growth in structured form."""
    first = AnalysisState(
        turn_index=1,
        intents=(TrackedIntent(intent_id="i1", description="a", status="pending"),),
    )
    await store.apply_analysis_state("sess-1", 1, lambda _live: first)
    second = AnalysisState(
        turn_index=1,
        intents=(TrackedIntent(intent_id="i1", description="a", status="completed",
                               evidence_tool_call_id="call_1"),),
    )
    returned = await store.apply_analysis_state("sess-1", 1, lambda _live: second)

    assert returned == second
    doc = await store.get_or_create_session("sess-1")
    assert doc.analysis_state == second


async def test_the_merge_callback_receives_the_live_state_only(
    store: InMemorySessionStore,
) -> None:
    """`merge` is handed `live_analysis_state(doc, turn_index)`, so a state from
    another turn arrives as `None` — the callback never has to re-implement the
    A.1 gate, and cannot forget to."""
    await store.apply_analysis_state(
        "sess-1",
        1,
        lambda _live: AnalysisState(
            turn_index=1,
            intents=(TrackedIntent(intent_id="i1", description="a", status="pending"),),
        ),
    )
    seen: list[AnalysisState | None] = []

    def _merge(live: AnalysisState | None) -> AnalysisState:
        seen.append(live)
        return AnalysisState(
            turn_index=2,
            intents=(TrackedIntent(intent_id="i1", description="b", status="pending"),),
        )

    await store.apply_analysis_state("sess-1", 2, _merge)
    assert seen == [None]


async def test_a_raising_merge_leaves_the_document_untouched(
    store: InMemorySessionStore,
) -> None:
    """State-dependent validation runs INSIDE the merge and aborts the write by
    raising, so a rejected state call persists nothing."""
    existing = AnalysisState(
        turn_index=1,
        intents=(TrackedIntent(intent_id="i1", description="a", status="pending"),),
    )
    await store.apply_analysis_state("sess-1", 1, lambda _live: existing)

    def _boom(_live: AnalysisState | None) -> AnalysisState:
        raise ValueError("rejected")

    with pytest.raises(ValueError, match="rejected"):
        await store.apply_analysis_state("sess-1", 1, _boom)

    doc = await store.get_or_create_session("sess-1")
    assert doc.analysis_state == existing


async def test_analysis_state_survives_a_pause_and_resume(
    store: InMemorySessionStore,
) -> None:
    """`turn_index`, not the budget window, is the unit — so the state a turn
    declared before an `askUser` pause still governs the same turn after it."""
    await store.append_message(
        "sess-1", TurnMessage(turn_index=3, role="user", content="two things", ts="t0")
    )
    await store.apply_analysis_state(
        "sess-1",
        3,
        lambda _live: AnalysisState(
            turn_index=3,
            intents=(TrackedIntent(intent_id="i1", description="a", status="pending"),),
        ),
    )
    await store.write_pause_checkpoint(
        "sess-1",
        PauseCheckpoint(
            reason="askUser",
            pending_question={"question": "which?", "options": None},
            awaiting="user_answer",
            consumed=False,
            budget_window_count=1,
        ),
    )
    _, cas = await store.get_session_with_cas("sess-1")
    resumed = await store.resume_checkpoint("sess-1", cas, "the sales department")

    assert live_analysis_state(resumed, 3) is not None
    assert live_analysis_state(resumed, 4) is None


async def test_the_intent_tag_survives_a_pause_and_resume_and_the_couchbase_wire(
    store: InMemorySessionStore,
) -> None:
    """The call-time tag is the PRIMARY completion binding, so it has to outlive a
    pause: `updateAnalysisState` re-reads the trail from the document on every call,
    and an intent tagged in the round before an `askUser` is normally closed in the
    round after it.

    Both `SessionStore` implementations are covered. The in-memory store is driven
    directly; `CouchbaseSessionStore` persists `SessionDoc.to_doc()` and parses the
    document back, so the JSON round-trip below is exactly its wire path — asserted
    here rather than only in the live-cluster tests, which are skipped by default.
    """
    tagged = TrailEntry(
        turn_index=3,
        tool_call_id="call_b1",
        tool_name="runBlueprint",
        args={"id": "bp-headcount", "slot_bindings": {}},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref=None,
        ts="2026-08-12T00:00:00+00:00",
        authoritative=True,
        serves_intent="i1",
    )
    await store.append_message(
        "sess-tag", TurnMessage(turn_index=3, role="user", content="two things", ts="t0")
    )
    await store.append_trail_entry("sess-tag", tagged)
    await store.write_pause_checkpoint(
        "sess-tag",
        PauseCheckpoint(
            reason="askUser",
            pending_question={"question": "which?", "options": None},
            awaiting="user_answer",
            consumed=False,
            budget_window_count=1,
            serves_intent="i1",
        ),
    )
    _, cas = await store.get_session_with_cas("sess-tag")
    resumed = await store.resume_checkpoint("sess-tag", cas, "the sales department")

    assert [e.serves_intent for e in resumed.tool_trail] == ["i1"]
    assert resumed.pause_checkpoint.serves_intent == "i1"

    # The Couchbase wire: dict -> JSON -> dict -> dataclass.
    on_the_wire = json.loads(json.dumps(resumed.to_doc()))
    restored = SessionDoc.from_doc(on_the_wire)
    assert [e.serves_intent for e in restored.tool_trail] == ["i1"]
    assert restored.pause_checkpoint.serves_intent == "i1"


# --- claim_finalization_block (Release 1, 05 §C.1) --------------------------


async def test_the_finalization_block_is_claimable_once_per_window(
    store: InMemorySessionStore,
) -> None:
    """"One forced re-round per budget window" is LITERAL: the counter is keyed by
    (turn, window) on the session doc, so it survives the `_run_loop_body` re-entry
    that every resume performs while `window_count` stands still."""
    assert await store.claim_finalization_block("sess-1", 0, 1, "intents") is True
    assert await store.claim_finalization_block("sess-1", 0, 1, "intents") is False
    assert await store.claim_finalization_block("sess-1", 0, 1, "intents") is False

    doc = await store.get_or_create_session("sess-1")
    assert doc.finalization_blocks == {"0:1:intents": 1}


async def test_each_block_kind_has_its_own_allowance(
    store: InMemorySessionStore,
) -> None:
    """05 §J.3. The two gates that can refuse a finish hold INDEPENDENT per-window
    allowances, and this is the store-level statement of it: spending `intents`
    leaves `answer_shape` untouched, and vice versa.

    They shared one allowance for a release. Live, that meant the pending-intents
    nudge took it first on multi-intent questions and the answer-shape gate was
    starved in 2 of 4 runs — the two gates fire in SEQUENCE on those turns (prose
    with intents pending, then prose with tables untabled), not in competition, so
    one allowance could only ever serve the first.
    """
    assert await store.claim_finalization_block("sess-1", 0, 1, "intents") is True
    assert await store.claim_finalization_block("sess-1", 0, 1, "intents") is False
    # The OTHER kind, same turn and window, is untouched by that.
    assert await store.claim_finalization_block("sess-1", 0, 1, "answer_shape") is True
    assert await store.claim_finalization_block("sess-1", 0, 1, "answer_shape") is False

    doc = await store.get_or_create_session("sess-1")
    assert doc.finalization_blocks == {"0:1:intents": 1, "0:1:answer_shape": 1}


async def test_an_unknown_block_kind_is_refused_rather_than_minting_an_allowance(
    store: InMemorySessionStore,
) -> None:
    """The kind becomes part of a PERSISTED key, so an unrecognised one would not
    fail — it would quietly mint a brand-new, unbounded allowance that no bound
    applies to. `finalization_block_key` validates instead, and the loop's claim
    wrapper treats any exception as "no re-round available"."""
    with pytest.raises(ValueError, match="unknown finalization block kind"):
        await store.claim_finalization_block("sess-1", 0, 1, "not_a_kind")  # type: ignore[arg-type]

    doc = await store.get_or_create_session("sess-1")
    assert not doc.finalization_blocks


async def test_a_fresh_budget_window_gets_its_own_block(
    store: InMemorySessionStore,
) -> None:
    """A budget-cap "continue" grants a new window (D55), and with it a new
    allowance."""
    assert await store.claim_finalization_block("sess-1", 0, 1, "intents") is True
    assert await store.claim_finalization_block("sess-1", 0, 2, "intents") is True
    assert await store.claim_finalization_block("sess-1", 0, 2, "intents") is False

    doc = await store.get_or_create_session("sess-1")
    assert doc.finalization_blocks == {"0:1:intents": 1, "0:2:intents": 1}


async def test_a_fresh_turn_gets_its_own_block_at_the_same_window_number(
    store: InMemorySessionStore,
) -> None:
    """The reason the key carries the turn index. `AgentLoop.run` starts EVERY
    external turn at `window_count=1` while this map persists for the whole session
    and is never cleared, so a window-only key made turn 1's window 1 collide with
    turn 0's — killing the forced re-round from a session's second block-spending
    turn onward."""
    assert await store.claim_finalization_block("sess-1", 0, 1, "intents") is True
    assert await store.claim_finalization_block("sess-1", 0, 1, "intents") is False

    assert await store.claim_finalization_block("sess-1", 1, 1, "intents") is True
    assert await store.claim_finalization_block("sess-1", 1, 1, "intents") is False

    doc = await store.get_or_create_session("sess-1")
    assert doc.finalization_blocks == {"0:1:intents": 1, "1:1:intents": 1}


async def test_the_block_counter_survives_a_pause_and_resume(
    store: InMemorySessionStore,
) -> None:
    """The whole reason it is persisted: an exit-#1 refusal leaves NO trail entry
    by design, so a resume cannot reconstruct it from the trail."""
    await store.claim_finalization_block("sess-1", 0, 1, "intents")
    await store.write_pause_checkpoint(
        "sess-1",
        PauseCheckpoint(
            reason="askUser",
            pending_question={"question": "which?", "options": None},
            awaiting="user_answer",
            consumed=False,
            budget_window_count=1,
        ),
    )
    _, cas = await store.get_session_with_cas("sess-1")
    resumed = await store.resume_checkpoint("sess-1", cas, "an answer")

    assert resumed.finalization_blocks == {"0:1:intents": 1}
    assert await store.claim_finalization_block("sess-1", 0, 1, "intents") is False
