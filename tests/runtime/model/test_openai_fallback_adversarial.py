"""Adversarial D71 Responses->Chat fallback coverage (QA hardening pass).

`tests/runtime/model/test_openai_client.py` already proves the core fallback
trigger taxonomy (5xx, connection error), mid-turn stickiness, and
begin_turn() reset. This file adds the harsher case the QA brief calls out
explicitly: `tool_call_id` correlation preserved ACROSS the shape translation
in BOTH directions —

  1. canonical messages (assistant `tool_calls[].id` / tool `tool_call_id`)
     -> Responses `input` items (`function_call.call_id` /
     `function_call_output.call_id`) on the PRIMARY path, and
  2. the same canonical messages -> Chat Completions' nested
     `tool_calls[].id` / `{"role": "tool", "tool_call_id": ...}` shape on the
     FALLBACK path,

with MULTIPLE concurrent tool calls (to catch id-swapping/reordering bugs
that a single-tool-call test cannot), and a timeout-triggered fallback (not
just 5xx/connection-error) per the OQ-C taxonomy.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import openai

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


def _http_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def _server_error() -> openai.InternalServerError:
    response = httpx.Response(500, request=_http_request())
    return openai.InternalServerError(message="boom", response=response, body=None)


def _timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=_http_request())


def _chat_message_result(text: str) -> SimpleNamespace:
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
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
    def __init__(self, responses_effects: list[Any], chat_effects: list[Any] | None = None) -> None:
        self.responses = _FakeResponses(responses_effects)
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(chat_effects or []))


def _assistant_with_two_tool_calls() -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_A_first",
                "type": "function",
                "function": {"name": "runQuery", "arguments": '{"sql": "SELECT 1"}'},
            },
            {
                "id": "call_B_second",
                "type": "function",
                "function": {"name": "runQuery", "arguments": '{"sql": "SELECT 2"}'},
            },
        ],
    }


def _tool_results_for_both() -> list[dict[str, Any]]:
    return [
        {"role": "tool", "tool_call_id": "call_A_first", "content": '{"status": "ok", "result": 1}'},
        {"role": "tool", "tool_call_id": "call_B_second", "content": '{"status": "ok", "result": 2}'},
    ]


def _history_with_two_prior_tool_calls() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "You are a data analyst."},
        {"role": "user", "content": "Run two queries."},
        _assistant_with_two_tool_calls(),
        *_tool_results_for_both(),
        {"role": "user", "content": "Now summarize both."},
    ]


async def test_tool_call_id_correlation_preserved_into_responses_input_shape() -> None:
    """PRIMARY path translation: canonical messages -> Responses `input`
    items must preserve call_id correlation for BOTH tool calls, in order,
    with the correct assistant/tool-result pairing (no id swap/merge)."""
    fake_client = _FakeOpenAIClient(
        responses_effects=[
            SimpleNamespace(
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="output_text", text="summary")],
                    )
                ],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
            )
        ]
    )
    model_client = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    await model_client.send_turn(_history_with_two_prior_tool_calls(), RESPONSES_TOOLS)

    sent_input = fake_client.responses.calls[0]["input"]
    function_calls = [item for item in sent_input if item.get("type") == "function_call"]
    function_call_outputs = [item for item in sent_input if item.get("type") == "function_call_output"]

    assert [fc["call_id"] for fc in function_calls] == ["call_A_first", "call_B_second"]
    assert [fco["call_id"] for fco in function_call_outputs] == ["call_A_first", "call_B_second"]
    # The output content for each call_id must correlate to the RIGHT tool
    # result, not just be present somewhere in the payload.
    output_by_id = {fco["call_id"]: fco["output"] for fco in function_call_outputs}
    assert '"result": 1' in output_by_id["call_A_first"]
    assert '"result": 2' in output_by_id["call_B_second"]


async def test_tool_call_id_correlation_preserved_across_fallback_to_chat() -> None:
    """FALLBACK path translation: the SAME history (with two prior tool
    calls) must retain correct id correlation when translated into Chat
    Completions' nested shape after a mid-turn fallback."""
    fake_client = _FakeOpenAIClient(
        responses_effects=[_server_error()],
        chat_effects=[_chat_message_result("fallback summary")],
    )
    model_client = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    result = await model_client.send_turn(_history_with_two_prior_tool_calls(), RESPONSES_TOOLS)
    assert result.assistant_text == "fallback summary"

    sent_messages = fake_client.chat.completions.calls[0]["messages"]
    assistant_message = next(m for m in sent_messages if m["role"] == "assistant" and m.get("tool_calls"))
    sent_ids = [tc["id"] for tc in assistant_message["tool_calls"]]
    assert sent_ids == ["call_A_first", "call_B_second"]

    tool_messages = [m for m in sent_messages if m["role"] == "tool"]
    tool_ids_in_order = [m["tool_call_id"] for m in tool_messages]
    assert tool_ids_in_order == ["call_A_first", "call_B_second"]
    content_by_id = {m["tool_call_id"]: m["content"] for m in tool_messages}
    assert '"result": 1' in content_by_id["call_A_first"]
    assert '"result": 2' in content_by_id["call_B_second"]


