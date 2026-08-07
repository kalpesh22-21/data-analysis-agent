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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from data_agent.runtime.dispatch.denial_mapping import classify_denial
from data_agent.runtime.retrieval.render import _USER_CONTEXT_PREFIX as _RETRIEVAL_CONTEXT_PREFIX
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
    #
    # Part 3 fix (2026-08): the newest entry is NO LONGER force-kept verbatim when
    # it alone exceeds `token_budget`. The old guard (`if verbatim and ...`) always
    # admitted the FIRST (newest) entry regardless of size, so one giant entry
    # (e.g. an un-truncated getTableSchema ~30k tokens) survived verbatim and blew
    # the budget the walk is meant to enforce. Dropping the guard means an
    # over-budget newest entry falls into `summarized` instead — it is still kept
    # in a BOUNDED form (folded into the compaction summary), just not verbatim, so
    # the compacted trail is actually bounded by `token_budget`. Combined with the
    # per-result cap (dispatch/tool_dispatcher.py::_build_preview) the newest entry
    # is no longer 30k in practice; this keeps the walk correct even if one is.
    for i in range(len(trail) - 1, -1, -1):
        entry = trail[i]
        size = _entry_render_size(entry, preview_row_count)
        if running_tokens + size > token_budget:
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


# The compaction summary is rendered under a NON-system role so it can never
# compete with the base prompt that `context/assembly.py` inserts at index 0 —
# which is the SINGLE `role: "system"` message the assembler emits (the
# retrieval cards block, `retrieval/render.py`, is likewise a `user` message, so
# even on a retrieval-enabled deploy the base prompt is the sole system message).
# Some OpenAI-compatible endpoints (self-hosted vLLM/TGI chat templates) honor
# only the first OR only the last system message, so a second `role: "system"`
# summary could silently override the agent's base instructions after N turns.
# `user` is the most portable non-system role. The prefix marks the content as
# compacted prior-context so the model never mistakes it for the current user
# question to answer.
_SUMMARY_CONTEXT_PREFIX = "[Earlier steps in this session were summarized to save space]\n\n"


def render_messages(
    compaction: CompactionResult, *, preview_row_count: int = 20
) -> list[dict[str, Any]]:
    """Render a `CompactionResult` into the ordered, model-facing message list (D50 step 4)."""
    messages: list[dict[str, Any]] = []
    if compaction.summary_text:
        messages.append(
            {"role": "user", "content": _SUMMARY_CONTEXT_PREFIX + compaction.summary_text}
        )
    for entry in compaction.verbatim:
        messages.append(_render_entry(entry, preview_row_count))
    return messages


# --- Total-request token budget (2026-08 fix) ---------------------------------
# The trail compaction above bounds ONLY the tool trail. The FULL canonical
# request that `ModelClient.send_turn` receives is trail + base prompt +
# retrieval/summary context + the appended conversation history + the current
# question — and it had NO total budget, so after N turns it grew past the model
# context window. Because the base prompt is sent as an ordinary LEADING message
# (not a protected param), an over-window request lets the endpoint FRONT-truncate
# the base prompt out first (production trace: messages[0] became an assistant
# tool_call). `fit_request_to_budget` is the final fit step
# (`loop/agent_loop.py::_build_canonical_messages`, right before send_turn) that
# bounds the whole list while pinning the base prompt and the current question.


def estimate_message_tokens(message: Mapping[str, Any]) -> int:
    """Token estimate for one canonical `send_turn` message — reuses the SAME
    chars/4 heuristic (`_estimate_tokens`) the trail budget walk uses, so the fit
    step and the compaction walk agree on token cost and there is only one
    estimator to swap for a real tokenizer later."""
    return _estimate_tokens(json.dumps(message, default=str))


@dataclass(frozen=True)
class RequestFitResult:
    """The outcome of one `fit_request_to_budget` call."""

    messages: list[dict[str, Any]]  # the trimmed (or original) canonical list
    dropped_units: int  # message-UNITS dropped (a tool_call/tool pair counts as 1)
    dropped_messages: int  # raw messages dropped (a pair counts as 2)
    dropped_tokens: int  # approx tokens dropped
    kept_tokens: int  # approx tokens in the returned list (<= token_budget when possible)
    # By-KIND breakdown of dropped units (see `_unit_kind`) so trimming is visible
    # in telemetry — e.g. {"conversation": 3, "trail": 5}. Only non-zero kinds are
    # present; an empty dict means nothing was dropped.
    dropped_by_kind: Mapping[str, int]


# Unit kinds, in DROP-PRIORITY order (lower index = dropped FIRST). Stale
# conversation turns and old trail pairs go before the CURRENT question's
# retrieval-cards block (candidate blueprints + knowledge + access rules) and the
# compaction summary, which are the most useful middle content under tight
# context — so those are dropped only if still over budget after the rest is gone.
_UNIT_KIND_CONVERSATION = "conversation"
_UNIT_KIND_TRAIL = "trail"
_UNIT_KIND_SUMMARY = "summary"
_UNIT_KIND_RETRIEVAL = "retrieval"
_DROP_PRIORITY: dict[str, int] = {
    _UNIT_KIND_CONVERSATION: 0,
    _UNIT_KIND_TRAIL: 0,
    _UNIT_KIND_SUMMARY: 1,
    _UNIT_KIND_RETRIEVAL: 1,
}


