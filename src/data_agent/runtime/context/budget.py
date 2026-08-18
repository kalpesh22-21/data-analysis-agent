"""D46/D50 — preview-only entry rendering + the total-request token budget.

Ordering contract, enforced by the CALLER (`context/assembly.py`): this module must only
ever be handed an already scope-filtered trail — it has no scope information of its own
and performs no filtering.

`_render_entry` is the SINGLE producer of every model-facing tool message;
`fit_request_to_budget` is the final size bound on the FULL canonical request, applied
right before `send_turn`. Trail compaction is bypassed entirely — every in-scope turn
interleaves verbatim.

Previews are built once at write-time by `dispatch/tool_dispatcher.py`; this module only
re-truncates defensively to the CURRENT `preview_row_count`, and can never expose more
than the stored preview (`TrailEntry` holds no `result_full`, only a ref).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from data_agent.runtime.dispatch.denial_mapping import classify_denial
from data_agent.runtime.retrieval.render import _USER_CONTEXT_PREFIX as _RETRIEVAL_CONTEXT_PREFIX
from data_agent.runtime.session.models import TrailEntry


def _estimate_tokens(text: str) -> int:
    """Crude token estimate (chars/4) — a budget heuristic, not a tokenizer."""
    return max(1, len(text) // 4)


def _render_entry(entry: TrailEntry, preview_row_count: int) -> dict[str, Any]:
    """Render one verbatim `TrailEntry` into a plain, model-facing dict.

        SQL and every other model-supplied arg is preserved verbatim — this function never
        paraphrases. Only the already-persisted preview is exposed, defensively re-truncated
        to *preview_row_count* rows in case the setting shrank since write-time (never grown
        back).

        `user_message`: for a non-`"ok"` entry, re-derives the static, PII-safe denial
        message from `entry.error_code` via `dispatch/denial_mapping.py::classify_denial` —
        never the raw MCP error text, and never re-persisted on the `TrailEntry` itself — so
        the model can see WHY a retryable call failed and self-correct.
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
    # The model-facing reason for a non-`ok` entry. `denial_detail` — set only where
    # the runtime already decided the specific text is safe (the dispatcher's
    # COLUMN_SCOPE_VIOLATION carve-out, the answerWithTable nudge) — wins, because it
    # names the thing the model must change: WHICH column is out of scope, WHICH
    # blueprint was not run. Everything else falls back to the canned
    # `denial_mapping.py` string, so an ordinary denial is byte-identical to before.
    #
    # This function is the SINGLE producer of every model-facing tool message, which
    # is why a specific reason that is not persisted on the entry cannot reach the
    # model at all — `ToolResult.user_message` has no field here and is dropped.
    user_message = (
        (entry.denial_detail or classify_denial(entry.error_code).user_message)
        if entry.status != "ok"
        else None
    )
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
    # J7: the data-anchored window note, carried through on the same terms — emitted only
    # when the entry has one, so a runQuery/denial/non-windowed blueprint entry is
    # byte-identical to before. It is deliberately NOT folded into the `authoritative`
    # branch above: the two are independent (a blueprint that failed verify still ran a
    # data-anchored window), and nesting them would silently drop the note on exactly the
    # results the model is most likely to second-guess.
    if entry.window_note:
        rendered["window_note"] = entry.window_note
    return rendered


# Prefix of a compaction-summary block. No producer exists on the Phase-1 path
# (compaction is bypassed); `_unit_kind` below still recognizes it so a summary
# block, if a later phase reintroduces one, is classified and drop-prioritized
# correctly rather than being mistaken for ordinary conversation.
#
# A summary is rendered under a NON-system role so it can never compete with the
# base prompt that `context/assembly.py` inserts at index 0 — which is the SINGLE
# `role: "system"` message the assembler emits (the retrieval cards block,
# `retrieval/render.py`, is likewise a `user` message, so even on a
# retrieval-enabled deploy the base prompt is the sole system message). Some
# OpenAI-compatible endpoints (self-hosted vLLM/TGI chat templates) honor only the
# first OR only the last system message, so a second `role: "system"` summary
# could silently override the agent's base instructions after N turns. `user` is
# the most portable non-system role. The prefix marks the content as compacted
# prior-context so the model never mistakes it for the current user question.
_SUMMARY_CONTEXT_PREFIX = "[Earlier steps in this session were summarized to save space]\n\n"