async def test_timeout_error_triggers_fallback_per_oq_c_taxonomy() -> None:
    """OQ-C's locked taxonomy explicitly includes APITimeoutError, not just
    5xx/connection errors — this is a distinct exception type/path and is
    worth its own adversarial check (a narrower `except` clause that only
    caught APIConnectionError would silently NOT fall back on a timeout)."""
    fake_client = _FakeOpenAIClient(
        responses_effects=[_timeout_error()],
        chat_effects=[_chat_message_result("recovered from timeout")],
    )
    model_client = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    result = await model_client.send_turn(
        [{"role": "user", "content": "hi"}], RESPONSES_TOOLS
    )
    assert result.assistant_text == "recovered from timeout"
    assert len(fake_client.chat.completions.calls) == 1


async def test_fallback_mid_turn_stickiness_holds_across_three_tool_call_iterations() -> None:
    """A harsher stickiness check than the existing 2-call test: THREE
    send_turn calls within one external turn, each carrying an ever-growing
    tool-call history, must all stay on Chat after the first fallback — and
    each translation must still correlate ids correctly at every depth."""
    fake_client = _FakeOpenAIClient(
        responses_effects=[_server_error()],
        chat_effects=[
            _chat_message_result("iter1"),
            _chat_message_result("iter2"),
            _chat_message_result("iter3"),
        ],
    )
    model_client = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    history = [{"role": "user", "content": "start"}]
    r1 = await model_client.send_turn(history, RESPONSES_TOOLS)
    assert r1.assistant_text == "iter1"

    history = history + [_assistant_with_two_tool_calls(), *_tool_results_for_both()]
    r2 = await model_client.send_turn(history, RESPONSES_TOOLS)
    assert r2.assistant_text == "iter2"
    assert len(fake_client.responses.calls) == 1  # still sticky — no 2nd Responses attempt

    history = history + [_assistant_with_two_tool_calls(), *_tool_results_for_both()]
    r3 = await model_client.send_turn(history, RESPONSES_TOOLS)
    assert r3.assistant_text == "iter3"
    assert len(fake_client.responses.calls) == 1  # still sticky through iteration 3
    assert len(fake_client.chat.completions.calls) == 3

    # id correlation intact at the deepest iteration too.
    last_call_messages = fake_client.chat.completions.calls[2]["messages"]
    tool_ids = [m["tool_call_id"] for m in last_call_messages if m["role"] == "tool"]
    assert tool_ids.count("call_A_first") == 2  # appears once per prior iteration's exchange
    assert tool_ids.count("call_B_second") == 2


# ---------------------------------------------------------------------------
# B3 — fallback stickiness must be per-turn, not per shared-instance
# ---------------------------------------------------------------------------


async def test_two_concurrent_turns_sharing_one_client_do_not_share_fallback_state() -> None:
    """`app.py`'s composition root shares ONE `OpenAIModelClient` across every
    concurrent `/turn` request (and the summarizer). `begin_turn()` must
    therefore return an INDEPENDENT per-turn handle, not mutate shared
    instance state — otherwise turn A falling back to Chat would silently
    force turn B onto Chat too, even though turn B's own Responses attempt
    would have succeeded."""
    fake_client = _FakeOpenAIClient(
        responses_effects=[
            _server_error(),  # turn A's Responses attempt -> falls back to Chat
            SimpleNamespace(  # turn B's (separate) Responses attempt -> succeeds
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="output_text", text="turn B stayed on responses")],
                    )
                ],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
            ),
        ],
        chat_effects=[_chat_message_result("turn A fell back")],
    )
    shared_singleton = OpenAIModelClient(fake_client, model="gpt-4.1", sleep=_no_sleep)

    # Two "turns" begin on the SAME shared singleton (mirrors app.py's
    # composition root), each getting its own independent handle.
    turn_a = shared_singleton.begin_turn()
    turn_b = shared_singleton.begin_turn()
    assert turn_a is not turn_b
    assert turn_a is not shared_singleton
    assert turn_b is not shared_singleton

    # Turn A hits a 5xx on Responses and falls back to Chat.
    result_a = await turn_a.send_turn([{"role": "user", "content": "A"}], RESPONSES_TOOLS)
    assert result_a.assistant_text == "turn A fell back"

    # Turn B — interleaved on the SAME shared singleton — must NOT inherit
    # turn A's fallback stickiness: it retries Responses first and succeeds.
    result_b = await turn_b.send_turn([{"role": "user", "content": "B"}], RESPONSES_TOOLS)
    assert result_b.assistant_text == "turn B stayed on responses"

    assert len(fake_client.responses.calls) == 2  # A's failed attempt + B's successful one
    assert len(fake_client.chat.completions.calls) == 1  # only A ever fell back to Chat

    # The shared singleton's own instance state is untouched by either turn
    # (the fix's load-bearing property): a brand-new turn off it still
    # attempts Responses first, unaffected by A's or B's history.
    fake_client.responses._side_effects.append(
        SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="turn C also on responses")],
                )
            ],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        )
    )
    turn_c = shared_singleton.begin_turn()
    result_c = await turn_c.send_turn([{"role": "user", "content": "C"}], RESPONSES_TOOLS)
    assert result_c.assistant_text == "turn C also on responses"
