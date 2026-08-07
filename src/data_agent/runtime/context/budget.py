"""D46/D50 — preview-only rendering + token-budgeted history + compaction cache.

Ordering contract (D50, enforced by the caller `context/assembly.py`, not
here): this module must only ever be handed an already scope-filtered trail
(§5 "filter-strictly-before-compact") — it has no scope information of its
own and performs no filtering.

Compaction: newest-first, verbatim entries are kept up to `token_budget`;
older overflow entries are handed to an injected `summarizer` callable and
replaced by a single synthetic summary. The summarizer receives the full
`TrailEntry` objects (including verbatim SQL in `entry.args`), so a real
(Pass B) LLM-backed summarizer can satisfy "every kept turn's SQL is
preserved verbatim, only prose is paraphrased" for the entries *it* sees —
Pass A's default summarizer is a deliberate no-op/identity placeholder
(`_default_summarizer`) that does NOT call any model; Pass B swaps it for a
real summarization call without changing this module's public interface.

Caching: an in-process LRU keyed by `(scope_hash, content_hash_of_compacted_set)`
(OQ-E: in-process only for Phase 0 — a cache miss is always safe/correct,
just slower; never a correctness dependency).

Preview object: NOT built here — `dispatch/tool_dispatcher.py` builds
`TrailEntry.result_preview` once at write-time. This module only re-truncates
defensively to the *current* `preview_row_count` setting when rendering (in
case the setting changed since the entry was written), and never exposes
anything beyond the stored preview (`TrailEntry` has no `result_full` field —
only `result_full_ref` — so budget.py cannot leak full results even by bug).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from data_agent.runtime.dispatch.denial_mapping import classify_denial
from data_agent.runtime.session.models import TrailEntry

Summarizer = Callable[[Sequence[TrailEntry]], str]


def _default_summarizer(entries: Sequence[TrailEntry]) -> str:
    """Pass-A no-op summarizer — makes no model call, paraphrases nothing.

    Returns a placeholder string naming the omitted turns so the model at
    least knows history was compacted. Pass B replaces this with a real
    LLM-backed summarizer that preserves SQL verbatim and paraphrases only
    prose (design §5).
    """
    if not entries:
        return ""
    tool_calls = ", ".join(f"{e.tool_name}#{e.tool_call_id}" for e in entries)
    return f"[{len(entries)} older turn(s) omitted from context: {tool_calls}]"


# Public alias — the Pass-A default summarizer dependency (see module docstring).
default_summarizer: Summarizer = _default_summarizer


def _estimate_tokens(text: str) -> int:
    """Crude token estimate (chars/4) — good enough for a budget heuristic; not
    a tokenizer. Pass B may swap in a real tokenizer without changing callers."""
    return max(1, len(text) // 4)


def _entry_render_size(entry: TrailEntry, preview_row_count: int) -> int:
    """Approximate token cost of rendering *entry* verbatim (used for the budget walk)."""
    payload = _render_entry(entry, preview_row_count)
    return _estimate_tokens(json.dumps(payload))


def _content_hash(entries: Sequence[TrailEntry]) -> str:
    """Deterministic hash of the compacted (older) entries' identifying content."""
    payload = [
        {"tool_call_id": e.tool_call_id, "tool_name": e.tool_name, "status": e.status}
        for e in entries
    ]
    blob = json.dumps(payload, sort_keys=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class SummaryCache:
    """A tiny in-process LRU cache: `(scope_hash, content_hash) -> summary_text`."""

    def __init__(self, max_size: int = 128) -> None:
        self._max_size = max_size
        self._store: dict[tuple[str, str], str] = {}
        self._order: list[tuple[str, str]] = []

    def get(self, key: tuple[str, str]) -> str | None:
        return self._store.get(key)

    def put(self, key: tuple[str, str], value: str) -> None:
        if key in self._store:
            self._order.remove(key)
        elif len(self._store) >= self._max_size:
            oldest = self._order.pop(0)
            del self._store[oldest]
        self._store[key] = value
        self._order.append(key)

    def __len__(self) -> int:
        return len(self._store)


@dataclass(frozen=True)
class CompactionResult:
    """The output of `compact_trail`."""

    verbatim: list[TrailEntry]  # chronological order, newest-budget survivors
    summarized: list[TrailEntry]  # chronological order, entries folded into summary_text
    summary_text: str | None  # None iff nothing was compacted
    cache_hit: bool


def _split_for_budget(
    trail: Sequence[TrailEntry], *, token_budget: int, preview_row_count: int
) -> tuple[list[TrailEntry], list[TrailEntry]]:
    """Newest-first budget walk shared by `compact_trail`/`compact_trail_async`
    (S2): pure/cheap CPU work, never itself a reason to leave the event-loop
    thread. Returns `(verbatim, summarized)`, both in chronological order."""
    verbatim: list[TrailEntry] = []
    running_tokens = 0
    cutoff_index = len(trail)  # index (exclusive) where verbatim entries start

    # Walk newest-first, accumulate until the budget would be exceeded.
    for i in range(len(trail) - 1, -1, -1):
        entry = trail[i]
        size = _entry_render_size(entry, preview_row_count)
        if verbatim and running_tokens + size > token_budget:
            cutoff_index = i + 1
            break
        running_tokens += size
        verbatim.append(entry)
        cutoff_index = i
    verbatim.reverse()

    summarized = list(trail[:cutoff_index])
    return verbatim, summarized


def compact_trail(
    trail: Sequence[TrailEntry],
    *,
    token_budget: int,
    scope_hash: str,
    preview_row_count: int = 20,
    summarizer: Summarizer = _default_summarizer,
    cache: SummaryCache | None = None,
) -> CompactionResult:
    """Split *trail* into newest-verbatim (within `token_budget`) + older-summarized.

    *trail* MUST already be scope-filtered (D50 ordering) — this function
    performs no scope logic. Synchronous — on a cache miss this calls
    *summarizer* directly on the calling thread; callers running inside an
    asyncio event loop that care about not blocking it on a cache miss (e.g.
    an LLM-backed summarizer) should use `compact_trail_async` instead (S2).
    """
    verbatim, summarized = _split_for_budget(
        trail, token_budget=token_budget, preview_row_count=preview_row_count
    )
    if not summarized:
        return CompactionResult(
            verbatim=verbatim, summarized=[], summary_text=None, cache_hit=False
        )

    content_hash = _content_hash(summarized)
    cache_key = (scope_hash, content_hash)
    cache_hit = False
    summary_text: str | None = None
    if cache is not None:
        summary_text = cache.get(cache_key)
        cache_hit = summary_text is not None
    if summary_text is None:
        summary_text = summarizer(summarized)
        if cache is not None:
            cache.put(cache_key, summary_text)

    return CompactionResult(
        verbatim=verbatim, summarized=summarized, summary_text=summary_text, cache_hit=cache_hit
    )


async def compact_trail_async(
    trail: Sequence[TrailEntry],
    *,
    token_budget: int,
    scope_hash: str,
    preview_row_count: int = 20,
    summarizer: Summarizer = _default_summarizer,
    cache: SummaryCache | None = None,
) -> CompactionResult:
    """Same contract as `compact_trail`, but never blocks the calling event
    loop on a cache-miss summarizer call (S2 — the LLM-backed summarizer in
    `context/llm_summarizer.py` does a genuinely blocking wait internally).

    A cache HIT resolves exactly like `compact_trail` — synchronously, no
    thread hop, no added latency. Only a cache MISS's `summarizer(...)` call
    is offloaded via `asyncio.to_thread`, so one session's slow summarization
    can never stall other concurrent sessions' event-loop work.
    """
    verbatim, summarized = _split_for_budget(
        trail, token_budget=token_budget, preview_row_count=preview_row_count
    )
    if not summarized:
        return CompactionResult(
            verbatim=verbatim, summarized=[], summary_text=None, cache_hit=False
        )

    content_hash = _content_hash(summarized)
    cache_key = (scope_hash, content_hash)
    cache_hit = False
    summary_text: str | None = None
    if cache is not None:
        summary_text = cache.get(cache_key)
        cache_hit = summary_text is not None
    if summary_text is None:
        summary_text = await asyncio.to_thread(summarizer, summarized)
        if cache is not None:
            cache.put(cache_key, summary_text)

    return CompactionResult(
        verbatim=verbatim, summarized=summarized, summary_text=summary_text, cache_hit=cache_hit
    )


def _render_entry(entry: TrailEntry, preview_row_count: int) -> dict[str, Any]:
    """Render one verbatim `TrailEntry` into a plain, model-facing dict.

    SQL (and every other model-supplied arg) is preserved verbatim — this
    function never paraphrases. Only the already-persisted preview is
    exposed; it is defensively re-truncated to *preview_row_count* rows in
    case the setting shrank since write-time (never grown back — a smaller
    stored preview stays smaller).

    `user_message` (S4): for a non-`"ok"` entry, re-derives the static,
    PII-safe denial message from `entry.error_code` via
    `dispatch/denial_mapping.py::classify_denial` (the same canned lookup
    `ToolDispatcher` used at dispatch time — never the raw MCP error text,
    never re-persisted on `TrailEntry` itself) so the model can see WHY a
    retryable tool call failed and self-correct, instead of only a bare
    `error_code`.
    """
    preview: dict[str, Any] | None = None
    if entry.result_preview is not None:
        rows = entry.result_preview.preview_rows[:preview_row_count]
        preview = {
            "columns": entry.result_preview.columns,
            "row_count": entry.result_preview.row_count,
            "truncated": entry.result_preview.truncated or len(rows) < len(
                entry.result_preview.preview_rows
            ),
            "preview_rows": rows,
        }
    user_message = classify_denial(entry.error_code).user_message if entry.status != "ok" else None
    rendered: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": entry.tool_call_id,
        "tool_name": entry.tool_name,
        "args": dict(entry.args),
        "status": entry.status,
        "error_code": entry.error_code,
        "user_message": user_message,
        "result_preview": preview,
    }
    # Carry the verified-blueprint "authoritative" marker through so the canonical
    # tool message (loop/agent_loop.py::_tool_trail_entry_to_canonical) can flag it
    # to the model. Emitted only when set (a plain runQuery/denial entry is
    # byte-identical to before — the key is simply absent).
    if entry.authoritative:
        rendered["authoritative"] = True
    return rendered


def render_messages(
    compaction: CompactionResult, *, preview_row_count: int = 20
) -> list[dict[str, Any]]:
    """Render a `CompactionResult` into the ordered, model-facing message list (D50 step 4)."""
    messages: list[dict[str, Any]] = []
    if compaction.summary_text:
        messages.append({"role": "system", "content": compaction.summary_text})
    for entry in compaction.verbatim:
        messages.append(_render_entry(entry, preview_row_count))
    return messages
