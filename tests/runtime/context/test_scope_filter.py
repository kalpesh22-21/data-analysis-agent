"""Unit tests for context/scope_filter.py — the D44 scope re-filter (Layer 1)."""

from __future__ import annotations

import pytest

from data_agent.runtime.context.scope_filter import (
    compute_scope_hash,
    filter_messages,
    filter_trail,
    is_entry_in_scope,
    is_message_in_scope,
)
from data_agent.runtime.session.models import ResultPreview, TrailEntry, TurnMessage

_P = "dbpcm_warehouse.payroll"
_E = "dbpcm_warehouse.employee"


def _entry(
    tool_call_id: str,
    provenance: frozenset[tuple[str, str]] | None,
    *,
    turn_index: int = 0,
    status: str = "ok",
    result_preview: ResultPreview | None = None,
) -> TrailEntry:
    return TrailEntry(
        turn_index=turn_index,
        tool_call_id=tool_call_id,
        tool_name="runQuery",
        args={"sql": "SELECT ..."},
        status=status,
        error_code=None if status == "ok" else "SOME_ERROR",
        provenance=provenance,
        result_preview=result_preview,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )


def test_allow_all_empty_scope_keeps_determined_entries() -> None:
    entry = _entry("c1", frozenset({(_P, "Amount")}))
    assert is_entry_in_scope(entry, frozenset()) is True


def test_allow_all_empty_scope_still_drops_undetermined() -> None:
    entry = _entry("c1", None)
    assert is_entry_in_scope(entry, frozenset()) is False


def test_subset_of_scope_is_kept() -> None:
    entry = _entry("c1", frozenset({(_E, "Department")}))
    scope = frozenset({f"{_E}.Department", f"{_E}.EmployeeCode"})
    assert is_entry_in_scope(entry, scope) is True


def test_superset_out_of_scope_is_dropped() -> None:
    entry = _entry("c1", frozenset({(_E, "Department"), (_P, "Amount")}))
    scope = frozenset({f"{_E}.Department"})  # Amount not in scope
    assert is_entry_in_scope(entry, scope) is False


def test_undetermined_provenance_always_dropped_even_with_narrow_scope() -> None:
    entry = _entry("c1", None)
    scope = frozenset({f"{_E}.Department"})
    assert is_entry_in_scope(entry, scope) is False


def test_empty_but_determined_provenance_always_kept() -> None:
    entry = _entry("c1", frozenset())
    assert is_entry_in_scope(entry, frozenset({f"{_E}.Department"})) is True
    assert is_entry_in_scope(entry, frozenset()) is True


def test_scratch_columns_excluded_from_allowlist_check() -> None:
    entry = _entry("c1", frozenset({("scratch.s_sess1_upload", "col_a"), (_E, "Department")}))
    scope = frozenset({f"{_E}.Department"})  # scratch column not listed, but session-gated not scope-gated
    assert is_entry_in_scope(entry, scope) is True


@pytest.mark.parametrize(
    ("entries", "scope", "expected_ids"),
    [
        (
            [_entry("c1", frozenset({(_E, "Department")})), _entry("c2", None)],
            frozenset(),
            ["c1"],
        ),
        (
            [
                _entry("c1", frozenset({(_E, "Department")})),
                _entry("c2", frozenset({(_P, "Amount")})),
            ],
            frozenset({f"{_E}.Department"}),
            ["c1"],
        ),
        (
            [_entry("c1", frozenset({(_E, "Department")})), _entry("c2", frozenset())],
            frozenset({f"{_E}.Department"}),
            ["c1", "c2"],
        ),
    ],
)
def test_filter_trail(entries, scope, expected_ids) -> None:
    filtered = filter_trail(entries, scope)
    assert [e.tool_call_id for e in filtered] == expected_ids


