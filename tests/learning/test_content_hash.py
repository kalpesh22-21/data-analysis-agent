"""D96-content-hash-canonical (design §5, matrix row 4).

`compute_content_hash` must be:
  - DETERMINISTIC across processes/runs and independent of dict/arg key-order;
  - INVARIANT under the deliberately-excluded fields (all timestamps,
    `learning_status`, `learning_content_hash`, `result_full_ref`,
    `result_preview`, `pause_checkpoint`, and every `provenance` set);
  - SENSITIVE to any change in the transcript-identifying subset (a message's
    turn/role/content; a tool entry's turn/tool_call_id/tool_name/args/status/
    error_code).
"""

from __future__ import annotations

from dataclasses import replace

from data_agent.learning.models import compute_content_hash
from data_agent.runtime.session.models import (
    PauseCheckpoint,
    ResultPreview,
    SessionDoc,
)

from .conftest import make_message, make_trail_entry

# The exact args carried by the baseline trail entry — reused by every variant
# that must differ ONLY in an excluded field, so the transcript subset stays fixed.
_BASE_ARGS = {"sql": "SELECT sum(ot) FROM pay", "limit": 100}


def _doc(**overrides) -> SessionDoc:
    base = dict(
        session_id="sess-hash",
        created_at="2026-07-01T00:00:00+00:00",
        last_activity="2026-07-01T00:05:00+00:00",
        learning_status="active",
        messages=[
            make_message(0, "user", "how much overtime did sales pay?"),
            make_message(0, "assistant", "Let me check."),
        ],
        tool_trail=[
            make_trail_entry(
                turn_index=0,
                tool_call_id="call_1",
                tool_name="runQuery",
                args=dict(_BASE_ARGS),
                status="ok",
                error_code=None,
            )
        ],
    )
    base.update(overrides)
    return SessionDoc(**base)


def test_hash_is_deterministic_and_hex_sha256() -> None:
    h1 = compute_content_hash(_doc())
    h2 = compute_content_hash(_doc())
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_hash_ignores_arg_key_order() -> None:
    a = _doc(
        tool_trail=[
            make_trail_entry(args={"sql": "SELECT 1", "limit": 100, "database": "hr"})
        ]
    )
    b = _doc(
        tool_trail=[
            make_trail_entry(args={"limit": 100, "database": "hr", "sql": "SELECT 1"})
        ]
    )
    assert compute_content_hash(a) == compute_content_hash(b)


def test_hash_ignores_all_timestamps() -> None:
    baseline = compute_content_hash(_doc())
    # created_at / last_activity differ:
    assert compute_content_hash(
        _doc(created_at="1999-01-01T00:00:00+00:00", last_activity="2030-01-01T00:00:00+00:00")
    ) == baseline
    # per-message ts differs:
    assert compute_content_hash(
        _doc(
            messages=[
                make_message(0, "user", "how much overtime did sales pay?", ts="2000-01-01T00:00:00+00:00"),
                make_message(0, "assistant", "Let me check.", ts="2000-01-01T00:00:00+00:00"),
            ]
        )
    ) == baseline
    # per-trail-entry ts differs:
    assert compute_content_hash(
        _doc(tool_trail=[make_trail_entry(args=dict(_BASE_ARGS), ts="2000-01-01T00:00:00+00:00")])
    ) == baseline


def test_hash_ignores_learning_status_and_content_hash() -> None:
    baseline = compute_content_hash(_doc())
    for status in ("pending", "queued", "processing", "done", "dead_letter"):
        assert compute_content_hash(_doc(learning_status=status)) == baseline
    assert compute_content_hash(_doc(learning_content_hash="deadbeef")) == baseline


def test_hash_ignores_result_ref_preview_and_provenance() -> None:
    baseline = compute_content_hash(_doc())
    preview = ResultPreview(
        columns=["a"], row_count=5, truncated=True, preview_rows=[[1], [2]]
    )
    varied = _doc(
        tool_trail=[
            make_trail_entry(
                args=dict(_BASE_ARGS),
                result_full_ref="result::abc-123",
                result_preview=preview,
                provenance=frozenset({("hr.pay", "ot"), ("hr.pay", "dept")}),
            )
        ]
    )
    assert compute_content_hash(varied) == baseline
    # message-level provenance is also excluded:
    msgs = [
        replace(make_message(0, "user", "how much overtime did sales pay?"),
                provenance=frozenset({("hr.pay", "ot")})),
        replace(make_message(0, "assistant", "Let me check."), provenance=None),
    ]
    assert compute_content_hash(_doc(messages=msgs)) == baseline


