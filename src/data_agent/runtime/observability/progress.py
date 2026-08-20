"""ProgressEmitter — UI-facing progress events (D61).

Same stage boundaries as `tracing.py`'s spans but coarser and PII-safe by construction:
a `ProgressEvent` carries a human-readable `step` plus a `shape` dict drawn from an
explicit allowlist (tool name, error code, budget window, tool-call count) — never SQL
text, never row or cell values, never the JWT or raw scope. `observe` is wire-compatible
with both `ToolDispatcher`'s and `AgentLoop`'s observer argument; `combine_observers`
fans one call out to several observers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

# Only these payload keys are ever allowed to flow into a progress event's
# `shape` — an explicit allowlist, not a denylist, so a new observer event
# added later never accidentally leaks a sensitive field by default.
_SHAPE_ALLOWLIST = (
    "tool_name",
    "tool_call_id",
    "error_code",
    "window",
    "tool_calls_made",
    "question",
    # Slice-1 retrieval (design §3.5): shape-only pre-injection counts — the
    # number of blueprint thin cards / knowledge hits surfaced. Never any card
    # text, chunk text, ids, scores, the question, or the scope (D25).
    "blueprints",
    "knowledge",
    # D67 resolve_via rule-resolution telemetry (blueprint/rules.py): shape-only
    # selection counts + aggregate ranking scores for tuning the gap/confidence
    # cutoffs. `rule_id` is an AUTHORED blueprint-rule id (not a resolved value);
    # the counts/scores are aggregates — NEVER the resolved code strings (D25:
    # resolved domain values never enter telemetry).
    "rule_id",
    "selected_count",
    "dropped_count",
    "top_score",
    "cut_gap",
)

# Fine-grained observer events (from ToolDispatcher / AgentLoop) -> coarse,
# human-readable progress step labels (design §7 "same stage boundaries...
# coarser"). `{}`-style placeholders are filled from the allowlisted shape.
_STEP_LABELS: dict[str, str] = {
    # Retrieval fires TWO shape-only steps (design §3.5): a start signal while it
    # searches, and a completion step carrying the (blueprints, knowledge) counts.
    "retrieval_start": "searching for a matching blueprint…",
    "retrieval": "found matching context",
    "blueprint_rule_resolved": "resolved the filter set",
    "tool_dispatch_start": "running {tool_name}…",
    "tool_dispatch_ok": "step complete: {tool_name}",
    "tool_dispatch_denied": "step denied: {tool_name}",
    "loop_model_call_start": "thinking…",
    "loop_turn_done": "done",
    "loop_paused_ask_user": "waiting for your answer…",
    "loop_paused_budget_cap": "this is taking a while — continue, refine, or stop?",
    "loop_hard_ceiling_stop": "stopping — budget exhausted",
}


@dataclass(frozen=True)
class ProgressEvent:
    """One coarse, PII-safe progress step for the UI (D61)."""

    step: str
    shape: dict[str, Any] = field(default_factory=dict)


# The LLM-generated progress-summary channel (opt-in, `progress_summary_enabled`).
# Unlike every other event above, its `step` is NOT drawn from `_STEP_LABELS` and
# is NOT `.format()`-templated — the LLM-authored, present-tense line is used
# VERBATIM. This channel DELIBERATELY RELAXES the D25 "no cell/slot values in
# progress" rule: the line MAY carry concrete parameters drawn from the tool
# arguments (a period, a department, …) — that value-richness is the whole point
# of the feature, and why it is gated off by default. Only the `shape` (here just
# the machine-readable `tool_name`) is still governed by `_SHAPE_ALLOWLIST`; the
# value-bearing text lives in `step`, never in `shape`. See docs/08-ui.md.
#
# WHAT KEEPS THIS SAFE IS AT THE OTHER END. Because the text is used verbatim, the
# guard cannot live here — it lives in `progress_summarizer.py`, which shows the
# small model only a per-tool ALLOWLISTED projection of the arguments (no `sql`,
# no database/table/column, nothing at all for an unlisted tool) and replaces a
# line that repeats a withheld identifier with a static safe one. The relaxation
# admits BUSINESS values (a period, a department); it never admits internal
# database structure.
_PROGRESS_SUMMARY_EVENT = "tool_progress_summary"


def to_progress_event(event: str, payload: dict[str, Any]) -> ProgressEvent | None:
    """Translate one observer `(event, payload)` call into a `ProgressEvent`.

        Returns `None` for observer events that have no user-facing progress label; callers
        simply drop those.
    """
    if event == _PROGRESS_SUMMARY_EVENT:
        # Value-rich, LLM-authored line → straight into `step` (verbatim, never
        # templated so a `{` in the text cannot raise). D25-relaxed channel; see
        # the module note above. Drop a missing/blank summary (fail-soft parity).
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return None
        shape = {key: payload[key] for key in _SHAPE_ALLOWLIST if key in payload}
        return ProgressEvent(step=summary.strip(), shape=shape)
    label = _STEP_LABELS.get(event)
    if label is None:
        return None
    shape = {key: payload[key] for key in _SHAPE_ALLOWLIST if key in payload}
    try:
        step = label.format(**shape)
    except KeyError:
        step = label
    return ProgressEvent(step=step, shape=shape)


class ProgressEmitter:
    """Renders observer calls into `ProgressEvent`s on an asyncio queue —
    one instance per in-flight turn, consumed by `app.py`'s SSE endpoint."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        self._closed = False

    def observe(self, event: str, payload: dict[str, Any]) -> None:
        """The `(event, payload) -> None` callback — wire directly into
        `ToolDispatcher(observer=...)` / `AgentLoop(observer=...)`."""
        if self._closed:
            return
        progress_event = to_progress_event(event, payload)
        if progress_event is not None:
            self._queue.put_nowait(progress_event)

    def close(self) -> None:
        """Signal end-of-stream — call once the turn completes."""
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)

    async def stream(self) -> AsyncIterator[ProgressEvent]:
        """Async-iterate progress events until `close()` is called."""
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


def combine_observers(
    *observers: Callable[[str, dict[str, Any]], None],
) -> Callable[[str, dict[str, Any]], None]:
    """Fan one `(event, payload)` call out to multiple observers.

        `app.py` wires a tracing-span observer and a `ProgressEmitter.observe` together into
        the single callback `ToolDispatcher`/`AgentLoop` accept.
    """

    def _combined(event: str, payload: dict[str, Any]) -> None:
        for observer in observers:
            observer(event, payload)

    return _combined


__all__ = ["ProgressEmitter", "ProgressEvent", "combine_observers", "to_progress_event"]
