"""Layer-1: §6.2 duplicate-tool-call-id defensive dedup at REPLAY.

The OpenAI API requires every `tool_call_id` in a turn to be unique with exactly
one matching `tool` response. A legacy/corrupt trail (or a paused-and-resumed DAG
re-appending a colliding id) would otherwise emit two `tool` messages with the
same id → an API 400 that aborts the turn. `_assembled_to_canonical` drops the
duplicate (keeping the FIRST), fail-closed toward a valid (if lossy) replay.
"""

from __future__ import annotations

from data_agent.runtime.loop.agent_loop import _assembled_to_canonical


def _tool_msg(tool_call_id: str, name: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "tool_name": name,
        "args": {},
        "status": "ok",
        "error_code": None,
        "user_message": None,
        "result_preview": None,
    }


def test_duplicate_tool_call_id_is_dropped_keeping_first() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        _tool_msg("dup", "searchBlueprints"),
        _tool_msg("dup", "searchBlueprints"),  # collides — must be dropped
        _tool_msg("other", "runQuery"),
    ]
    canonical = _assembled_to_canonical(messages)
    tool_ids = [m["tool_call_id"] for m in canonical if m["role"] == "tool"]
    # Exactly one `tool` message per unique id — the API-valid shape.
    assert tool_ids == ["dup", "other"]
    # Each tool message pairs with exactly one assistant tool_call.
    assistant_ids = [
        tc["id"]
        for m in canonical
        if m["role"] == "assistant"
        for tc in m["tool_calls"]
    ]
    assert assistant_ids == ["dup", "other"]


def test_unique_ids_pass_through_unchanged() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        _tool_msg("a", "runQuery"),
        _tool_msg("b", "runQuery"),
    ]
    canonical = _assembled_to_canonical(messages)
    tool_ids = [m["tool_call_id"] for m in canonical if m["role"] == "tool"]
    assert tool_ids == ["a", "b"]
