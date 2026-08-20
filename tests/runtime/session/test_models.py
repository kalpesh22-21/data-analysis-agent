"""Unit tests for session dataclass (de)serialization (Layer 1 — no infra)."""

from __future__ import annotations

from typing import get_args

import pytest

from data_agent.runtime.session.models import (
    FINALIZATION_BLOCK_KINDS,
    INTENT_STATUSES,
    MODEL_REASON_CODES,
    REASON_CODES,
    RUNTIME_REASON_CODES,
    AnalysisState,
    FinalizationBlockKind,
    PauseCheckpoint,
    ResultPreview,
    SessionDoc,
    TrackedIntent,
    TrailEntry,
    TurnMessage,
    finalization_block_key,
    live_analysis_state,
)


def _sample_preview() -> ResultPreview:
    return ResultPreview(
        columns=["dept", "gross_pay"],
        row_count=4213,
        truncated=True,
        preview_rows=[["Sales", 128000.0], ["Eng", 341000.0]],
    )


def test_result_preview_roundtrip() -> None:
    preview = _sample_preview()
    doc = preview.to_doc()
    restored = ResultPreview.from_doc(doc)
    assert restored == preview


def test_turn_message_roundtrip() -> None:
    msg = TurnMessage(turn_index=0, role="user", content="how many employees?", ts="2026-07-01T00:00:00+00:00")
    restored = TurnMessage.from_doc(msg.to_doc())
    assert restored == msg


def test_trail_entry_roundtrip_with_provenance() -> None:
    entry = TrailEntry(
        turn_index=0,
        tool_call_id="call_abc123",
        tool_name="runQuery",
        args={"sql": "SELECT GrossPay FROM payroll_fact", "limit": None},
        status="ok",
        error_code=None,
        provenance=frozenset(
            {("dbpcm_warehouse.payroll_fact", "GrossPay"), ("dbpcm_warehouse.payroll_fact", "PayPeriod")}
        ),
        result_preview=_sample_preview(),
        result_full_ref="result::uuid-1",
        ts="2026-07-01T00:00:00+00:00",
    )
    doc = entry.to_doc()
    # Provenance is stored as a list of [db.table, column] pairs (design §6).
    assert isinstance(doc["provenance"], list)
    assert all(isinstance(pair, list) and len(pair) == 2 for pair in doc["provenance"])

    restored = TrailEntry.from_doc(doc)
    assert restored == entry


def test_trail_entry_authoritative_roundtrips() -> None:
    # A verified-blueprint entry persists its `authoritative` marker across the wire
    # so a D45 replay/resume re-derives the same in-band signal.
    entry = TrailEntry(
        turn_index=0,
        tool_call_id="bp_1",
        tool_name="runBlueprint",
        args={"id": "bp.headcount"},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref="result::uuid-9",
        ts="2026-07-01T00:00:00+00:00",
        authoritative=True,
    )
    doc = entry.to_doc()
    assert doc["authoritative"] is True
    restored = TrailEntry.from_doc(doc)
    assert restored.authoritative is True
    assert restored == entry


def test_trail_entry_serves_intent_roundtrips() -> None:
    """The call-time intent tag is the PRIMARY completion binding, so it has to
    survive the wire: `updateAnalysisState` resolves an intent's evidence by reading
    this field off the persisted trail, and the trail is re-read from the document
    on every state call (and after every pause/resume)."""
    entry = TrailEntry(
        turn_index=3,
        tool_call_id="call_q1",
        tool_name="runQuery",
        args={"sql": "SELECT 1"},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref=None,
        ts="2026-08-12T00:00:00+00:00",
        serves_intent="i2",
    )
    doc = entry.to_doc()
    assert doc["serves_intent"] == "i2"
    # The tag lives on its OWN field, never inside `args` — those are dispatched to
    # the MCP, which rejects an argument its schema does not declare.
    assert "serves_intent" not in doc["args"]
    restored = TrailEntry.from_doc(doc)
    assert restored.serves_intent == "i2"
    assert restored == entry


