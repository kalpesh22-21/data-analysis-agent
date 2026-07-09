"""Base agent system prompt — always-present leading system message.

Covers: the assembled model messages begin with the base prompt; it precedes the
retrieval block when retrieval is present; a D45 rebuild re-derives byte-identical
messages (prompt still first); it survives history-token-budget trimming; and the
disabled/None toggle reproduces the exact prompt-less message list.
"""

from __future__ import annotations

from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT
from data_agent.runtime.retrieval.models import RetrievedContext, ThinCard
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry

_E = "dbpcm_warehouse.employee"


def _entry(tool_call_id: str, sql: str, *, turn_index: int = 0) -> TrailEntry:
    return TrailEntry(
        turn_index=turn_index,
        tool_call_id=tool_call_id,
        tool_name="runQuery",
        args={"sql": sql},
        status="ok",
        error_code=None,
        provenance=frozenset({(_E, "Department")}),
        result_preview=None,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )


class _StubRetrieval:
    """Minimal RetrievalPipeline stand-in returning one non-empty card block."""

    async def retrieve(self, *, question, column_scope, user_id, observer):  # noqa: ANN001, ARG002
        return RetrievedContext(
            thin_cards=[
                ThinCard(id="bp.headcount", intent="Count employees", slots_summary="", score=1.0)
            ],
            reranked=True,
        )


async def test_base_prompt_is_first_message() -> None:
    store = InMemorySessionStore()
    await store.append_trail_entry("s1", _entry("c1", "SELECT Department FROM employee"))

    assembler = ContextAssembler(
        store, history_token_budget=100_000, base_system_prompt=AGENT_SYSTEM_PROMPT
    )
    scope = frozenset({f"{_E}.Department"})
    assembled = await assembler.assemble("s1", scope)

    assert assembled.messages[0] == {"role": "system", "content": AGENT_SYSTEM_PROMPT}


async def test_base_prompt_precedes_retrieval_block() -> None:
    store = InMemorySessionStore()
    assembler = ContextAssembler(
        store,
        history_token_budget=100_000,
        base_system_prompt=AGENT_SYSTEM_PROMPT,
        retrieval=_StubRetrieval(),
    )
    assembled = await assembler.assemble(
        "s1", frozenset(), current_turn_index=0, user_message="how many employees?"
    )

    # base prompt first, then the retrieval card block, both system messages.
    assert assembled.messages[0]["content"] == AGENT_SYSTEM_PROMPT
    assert assembled.messages[1]["role"] == "system"
    assert "bp.headcount" in assembled.messages[1]["content"]
    assert assembled.retrieved_counts == (1, 0)


async def test_disabled_toggle_reproduces_promptless_messages() -> None:
    store = InMemorySessionStore()
    await store.append_trail_entry("s1", _entry("c1", "SELECT Department FROM employee"))
    scope = frozenset({f"{_E}.Department"})

    with_none = await ContextAssembler(
        store, history_token_budget=100_000, base_system_prompt=None
    ).assemble("s1", scope)
    baseline = await ContextAssembler(store, history_token_budget=100_000).assemble("s1", scope)

    assert with_none.messages == baseline.messages
    assert all(m.get("content") != AGENT_SYSTEM_PROMPT for m in with_none.messages)


async def test_d45_rebuild_is_byte_identical() -> None:
    store = InMemorySessionStore()
    await store.append_trail_entry("s1", _entry("c1", "SELECT Department FROM employee"))
    scope = frozenset({f"{_E}.Department"})
    assembler = ContextAssembler(
        store, history_token_budget=100_000, base_system_prompt=AGENT_SYSTEM_PROMPT
    )

    first = await assembler.assemble("s1", scope)
    second = await assembler.assemble("s1", scope)

    assert first.messages == second.messages
    assert second.messages[0]["content"] == AGENT_SYSTEM_PROMPT


async def test_base_prompt_survives_history_budget_trimming() -> None:
    store = InMemorySessionStore()
    # Many entries + a tiny token budget forces compaction/summarization; the
    # base prompt must still lead and never be trimmed.
    for i in range(30):
        await store.append_trail_entry(
            "s1", _entry(f"c{i}", f"SELECT Department FROM employee WHERE id = {i}", turn_index=i)
        )
    scope = frozenset({f"{_E}.Department"})
    assembler = ContextAssembler(
        store, history_token_budget=1, base_system_prompt=AGENT_SYSTEM_PROMPT
    )
    assembled = await assembler.assemble("s1", scope)

    assert assembled.messages[0] == {"role": "system", "content": AGENT_SYSTEM_PROMPT}
    assert assembled.compaction_applied is True
    # The base prompt appears exactly once (not duplicated into the summary block).
    assert sum(m.get("content") == AGENT_SYSTEM_PROMPT for m in assembled.messages) == 1
