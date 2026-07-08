"""D25 online-invariant: the auto-instrumented OpenAI `LLM` span must NOT carry
raw prompt/completion content — nor a content-bearing exception event — BY
DEFAULT.

Tags:
  - runtime-llm-span-content-hidden-by-default
  - runtime-llm-span-no-exception-content-when-hidden

Unlike the manual AGENT/TOOL/CHAIN spans (which redact SQL literals / bound-slot
values before setting an attribute), the OpenInference OpenAI auto-instrumentor
(D24) captures the model's raw prompt AND completion by default — and, on a
failed request, `record_exception(exc)` attaches the response error body to the
span as an `exception` EVENT. Both are content leaks into the online per-turn
Phoenix project (a shape/count/latency-only surface). The runtime closes both:
`instrument_openai(hide_content=True)` (a `TraceConfig` masking ATTRIBUTES) plus
`LLMExceptionEventScrubber` (a span processor dropping content-bearing EVENTS),
both gated on `otlp_hide_llm_content` and wired by `configure_tracing`.

These tests exercise the REAL wiring (`configure_tracing` + the real
`OpenAIInstrumentor`) over a REAL `openai.AsyncOpenAI` whose transport is a local
`httpx.MockTransport` (no network, no OpenAI key/spend), so the assertion is over
the ACTUAL emitted span, not a config proxy. Both the Responses API (production-
primary) and Chat Completions (fallback) code paths are driven. The instrumentor
is a process-global singleton, so each run uninstruments first (deterministic
regardless of suite order) and an autouse fixture uninstruments on cleanup.
"""

from __future__ import annotations

import httpx
import openai
import pytest
from openinference.instrumentation.openai import OpenAIInstrumentor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.observability import tracing

_QUESTION = "How many employees are in the Sales department?"
_ANSWER = "There are 2 active employees in the Sales department."
_SECRET = "SECRET_ERROR_BODY_MUST_NOT_RENDER_zzz42"
_REDACTED = "__REDACTED__"
# The coarse content channels OpenInference REDACTS in place (value → __REDACTED__)
# under hide_inputs/hide_outputs; the per-message channels (llm.input_messages.* /
# llm.output_messages.*) are DROPPED entirely by hide_input_messages/hide_output_
# messages. Either way, no raw prompt/completion text survives.
_REDACTED_IN_PLACE_KEYS = ("input.value", "output.value")

_RESPONSES_BODY = {
    "id": "resp_x",
    "object": "response",
    "created_at": 0,
    "model": "gpt-test",
    "status": "completed",
    "error": None,
    "incomplete_details": None,
    "instructions": None,
    "max_output_tokens": None,
    "parallel_tool_calls": True,
    "temperature": 1.0,
    "tool_choice": "auto",
    "tools": [],
    "top_p": 1.0,
    "output": [
        {
            "type": "message",
            "id": "msg_1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": _ANSWER, "annotations": []}],
        }
    ],
    "usage": {
        "input_tokens": 11,
        "output_tokens": 9,
        "total_tokens": 20,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    },
}

_CHAT_BODY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-test",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": _ANSWER},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 9, "total_tokens": 20},
}