def test_hash_ignores_pause_checkpoint() -> None:
    """The `context_summary_cache` half of this test went with the field (L2, cleanup
    2026-08). Its exclusion needed no replacement pin: `compute_content_hash` builds its
    payload from an ALLOWLIST (`session_id` + messages + tool trail), so a session-doc
    field it does not name cannot reach the digest whether or not that field exists.
    Verified empirically at the removal — the baseline hash of `_doc()` was byte-
    identical before and after.
    """
    baseline = compute_content_hash(_doc())
    checkpoint = PauseCheckpoint(
        reason="askUser",
        pending_question={"question": "Which dept?", "options": None},
        awaiting="user_answer",
        consumed=False,
    )
    assert compute_content_hash(_doc(pause_checkpoint=checkpoint)) == baseline


# --- SENSITIVITY: any transcript change MUST flip the hash -------------------


def test_hash_changes_when_message_content_changes() -> None:
    baseline = compute_content_hash(_doc())
    changed = _doc(
        messages=[
            make_message(0, "user", "how much overtime did MARKETING pay?"),
            make_message(0, "assistant", "Let me check."),
        ]
    )
    assert compute_content_hash(changed) != baseline


def test_hash_changes_when_message_role_changes() -> None:
    baseline = compute_content_hash(_doc())
    changed = _doc(
        messages=[
            make_message(0, "assistant", "how much overtime did sales pay?"),
            make_message(0, "assistant", "Let me check."),
        ]
    )
    assert compute_content_hash(changed) != baseline


def test_hash_changes_when_message_turn_index_changes() -> None:
    baseline = compute_content_hash(_doc())
    changed = _doc(
        messages=[
            make_message(0, "user", "how much overtime did sales pay?"),
            make_message(1, "assistant", "Let me check."),
        ]
    )
    assert compute_content_hash(changed) != baseline


def test_hash_changes_when_a_message_is_added() -> None:
    baseline = compute_content_hash(_doc())
    changed = _doc(
        messages=[
            make_message(0, "user", "how much overtime did sales pay?"),
            make_message(0, "assistant", "Let me check."),
            make_message(1, "user", "and marketing?"),
        ]
    )
    assert compute_content_hash(changed) != baseline


def test_hash_changes_when_tool_name_changes() -> None:
    baseline = compute_content_hash(_doc())
    changed = _doc(tool_trail=[make_trail_entry(tool_name="explainQuery",
                                                args={"sql": "SELECT sum(ot) FROM pay", "limit": 100})])
    assert compute_content_hash(changed) != baseline


def test_hash_changes_when_tool_arg_value_changes() -> None:
    baseline = compute_content_hash(_doc())
    changed = _doc(
        tool_trail=[make_trail_entry(args={"sql": "SELECT sum(ot) FROM pay2", "limit": 100})]
    )
    assert compute_content_hash(changed) != baseline


def test_hash_changes_when_tool_status_changes() -> None:
    baseline = compute_content_hash(_doc())
    changed = _doc(
        tool_trail=[make_trail_entry(args={"sql": "SELECT sum(ot) FROM pay", "limit": 100},
                                     status="error", error_code=None)]
    )
    assert compute_content_hash(changed) != baseline


def test_hash_changes_when_tool_error_code_changes() -> None:
    baseline = compute_content_hash(_doc())
    changed = _doc(
        tool_trail=[make_trail_entry(args={"sql": "SELECT sum(ot) FROM pay", "limit": 100},
                                     status="error", error_code="SYNTAX_ERROR")]
    )
    assert compute_content_hash(changed) != baseline


def test_hash_changes_when_tool_call_id_changes() -> None:
    baseline = compute_content_hash(_doc())
    changed = _doc(
        tool_trail=[make_trail_entry(tool_call_id="call_999",
                                     args={"sql": "SELECT sum(ot) FROM pay", "limit": 100})]
    )
    assert compute_content_hash(changed) != baseline


def test_hash_changes_when_session_id_changes() -> None:
    assert compute_content_hash(_doc(session_id="sess-A")) != compute_content_hash(
        _doc(session_id="sess-B")
    )