def test_filter_trail_preserves_order() -> None:
    entries = [_entry(f"c{i}", frozenset()) for i in range(5)]
    filtered = filter_trail(entries, frozenset())
    assert [e.tool_call_id for e in filtered] == [f"c{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# Turn-scoped continuity (2026-07-01): filter_trail's current_turn_index exemption
# ---------------------------------------------------------------------------


def test_current_turn_index_none_default_leaves_strict_filter_unchanged() -> None:
    """No `current_turn_index` argument at all -> byte-for-byte the original
    all-strict D44 behavior (the exact call shape the QA-locked
    `test_fail_closed_replay_adversarial.py` still uses)."""
    entry = _entry("c1", None, turn_index=0)
    assert filter_trail([entry], frozenset()) == []


def test_current_turn_exemption_keeps_none_provenance_entry_for_current_turn() -> None:
    """A `None`-provenance, denied (status != "ok") entry belonging to the
    turn CURRENTLY in progress is kept — it carries no result rows
    regardless, so surfacing it to the model for self-correction leaks
    nothing."""
    entry = _entry("c1", None, turn_index=2, status="denied")
    filtered = filter_trail([entry], frozenset(), current_turn_index=2)
    assert [e.tool_call_id for e in filtered] == ["c1"]


def test_current_turn_exemption_does_not_apply_to_prior_turns() -> None:
    """The identical `None`-provenance denied entry, once it is a PRIOR turn
    (the loop has moved on to `current_turn_index=3`), is dropped exactly as
    before — cross-turn D44 is untouched by the exemption."""
    entry = _entry("c1", None, turn_index=2, status="denied")
    filtered = filter_trail([entry], frozenset(), current_turn_index=3)
    assert filtered == []


def test_current_turn_exemption_still_applies_normal_scope_check_to_other_turns() -> None:
    """A mixed trail: the current turn's denied entry is kept, a prior turn's
    denied entry is dropped, and a prior turn's determined in-scope entry is
    kept via the ordinary (unexempted) path."""
    entries = [
        _entry("prior_denied", None, turn_index=0, status="denied"),
        _entry("prior_ok", frozenset({(_E, "Department")}), turn_index=0),
        _entry("current_denied", None, turn_index=1, status="denied"),
    ]
    scope = frozenset({f"{_E}.Department"})
    filtered = filter_trail(entries, scope, current_turn_index=1)
    assert [e.tool_call_id for e in filtered] == ["prior_ok", "current_denied"]


# ---------------------------------------------------------------------------
# BLOCKER regression (2026-07-01 second pass): the current-turn exemption
# must be STATUS-GATED. A successful (status="ok"), data-bearing current-turn
# entry whose provenance is NOT a subset of column_scope must always be
# DROPPED — even though it belongs to the turn currently in progress — because
# it carries real result rows (result_preview is populated for status="ok").
# Only denied/errored (no-result-rows) current-turn entries may be exempt.
# ---------------------------------------------------------------------------


def test_successful_current_turn_entry_out_of_scope_is_still_dropped() -> None:
    """A successful `sampleRows`-style entry (declarative all-columns
    provenance) that exceeds `column_scope`, belonging to the turn CURRENTLY
    in progress, must be DROPPED, not exempted — it carries real result rows
    (PII), unlike a denied/errored entry."""
    preview = ResultPreview(
        columns=["EmployeeCode", "Name", "Salary"],
        row_count=1,
        truncated=False,
        preview_rows=[["E1", "Jane Doe", "999999"]],
    )
    entry = _entry(
        "c1",
        frozenset({(_E, "EmployeeCode"), (_E, "Name"), (_E, "Salary")}),
        turn_index=2,
        status="ok",
        result_preview=preview,
    )
    scope = frozenset({f"{_E}.EmployeeCode"})  # narrow scope — Name/Salary not granted
    filtered = filter_trail([entry], scope, current_turn_index=2)
    assert filtered == []


def test_successful_current_turn_entry_in_scope_is_kept() -> None:
    """A successful current-turn entry whose provenance IS a subset of scope
    is kept via the ordinary (non-exempt) path — status-gating never drops a
    legitimately in-scope result."""
    entry = _entry(
        "c1", frozenset({(_E, "EmployeeCode")}), turn_index=2, status="ok"
    )
    scope = frozenset({f"{_E}.EmployeeCode"})
    filtered = filter_trail([entry], scope, current_turn_index=2)
    assert [e.tool_call_id for e in filtered] == ["c1"]


def test_denied_current_turn_entry_still_exempt_after_status_gate() -> None:
    """The intended self-correction exemption still applies to a denied/
    errored (no-row) current-turn entry, whose provenance is always `None`."""
    entry = _entry("c1", None, turn_index=2, status="denied")
    filtered = filter_trail([entry], frozenset({f"{_E}.EmployeeCode"}), current_turn_index=2)
    assert [e.tool_call_id for e in filtered] == ["c1"]


def test_errored_current_turn_entry_still_exempt_after_status_gate() -> None:
    entry = _entry("c1", None, turn_index=2, status="error")
    filtered = filter_trail([entry], frozenset({f"{_E}.EmployeeCode"}), current_turn_index=2)
    assert [e.tool_call_id for e in filtered] == ["c1"]


def test_compute_scope_hash_is_stable_and_order_independent() -> None:
    scope_a = frozenset({f"{_E}.Department", f"{_E}.EmployeeCode"})
    scope_b = frozenset({f"{_E}.EmployeeCode", f"{_E}.Department"})
    assert compute_scope_hash(scope_a) == compute_scope_hash(scope_b)


def test_compute_scope_hash_differs_for_different_scopes() -> None:
    assert compute_scope_hash(frozenset({f"{_E}.Department"})) != compute_scope_hash(
        frozenset({f"{_P}.Amount"})
    )


# ---------------------------------------------------------------------------
# B1 (2026-07-01 D44 clarification): the same filter, for TurnMessages
# ---------------------------------------------------------------------------


def _message(
    role: str, provenance: frozenset[tuple[str, str]] | None, content: str = "..."
) -> TurnMessage:
    return TurnMessage(
        turn_index=0, role=role, content=content, ts="2026-07-01T00:00:00+00:00", provenance=provenance
    )


def test_user_message_always_kept_regardless_of_provenance() -> None:
    # A user message's own default provenance is frozenset() (never None),
    # but even a pathological None must still be kept — user input carries
    # no warehouse-derived data by construction.
    assert is_message_in_scope(_message("user", frozenset()), frozenset({f"{_E}.Department"})) is True
    assert is_message_in_scope(_message("user", None), frozenset({f"{_E}.Department"})) is True


def test_assistant_message_with_out_of_scope_provenance_is_dropped() -> None:
    msg = _message("assistant", frozenset({(_P, "Amount")}))
    scope = frozenset({f"{_E}.Department"})  # payroll.Amount not granted
    assert is_message_in_scope(msg, scope) is False


def test_assistant_message_with_in_scope_provenance_is_kept() -> None:
    msg = _message("assistant", frozenset({(_E, "Department")}))
    scope = frozenset({f"{_E}.Department"})
    assert is_message_in_scope(msg, scope) is True


def test_assistant_message_determined_empty_provenance_always_kept() -> None:
    """A clarification-only turn (no tool calls) tags its assistant message
    with frozenset() — always kept, any scope."""
    msg = _message("assistant", frozenset())
    assert is_message_in_scope(msg, frozenset({f"{_E}.Department"})) is True
    assert is_message_in_scope(msg, frozenset()) is True


def test_assistant_message_undetermined_provenance_always_dropped() -> None:
    msg = _message("assistant", None)
    assert is_message_in_scope(msg, frozenset()) is False  # even allow-all
    assert is_message_in_scope(msg, frozenset({f"{_E}.Department"})) is False


def test_filter_messages_preserves_order_and_drops_out_of_scope() -> None:
    messages = [
        _message("user", frozenset(), content="u1"),
        _message("assistant", frozenset({(_E, "Department")}), content="a1_in_scope"),
        _message("assistant", frozenset({(_P, "Amount")}), content="a2_out_of_scope"),
        _message("assistant", None, content="a3_undetermined"),
        _message("user", frozenset(), content="u2"),
    ]
    scope = frozenset({f"{_E}.Department"})
    filtered = filter_messages(messages, scope)
    assert [m.content for m in filtered] == ["u1", "a1_in_scope", "u2"]