def test_trail_entry_legacy_doc_without_serves_intent_loads_as_none() -> None:
    """Additive: a document written before tagging existed loads untagged, which is
    exactly "this call was not bound to any intent"."""
    legacy_doc = {
        "turn_index": 0,
        "tool_call_id": "call_legacy",
        "tool_name": "runQuery",
        "args": {"sql": "SELECT 1"},
        "status": "ok",
        "error_code": None,
        "provenance": [],
        "result_preview": None,
        "result_full_ref": None,
        "ts": "2026-07-01T00:00:00+00:00",
    }
    assert TrailEntry.from_doc(legacy_doc).serves_intent is None


def test_pause_checkpoint_carries_the_intent_tag() -> None:
    """A pausing `runBlueprint` writes NO trail entry — the entry is minted by
    `_resume_blueprint` under a fresh id after the answer comes back. Without the
    tag on the checkpoint it dies at the pause, and the intent that blueprint was
    run for becomes closable only by citing an id the model never chose."""
    checkpoint = PauseCheckpoint(
        reason="blueprint_slot",
        pending_question={"question": "which department?"},
        awaiting="user_answer",
        consumed=False,
        blueprint_id="bp-headcount",
        serves_intent="i1",
    )
    restored = PauseCheckpoint.from_doc(checkpoint.to_doc())
    assert restored.serves_intent == "i1"
    assert restored == checkpoint
    legacy = checkpoint.to_doc()
    del legacy["serves_intent"]
    assert PauseCheckpoint.from_doc(legacy).serves_intent is None


def test_trail_entry_legacy_doc_without_authoritative_loads_as_false() -> None:
    # D45 backward-compat: a doc persisted BEFORE the marker existed has no
    # `authoritative` key and must load as False (never a spurious marker on replay).
    legacy_doc = {
        "turn_index": 0,
        "tool_call_id": "call_legacy",
        "tool_name": "runQuery",
        "args": {"sql": "SELECT 1"},
        "status": "ok",
        "error_code": None,
        "provenance": [],
        "result_preview": None,
        "result_full_ref": None,
        "ts": "2026-07-01T00:00:00+00:00",
    }
    restored = TrailEntry.from_doc(legacy_doc)
    assert restored.authoritative is False


def test_trail_entry_undetermined_provenance_is_null_on_wire() -> None:
    entry = TrailEntry(
        turn_index=0,
        tool_call_id="call_x",
        tool_name="runQuery",
        args={"sql": "SELECT * FROM generateRandom(...)"},
        status="ok",
        error_code=None,
        provenance=None,
        result_preview=None,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )
    doc = entry.to_doc()
    assert doc["provenance"] is None
    restored = TrailEntry.from_doc(doc)
    assert restored.provenance is None


def test_trail_entry_empty_provenance_is_empty_list_on_wire() -> None:
    entry = TrailEntry(
        turn_index=0,
        tool_call_id="call_y",
        tool_name="listDatabases",
        args={},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )
    doc = entry.to_doc()
    assert doc["provenance"] == []
    restored = TrailEntry.from_doc(doc)
    assert restored.provenance == frozenset()


def test_pause_checkpoint_roundtrip() -> None:
    checkpoint = PauseCheckpoint(
        reason="askUser",
        pending_question={"question": "Which department?", "options": None},
        awaiting="user_answer",
        consumed=False,
        budget_window_count=0,
    )
    restored = PauseCheckpoint.from_doc(checkpoint.to_doc())
    assert restored == checkpoint


