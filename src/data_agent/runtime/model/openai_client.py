"""OpenAIModelClient — Responses-primary / Chat-fallback `ModelClient` (D71).

Fallback triggers (OQ-C): 5xx, `APIConnectionError`, `APITimeoutError`, or an explicit
"feature unsupported" 4xx; once a turn falls back it stays on Chat Completions for the
rest of that turn. That stickiness is PER-TURN, held on the handle `begin_turn()`
returns — `app.py` shares one instance across concurrent turns, so mutating `self`
would let one turn stomp another's. The hand-rolled retry/backoff applies to the Chat
fallback only: a qualifying error on the Responses attempt IS the trigger to fall back.
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


def _responses_incomplete_reason(response: Any) -> str | None:
    """Why the provider ended this Responses round-trip early, or `None` for an ordinary
        completion.

        A Responses object carries `status="incomplete"` with an `incomplete_details.reason`
        (`max_output_tokens`, `content_filter`, ...) and, crucially, an `output` list that may
        hold NO message item at all — so the turn arrives at the loop looking exactly like a
        model that chose to say nothing. The reason is the only thing that tells those two
        apart, and it is carried out for TELEMETRY, not for branching (see
        `ModelTurnResult.incomplete_reason`).

        `"incomplete"` is the fallback when the status says the response was cut short but no
        reason is given: an empty string here would read as "ordinary completion" at every
        `if reason:` downstream, which is the one thing this must never do.
    """
    if getattr(response, "status", None) != "incomplete":
        return None
    details = getattr(response, "incomplete_details", None)
    reason = getattr(details, "reason", None) if details is not None else None
    return reason or "incomplete"


def _responses_result_to_turn(response: Any) -> ModelTurnResult:
    assistant_text_parts: list[str] = []
    tool_calls: list[ToolCallRequest] = []
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "message":
            for block in getattr(item, "content", None) or []:
                # BOTH CONTENT SHAPES, and the second one is the bug this reads for.
                # An `output_text` block carries `.text`; a REFUSAL block carries
                # `.refusal` and NO `.text` at all, so a `.text`-only read dropped
                # every refusal on the floor — the model had answered, the runtime
                # finished the turn `done` with `assistant_text=None`, and the user
                # got a blank bubble while the trace (which reads refusals) showed
                # the words. A refusal IS the assistant's turn-ending prose here:
                # nothing downstream distinguishes it, and nothing should — the
                # alternative is the silence that hid it.
                text = getattr(block, "text", None) or getattr(block, "refusal", None)
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
    if assistant_text is None and not tool_calls:
        # LAST RESORT, and deliberately only on the empty path: `output_text` is the
        # SDK's own convenience join over the output items, so it can rescue a shape
        # the walk above does not know (a future content block type). It is NOT
        # consulted when the walk found text — that would risk a same-text double
        # source disagreeing about ordering — and NOT when there are tool calls,
        # where empty prose is the normal, correct shape.
        assistant_text = (getattr(response, "output_text", None) or "").strip() or None
    usage = _extract_usage(getattr(response, "usage", None), style="responses")
    return ModelTurnResult(
        assistant_text=assistant_text,
        tool_calls=tool_calls,
        usage=usage,
        incomplete_reason=_responses_incomplete_reason(response),
    )


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


# `finish_reason` values that mean the completion was CUT SHORT rather than
# finished — the Chat-Completions counterpart of a Responses `status="incomplete"`,
# and reported through the same `ModelTurnResult.incomplete_reason` channel so one
# telemetry field covers both transports. `"stop"` and `"tool_calls"` are ordinary
# completions and are deliberately absent.
_CHAT_INCOMPLETE_FINISH_REASONS = frozenset({"length", "content_filter"})


def _chat_result_to_turn(response: Any) -> ModelTurnResult:
    choice = response.choices[0]
    message = choice.message
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
    # THE REFUSAL, on this transport too: a Chat message carries it as a sibling
    # FIELD of `content` (not as a content block), and `content` is `None` whenever
    # it is set. Same rule as the Responses walk — a refusal is turn-ending prose,
    # and dropping it is what produced a blank `done`.
    assistant_text = getattr(message, "content", None) or getattr(message, "refusal", None)
    finish_reason = getattr(choice, "finish_reason", None)
    usage = _extract_usage(getattr(response, "usage", None), style="chat")
    return ModelTurnResult(
        assistant_text=assistant_text,
        tool_calls=tool_calls,
        usage=usage,
        incomplete_reason=(
            finish_reason if finish_reason in _CHAT_INCOMPLETE_FINISH_REASONS else None
        ),
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
        """Return a FRESH handle with reset Responses/Chat fallback stickiness.

                Deliberately does NOT mutate `self`, which may be a process-wide singleton
                shared across concurrent `/turn` requests. The returned wrapper reuses the same
                `AsyncOpenAI` transport; callers must use it for every `send_turn` in the turn.
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

        Kept separate from `__init__` so tests can inject a mock `client` without
        constructing a real `AsyncOpenAI` (which validates `api_key` eagerly).
    """
    client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url or None)
    return OpenAIModelClient(client, model=model)


__all__ = [
    "OpenAIModelClient",
    "build_openai_model_client",
]