# --- Total-request token budget (2026-08 fix) ---------------------------------
# The FULL canonical
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
    """Token estimate for one canonical `send_turn` message — the chars/4 heuristic, and
        the single estimator to swap for a real tokenizer later.
    """
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


# Unit kinds, in DROP-PRIORITY order (lower index = dropped FIRST). PRIOR-turn
# conversation turns and PRIOR-turn trail pairs go before the CURRENT question's
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

# TIER-2 (2026-08 interleave blocker fix): the CURRENT turn's OWN older tool pairs
# — those beyond the most-recent K that are pinned. In the interleaved layout the
# current turn's tool pairs have `ts` AFTER the question, so they land in the tail;
# if the whole tail were pinned, a single non-terminating turn would accumulate an
# UN-trimmable tail of ~30k-token results that exceeds the request budget (then the
# real model window) — reproducing the very front-truncation bug the budget exists
# to prevent. Making the current turn's OLDER tool pairs a last-resort droppable
# tier (dropped only AFTER every prior-turn unit is gone, oldest-first) keeps the
# request bounded even under a runaway turn, while the most-recent K pairs + the
# question + retrieval stay pinned (K protects the D94 re-fetch/self-correct loop).
_TIER_CURRENT_OLDER_TRAIL = 2
# Default K: how many of the current turn's most-recent tool pairs stay pinned.
_DEFAULT_PINNED_RECENT_TOOL_PAIRS = 3