def test_session_doc_roundtrip_full() -> None:
    doc = SessionDoc(
        session_id="sess-1",
        created_at="2026-07-01T00:00:00+00:00",
        last_activity="2026-07-01T00:03:41+00:00",
        messages=[TurnMessage(turn_index=0, role="user", content="hi", ts="2026-07-01T00:00:00+00:00")],
        tool_trail=[
            TrailEntry(
                turn_index=0,
                tool_call_id="call_1",
                tool_name="listDatabases",
                args={},
                status="ok",
                error_code=None,
                provenance=frozenset(),
                result_preview=None,
                result_full_ref=None,
                ts="2026-07-01T00:00:00+00:00",
            )
        ],
        pause_checkpoint=None,
    )
    wire = doc.to_doc()
    assert wire["_id"] == "session::sess-1"
    restored = SessionDoc.from_doc(wire)
    assert restored.session_id == doc.session_id
    assert restored.messages == doc.messages
    assert restored.tool_trail == doc.tool_trail
    assert restored.pause_checkpoint is None


def test_session_doc_reader_tolerates_keys_it_has_never_heard_of() -> None:
    """The persisted-schema contract that let `context_summary_cache` be DELETED (L2).

    A stored Couchbase document outlives the class that wrote it, so the reader has to
    survive keys the class no longer declares — `from_doc` NAMES every key it reads and
    never spreads the document into the constructor, which is what makes deleting a
    persisted field a code change rather than a migration. `context_summary_cache` is
    the real removed key; the second is a control, so this pin keeps meaning after that
    name is forgotten.

    The write half matters as much: `to_doc` emits only declared fields and the store
    writes whole documents, so the stray key is DROPPED the first time the session is
    written. Old docs converge on their own.
    """
    stored = SessionDoc(
        session_id="sess-old",
        created_at="2026-07-01T00:00:00+00:00",
        last_activity="2026-07-01T00:03:41+00:00",
    ).to_doc()
    stored["context_summary_cache"] = {"summary": "prior turns", "tokens": 42}
    stored["a_field_this_class_never_had"] = {"x": 1}

    restored = SessionDoc.from_doc(stored)

    assert restored.session_id == "sess-old"
    assert not hasattr(restored, "context_summary_cache")
    rewritten = restored.to_doc()
    assert "context_summary_cache" not in rewritten
    assert "a_field_this_class_never_had" not in rewritten


# --- analysisState (Release 1, 03 §A/§B) ------------------------------------


def test_analysis_state_roundtrips_on_the_session_doc() -> None:
    state = AnalysisState(
        turn_index=4,
        intents=(
            TrackedIntent(intent_id="i1", description="headcount by department", status="pending"),
            TrackedIntent(
                intent_id="i2",
                description="average salary by department",
                status="completed",
                evidence_tool_call_id="call_7",
            ),
            TrackedIntent(
                intent_id="i3",
                description="who left last month",
                status="blocked",
                evidence_tool_call_id="call_9",
                reason_code="NO_ACCESS",
            ),
        ),
    )
    doc = SessionDoc(
        session_id="sess-1",
        created_at="2026-08-11T00:00:00+00:00",
        last_activity="2026-08-11T00:00:00+00:00",
        analysis_state=state,
        # 05 §C.1 owns the writer; 03 §B owns the field. Keyed by
        # (TURN, WINDOW, KIND) — `models.finalization_block_key`, because
        # `window_count` restarts at 1 on every turn while this map persists for the
        # session, and because the two gates that can refuse a finish hold separate
        # allowances (05 §J.3).
        finalization_blocks={"0:2:intents": 1},
    )
    restored = SessionDoc.from_doc(doc.to_doc())
    assert restored.analysis_state == state
    assert restored.finalization_blocks == {"0:2:intents": 1}


def test_a_document_written_before_the_fields_existed_loads_unchanged() -> None:
    """Both fields are additive and read with `.get`, so a legacy doc round-trips
    byte-identically rather than raising."""
    legacy = SessionDoc(
        session_id="sess-1", created_at="t0", last_activity="t0"
    ).to_doc()
    del legacy["analysis_state"]
    del legacy["finalization_blocks"]
    restored = SessionDoc.from_doc(legacy)
    assert restored.analysis_state is None
    assert restored.finalization_blocks is None


