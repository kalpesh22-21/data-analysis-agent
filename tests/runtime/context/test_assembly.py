"""Unit tests for context/assembly.py — D50 fixed-order pipeline (Layer 1, InMemorySessionStore)."""

from __future__ import annotations

import asyncio
import time

from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry

_P = "dbpcm_warehouse.payroll"
_E = "dbpcm_warehouse.employee"


def _entry(
    tool_call_id: str,
    provenance: frozenset | None,
    sql: str = "SELECT ...",
    *,
    turn_index: int = 0,
    status: str = "ok",
) -> TrailEntry:
    return TrailEntry(
        turn_index=turn_index,
        tool_call_id=tool_call_id,
        tool_name="runQuery",
        args={"sql": sql},
        status=status,
        error_code=None if status == "ok" else "SOME_ERROR",
        provenance=provenance,
        result_preview=None,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )


async def test_assemble_filters_out_of_scope_before_compaction() -> None:
    store = InMemorySessionStore()
    in_scope_entry = _entry("c1", frozenset({(_E, "Department")}), sql="SELECT Department FROM employee")
    out_of_scope_entry = _entry("c2", frozenset({(_P, "Amount")}), sql="SELECT Amount FROM payroll")
    undetermined_entry = _entry("c3", None, sql="SELECT * FROM generateRandom(...)")

    await store.append_trail_entry("sess-1", in_scope_entry)
    await store.append_trail_entry("sess-1", out_of_scope_entry)
    await store.append_trail_entry("sess-1", undetermined_entry)

    assembler = ContextAssembler(store, history_token_budget=100_000)
    scope = frozenset({f"{_E}.Department"})
    assembled = await assembler.assemble("sess-1", scope)

    tool_call_ids = [m["tool_call_id"] for m in assembled.messages if "tool_call_id" in m]
    assert tool_call_ids == ["c1"]
    assert assembled.dropped_by_scope_count == 2


async def test_assemble_allow_all_empty_scope_keeps_determined_entries() -> None:
    store = InMemorySessionStore()
    await store.append_trail_entry("sess-1", _entry("c1", frozenset({(_P, "Amount")})))
    await store.append_trail_entry("sess-1", _entry("c2", None))  # still dropped, undetermined

    assembler = ContextAssembler(store, history_token_budget=100_000)
    assembled = await assembler.assemble("sess-1", frozenset())

    tool_call_ids = [m["tool_call_id"] for m in assembled.messages if "tool_call_id" in m]
    assert tool_call_ids == ["c1"]
    assert assembled.dropped_by_scope_count == 1


async def test_assemble_never_shows_summarizer_an_out_of_scope_entry() -> None:
    """D50: filtering strictly before compaction — the summarizer never sees a dropped entry."""
    seen_by_summarizer: list = []

    def spy_summarizer(entries):
        seen_by_summarizer.extend(entries)
        return "summary"

    store = InMemorySessionStore()
    scope = frozenset({f"{_E}.Department"})
    for i in range(10):
        await store.append_trail_entry(
            "sess-1", _entry(f"in-{i}", frozenset({(_E, "Department")}), sql=f"SELECT {i}")
        )
    await store.append_trail_entry(
        "sess-1", _entry("forbidden", frozenset({(_P, "Amount")}), sql="SELECT Amount")
    )

    assembler = ContextAssembler(
        store, history_token_budget=1, summarizer=spy_summarizer
    )
    await assembler.assemble("sess-1", scope)

    seen_ids = {e.tool_call_id for e in seen_by_summarizer}
    assert "forbidden" not in seen_ids


async def test_assemble_compaction_applied_flag() -> None:
    store = InMemorySessionStore()
    for i in range(5):
        await store.append_trail_entry("sess-1", _entry(f"c{i}", frozenset(), sql=f"SELECT {i} FROM padding"))

    tiny_budget_assembler = ContextAssembler(store, history_token_budget=1)
    assembled_tiny = await tiny_budget_assembler.assemble("sess-1", frozenset())
    assert assembled_tiny.compaction_applied is True

    huge_budget_assembler = ContextAssembler(store, history_token_budget=1_000_000)
    assembled_huge = await huge_budget_assembler.assemble("sess-1", frozenset())
    assert assembled_huge.compaction_applied is False


# ---------------------------------------------------------------------------
# S2: a cache-miss summarizer call must never block the event loop
# ---------------------------------------------------------------------------


