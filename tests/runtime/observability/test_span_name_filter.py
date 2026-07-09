"""Span-name export filter (design §7 noise reduction).

Drives a representative set of runtime spans through the REAL `configure_tracing`
wiring + an `InMemorySpanExporter` and asserts the `_NameFilteringSpanExporter`
exports only the KEEP spans and drops the STRIP (plumbing) spans — and that the
filter is fully tunable (an empty denylist exports everything, so existing
span-assertion tests and other consumers are unaffected).

Layer 1: real OTel SDK + InMemorySpanExporter, no Phoenix collector.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.tracing import DEFAULT_DROP_SPAN_NAMES

# The spans a reviewer wants to keep on a runtime turn (task KEEP list). `Response`
# (the auto-instrumented OpenAI LLM span) is emitted by the OpenAI SDK, not by our
# helpers, so it is exercised elsewhere; here we cover every MANUAL span plus the
# `tool.<name>` dispatch span and the retained guard span.
KEEP_NAMES = frozenset(
    {
        "agent.turn",
        "tool.runQuery",
        "embedding",
        "rerank",
        "retrieval.recall",
        "loop_repeated_idempotent_read_guarded",
    }
)
STRIP_NAMES = frozenset({"context.assembly", "loop_model_call_start", "loop_turn_done"})


def _tracer_with_filter(
    drop_span_names: frozenset[str],
) -> tuple[tracing.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = tracing.configure_tracing(
        otlp_endpoint="",
        service_name="data-agent-runtime",
        span_exporter=exporter,
        drop_span_names=drop_span_names,
    )
    return tracing.get_tracer(provider), exporter


def _emit_representative_turn(tracer: tracing.Tracer) -> None:
    """Emit the span shape of a real turn: `agent.turn` wraps `context.assembly`,
    which in turn wraps the retrieval spans (embedding/rerank/retrieval.recall) —
    mirroring context/assembly.py running retrieval INSIDE the assembly span — plus
    a sibling `tool.<name>` dispatch span and the `loop_*` observer spans."""
    observe = tracing.guardrail_observer(tracer)
    with tracing.agent_span(tracer, scope_hash="deadbeef", turn_index=0):
        observe("loop_model_call_start", {"window": 1})
        with tracing.chain_span(tracer, "context.assembly", attributes={"scope_hash": "deadbeef"}):
            with tracing.embedding_span(tracer, model="all-mpnet-base-v2", input_count=4):
                pass
            with tracing.recall_span(
                tracer,
                corpus="blueprint",
                recall_k=30,
                candidate_count=12,
                dropped_by_scope_count=0,
            ):
                pass
            with tracing.rerank_span(tracer, model="ms-marco", document_count=12, reranked=True):
                pass
        with tracing.tool_span(
            tracer, tool_name="runQuery", args={"sql": "SELECT 1"}, status="ok", error_code=None
        ):
            pass
        observe(
            "loop_repeated_idempotent_read_guarded",
            {"tool_name": "runQuery", "deduped": True},
        )
        observe("loop_turn_done", {"tool_calls_made": 1})


def _names(spans: tuple[ReadableSpan, ...]) -> set[str]:
    return {span.name for span in spans}


def test_default_filter_keeps_meaningful_spans_and_drops_plumbing() -> None:
    tracer, exporter = _tracer_with_filter(DEFAULT_DROP_SPAN_NAMES)
    _emit_representative_turn(tracer)

    exported = _names(exporter.get_finished_spans())
    # Every KEEP span survived...
    assert KEEP_NAMES <= exported, f"missing KEEP spans: {KEEP_NAMES - exported}"
    # ...and every STRIP span was dropped.
    assert not (STRIP_NAMES & exported), f"leaked STRIP spans: {STRIP_NAMES & exported}"


def test_empty_denylist_exports_everything() -> None:
    # The tunable escape hatch: an empty drop-set disables the filter entirely, so
    # existing span-assertion tests / other consumers keep seeing every span.
    tracer, exporter = _tracer_with_filter(frozenset())
    _emit_representative_turn(tracer)

    exported = _names(exporter.get_finished_spans())
    assert KEEP_NAMES <= exported
    assert STRIP_NAMES <= exported, f"filter dropped spans while disabled: {STRIP_NAMES - exported}"


def test_denylist_is_arbitrary_and_name_based() -> None:
    # A custom denylist drops exactly the named spans and nothing else — proving the
    # policy is a plain, tunable name set (e.g. an operator adding a status span).
    tracer, exporter = _tracer_with_filter(frozenset({"tool.runQuery", "rerank"}))
    _emit_representative_turn(tracer)

    exported = _names(exporter.get_finished_spans())
    assert "tool.runQuery" not in exported
    assert "rerank" not in exported
    # Untargeted spans (including the default plumbing) are untouched.
    assert {"agent.turn", "context.assembly", "embedding", "loop_turn_done"} <= exported


def test_dropping_mid_tree_span_does_not_drop_or_reparent_its_children() -> None:
    """`context.assembly` is a MID-TREE span: `embedding` runs INSIDE it. Dropping
    it by name must NOT drop `embedding`; the child keeps its original
    `parent_span_id` (pointing at the dropped span) so a trace UI re-roots it under
    the trace — flattened, never severed."""
    tracer, exporter = _tracer_with_filter(DEFAULT_DROP_SPAN_NAMES)

    with tracing.agent_span(tracer, scope_hash="deadbeef", turn_index=0):
        with tracing.chain_span(tracer, "context.assembly", attributes={}) as assembly_span:
            assembly_span_id = assembly_span.get_span_context().span_id
            with tracing.embedding_span(tracer, model="m", input_count=1):
                pass

    spans = exporter.get_finished_spans()
    by_name = {span.name: span for span in spans}
    assert "context.assembly" not in by_name  # the plumbing parent was dropped
    assert "embedding" in by_name  # the child survived
    # The child still points at the (now-absent) assembly span — Phoenix renders
    # such an orphan under the trace root rather than discarding it.
    assert by_name["embedding"].parent is not None
    assert by_name["embedding"].parent.span_id == assembly_span_id


def test_filtering_exporter_forwards_flush_and_export_result() -> None:
    # The decorator is a faithful SpanExporter: an all-dropped export still reports
    # SUCCESS without a downstream call, and force_flush delegates to the inner one.
    from opentelemetry.sdk.trace.export import SpanExportResult

    # A captured span object to feed the decorator directly (name matches the drop).
    captured = InMemorySpanExporter()
    provider = tracing.configure_tracing(otlp_endpoint="", service_name="t", span_exporter=captured)
    with tracing.chain_span(tracing.get_tracer(provider), "drop.me"):
        pass
    (dropped_span,) = captured.get_finished_spans()

    inner = InMemorySpanExporter()
    filtering = tracing._NameFilteringSpanExporter(inner, frozenset({"drop.me"}))
    assert filtering.export([dropped_span]) is SpanExportResult.SUCCESS
    assert inner.get_finished_spans() == ()  # nothing forwarded (all dropped)
    assert filtering.force_flush() is True
