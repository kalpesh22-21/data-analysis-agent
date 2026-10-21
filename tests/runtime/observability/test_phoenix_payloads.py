"""Export-only compaction and selective masking, including real SDK instrumentation."""

import json

import pytest
from openinference.instrumentation.openai import OpenAIInstrumentor
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext

from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.phoenix_payloads import PhoenixPayloadExporter, sanitize
from tests.runtime.observability.test_llm_content_hiding import _ANSWER, _mock_client


def make_span(request, *, session="session-a", span_id=1):
    return ReadableSpan(
        "llm",
        context=SpanContext(1, span_id, False),
        attributes={
            "session.id": session,
            "openinference.span.kind": "LLM",
            "input.value": json.dumps(request),
            "llm.token_count.total": 123,
        },
    )


def test_deltas_session_isolation_and_original_unchanged():
    sink = InMemorySpanExporter()
    exporter = PhoenixPayloadExporter(sink)
    request = {
        "messages": [
            {"role": "system", "content": "Rules " * 500},
            {"role": "user", "content": "Payroll " * 500},
        ],
        "tools": [{"name": "query", "description": "SQL " * 500}],
    }
    first = make_span(request)
    original = dict(first.attributes)
    exporter.export([first, make_span(request, span_id=2)])
    a, b = sink.get_finished_spans()
    assert len(b.attributes["input.value"]) < len(a.attributes["input.value"]) / 2
    second = json.loads(b.attributes["input.value"])
    assert second["messages"] == [request["messages"][1]]
    assert "omitted" in second["history_note"]
    assert "ref" not in b.attributes["input.value"]
    assert first.attributes == original
    exporter.export([make_span(request, session="session-b", span_id=3)])
    third = json.loads(sink.get_finished_spans()[-1].attributes["input.value"])
    assert third["messages"] == [request["messages"][1]]
    assert "tools" not in third


def test_redaction_preserves_text_structure_metrics_and_masks_result_strings():
    source = {
        "query": 'SELECT "employee_code", SUM(pay) FROM payroll WHERE pay > 500',
        "question": "Show Sales payroll for 2026",
        "row_count": 2,
        "content": json.dumps(
            {
                "result_id": "r_123",
                "columns": ["name", "pay"],
                "rows": [["Alice", 500], ["Bob", 600]],
            }
        ),
        "authorization": "Bearer private",
        "service_key": "private",
    }
    result = sanitize(source, redact=True)
    assert '"employee_code"' in result["query"] and "500" not in result["query"]
    assert result["question"] == "Show Sales payroll for [number]"
    assert result["row_count"] == 2
    assert "Alice" not in result["content"] and "500" not in result["content"]
    assert json.loads(result["content"])["result_id"] == "r_123"
    assert "private" not in json.dumps(result)
    assert source["authorization"] == "Bearer private"


@pytest.mark.parametrize("redact", [False, True])
def test_compaction_and_secret_removal_always_apply(redact):
    exporter = PhoenixPayloadExporter(InMemorySpanExporter(), redact=redact)
    span = make_span(
        {
            "messages": [{"role": "tool", "content": json.dumps({"rows": [["Alice", 500]]})}],
            "api_key": "secret",
        }
    )
    result = exporter.transform(span)
    assert "Alice" not in result.attributes["input.value"]
    assert "500" not in result.attributes["input.value"]
    assert json.loads(result.attributes["input.value"])["api_key"] == "[secret]"


def test_failed_export_does_not_leave_dangling_references():
    class FailingExporter(InMemorySpanExporter):
        def export(self, spans):
            return SpanExportResult.FAILURE

    exporter = PhoenixPayloadExporter(FailingExporter())
    exporter.export([make_span({"messages": [{"role": "user", "content": "Hello"}]})])
    assert not exporter.seen


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("redact", [False, True])
async def test_real_instrumentor_exports_readable_redacted_deltas(api, redact):
    instrumentor = OpenAIInstrumentor()
    instrumentor.uninstrument()
    sink = InMemorySpanExporter()
    provider = tracing.configure_tracing(
        otlp_endpoint="",
        service_name="test",
        span_exporter=sink,
        phoenix_export=True,
        selective_redaction=redact,
    )
    tracing.instrument_openai(provider, hide_content=False)
    client = _mock_client(api=api, fail=False)
    try:
        for turn in range(2):
            with tracing.agent_span(
                tracing.get_tracer(provider),
                scope_hash="scope",
                turn_index=turn,
                session_id="session-a",
            ):
                messages = [{"role": "user", "content": "Show Sales payroll over 500"}]
                if api == "responses":
                    response = await client.responses.create(model="gpt-test", input=messages)
                    assert response.output_text == _ANSWER
                else:
                    response = await client.chat.completions.create(
                        model="gpt-test", messages=messages
                    )
                    assert response.choices[0].message.content == _ANSWER
    finally:
        await client.close()
        instrumentor.uninstrument()
    spans = [
        s for s in sink.get_finished_spans() if s.attributes.get("openinference.span.kind") == "LLM"
    ]
    assert len(spans) == 2
    first, second = spans
    assert first.attributes["session.id"] == "session-a"
    assert first.attributes["llm.input_messages.0.message.role"] == "user"
    assert first.attributes["llm.input_messages.0.message.content"] == (
        "Show Sales payroll over [number]" if redact else "Show Sales payroll over 500"
    )
    assert ("500" not in first.attributes["input.value"]) == redact
    expected = "Show Sales payroll over [number]" if redact else "Show Sales payroll over 500"
    assert expected in first.attributes["input.value"]
    assert second.attributes["llm.input_messages.0.message.content"] == (
        "Show Sales payroll over [number]" if redact else "Show Sales payroll over 500"
    )
    assert any(k.startswith("llm.output_messages.") for k in second.attributes)
    assert first.attributes["llm.token_count.total"] == 20
    assert "Sales department" in first.attributes["output.value"]


def test_native_messages_keep_new_tool_results_and_drop_repeated_history():
    sink = InMemorySpanExporter()
    exporter = PhoenixPayloadExporter(sink, redact=True)
    attrs = {
        "session.id": "native",
        "input.value": "{}",
        "llm.input_messages.0.message.role": "system",
        "llm.input_messages.0.message.content": "Detailed rules " * 100,
        "llm.input_messages.1.message.role": "user",
        "llm.input_messages.1.message.content": "Show payroll above 500",
    }
    first = ReadableSpan("llm", context=SpanContext(1, 1, False), attributes=attrs)
    second_attrs = dict(attrs)
    second_attrs.update(
        {
            "llm.input_messages.2.message.role": "tool",
            "llm.input_messages.2.message.tool_call_id": "call_abc",
            "llm.input_messages.2.message.content": json.dumps(
                {"columns": ["name", "pay"], "rows": [["Alice", 500]], "row_count": 1}
            ),
        }
    )
    second = ReadableSpan("llm", context=SpanContext(1, 2, False), attributes=second_attrs)
    exporter.export([first, second])
    result = sink.get_finished_spans()[1].attributes
    assert result["llm.input_messages.0.message.role"] == "user"
    assert result["llm.input_messages.0.message.content"] == "Show payroll above [number]"
    assert result["llm.input_messages.1.message.role"] == "tool"
    assert result["llm.input_messages.1.message.tool_call_id"] == "call_abc"
    assert "Alice" not in result["llm.input_messages.1.message.content"]
    assert result["phoenix.history.omitted_messages"] == 1
    assert not any("Detailed rules" in str(v) for v in result.values())
