"""Phase-1 interleave tests for `context/assembly.py`.

These lock the redesign's core promise: the model request is a TRUE chronological
interleave BY TURN — `[system, (prior turns: question -> tool pairs -> answer)…,
retrieval(user), current question, current tool pairs]` — produced by a STABLE
SORT on the merge key `(turn_index, ts, stream_rank)` over BOTH `doc.tool_trail`
(tool pairs) and `doc.messages` (user/assistant dialogue) loaded from the SAME
session doc. `ts` (an ISO-8601 `_now_iso()` stamp on both streams) drives real
order; `stream_rank` (user=0, trail=1, assistant=2) is only a tie-break for an
identical `(turn_index, ts)`.

The correctness win these guard (over the old two-block layout) is that a turn's
tool results now sit chronologically NEXT TO the question that triggered them —
including an askUser mid-turn user answer landing BETWEEN the tool pairs its `ts`
falls between.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.context.assembly import DATE_ANCHOR_SQL_NOTE, ContextAssembler
from data_agent.runtime.context.budget import fit_request_to_budget
from data_agent.runtime.loop.agent_loop import _assembled_to_canonical
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry, TurnMessage

_E = "dbpcm_warehouse.employee"
_P = "dbpcm_warehouse.payroll"
_IN_SCOPE = frozenset({(_E, "Department")})
_OUT_SCOPE = frozenset({(_P, "Amount")})
_SCOPE = frozenset({f"{_E}.Department"})


def _ts(n: int) -> str:
    """A distinct, lexicographically-sortable ISO timestamp for ordinal *n*."""
    return f"2026-07-01T00:00:{n:02d}+00:00"


# The DATE ANCHOR render item, which lands immediately before the current
# question on every assemble that is given a `current_turn_index`. Named rather
# than spelled out at each call site so these lists stay about ORDER, which is
# what they exist to lock — and derived from `_ts` so the date and the fixture
# clock cannot drift apart.
_ANCHOR = f"user:Today's date is {_ts(0)[:10]}.{DATE_ANCHOR_SQL_NOTE}"


def _msg(turn: int, role: str, content: str, ts: str, provenance=_IN_SCOPE) -> TurnMessage:
    # User messages carry no warehouse data (always kept); assistant messages are
    # provenance-tagged like the trail. Default in-scope so the merge-order tests
    # are not perturbed by the scope filter.
    return TurnMessage(
        turn_index=turn,
        role=role,
        content=content,
        ts=ts,
        provenance=frozenset() if role == "user" else provenance,
    )


def _tool(
    turn: int,
    tool_call_id: str,
    ts: str,
    *,
    provenance: frozenset | None = _IN_SCOPE,
    status: str = "ok",
    sql: str = "SELECT Department FROM employee",
) -> TrailEntry:
    return TrailEntry(
        turn_index=turn,
        tool_call_id=tool_call_id,
        tool_name="runQuery",
        args={"sql": sql},
        status=status,
        error_code=None if status == "ok" else "SOME_ERROR",
        provenance=provenance,
        result_preview=None,
        result_full_ref=None,
        ts=ts,
    )


def _ident(m: dict[str, Any]) -> str:
    """A compact identity label for one assemble()-render item."""
    role = m["role"]
    if role == "tool":
        suffix = "*" if m.get("withheld_sentinel") else ""
        return f"tool:{m['tool_call_id']}{suffix}"
    return f"{role}:{m['content']}"


def _pairing_intact(messages: list[dict[str, Any]]) -> bool:
    """Every `tool` message is immediately answerable to a preceding assistant
    `tool_calls` with a matching id, and no `tool_calls` is left dangling."""
    open_ids: set[str] = set()
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                open_ids.add(tc["id"])
        elif m["role"] == "tool":
            if m["tool_call_id"] not in open_ids:
                return False
            open_ids.discard(m["tool_call_id"])
    return not open_ids


async def test_three_turn_interleave_including_askuser_midturn_answer() -> None:
    """The exact interleaved sequence for a 3-turn session — each turn is
    question -> tool pair(s) -> answer — and turn 1 is an askUser turn whose
    RESUMED user answer lands BETWEEN its two tool pairs by `ts` (the correctness
    win the redesign exists for: the old two-block layout put every tool result
    before every dialogue message)."""
    store = InMemorySessionStore()

    # Turn 0: q0 -> tool t0 -> a0.
    await store.append_message("s", _msg(0, "user", "q0", _ts(1)))
    await store.append_trail_entry("s", _tool(0, "t0", _ts(2)))
    await store.append_message("s", _msg(0, "assistant", "a0", _ts(3)))

    # Turn 1 (askUser): q1 -> tool t1a -> [resumed user answer u1] -> tool t1b -> a1.
    await store.append_message("s", _msg(1, "user", "q1", _ts(4)))
    await store.append_trail_entry("s", _tool(1, "t1a", _ts(5)))
    await store.append_message("s", _msg(1, "user", "u1", _ts(6)))  # askUser answer, mid-turn
    await store.append_trail_entry("s", _tool(1, "t1b", _ts(7)))
    await store.append_message("s", _msg(1, "assistant", "a1", _ts(8)))

    # Turn 2 (current, no answer yet): q2 -> tool t2.
    await store.append_message("s", _msg(2, "user", "q2", _ts(9)))
    await store.append_trail_entry("s", _tool(2, "t2", _ts(10)))

    assembler = ContextAssembler(store)
    assembled = await assembler.assemble("s", _SCOPE, current_turn_index=2)

    assert [_ident(m) for m in assembled.messages] == [
        "user:q0",
        "tool:t0",
        "assistant:a0",
        "user:q1",
        "tool:t1a",
        "user:u1",  # the askUser answer — BETWEEN t1a and t1b, by ts (the win)
        "tool:t1b",
        "assistant:a1",
        _ANCHOR,  # inserted immediately before the current question
        "user:q2",
        "tool:t2",
    ]


async def test_offpath_pure_interleaved_history_no_injection() -> None:
    """With NO base prompt, NO retrieval, NO discovery and NO fit budget, assemble
    returns a PURE interleaved history list — nothing is injected. Guards against a
    Layer-1 fake (retrieval/summary/base) silently entering the unconfigured path."""
    store = InMemorySessionStore()
    await store.append_message("s", _msg(0, "user", "q0", _ts(1)))
    await store.append_trail_entry("s", _tool(0, "c0", _ts(2)))
    await store.append_message("s", _msg(0, "assistant", "a0", _ts(3)))
    await store.append_message("s", _msg(1, "user", "q1", _ts(4)))
    await store.append_trail_entry("s", _tool(1, "c1", _ts(5)))

    assembler = ContextAssembler(store)  # base=None, retrieval=None
    assembled = await assembler.assemble("s", _SCOPE, current_turn_index=1)

    # "Nothing is injected" means nothing OPTIONAL: the date anchor is
    # unconditional whenever the turn is identified, because a model with no
    # grounded present silently mis-resolves every relative window (issues-stack
    # B2). It is `role:"user"`, so the no-system-message guarantee below is exactly
    # as strong as it was.
    assert [_ident(m) for m in assembled.messages] == [
        "user:q0",
        "tool:c0",
        "assistant:a0",
        _ANCHOR,
        "user:q1",
        "tool:c1",
    ]
    assert all(m["role"] != "system" for m in assembled.messages)


async def test_scope_filter_applied_to_both_streams_no_orphan() -> None:
    """A prior turn's OUT-OF-SCOPE tool entry AND its OUT-OF-SCOPE assistant answer
    are both absent under a narrowed scope, while that turn's user question (no
    warehouse data) remains — and no orphaned `tool` render item is left behind."""
    store = InMemorySessionStore()
    # Turn 0: question kept; tool + assistant answer are payroll-scoped → dropped.
    await store.append_message("s", _msg(0, "user", "q0", _ts(1)))
    await store.append_trail_entry("s", _tool(0, "t0", _ts(2), provenance=_OUT_SCOPE))
    await store.append_message("s", _msg(0, "assistant", "a0", _ts(3), provenance=_OUT_SCOPE))
    # Turn 1 (current): in-scope question + tool survive.
    await store.append_message("s", _msg(1, "user", "q1", _ts(4)))
    await store.append_trail_entry("s", _tool(1, "t1", _ts(5)))

    assembler = ContextAssembler(store)
    assembled = await assembler.assemble("s", _SCOPE, current_turn_index=1)

    idents = [_ident(m) for m in assembled.messages]
    assert idents == ["user:q0", _ANCHOR, "user:q1", "tool:t1"]
    assert "tool:t0" not in idents  # out-of-scope tool dropped...
    assert "assistant:a0" not in idents  # ...along with the answer derived from it
    assert assembled.dropped_by_scope_count == 1  # only the trail counts here
    # And after pair-expansion there is no dangling tool.
    assert _pairing_intact(_assembled_to_canonical(assembled.messages))


async def test_current_turn_sentinel_lands_in_its_ts_slot() -> None:
    """A current-turn `ok`+`None` (stranded) entry becomes a withheld sentinel that
    occupies ITS OWN `ts` slot — chronologically BETWEEN the surrounding in-scope
    tool entries of the same turn, not appended after them."""
    store = InMemorySessionStore()
    await store.append_message("s", _msg(0, "user", "q0", _ts(1)))
    await store.append_trail_entry("s", _tool(0, "x", _ts(2)))  # in scope
    await store.append_trail_entry("s", _tool(0, "s", _ts(3), provenance=None))  # stranded → sentinel
    await store.append_trail_entry("s", _tool(0, "y", _ts(4)))  # in scope

    assembler = ContextAssembler(store)
    assembled = await assembler.assemble("s", _SCOPE, current_turn_index=0)

    assert [_ident(m) for m in assembled.messages] == [
        _ANCHOR,  # before the ONLY user message, which is this turn's question
        "user:q0",
        "tool:x",
        "tool:s*",  # the sentinel, in its ts slot BETWEEN x and y
        "tool:y",
    ]
    sentinel = next(m for m in assembled.messages if m.get("withheld_sentinel"))
    assert sentinel["tool_call_id"] == "s"


async def test_colliding_ts_tie_break_is_deterministic_by_stream_rank() -> None:
    """Items sharing an identical `(turn_index, ts)` sort deterministically by
    `stream_rank` (user=0, trail=1, assistant=2) — no crash, stable — and two trail
    entries at the SAME ts keep raw-trail insertion order."""
    store = InMemorySessionStore()
    same = _ts(5)
    # All four at the identical ts; two trail entries share it too.
    await store.append_message("s", _msg(0, "assistant", "a0", same))
    await store.append_trail_entry("s", _tool(0, "cB", same))
    await store.append_message("s", _msg(0, "user", "q0", same))
    await store.append_trail_entry("s", _tool(0, "cA", same))

    assembler = ContextAssembler(store)
    first = await assembler.assemble("s", _SCOPE, current_turn_index=0)
    second = await assembler.assemble("s", _SCOPE, current_turn_index=0)

    idents = [_ident(m) for m in first.messages]
    # user(rank 0) -> trail(rank 1, in insertion order cB then cA) -> assistant(rank 2).
    assert idents == [_ANCHOR, "user:q0", "tool:cB", "tool:cA", "assistant:a0"]
    # Deterministic across rebuilds (D45 byte-stability under a colliding ts).
    assert [_ident(m) for m in second.messages] == idents


async def test_pairing_atomic_after_interleave_and_after_fit() -> None:
    """The assistant(tool_calls)/tool pairing is atomic through the whole pipeline:
    after the interleave + pair-expansion, AND after `fit_request_to_budget` drops
    the oldest middle units under a tiny budget — no orphan tool, no dangling
    tool_calls."""
    store = InMemorySessionStore()
    pad = " -- " + "padding " * 200  # make each turn a heavy, droppable unit
    # Turns 0..2 completed (question -> tool -> answer); turn 3 is the CURRENT
    # in-progress turn (question -> tool, NO answer persisted yet — as in the loop).
    for turn in range(3):
        base = turn * 4
        await store.append_message("s", _msg(turn, "user", f"q{turn}", _ts(base + 1)))
        await store.append_trail_entry(
            "s", _tool(turn, f"c{turn}", _ts(base + 2), sql=f"SELECT {turn}{pad}")
        )
        await store.append_message("s", _msg(turn, "assistant", f"a{turn}", _ts(base + 3)))
    await store.append_message("s", _msg(3, "user", "q3", _ts(13)))
    await store.append_trail_entry("s", _tool(3, "c3", _ts(14), sql=f"SELECT 3{pad}"))

    assembler = ContextAssembler(store)
    assembled = await assembler.assemble("s", _SCOPE, current_turn_index=3)
    canonical = _assembled_to_canonical(assembled.messages)

    # After interleave + expansion.
    assert _pairing_intact(canonical)

    # After a tiny total-request fit that MUST drop whole oldest units.
    fit = fit_request_to_budget(canonical, token_budget=300)
    assert fit.dropped_messages > 0
    assert _pairing_intact(fit.messages)
    # The current question is pinned as the tail and survives the trim.
    assert any(m["role"] == "user" and m.get("content") == "q3" for m in fit.messages)
