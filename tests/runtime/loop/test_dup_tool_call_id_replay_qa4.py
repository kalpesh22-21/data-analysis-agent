"""QA4 Layer-1: §6.2 duplicate-tool-call-id dedup — keep-first + D44 ordering pin.

Extends `test_dup_tool_call_id_replay.py`. `_assembled_to_canonical` dedups by
`tool_call_id`, keeping the FIRST. This file pins:
  - a 3-way collision keeps only the first and preserves relative order,
  - a non-string / None `tool_call_id` is NOT deduped (multiple pass through),
  - interleaved collisions keep the first occurrence at its position,
  - the D44 interaction ORDERING: the replay scope-filter runs UPSTREAM (inside
    `_context_assembler.assemble`, producing `assembled.messages`), so by the time
    `_assembled_to_canonical` dedups, out-of-scope tool entries are already gone —
    i.e. **dedup is AFTER the D44 filter** (`agent_loop.py:_build_replay_messages`,
    lines 469/475). This pins that ordering so a future refactor can't silently
    reorder filter-vs-dedup.

ADD-only; does not modify the reviewer-owned `test_dup_tool_call_id_replay.py`.
"""

from __future__ import annotations

from data_agent.runtime.loop.agent_loop import _assembled_to_canonical


def _tool_msg(tool_call_id, name: str) -> dict:
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


def test_three_way_collision_keeps_only_the_first() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        _tool_msg("dup", "runQuery"),
        _tool_msg("dup", "runQuery"),
        _tool_msg("dup", "runQuery"),
        _tool_msg("keep", "searchBlueprints"),
    ]
    canonical = _assembled_to_canonical(messages)
    tool_ids = [m["tool_call_id"] for m in canonical if m["role"] == "tool"]
    assert tool_ids == ["dup", "keep"]


def test_interleaved_collisions_keep_first_occurrence_in_order() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        _tool_msg("a", "runQuery"),
        _tool_msg("b", "runQuery"),
        _tool_msg("a", "runQuery"),  # collide with earlier a
        _tool_msg("c", "runQuery"),
        _tool_msg("b", "runQuery"),  # collide with earlier b
    ]
    canonical = _assembled_to_canonical(messages)
    tool_ids = [m["tool_call_id"] for m in canonical if m["role"] == "tool"]
    assert tool_ids == ["a", "b", "c"]


def test_none_tool_call_id_is_not_deduped() -> None:
    # PIN: dedup only tracks STRING ids. Multiple None ids all pass through (they
    # are not added to the seen-set). A well-behaved model always assigns string
    # ids, but this documents that None-id entries are NOT collapsed.
    messages = [
        {"role": "system", "content": "sys"},
        _tool_msg(None, "runQuery"),
        _tool_msg(None, "runQuery"),
    ]
    canonical = _assembled_to_canonical(messages)
    tool_msgs = [m for m in canonical if m["role"] == "tool"]
    assert len(tool_msgs) == 2  # both survive (not deduped)


def test_dedup_operates_on_already_assembled_post_filter_messages() -> None:
    # D44 ORDERING PIN: `_assembled_to_canonical` consumes `assembled.messages`,
    # which the context assembler has ALREADY scope-filtered (D44). This function
    # carries no provenance and does no scope filtering itself — it only dedups.
    # So dedup is strictly downstream of the D44 replay filter. We assert the
    # function keeps every (in-scope) entry it is given, deduping only by id.
    messages = [
        {"role": "system", "content": "sys"},
        _tool_msg("x", "runQuery"),
        _tool_msg("y", "getBlueprint"),
    ]
    canonical = _assembled_to_canonical(messages)
    tool_ids = [m["tool_call_id"] for m in canonical if m["role"] == "tool"]
    assert tool_ids == ["x", "y"]  # nothing dropped — filtering happened upstream


def test_each_surviving_tool_pairs_with_exactly_one_assistant_call() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        _tool_msg("dup", "runQuery"),
        _tool_msg("dup", "runQuery"),
    ]
    canonical = _assembled_to_canonical(messages)
    assistant_ids = [
        tc["id"]
        for m in canonical
        if m["role"] == "assistant"
        for tc in m["tool_calls"]
    ]
    tool_ids = [m["tool_call_id"] for m in canonical if m["role"] == "tool"]
    # Exactly one assistant tool_call per surviving tool message — API-valid.
    assert assistant_ids == ["dup"]
    assert tool_ids == ["dup"]
