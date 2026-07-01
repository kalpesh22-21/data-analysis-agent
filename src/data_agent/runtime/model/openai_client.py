"""OpenAIModelClient — Responses-primary / Chat-fallback `ModelClient` (D71, design §4.2).

Fallback taxonomy (OQ-C, LOCKED by the orchestrator brief): OpenAI 5xx,
`APIConnectionError`, `APITimeoutError`, or an explicit "feature unsupported"
4xx trigger an immediate fallback from `client.responses.create(...)` to
`client.chat.completions.create(...)` for the *same* logical turn. Once a turn
has fallen back, every subsequent `send_turn` call within that turn stays on
Chat Completions.

Per-turn stickiness, not per-instance (B3 fix, 2026-07-01): `begin_turn()` —
called once per external turn by `loop/agent_loop.py::_begin_model_turn` (and
by `context/llm_summarizer.py` before each summarization call) — returns a
FRESH `OpenAIModelClient` handle with its own independent
`_fell_back_this_turn` flag, rather than mutating `self`. This matters because
`app.py`'s composition root constructs exactly ONE `OpenAIModelClient` and
shares it across every concurrent `/turn` request AND the summarizer's
background calls; if `begin_turn()` mutated shared instance state, two
interleaved turns (or a turn and a concurrent summarization) could stomp each
other's Responses-vs-Chat stickiness mid-turn. Every caller MUST use the
handle `begin_turn()` returns for the rest of that turn's `send_turn` calls —
`model/client.py::begin_turn_client()` is the single shared call site that
enforces this.

Retry/backoff is hand-rolled (no `tenacity`, per design §9): a qualifying
transient error on the Chat Completions fallback itself is retried up to
`max_retries` times with exponential backoff (`sleep` is injectable so tests
never actually sleep). The Responses attempt itself is not internally
retried — a qualifying error there *is* the trigger to fall back, not to
retry the same endpoint.

Translation (design §4.2 "translating tool-call/tool-result shapes between the
two APIs' formats"): `model/client.py`'s canonical message shape is
Chat-Completions-flavored (assistant `tool_calls` nested under
`{"function": {"name", "arguments"}}`, tool results as `{"role": "tool",
"tool_call_id", "content"}`). `tool_schema.py` emits tool declarations in the
Responses API's flat shape (`{"type": "function", "name", ...}` — no nested
`"function"` key), so translation is needed in the *other* direction for the
Chat Completions fallback (nest under `"function"`), and messages need
translating *into* Responses `input` items (flat `function_call` /
`function_call_output` items rather than nested `tool_calls`) for the primary
path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import openai

from .client import ModelTurnResult, ToolCallRequest

_RETRYABLE_STATUS_THRESHOLD = 500
_UNSUPPORTED_MARKERS = ("not supported", "unsupported")


def _is_fallback_error(exc: Exception) -> bool:
    """OQ-C taxonomy: 5xx / connection / timeout / explicit "unsupported" 4xx."""
    if isinstance(exc, openai.APIConnectionError | openai.APITimeoutError):
        return True
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status >= _RETRYABLE_STATUS_THRESHOLD:
            return True
        message = str(exc).lower()
        if any(marker in message for marker in _UNSUPPORTED_MARKERS):
            return True
    return False


def _safe_json_loads(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_plain_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return vars(obj)


def _extract_usage(usage: Any, *, style: str) -> dict[str, Any]:
    data = _as_plain_dict(usage)
    if not data:
        return {}
    if style == "responses":
        return {
            "prompt_tokens": data.get("input_tokens"),
            "completion_tokens": data.get("output_tokens"),
            "total_tokens": data.get("total_tokens"),
        }
    return {
        "prompt_tokens": data.get("prompt_tokens"),
        "completion_tokens": data.get("completion_tokens"),
        "total_tokens": data.get("total_tokens"),
    }


# ---------------------------------------------------------------------------
# messages/tools -> Responses API shapes
# ---------------------------------------------------------------------------


def _messages_to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role in ("system", "user"):
            items.append({"role": role, "content": message.get("content") or ""})
        elif role == "assistant":
            content = message.get("content")
            if content:
                items.append({"role": "assistant", "content": content})
            for tool_call in message.get("tool_calls") or []:
                function = tool_call["function"]
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call["id"],
                        "name": function["name"],
                        "arguments": function["arguments"],
                    }
                )
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": message.get("content") or "",
                }
            )
        else:
            raise ValueError(f"Unknown canonical message role: {role!r}")
    return items


def _responses_result_to_turn(response: Any) -> ModelTurnResult:
    assistant_text_parts: list[str] = []
    tool_calls: list[ToolCallRequest] = []
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "message":
            for block in getattr(item, "content", None) or []:
                text = getattr(block, "text", None)
                if text:
                    assistant_text_parts.append(text)
        elif item_type == "function_call":
            tool_calls.append(
                ToolCallRequest(
                    id=item.call_id,
                    name=item.name,
                    arguments=_safe_json_loads(item.arguments),
                )
            )
    assistant_text = "\n".join(assistant_text_parts) if assistant_text_parts else None
    usage = _extract_usage(getattr(response, "usage", None), style="responses")
    return ModelTurnResult(assistant_text=assistant_text, tool_calls=tool_calls, usage=usage)


# ---------------------------------------------------------------------------
# messages/tools -> Chat Completions API shapes
# ---------------------------------------------------------------------------


def _messages_to_chat(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chat_messages: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
            tool_calls = message.get("tool_calls")
            if tool_calls:
                entry["tool_calls"] = [dict(tc) for tc in tool_calls]
            chat_messages.append(entry)
        elif role == "tool":
            chat_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": message["tool_call_id"],
                    "content": message.get("content") or "",
                }
            )
        else:
            chat_messages.append({"role": role, "content": message.get("content") or ""})
    return chat_messages


def _tools_to_chat(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chat_tools: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            chat_tools.append(tool)
            continue
        chat_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                },
            }
        )
    return chat_tools


def _chat_result_to_turn(response: Any) -> ModelTurnResult:
    message = response.choices[0].message
    tool_calls: list[ToolCallRequest] = []
    for tool_call in getattr(message, "tool_calls", None) or []:
        function = tool_call.function
        tool_calls.append(
            ToolCallRequest(
                id=tool_call.id,
                name=function.name,
                arguments=_safe_json_loads(function.arguments),
            )
        )
    usage = _extract_usage(getattr(response, "usage", None), style="chat")
    return ModelTurnResult(
        assistant_text=getattr(message, "content", None), tool_calls=tool_calls, usage=usage
    )


_AsyncSleep = Callable[[float], Awaitable[None]]


class OpenAIModelClient:
    """`ModelClient` over the real OpenAI SDK — Responses primary, Chat fallback."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.5,
        sleep: _AsyncSleep = asyncio.sleep,
    ) -> None:
        self._client = client
        self._model = model
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._sleep = sleep
        self._fell_back_this_turn = False

    def begin_turn(self) -> OpenAIModelClient:
        """Return a FRESH `OpenAIModelClient` handle with reset Responses/Chat
        fallback stickiness (design §4.2; B3 fix, 2026-07-01).

        Deliberately does NOT mutate `self._fell_back_this_turn` — `self` may
        be a single process-wide singleton shared across many concurrent
        `/turn` requests (`app.py`'s composition root) and the context
        summarizer's background calls (`context/llm_summarizer.py`); mutating
        shared instance state here would let one turn's fallback stomp
        another's mid-turn. The returned object is a cheap wrapper reusing the
        SAME underlying `AsyncOpenAI` transport client/model/retry config —
        only the turn-local `_fell_back_this_turn` flag is fresh. Callers
        (`loop/agent_loop.py::_begin_model_turn`, `context/llm_summarizer.py`)
        must use the RETURNED handle for every `send_turn` call within that
        turn, not `self` directly.
        """
        return OpenAIModelClient(
            self._client,
            model=self._model,
            max_retries=self._max_retries,
            backoff_base_seconds=self._backoff_base_seconds,
            sleep=self._sleep,
        )

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        if not self._fell_back_this_turn:
            try:
                return await self._call_responses(messages, tools)
            except Exception as exc:
                if not _is_fallback_error(exc):
                    raise
                self._fell_back_this_turn = True
        return await self._call_chat_with_retry(messages, tools)

    async def _call_responses(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        response = await self._client.responses.create(
            model=self._model,
            input=_messages_to_responses_input(messages),
            tools=list(tools),
        )
        return _responses_result_to_turn(response)

    async def _call_chat_with_retry(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        chat_messages = _messages_to_chat(messages)
        chat_tools = _tools_to_chat(tools)
        attempt = 0
        while True:
            try:
                response = await self._client.chat.completions.create(
                    model=self._model, messages=chat_messages, tools=chat_tools
                )
                return _chat_result_to_turn(response)
            except Exception as exc:
                if not _is_fallback_error(exc) or attempt >= self._max_retries:
                    raise
                await self._sleep(self._backoff_base_seconds * (2**attempt))
                attempt += 1


def build_openai_model_client(
    *, api_key: str, model: str, base_url: str = ""
) -> OpenAIModelClient:
    """Construct an `OpenAIModelClient` wired to a real `AsyncOpenAI` client.

    Kept separate from `__init__` so tests can inject a mock `client` directly
    without constructing a real `AsyncOpenAI` (which validates `api_key` at
    construction time). `app.py` (the composition root) is the only caller.
    """
    client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url or None)
    return OpenAIModelClient(client, model=model)


__all__ = [
    "OpenAIModelClient",
    "build_openai_model_client",
]
