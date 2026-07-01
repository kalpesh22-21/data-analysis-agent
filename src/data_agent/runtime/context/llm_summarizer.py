"""LLM-backed `Summarizer` (design §5) — the Pass-B real implementation of
`context/budget.py`'s `Summarizer` seam.

`context/budget.py::Summarizer` is deliberately a **synchronous** callable
(`Callable[[Sequence[TrailEntry]], str]`) — `compact_trail` (also sync/pure)
invokes it directly, and that module's own docstring states Pass B "swaps it
for a real summarization call without changing this module's public
interface". Because summarizing the older, overflowed trail entries
genuinely requires an LLM round-trip (`ModelClient.send_turn`, which is
async), this module bridges sync -> async by running the model call to
completion on a short-lived background thread with its own event loop
(`asyncio.run()` cannot be called from a thread that already has a running
loop — which the calling thread always does, since `ContextAssembler.assemble`
is itself a coroutine). This blocks the calling (event-loop) thread for the
duration of the summarization call — an accepted Phase-0 trade-off:
`context/budget.py`'s in-process `SummaryCache` means this only runs on a
genuine cache miss (the compacted-entry set changed), not on every turn.

Contract preserved from `_default_summarizer` (design §5 / budget.py
module docstring):
    - Every kept (verbatim) entry's SQL is untouched — this module only ever
      sees the *summarized* (overflow) entries, never the verbatim ones
      (`compact_trail` guarantees this split).
    - The summarizer paraphrases prose ONLY; each summarized entry's SQL is
      quoted verbatim in the prompt with an explicit instruction to preserve
      it verbatim in the output.
    - On any failure (model error, empty output), falls back to the Pass-A
      `default_summarizer` placeholder — never raises out of
      `compact_trail`'s synchronous call path, and never silently drops
      entries (compaction always produces *some* string).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable, Sequence

from data_agent.runtime.context.budget import Summarizer, default_summarizer
from data_agent.runtime.model.client import ModelClient, begin_turn_client
from data_agent.runtime.session.models import TrailEntry

_SUMMARIZER_SYSTEM_PROMPT = (
    "You are summarizing older tool-call history for an HR data-analysis agent. "
    "For each entry, keep its SQL EXACTLY as given (verbatim, unchanged) and write "
    "one short paraphrased sentence of what it found (counts/shape only, no PII "
    "values). Output one line per entry: '<sql> -- <paraphrase>'. Do not add any "
    "entries, do not omit any, do not invent data."
)


def _render_entry_for_prompt(entry: TrailEntry) -> str:
    sql = entry.args.get("sql") if entry.tool_name == "runQuery" else None
    sql_text = sql if isinstance(sql, str) else f"{entry.tool_name}({entry.args})"
    row_count = entry.result_preview.row_count if entry.result_preview else None
    return f"{sql_text} -- status={entry.status}, rows={row_count}"


def _run_sync(coro_factory: Callable[[], Awaitable[str]]) -> str:
    """Run *coro_factory()* to completion on a fresh background thread/event
    loop, blocking the caller — see module docstring for why this is needed."""

    def _runner() -> str:
        return asyncio.run(coro_factory())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_runner)
        return future.result()


def build_llm_summarizer(model_client: ModelClient) -> Summarizer:
    """Build a `Summarizer` backed by *model_client* (design §5, Pass B)."""

    def _summarize(entries: Sequence[TrailEntry]) -> str:
        if not entries:
            return ""

        async def _call() -> str:
            prompt_lines = [_render_entry_for_prompt(entry) for entry in entries]
            messages = [
                {"role": "system", "content": _SUMMARIZER_SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(prompt_lines)},
            ]
            # B3: use a per-call-scoped handle, never the shared model_client
            # instance directly — this summarizer call may run concurrently
            # with an in-flight /turn request also using the SAME shared
            # OpenAIModelClient singleton (app.py's composition root).
            turn_client = begin_turn_client(model_client)
            result = await turn_client.send_turn(messages, tools=[])
            return result.assistant_text or ""

        try:
            summary = _run_sync(_call)
        except Exception:
            # Fail-soft: a flaky summarizer call must never break context
            # assembly (D50 correctness never depends on the summary being
            # LLM-authored — the placeholder is always safe, just less useful).
            return default_summarizer(entries)

        return summary or default_summarizer(entries)

    return _summarize


__all__ = ["build_llm_summarizer"]
