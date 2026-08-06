"""ProgressSummarizer — a small/cheap side LLM that turns one tool CALL (name +
args, NOT results) into a natural-language, present-tense UI progress line
(design §7 / docs/08-ui.md; opt-in behind `progress_summary_enabled`).

Today the progress stream shows raw tool-name templates ("running runQuery…").
When enabled, the agent loop fires this summarizer CONCURRENTLY with each tool
dispatch (never awaited before dispatch — it must not add latency to the tool)
to mint a richer line ("Querying overtime pay by department for January") that
is streamed to the UI as it arrives, additive to the instant template label.

D25 relaxation (this channel ONLY, gated): the produced line is deliberately
VALUE-RICH — it MAY include concrete parameters drawn from the tool arguments
(e.g. a period, a department). This relaxes the D25 "no cell/slot values in
progress" rule for the progress-summary channel and is why the feature is
opt-in. The machine-readable tool name stays in the progress event's `shape`.

Fail-soft everywhere (load-bearing): `summarize` returns `None` on ANY error,
timeout, or empty output — a flaky/slow summarizer must never break a turn nor
delay a tool. Token cost is bounded: each argument value is truncated before it
is serialized into the prompt, and the whole call is wrapped in a timeout.

Reuses the per-call-scoped model handle pattern (`begin_turn_client`, B3): this
call may run concurrently with an in-flight `/turn` also using a shared
`OpenAIModelClient`, so it never touches the shared instance's fallback
stickiness directly. The auto-instrumented OpenAI span covers this extra call.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from data_agent.runtime.model.client import ModelClient, begin_turn_client

_SYSTEM_PROMPT = (
    "You write a single short present-tense progress line (max ~12 words) for a "
    "data-analysis assistant's UI, describing what it is doing with this tool call. "
    "You MAY include specific parameters from the arguments. No preamble, no quotes, "
    "no trailing period, no fluff. Output only the line."
)

# Per-argument-value truncation bound (characters) applied BEFORE serialization,
# so a huge `sql` string or a long list argument cannot blow up the prompt token
# count. The line only needs the gist of the call, never the full payload.
_MAX_ARG_VALUE_CHARS = 300
# Overall cap on the compact-JSON arguments blob handed to the model (defense in
# depth over the per-value truncation, e.g. an args dict with very many keys).
_MAX_ARGS_BLOB_CHARS = 1200


def _truncate(value: Any) -> Any:
    """Bound one argument value's serialized size. Strings are cut to
    `_MAX_ARG_VALUE_CHARS`; lists/tuples are element-truncated then capped;
    dicts are recursively value-truncated; scalars pass through unchanged."""
    if isinstance(value, str):
        return value if len(value) <= _MAX_ARG_VALUE_CHARS else value[:_MAX_ARG_VALUE_CHARS] + "…"
    if isinstance(value, (list, tuple)):
        return [_truncate(item) for item in list(value)[:20]]
    if isinstance(value, dict):
        return {str(k): _truncate(v) for k, v in list(value.items())[:20]}
    return value


def _compact_args(arguments: dict[str, Any]) -> str:
    """Serialize *arguments* to a compact, token-bounded JSON blob for the prompt."""
    try:
        bounded = {str(k): _truncate(v) for k, v in list(arguments.items())[:20]}
        blob = json.dumps(bounded, ensure_ascii=False, default=str)
    except Exception:
        blob = str(arguments)[:_MAX_ARGS_BLOB_CHARS]
    if len(blob) > _MAX_ARGS_BLOB_CHARS:
        blob = blob[:_MAX_ARGS_BLOB_CHARS] + "…"
    return blob


class ProgressSummarizer:
    """One-shot tool-call → progress-line summarizer over a cheap `ModelClient`."""

    def __init__(self, model_client: ModelClient, *, timeout_seconds: float = 3.0) -> None:
        self._model_client = model_client
        self._timeout_seconds = timeout_seconds

    async def summarize(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """Return a short present-tense progress line for *tool_name*(*arguments*),
        or `None` on any error/timeout/empty output (fail-soft — never raises)."""
        try:
            return await asyncio.wait_for(
                self._summarize(tool_name, arguments), timeout=self._timeout_seconds
            )
        except Exception:
            # Fail-soft: a timeout (asyncio.TimeoutError), a transport error, or
            # any other failure drops the summary — the instant template label
            # already streamed, so the UI degrades to "running <tool>…".
            return None

    async def _summarize(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Tool: {tool_name}. Arguments: {_compact_args(arguments)}",
            },
        ]
        # B3: never mutate the shared client's fallback stickiness directly.
        turn_client = begin_turn_client(self._model_client)
        result = await turn_client.send_turn(messages, tools=[])
        text = (result.assistant_text or "").strip()
        # Strip a wrapping pair of quotes the model sometimes adds despite the prompt.
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1].strip()
        return text or None


__all__ = ["ProgressSummarizer"]