def test_live_analysis_state_gates_on_the_turn_index() -> None:
    """03 §A.1 — the single most important rule. A state persists after its turn
    ends, so it is HISTORY for every later turn and must be invisible to anything
    that initializes, validates or enforces. Without this gate a stale `pending`
    intent from an abandoned turn refuses an unrelated later turn AND writes that
    turn's verdict onto the abandoned turn's record."""
    state = AnalysisState(
        turn_index=4,
        intents=(TrackedIntent(intent_id="i1", description="d", status="pending"),),
    )
    doc = SessionDoc(
        session_id="s", created_at="t0", last_activity="t0", analysis_state=state
    )
    assert live_analysis_state(doc, 4) is state
    assert live_analysis_state(doc, 5) is None
    assert live_analysis_state(doc, 3) is None
    assert live_analysis_state(SessionDoc(session_id="s", created_at="t0",
                                          last_activity="t0"), 4) is None


def test_the_reason_code_split_is_structural() -> None:
    """03 §A.3: a comment would let the validator drift toward accepting runtime
    codes from the model. These are separate frozensets, and disjoint."""
    assert MODEL_REASON_CODES == frozenset({"NO_ACCESS", "REQUIRED_DATA_UNAVAILABLE"})
    assert RUNTIME_REASON_CODES == frozenset(
        {"BUDGET_EXHAUSTED", "USER_STOPPED", "ENFORCEMENT_EXHAUSTED"}
    )
    assert not (MODEL_REASON_CODES & RUNTIME_REASON_CODES)
    assert REASON_CODES == MODEL_REASON_CODES | RUNTIME_REASON_CODES
    assert INTENT_STATUSES == frozenset({"pending", "completed", "blocked"})


def test_the_finalization_block_key_spells_out_every_kind() -> None:
    """05 §J.3. The map now holds allowances of more than one kind, so every key
    names its own — including `intents`, which was the only kind for a release and
    could have kept an unsuffixed key for free. It does not: an unsuffixed key reads
    as "some allowance" in a map that holds several, and nothing is in production, so
    the migration cost of spelling it out is zero.
    """
    assert finalization_block_key(0, 1, "intents") == "0:1:intents"
    assert finalization_block_key(3, 2, "answer_shape") == "3:2:answer_shape"
    assert finalization_block_key(0, 1, "empty_answer") == "0:1:empty_answer"
    # Distinct keys is the whole mechanism — one map, N independent budgets. Every
    # declared kind against every other, so a third (05 §K) cannot collide with the
    # two that predate it.
    keys = {finalization_block_key(0, 1, kind) for kind in FINALIZATION_BLOCK_KINDS}
    assert len(keys) == len(FINALIZATION_BLOCK_KINDS)


def test_every_declared_block_kind_is_registered_in_the_tuple() -> None:
    """THE LITERAL AND THE TUPLE ARE ONE DECLARATION IN TWO PLACES, and only the
    tuple is enforced at runtime — `finalization_block_key` validates against it.

    A kind added to `FinalizationBlockKind` alone type-checks everywhere, reads as
    wired, and then FAILS SILENTLY IN EXACTLY ONE DIRECTION: the key mint raises,
    `FinalizationGate._grant_forced_reround` catches everything and degrades to "no
    re-round available", and the new gate never refuses anything. No event, no
    error, no test — the gate simply does not exist in production while every unit
    test of its own logic passes.

    So the two lists are pinned to each other here, at the only place that can see
    both.
    """
    assert get_args(FinalizationBlockKind) == FINALIZATION_BLOCK_KINDS


def test_an_unknown_finalization_block_kind_is_rejected_at_the_key() -> None:
    """This is the ONE place a persisted allowance key is minted, so it is the one
    place that can stop a typo'd or stale kind from creating an eighth budget that
    no `MAX_FINALIZATION_BLOCKS_PER_WINDOW` applies to. Raising is safe: the loop's
    claim wrapper catches everything and treats it as "no re-round available"."""
    for bad in ("Intents", "answer-shape", "", "answer_shape "):
        with pytest.raises(ValueError, match="unknown finalization block kind"):
            finalization_block_key(0, 1, bad)  # type: ignore[arg-type]
