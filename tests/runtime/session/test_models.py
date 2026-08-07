"""Unit tests for session dataclass (de)serialization (Layer 1 — no infra)."""

from __future__ import annotations

from data_agent.runtime.session.models import (
    PauseCheckpoint,
    ResultPreview,
    SessionDoc,
    TrailEntry,
    TurnMessage,
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
        context_summary_cache=None,
    )
    wire = doc.to_doc()
    assert wire["_id"] == "session::sess-1"
    restored = SessionDoc.from_doc(wire)
    assert restored.session_id == doc.session_id
    assert restored.messages == doc.messages
    assert restored.tool_trail == doc.tool_trail
    assert restored.pause_checkpoint is None
