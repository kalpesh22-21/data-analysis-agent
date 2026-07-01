"""Unit tests for model/openai_client.py (Layer 1 — mocked openai async client, no network)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from data_agent.runtime.model.openai_client import OpenAIModelClient

RESPONSES_TOOLS = [
    {
        "type": "function",
        "name": "runQuery",
        "description": "Run a SQL query.",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}},
    }
]


async def _no_sleep(_seconds: float) -> None:
    return None


def _responses_message_result(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
    )


def _responses_tool_call_result(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(
        output=[SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=arguments)],
        usage=SimpleNamespace(input_tokens=20, output_tokens=8, total_tokens=28),
    )


def _chat_message_result(
    text: str | None, tool_calls: list[SimpleNamespace] | None = None
) -> SimpleNamespace:
    message = SimpleNamespace(content=text, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=6, total_tokens=18),
    )


class _FakeResponses:
    def __init__(self, side_effects: list[Any]) -> None:
        self._side_effects = list(side_effects)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._side_effects.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeChatCompletions:
    def __init__(self, side_effects: list[Any]) -> None:
        self._side_effects = list(side_effects)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._side_effects.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeOpenAIClient:
    def __init__(
        self, responses_effects: list[Any], chat_effects: list[Any] | None = None
    ) -> None:
        self.responses = _FakeResponses(responses_effects)
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(chat_effects or []))


def _http_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=_http_request())


def _server_error() -> openai.InternalServerError:
    response = httpx.Response(500, request=_http_request())
    return openai.InternalServerError(message="boom", response=response, body=None)


async def test_send_turn_happy_path_uses_responses_api() -> None:
    fake_client = _FakeOpenAIClient(responses_effects=[_responses_message_result("hello")])
    model_client = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    result = await model_client.send_turn(
        [{"role": "user", "content": "hi"}], RESPONSES_TOOLS
    )

    assert result.assistant_text == "hello"
    assert result.tool_calls == []
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert len(fake_client.responses.calls) == 1
    assert len(fake_client.chat.completions.calls) == 0


async def test_send_turn_parses_tool_calls_from_responses_api() -> None:
    fake_client = _FakeOpenAIClient(
        responses_effects=[_responses_tool_call_result("call_1", "runQuery", '{"sql": "SELECT 1"}')]
    )
    model_client = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    result = await model_client.send_turn(
        [{"role": "user", "content": "run a query"}], RESPONSES_TOOLS
    )

    assert result.assistant_text is None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "runQuery"
    assert result.tool_calls[0].arguments == {"sql": "SELECT 1"}


async def test_5xx_falls_back_to_chat_completions_with_shape_translation() -> None:
    fake_client = _FakeOpenAIClient(
        responses_effects=[_server_error()],
        chat_effects=[_chat_message_result("fallback answer")],
    )
    model_client = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    result = await model_client.send_turn(
        [{"role": "user", "content": "hi"}], RESPONSES_TOOLS
    )

    assert result.assistant_text == "fallback answer"
    assert len(fake_client.responses.calls) == 1
    assert len(fake_client.chat.completions.calls) == 1
    # Tool schema was translated from the flat Responses shape to Chat's nested shape.
    sent_tools = fake_client.chat.completions.calls[0]["tools"]
    assert sent_tools[0]["type"] == "function"
    assert sent_tools[0]["function"]["name"] == "runQuery"


async def test_connection_error_triggers_fallback() -> None:
    fake_client = _FakeOpenAIClient(
        responses_effects=[_connection_error()],
        chat_effects=[_chat_message_result("fallback answer")],
    )
    model_client = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    result = await model_client.send_turn([{"role": "user", "content": "hi"}], RESPONSES_TOOLS)
    assert result.assistant_text == "fallback answer"


async def test_non_fallback_error_propagates_without_fallback() -> None:
    response = httpx.Response(400, request=_http_request())
    bad_request = openai.BadRequestError(message="bad input", response=response, body=None)
    fake_client = _FakeOpenAIClient(responses_effects=[bad_request])
    model_client = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    with pytest.raises(openai.BadRequestError):
        await model_client.send_turn([{"role": "user", "content": "hi"}], RESPONSES_TOOLS)
    assert len(fake_client.chat.completions.calls) == 0


async def test_fallback_is_sticky_within_a_turn_then_resets_on_begin_turn() -> None:
    fake_client = _FakeOpenAIClient(
        responses_effects=[_server_error()],
        chat_effects=[_chat_message_result("first"), _chat_message_result("second")],
    )
    model_client = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    first = await model_client.send_turn([{"role": "user", "content": "hi"}], RESPONSES_TOOLS)
    assert first.assistant_text == "first"
    assert len(fake_client.responses.calls) == 1  # fell back

    # Same turn: a second send_turn call must stay on Chat (no new Responses attempt).
    second = await model_client.send_turn([{"role": "user", "content": "more"}], RESPONSES_TOOLS)
    assert second.assistant_text == "second"
    assert len(fake_client.responses.calls) == 1  # still just the one, sticky
    assert len(fake_client.chat.completions.calls) == 2

    # Next external turn: begin_turn() returns a FRESH handle (B3 — never
    # mutates `model_client` itself, since it may be a shared singleton);
    # callers must use the RETURNED handle, which retries Responses first.
    fake_client.responses._side_effects.append(_responses_message_result("responses again"))
    next_turn_client = model_client.begin_turn()
    assert next_turn_client is not model_client
    third = await next_turn_client.send_turn(
        [{"role": "user", "content": "next turn"}], RESPONSES_TOOLS
    )
    assert third.assistant_text == "responses again"
    assert len(fake_client.responses.calls) == 2


async def test_chat_fallback_retries_transient_errors_with_backoff() -> None:
    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    fake_client = _FakeOpenAIClient(
        responses_effects=[_server_error()],
        chat_effects=[_server_error(), _chat_message_result("recovered")],
    )
    model_client = OpenAIModelClient(
        fake_client, model="gpt-4.1", max_retries=2, backoff_base_seconds=0.1, sleep=_record_sleep
    )

    result = await model_client.send_turn([{"role": "user", "content": "hi"}], RESPONSES_TOOLS)

    assert result.assistant_text == "recovered"
    assert len(fake_client.chat.completions.calls) == 2
    assert sleeps == [0.1]


async def test_chat_fallback_gives_up_after_max_retries() -> None:
    fake_client = _FakeOpenAIClient(
        responses_effects=[_server_error()],
        chat_effects=[_server_error(), _server_error(), _server_error()],
    )
    model_client = OpenAIModelClient(
        fake_client, model="gpt-4.1", max_retries=2, backoff_base_seconds=0.0, sleep=_no_sleep
    )

    with pytest.raises(openai.InternalServerError):
        await model_client.send_turn([{"role": "user", "content": "hi"}], RESPONSES_TOOLS)
    assert len(fake_client.chat.completions.calls) == 3  # initial + 2 retries
