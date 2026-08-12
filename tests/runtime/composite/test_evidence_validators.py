"""Unit tests for the two evidence validators (Release 1, doc 04 §E).

Pure functions over a trail — no store, no loop, no model. Everything the
enforcement mechanism guarantees rests here: if these accept weak evidence, the
model closes intents it did not answer and the ledger records it as done.
"""

from __future__ import annotations

from data_agent.runtime.composite.analysis_state import (
    validate_block_evidence,
    validate_completion_evidence,
)
from data_agent.runtime.context.assembly import IDEMPOTENT_READ_ALREADY_SERVED_CODE
from data_agent.runtime.session.models import ResultPreview, TrailEntry

TURN = 3


def _entry(
    tool_call_id: str,
    tool_name: str,
    *,
    status: str = "ok",
    error_code: str | None = None,
    turn_index: int = TURN,
    authoritative: bool = False,
    row_count: int | None = 1,
    args: dict | None = None,
) -> TrailEntry:
    preview = (
        None
        if row_count is None
        else ResultPreview(
            columns=["x"], row_count=row_count, truncated=False, preview_rows=[]
        )
    )
    return TrailEntry(
        turn_index=turn_index,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=args or {},
        status=status,
        error_code=error_code,
        provenance=frozenset(),
        result_preview=preview,
        result_full_ref=None,
        ts="2026-08-11T00:00:00+00:00",
        authoritative=authoritative,
    )


# ---------------------------------------------------------------------------
# Completion validator (04 §A)
# ---------------------------------------------------------------------------


def test_run_query_ok_this_turn_is_valid() -> None:
    trail = [_entry("call_1", "runQuery")]
    assert validate_completion_evidence("call_1", trail, TURN) is None


def test_run_blueprint_ok_and_authoritative_is_valid() -> None:
    trail = [_entry("call_1", "runBlueprint", authoritative=True)]
    assert validate_completion_evidence("call_1", trail, TURN) is None


def test_run_blueprint_ok_but_not_authoritative_is_invalid() -> None:
    """Condition 4 catches what condition 2 does not: a blueprint can return `ok`
    with an unclean verify block, and only `authoritative` gates the marker."""
    trail = [_entry("call_1", "runBlueprint", authoritative=False)]
    reason = validate_completion_evidence("call_1", trail, TURN)
    assert reason is not None
    assert "verification" in reason


def test_get_table_schema_ok_is_valid() -> None:
    """The accepted trade (04 §A): admitting `getTableSchema` closes the
    metadata-intent gap without an `intent.kind` field. Mitigated by the
    `loop_metadata_evidence_completion` counter, not by a structural rule."""
    trail = [_entry("call_1", "getTableSchema")]
    assert validate_completion_evidence("call_1", trail, TURN) is None


def test_get_table_schema_guard_marker_is_invalid_and_names_the_original() -> None:
    """Condition 5. The dedup marker entry is `status="ok"` and passes 1-4 having
    fetched NOTHING, so it must be rejected — and the validator holds the whole
    trail, so it can name the call that actually served the result. The loop
    cannot: `seen_read_calls` is a set of signatures with no ids."""
    args = {"database": "dbpcm_warehouse", "table": "employee"}
    trail = [
        _entry("call_original", "getTableSchema", args=args),
        _entry(
            "call_repeat",
            "getTableSchema",
            error_code=IDEMPOTENT_READ_ALREADY_SERVED_CODE,
            row_count=None,
            args=args,
        ),
    ]
    reason = validate_completion_evidence("call_repeat", trail, TURN)
    assert reason is not None
    assert "call_original" in reason
    assert "duplicate" in reason


def test_weak_tools_are_insufficient_for_completion() -> None:
    for tool_name in ("resolveValues", "sampleRows", "searchBlueprints", "searchKnowledge"):
        trail = [_entry("call_1", tool_name)]
        reason = validate_completion_evidence("call_1", trail, TURN)
        assert reason is not None, tool_name
        assert tool_name in reason


def test_prior_turn_evidence_is_invalid() -> None:
    """`entry.turn_index == turn_index` is written into the predicate explicitly —
    the same rule as `live_analysis_state`. Never enforce on another turn."""
    trail = [_entry("call_1", "runQuery", turn_index=TURN - 1)]
    reason = validate_completion_evidence("call_1", trail, TURN)
    assert reason is not None
    assert "earlier turn" in reason


def test_unknown_id_reason_names_both_possibilities() -> None:
    """The runtime cannot mechanically tell "you cited a call from this same
    message, which has not run yet" from "you invented that id" — distinguishing
    them would need the batch, and `TurnContext` carries `turn_index` only. So the
    one message must name BOTH, because the correct model behaviour differs."""
    reason = validate_completion_evidence("call_ghost", [], TURN)
    assert reason is not None
    assert "has not run yet" in reason
    assert "NEXT message" in reason
    assert "unknown" in reason


def test_denied_call_is_not_completion_evidence() -> None:
    trail = [_entry("call_1", "runQuery", status="denied", error_code="COLUMN_SCOPE_VIOLATION")]
    reason = validate_completion_evidence("call_1", trail, TURN)
    assert reason is not None
    assert "successful" in reason


def test_the_same_id_may_complete_two_intents() -> None:
    """Reuse IS allowed for completion: one query genuinely answers "headcount and
    average salary by department". The asymmetry with blocking is deliberate."""
    trail = [_entry("call_1", "runQuery")]
    assert validate_completion_evidence("call_1", trail, TURN) is None
    assert validate_completion_evidence("call_1", trail, TURN) is None


def test_blank_and_non_string_evidence_ids_are_invalid() -> None:
    trail = [_entry("call_1", "runQuery")]
    for bad in ("", "   ", None, 7):
        assert validate_completion_evidence(bad, trail, TURN) is not None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Block validator (04 §B)
# ---------------------------------------------------------------------------


def test_no_access_citing_a_denied_call_is_valid() -> None:
    trail = [
        _entry(
            "call_1",
            "runQuery",
            status="denied",
            error_code="COLUMN_SCOPE_VIOLATION",
            row_count=None,
        )
    ]
    assert validate_block_evidence("call_1", "NO_ACCESS", trail, TURN) is None


def test_required_data_unavailable_citing_zero_rows_is_valid() -> None:
    trail = [_entry("call_1", "runQuery", row_count=0)]
    assert validate_block_evidence("call_1", "REQUIRED_DATA_UNAVAILABLE", trail, TURN) is None


def test_required_data_unavailable_citing_a_non_empty_result_is_invalid() -> None:
    trail = [_entry("call_1", "runQuery", row_count=12)]
    reason = validate_block_evidence("call_1", "REQUIRED_DATA_UNAVAILABLE", trail, TURN)
    assert reason is not None
    assert "12" in reason


def test_block_evidence_from_a_prior_turn_is_invalid() -> None:
    trail = [
        _entry(
            "call_1",
            "runQuery",
            status="denied",
            error_code="COLUMN_SCOPE_VIOLATION",
            row_count=None,
            turn_index=TURN - 1,
        )
    ]
    assert validate_block_evidence("call_1", "NO_ACCESS", trail, TURN) is not None