def _mock_client(*, api: str, fail: bool) -> openai.AsyncOpenAI:
    """An `AsyncOpenAI` whose HTTP transport returns a canned success body (the
    answer embeds the query-derived '2 ... Sales' content) or, when *fail*, a 500
    whose error body echoes a secret — all without a network call."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(500, json={"error": {"message": _SECRET, "type": "server_error"}})
        return httpx.Response(200, json=_RESPONSES_BODY if api == "responses" else _CHAT_BODY)

    return openai.AsyncOpenAI(
        api_key="test-key-not-used",
        max_retries=0,  # a 500 must raise immediately, not retry the mock
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )


async def _emit_llm_span(*, hide_content: bool, api: str = "chat", fail: bool = False) -> list:
    """Instrument through the REAL `configure_tracing` wiring (TraceConfig +, when
    hidden, the LLMExceptionEventScrubber), drive ONE mocked OpenAI call over *api*
    (`chat`|`responses`), and return the spans captured by an in-memory exporter."""
    instrumentor = OpenAIInstrumentor()
    instrumentor.uninstrument()  # deterministic clean slate regardless of order
    exporter = InMemorySpanExporter()
    provider = tracing.configure_tracing(
        otlp_endpoint="",
        service_name="test",
        hide_llm_content=hide_content,
        span_exporter=exporter,
    )
    tracing.instrument_openai(provider, hide_content=hide_content)
    try:
        client = _mock_client(api=api, fail=fail)
        messages = [{"role": "user", "content": _QUESTION}]
        try:
            if api == "responses":
                await client.responses.create(model="gpt-test", input=messages)
            else:
                await client.chat.completions.create(model="gpt-test", messages=messages)
        except openai.APIStatusError:
            if not fail:
                raise  # only the failure-path tests expect an error
    finally:
        instrumentor.uninstrument()
    return exporter.get_finished_spans()


def _llm_span(spans: list):  # noqa: ANN001
    llm = [s for s in spans if s.attributes.get("openinference.span.kind") == "LLM"]
    assert llm, "expected one OpenInference OpenAI LLM span"
    return llm[0]


# ---------------------------------------------------------------------------
# TraceConfig helper (Layer-1, no instrumentor)
# ---------------------------------------------------------------------------


def test_trace_config_hidden_suppresses_all_content_channels() -> None:
    config = tracing.llm_content_trace_config(True)
    assert config is not None
    assert config.hide_inputs is True
    assert config.hide_outputs is True
    assert config.hide_input_messages is True
    assert config.hide_output_messages is True
    assert config.hide_prompts is True


def test_trace_config_reveal_is_none_library_default() -> None:
    # hide_content=False → None → the instrumentor's own default (content visible).
    assert tracing.llm_content_trace_config(False) is None


# ---------------------------------------------------------------------------
# Content masking — end-to-end over the real instrumentor + a mocked OpenAI call
# (both the Responses primary and the Chat fallback code paths)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api", ["responses", "chat"])
async def test_llm_span_content_hidden_by_default(api: str) -> None:
    """DEFAULT (hide_content=True): the LLM span carries NO raw prompt/completion
    — no question text, no query-derived answer — while the non-content shape
    attributes (model name, token counts) remain."""
    span = _llm_span(await _emit_llm_span(hide_content=True, api=api))
    attrs = span.attributes
    blob = str(dict(attrs))

    # "Sales" appears in BOTH the question and the answer, so its absence covers
    # both the raw question and the query-derived answer.
    assert _QUESTION not in blob, "raw question text leaked onto the LLM span"
    assert "Sales" not in blob, "query-derived content leaked onto the LLM span"
    assert _ANSWER not in blob

    for key in _REDACTED_IN_PLACE_KEYS:
        if key in attrs:
            assert attrs[key] == _REDACTED, f"{key} not redacted: {attrs[key]!r}"
    assert not any(
        k.startswith(("llm.input_messages", "llm.output_messages")) for k in attrs
    ), "per-message content channels should be dropped with hide_content=True"

    assert attrs.get("llm.model_name")
    assert attrs.get("llm.token_count.total") == 20


@pytest.mark.parametrize("api", ["responses", "chat"])
async def test_llm_span_content_visible_on_opt_in(api: str) -> None:
    """OPT-IN (hide_content=False): the span DOES carry the prompt + completion —
    the controlled-diagnostic posture the demo uses (positive control that the
    hidden assertion above is meaningful, not vacuous)."""
    span = _llm_span(await _emit_llm_span(hide_content=False, api=api))
    blob = str(dict(span.attributes))
    assert _QUESTION in blob, "opt-in reveal should carry the raw question"
    assert "Sales" in blob


# ---------------------------------------------------------------------------
# Exception-EVENT scrubbing — the residual channel TraceConfig leaves open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api", ["responses", "chat"])
async def test_llm_span_no_exception_content_when_hidden(api: str) -> None:
    """DEFAULT (hide_content=True): a FAILED OpenAI call records no `exception`
    event on the LLM span, and the response error body (a secret) appears nowhere
    on the span — TraceConfig masks attributes, LLMExceptionEventScrubber the
    events."""
    span = _llm_span(await _emit_llm_span(hide_content=True, api=api, fail=True))
    event_names = [e.name for e in span.events]
    assert "exception" not in event_names, f"exception event not scrubbed: {event_names}"
    assert span.events == (), "LLM span events should be scrubbed entirely when hidden"
    assert _SECRET not in str(dict(span.attributes)), "error-body secret leaked in attributes"


@pytest.mark.parametrize("api", ["responses", "chat"])
async def test_llm_span_exception_event_present_on_opt_in(api: str) -> None:
    """OPT-IN (hide_content=False): the failure is recorded as an `exception`
    event — positive control that the scrub above is what suppresses it, not the
    absence of an error."""
    span = _llm_span(await _emit_llm_span(hide_content=False, api=api, fail=True))
    assert any(e.name == "exception" for e in span.events), "expected a recorded exception event"


@pytest.fixture(autouse=True)
def _restore_instrumentor():
    """Leave the process-global instrumentor UNINSTRUMENTED after each test so a
    later suite module re-instruments cleanly via its own `create_app`."""
    yield
    OpenAIInstrumentor().uninstrument()
