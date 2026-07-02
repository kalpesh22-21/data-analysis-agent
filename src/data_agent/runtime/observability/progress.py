"""ProgressEmitter — UI-facing progress events (D61, design §7).

Same stage boundaries as `tracing.py`'s spans, but coarser and PII-safe by
construction: every emitted `ProgressEvent` carries only a human-readable
`step` label plus a `shape` dict drawn from an explicit allowlist of
non-sensitive fields (tool name, error code, budget window number, tool-call
count) — never SQL text, never row/cell values, never the JWT or raw scope
(design §7: "progress events... carry step/shape, not values").

`ProgressEmitter.observe` is directly wire-compatible with
`dispatch.tool_dispatcher.ToolObserver` and the same-shaped callback
`loop.agent_loop.AgentLoop`'s `observer` constructor argument expects, so one
`ProgressEmitter` instance can be handed to both. `combine_observers` fans a
single `(event, payload)` call out to multiple observers (e.g. this emitter
*and* a tracing-span-emitting observer) — the "one instrumentation, two
consumers" property (design §7).
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
    "error_code",
    "window",
    "tool_calls_made",
    "question",
    # Slice-1 retrieval (design §3.5): shape-only pre-injection counts — the
    # number of blueprint thin cards / knowledge hits surfaced. Never any card
    # text, chunk text, ids, scores, the question, or the scope (D25).
    "blueprints",
    "knowledge",
)

# Fine-grained observer events (from ToolDispatcher / AgentLoop) -> coarse,
# human-readable progress step labels (design §7 "same stage boundaries...
# coarser"). `{}`-style placeholders are filled from the allowlisted shape.
_STEP_LABELS: dict[str, str] = {
    # Retrieval fires TWO shape-only steps (design §3.5): a start signal while it
    # searches, and a completion step carrying the (blueprints, knowledge) counts.
    "retrieval_start": "searching for a matching blueprint…",
    "retrieval": "found matching context",
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


def to_progress_event(event: str, payload: dict[str, Any]) -> ProgressEvent | None:
    """Translate one observer `(event, payload)` call into a `ProgressEvent`.

    Returns `None` for observer events that have no user-facing progress
    label (e.g. internal-only telemetry) — callers should simply drop those.
    """
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
        """Signal end-of-stream — call once the turn completes (design §7)."""
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

    "One instrumentation, two consumers" (design §7): `app.py` wires this
    around a tracing-span-emitting observer and a `ProgressEmitter.observe`
    together into a single callback passed to `ToolDispatcher`/`AgentLoop`.
    """

    def _combined(event: str, payload: dict[str, Any]) -> None:
        for observer in observers:
            observer(event, payload)

    return _combined


__all__ = ["ProgressEmitter", "ProgressEvent", "combine_observers", "to_progress_event"]
