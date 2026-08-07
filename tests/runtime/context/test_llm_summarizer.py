"""Unit tests for context/llm_summarizer.py (Layer 1 — ScriptedModelClient, no live LLM).

Covers both the summarizer function in isolation (sync call, no running event
loop) and the harder case — wired into `ContextAssembler.assemble()`, called
from *within* an already-running asyncio event loop (the real call path;
proves the sync->async bridge actually works and does not deadlock).
"""

from __future__ import annotations

from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.budget import _SUMMARY_CONTEXT_PREFIX
from data_agent.runtime.context.llm_summarizer import build_llm_summarizer
from data_agent.runtime.model.client import ModelTurnResult
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, TrailEntry


def _entry(tool_call_id: str, sql: str, row_count: int = 1) -> TrailEntry:
    return TrailEntry(
        turn_index=0,
        tool_call_id=tool_call_id,
        tool_name="runQuery",
        args={"sql": sql},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=ResultPreview(
            columns=["a"], row_count=row_count, truncated=False, preview_rows=[]
        ),
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )


def test_empty_entries_returns_empty_string_without_calling_model() -> None:
    model = ScriptedModelClient([])  # no scripted turns — must never be consumed
    summarizer = build_llm_summarizer(model)
    assert summarizer([]) == ""
    assert len(model.calls) == 0


def test_summarizer_calls_model_and_returns_its_text() -> None:
    model = ScriptedModelClient(
        [ModelTurnResult(assistant_text="SELECT 1 -- returned 1 row\nSELECT 2 -- returned 2 rows")]
    )
    summarizer = build_llm_summarizer(model)
    entries = [_entry("c1", "SELECT 1"), _entry("c2", "SELECT 2", row_count=2)]

    summary = summarizer(entries)

    assert summary == "SELECT 1 -- returned 1 row\nSELECT 2 -- returned 2 rows"
    assert len(model.calls) == 1
    # SQL is quoted verbatim into the prompt (never paraphrased away).
    prompt_blob = str(model.calls[0].messages)
    assert "SELECT 1" in prompt_blob
    assert "SELECT 2" in prompt_blob


def test_summarizer_falls_back_to_placeholder_when_model_returns_empty_text() -> None:
    model = ScriptedModelClient([ModelTurnResult(assistant_text=None)])
    summarizer = build_llm_summarizer(model)
    entries = [_entry("c1", "SELECT 1")]

    summary = summarizer(entries)

    assert "c1" in summary  # the Pass-A default_summarizer placeholder format
    assert "older turn(s) omitted" in summary


def test_summarizer_falls_back_to_placeholder_on_model_error() -> None:
    class _BoomModelClient:
        async def send_turn(self, messages, tools):  # noqa: ARG002
            raise RuntimeError("boom")

    summarizer = build_llm_summarizer(_BoomModelClient())
    entries = [_entry("c1", "SELECT 1")]

    summary = summarizer(entries)

    assert "c1" in summary
    assert "older turn(s) omitted" in summary


async def test_summarizer_works_when_called_from_within_a_running_event_loop() -> None:
    """The real call path: ContextAssembler.assemble() is itself a coroutine,
    and compact_trail() invokes the sync Summarizer callable synchronously
    from inside it — proving the thread-bridged sync->async call does not
    deadlock against the already-running pytest-asyncio event loop."""
    model = ScriptedModelClient([ModelTurnResult(assistant_text="summarized older history")])
    summarizer = build_llm_summarizer(model)
    store = InMemorySessionStore()

    for i in range(20):
        await store.append_trail_entry("sess-1", _entry(f"c{i}", f"SELECT {i} FROM padding"))

    assembler = ContextAssembler(store, history_token_budget=1, summarizer=summarizer)
    assembled = await assembler.assemble("sess-1", frozenset())

    assert assembled.compaction_applied is True
    # No base prompt is configured here, so the summary is the leading message —
    # rendered under a NON-system (`user`) role with the context prefix, never a
    # `system` message.
    assert not any(m["role"] == "system" for m in assembled.messages)
    summary_messages = [
        m
        for m in assembled.messages
        if m["role"] == "user" and str(m.get("content", "")).startswith(_SUMMARY_CONTEXT_PREFIX)
    ]
    assert summary_messages[0]["content"] == _SUMMARY_CONTEXT_PREFIX + "summarized older history"