def _fit_units(
    messages: list[dict[str, Any]], head_end: int, tail_start: int
) -> list[tuple[int, int]]:
    """Group the droppable middle `messages[head_end:tail_start)` into pairing-preserving
        UNITS (half-open `(start, end)` index ranges).

        An assistant message carrying `tool_calls` plus its immediately following `tool`
        result message(s) form ONE atomic unit, dropped or kept together so a `tool` message
        is never orphaned from its announcing assistant (invariant 4). Every other message
        is its own single-message unit.
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
          * `retrieval`    — the current question's retrieved-context `user` block;
          * `summary`      — the compaction summary `user` block (by its prefix);
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


def _current_turn_start(messages: list[dict[str, Any]], head_end: int, n: int) -> int:
    """Index where the CURRENT (in-progress) turn's messages begin — the first `user`
        message after the last COMPLETED prior turn.

        A completed prior turn always ends in a plain `assistant` ANSWER (role `assistant`
        with NO `tool_calls`); the in-progress turn has none yet, and its synthetic
        assistant messages all carry `tool_calls`. So the current turn is the run after the
        LAST plain-assistant answer, starting at that run's first `user` message — the
        ORIGINATING question, even after an askUser resume appended a clarification answer.
        Identifying it positionally is what removes any need to thread `turn_index`.

        Returns `n` when there is no current-turn `user` message, so nothing is pinned as
        the current turn — which is what stops a lone trailing `tool` from being pinned
        while its announcing assistant unit stays droppable (invariant 4). If EVERY prior
        turn's assistant answer was scope-dropped, the boundary walks back to the first user
        message, harmlessly over-pinning.
    """
    last_answer = head_end - 1
    for i in range(n - 1, head_end - 1, -1):
        m = messages[i]
        if m.get("role") == "assistant" and not m.get("tool_calls"):
            last_answer = i
            break
    for i in range(max(last_answer + 1, head_end), n):
        if messages[i].get("role") == "user":
            return i
    return n


def fit_request_to_budget(
    messages: list[dict[str, Any]],
    *,
    token_budget: int,
    pinned_recent_tool_pairs: int = _DEFAULT_PINNED_RECENT_TOOL_PAIRS,
    pinned_tool_call_ids: frozenset[str] | None = None,
) -> RequestFitResult:
    """Fit the FULL canonical request to `token_budget` while honoring the send-seam
        invariants:

          1. the base prompt (the leading run of `role:"system"` messages) is NEVER dropped
             or truncated — it is pinned as the head;
          2. the returned list never exceeds `token_budget` tokens WHEN that is achievable
             without violating (1), (3) or (6);
          3. the CURRENT turn is pinned from its FIRST `user` message through the end, so
             its originating question, its retrieval-cards block and an askUser
             clarification answer are all undroppable;
          4. assistant `tool_calls` <-> `tool` result pairing is preserved — units are
             dropped or kept atomically (see `_fit_units`);
          5. under pressure droppable units go in DROP-PRIORITY tier order, not pure
             position: prior-turn conversation and trail first (tier 0), then a prior
             retrieval/summary block (tier 1), and only as a last resort the current turn's
             OLDER tool pairs (tier 2). Within a tier the oldest goes first;
          6. the current turn's most-recent `pinned_recent_tool_pairs` (K) tool pairs are
             PINNED — K protects the D94 withheld/idempotent-read self-correct loop, and
             making the pairs older than K droppable is what keeps a single non-terminating
             turn bounded by the budget;
          7. any unit whose `tool_call_id` is in *pinned_tool_call_ids* is PINNED regardless
             of tier or age. This carries the emulated-discovery pairs, which are anchored
             at the SESSION'S FIRST question and so classify as prior-turn trail, tier 0.
             Dropping them is uniquely harmful: the loop seeds its repeated-idempotent-read
             guard from the same sweep, so the model would be unable to see the tables AND
             unable to re-fetch them. `None`/empty behaves as if the parameter did not exist.

        Invariants 1/3/6/7 take precedence over 2, so in the pathological corner where the
        pinned material ALONE exceeds `token_budget` the result may still exceed it.
    """
    pinned_ids = pinned_tool_call_ids or frozenset()
    sizes = [estimate_message_tokens(m) for m in messages]
    total = sum(sizes)
    n = len(messages)
    if total <= token_budget or n == 0:
        return RequestFitResult(list(messages), 0, 0, 0, total, {})

    # Pin the leading run of `system` messages (the base prompt lives at index 0).
    head_end = 0
    while head_end < n and messages[head_end].get("role") == "system":
        head_end += 1

    turn_start = _current_turn_start(messages, head_end, n)

    # Unitize the WHOLE non-head range (prior turns AND the current turn), so the
    # current turn's older tool pairs are droppable units too — not a monolithic
    # pinned tail. Pairing stays atomic (an assistant `tool_calls` + its `tool`
    # result(s) are one unit).
    units = _fit_units(messages, head_end, n)
    unit_sizes = [sum(sizes[k] for k in range(s, e)) for (s, e) in units]
    kinds = [_unit_kind(messages, s, e) for (s, e) in units]

    # The current turn's tool-pair units, in position order; the LAST K are pinned.
    current_tool_units = [
        u
        for u, (s, _e) in enumerate(units)
        if s >= turn_start
        and messages[s].get("role") == "assistant"
        and messages[s].get("tool_calls")
    ]
    pinned_recent = (
        set(current_tool_units[-pinned_recent_tool_pairs:])
        if pinned_recent_tool_pairs > 0
        else set()
    )

    # Classify every unit: pinned (never dropped) or droppable with a tier.
    pinned: list[bool] = [False] * len(units)
    tiers: list[int] = [0] * len(units)
    droppable: list[int] = []
    for u, (s, e) in enumerate(units):
        is_current = s >= turn_start
        is_tool_pair = (
            messages[s].get("role") == "assistant" and messages[s].get("tool_calls")
        )
        if pinned_ids and any(
            messages[k].get("tool_call_id") in pinned_ids for k in range(s, e)
        ):
            pinned[u] = True  # invariant 7 — emulated discovery, never dropped.
        elif is_current and not is_tool_pair:
            # Current-turn question / retrieval block / askUser answer — pinned.
            pinned[u] = True
        elif is_current and is_tool_pair:
            if u in pinned_recent:
                pinned[u] = True  # most-recent K current-turn tool pairs — pinned.
            else:
                tiers[u] = _TIER_CURRENT_OLDER_TRAIL  # older current-turn pair (tier 2).
                droppable.append(u)
        else:
            tiers[u] = _DROP_PRIORITY[kinds[u]]  # prior-turn unit (tier 0 / 1).
            droppable.append(u)

    # Consideration order: by tier (0, then 1, then 2), then by position (oldest
    # first) within a tier — a stable sort on (tier, index).
    order = sorted(droppable, key=lambda u: (tiers[u], u))

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
    return RequestFitResult(
        fitted, dropped_units, dropped_messages, dropped_tokens, running, dropped_by_kind
    )