async def test_summarizer_cache_miss_never_blocks_other_concurrent_event_loop_work() -> None:
    """A blocking (synchronous `time.sleep`) summarizer — modeling
    `context/llm_summarizer.py`'s genuinely-blocking `future.result()` wait —
    must run off the event-loop thread (`asyncio.to_thread`, S2), so a SHORTER
    concurrent coroutine on the same loop finishes first instead of being
    starved until the summarizer returns."""
    order: list[str] = []

    def slow_blocking_summarizer(entries) -> str:  # noqa: ANN001
        time.sleep(0.2)
        order.append("summarizer_done")
        return "summary"

    store = InMemorySessionStore()
    for i in range(5):
        await store.append_trail_entry(
            "sess-1", _entry(f"c{i}", frozenset(), sql=f"SELECT {i} FROM padding_table_name_long")
        )

    assembler = ContextAssembler(store, history_token_budget=1, summarizer=slow_blocking_summarizer)

    async def _short_concurrent_task() -> None:
        await asyncio.sleep(0.02)
        order.append("short_task_done")

    assembled, _ = await asyncio.gather(
        assembler.assemble("sess-1", frozenset()), _short_concurrent_task()
    )
    assert assembled.compaction_applied is True
    # The short task must finish WHILE the summarizer is still blocking its
    # own worker thread — proving the event loop was never stalled by it.
    assert order == ["short_task_done", "summarizer_done"]


# ---------------------------------------------------------------------------
# Turn-scoped continuity (2026-07-01): assemble(..., current_turn_index=...)
# ---------------------------------------------------------------------------


async def test_assemble_default_current_turn_index_is_strict_unchanged() -> None:
    """Calling `assemble` with no `current_turn_index` (the QA-locked test's
    exact call shape) still drops an undetermined entry, whatever turn it
    belongs to."""
    store = InMemorySessionStore()
    await store.append_trail_entry("sess-1", _entry("c1", None, turn_index=0))

    assembler = ContextAssembler(store, history_token_budget=100_000)
    assembled = await assembler.assemble("sess-1", frozenset())

    tool_call_ids = [m["tool_call_id"] for m in assembled.messages if "tool_call_id" in m]
    assert tool_call_ids == []
    assert assembled.dropped_by_scope_count == 1


async def test_assemble_current_turn_index_exempts_only_that_turns_entries() -> None:
    """An undetermined, DENIED entry from the turn currently in progress is
    kept when `current_turn_index` matches it (status-gated: a denied entry
    carries no result rows); the SAME shape of entry from a prior turn is
    still dropped."""
    store = InMemorySessionStore()
    await store.append_trail_entry(
        "sess-1", _entry("prior_denied", None, turn_index=0, status="denied")
    )
    await store.append_trail_entry(
        "sess-1", _entry("current_denied", None, turn_index=1, status="denied")
    )

    assembler = ContextAssembler(store, history_token_budget=100_000)
    assembled = await assembler.assemble("sess-1", frozenset(), current_turn_index=1)

    tool_call_ids = [m["tool_call_id"] for m in assembled.messages if "tool_call_id" in m]
    assert tool_call_ids == ["current_denied"]
    assert assembled.dropped_by_scope_count == 1


async def test_summarizer_cache_hit_stays_synchronous_and_cheap() -> None:
    """A cache HIT must resolve without ever invoking `asyncio.to_thread` (no
    added latency/thread-hop) — the summarizer callable is not called again."""
    from data_agent.runtime.context.budget import SummaryCache

    calls = {"n": 0}

    def counting_summarizer(entries) -> str:  # noqa: ANN001
        calls["n"] += 1
        return f"summary-{calls['n']}"

    store = InMemorySessionStore()
    for i in range(5):
        await store.append_trail_entry(
            "sess-1", _entry(f"c{i}", frozenset(), sql=f"SELECT {i} FROM padding_table_name_long")
        )

    cache = SummaryCache()
    assembler = ContextAssembler(
        store, history_token_budget=1, summarizer=counting_summarizer, cache=cache
    )

    first = await assembler.assemble("sess-1", frozenset())
    assert first.compaction_applied is True
    assert calls["n"] == 1

    second = await assembler.assemble("sess-1", frozenset())
    assert second.compaction_applied is True
    assert calls["n"] == 1  # cache hit — summarizer NOT invoked again