def _fit_units(
    messages: list[dict[str, Any]], head_end: int, tail_start: int
) -> list[tuple[int, int]]:
    """Group the droppable middle `messages[head_end:tail_start)` into
    pairing-preserving UNITS (half-open `(start, end)` index ranges):

      * an assistant message carrying `tool_calls` + its immediately following
        `tool` result message(s) form ONE atomic unit — dropped/kept together so a
        `tool` message is never orphaned from its announcing assistant and vice
        versa (invariant 4);
      * every other message (a `user` retrieval/summary/conversation message, a
        plain assistant answer) is its own single-message unit.
    """
    units: list[tuple[int, int]] = []
    i = head_end
    while i < tail_start:
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls"):
            j = i + 1
            while j < tail_start and messages[j].get("role") == "tool":
                j += 1
            units.append((i, j))
            i = j
        else:
            units.append((i, i + 1))
            i += 1
    return units


def _unit_kind(messages: list[dict[str, Any]], start: int, end: int) -> str:
    """Classify a droppable unit for drop-priority + telemetry:

      * `trail`        — an assistant `tool_calls` + `tool` result pair;
      * `retrieval`    — the current question's retrieved-context `user` block
                         (candidate blueprints/knowledge), detected by its prefix;
      * `summary`      — the compaction summary `user` block (its prefix);
      * `conversation` — anything else (a prior-turn user/assistant exchange).
    """
    first = messages[start]
    if first.get("role") == "assistant" and first.get("tool_calls"):
        return _UNIT_KIND_TRAIL
    if first.get("role") == "user" and end - start == 1:
        content = first.get("content")
        if isinstance(content, str):
            if content.startswith(_RETRIEVAL_CONTEXT_PREFIX):
                return _UNIT_KIND_RETRIEVAL
            if content.startswith(_SUMMARY_CONTEXT_PREFIX):
                return _UNIT_KIND_SUMMARY
    return _UNIT_KIND_CONVERSATION


def fit_request_to_budget(
    messages: list[dict[str, Any]], *, token_budget: int
) -> RequestFitResult:
    """Fit the FULL canonical request to `token_budget` while honoring the
    send-seam invariants:

      1. the base prompt (the leading run of `role:"system"` messages) is NEVER
         dropped or truncated — it is pinned as the head;
      2. the returned list never exceeds `token_budget` tokens WHEN that is
         achievable without violating (1) or (3) — only the middle is droppable;
      3. the current question (the LAST `user` message) is NEVER dropped — it and
         anything after it are pinned as the tail;
      4. assistant `tool_calls` <-> `tool` result pairing is preserved (units are
         dropped/kept atomically, see `_fit_units`);
      5. under pressure the droppable middle goes in DROP-PRIORITY order, not pure
         position: the oldest CONVERSATION turns and oldest TRAIL pairs first
         (tier 0), and only if still over budget the current question's RETRIEVAL
         cards block + the compaction SUMMARY (tier 1) — those carry this
         question's blueprint candidates / knowledge / access rules and are the
         most useful middle content, so they survive longest. Within a tier the
         OLDEST unit (lowest position) goes first, keeping the most recent context.

    The head (base prompt) and tail (current question) are never dropped, so in the
    pathological corner where those two ALONE exceed `token_budget` the result may
    still exceed it — correctness of invariants 1+3 takes precedence over 2 (the
    base prompt and the live question must survive). In practice both are tiny.
    """
    sizes = [estimate_message_tokens(m) for m in messages]
    total = sum(sizes)
    n = len(messages)
    if total <= token_budget or n == 0:
        return RequestFitResult(list(messages), 0, 0, 0, total, {})

    # Pin the leading run of `system` messages (the base prompt lives at index 0).
    head_end = 0
    while head_end < n and messages[head_end].get("role") == "system":
        head_end += 1

    # Pin the tail: from the LAST `user` message (the current question) to the end.
    # `no_user_message` → tail_start stays `n` (an EMPTY tail slice `messages[n:]`),
    # so NOTHING is pinned as the tail and everything after the head is droppable
    # via whole units. That is what keeps a list ending in a `tool` result from
    # pinning that lone tool while its announcing assistant unit stays droppable
    # (which would orphan the tool and break invariant 4). The wired path always
    # has the current question, so this corner is public-API-only.
    tail_start = n
    for i in range(n - 1, head_end - 1, -1):
        if messages[i].get("role") == "user":
            tail_start = i
            break

    units = _fit_units(messages, head_end, tail_start)
    unit_sizes = [sum(sizes[k] for k in range(s, e)) for (s, e) in units]
    kinds = [_unit_kind(messages, s, e) for (s, e) in units]

    # Consideration order: by drop-priority tier (0 before 1), then by position
    # (oldest first) within a tier — a stable sort on (tier, index).
    order = sorted(range(len(units)), key=lambda u: (_DROP_PRIORITY[kinds[u]], u))

    keep = [True] * len(units)
    running = total
    dropped_units = dropped_messages = dropped_tokens = 0
    dropped_by_kind: dict[str, int] = {}
    for u in order:
        if running <= token_budget:
            break
        s, e = units[u]
        keep[u] = False
        running -= unit_sizes[u]
        dropped_tokens += unit_sizes[u]
        dropped_messages += e - s
        dropped_units += 1
        dropped_by_kind[kinds[u]] = dropped_by_kind.get(kinds[u], 0) + 1

    fitted: list[dict[str, Any]] = list(messages[:head_end])
    for idx, (s, e) in enumerate(units):
        if keep[idx]:
            fitted.extend(messages[s:e])
    fitted.extend(messages[tail_start:])
    return RequestFitResult(
        fitted, dropped_units, dropped_messages, dropped_tokens, running, dropped_by_kind
    )
