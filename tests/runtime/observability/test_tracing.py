"""Unit tests for observability/tracing.py (Layer 1 — SimpleSpanProcessor + InMemorySpanExporter,
no live OTLP/Phoenix collector required)."""

from __future__ import annotations

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.observability import tracing


def _provider_with_memory_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_configure_tracing_with_no_endpoint_is_a_no_op_provider() -> None:
    provider = tracing.configure_tracing(otlp_endpoint="", service_name="data-agent-runtime")
    tracer = tracing.get_tracer(provider)
    # Must not raise and must not attempt any network call.
    with tracing.agent_span(tracer, scope_hash="deadbeef", turn_index=0):
        pass


def test_agent_span_sets_openinference_kind_and_attributes() -> None:
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)

    with tracing.agent_span(tracer, scope_hash="deadbeef", turn_index=2):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs[SpanAttributes.OPENINFERENCE_SPAN_KIND] == OpenInferenceSpanKindValues.AGENT.value
    assert attrs["scope_hash"] == "deadbeef"
    assert attrs["turn.index"] == 2


def test_tool_span_masks_are_caller_responsibility_but_shape_is_recorded() -> None:
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)

    with tracing.tool_span(
        tracer,
        tool_name="runQuery",
        args={"sql": "SELECT * FROM t WHERE x = ''"},
        status="ok",
        error_code=None,
    ):
        pass

    spans = exporter.get_finished_spans()
    attrs = spans[0].attributes
    assert attrs[SpanAttributes.OPENINFERENCE_SPAN_KIND] == OpenInferenceSpanKindValues.TOOL.value
    assert attrs["tool.name"] == "runQuery"
    assert attrs["tool.status"] == "ok"
    assert "tool.error_code" not in attrs


def test_tool_span_records_error_code_when_denied() -> None:
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)

    with tracing.tool_span(
        tracer, tool_name="runQuery", args={}, status="denied", error_code="COLUMN_SCOPE_VIOLATION"
    ):
        pass

    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["tool.error_code"] == "COLUMN_SCOPE_VIOLATION"


def test_guardrail_and_chain_spans_use_correct_kinds() -> None:
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)

    with tracing.chain_span(tracer, "context.assembly", attributes={"dropped_by_scope_count": 3}):
        pass
    with tracing.guardrail_span(tracer, "budget.check", attributes={"window": 1}):
        pass

    spans = exporter.get_finished_spans()
    kinds = {s.name: s.attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND] for s in spans}
    assert kinds["context.assembly"] == OpenInferenceSpanKindValues.CHAIN.value
    assert kinds["budget.check"] == OpenInferenceSpanKindValues.GUARDRAIL.value


def test_instrument_openai_is_idempotent() -> None:
    provider = tracing.configure_tracing(otlp_endpoint="", service_name="data-agent-runtime")
    tracing.instrument_openai(provider)
    tracing.instrument_openai(provider)  # must not raise on a second call
