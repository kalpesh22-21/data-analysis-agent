"""Layer-1 tests: retrieval spans/progress carry shape only — question NEVER
logged (D25/D61, design §3.5)."""

from __future__ import annotations

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.reranker_client import FakeRerankerClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.progress import to_progress_event
from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_Q = "what is jane doe's overtime pay"  # deliberately PII-flavoured
_QVEC = [1.0, 0.0]


def _provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _index() -> FakeVectorIndex:
    return FakeVectorIndex(
        [
            (
                Candidate(
                    id="bp-a",
                    kind="blueprint",
                    text="overtime rollup",
                    uses=frozenset({"w.t.c"}),
                    payload={"intent": "overtime rollup", "slots_summary": "dept"},
                ),
                [1.0, 0.0],
            ),
            (
                Candidate(id="kn-a", kind="knowledge", text="OT is 1.5x", uses=None, payload={}),
                [1.0, 0.0],
            ),
        ]
    )


async def test_recall_and_rerank_spans_have_shape_only_no_question() -> None:
    provider, exporter = _provider()
    events: list[tuple[str, dict]] = []
    pipeline = RetrievalPipeline(
        embedding_client=FakeEmbeddingClient({_Q: _QVEC}),
        reranker=FakeRerankerClient({"overtime rollup": 0.5, "OT is 1.5x": 0.5}),
        vector_index=_index(),
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
        reranker_model="ms-marco",
        observer=lambda e, p: events.append((e, p)),
        tracer=tracing.get_tracer(provider),
    )
    await pipeline.retrieve(question=_Q, column_scope=frozenset(), user_id=None)

    spans = exporter.get_finished_spans()
    kinds = {s.name for s in spans}
    assert "retrieval.recall" in kinds
    assert "rerank" in kinds

    for span in spans:
        # The question must not appear anywhere in any attribute value.
        for value in span.attributes.values():
            assert _Q not in str(value)

    recall_spans = [s for s in spans if s.name == "retrieval.recall"]
    corpora = {s.attributes["retrieval.corpus"] for s in recall_spans}
    assert corpora == {"blueprint", "knowledge"}
    assert all(
        s.attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND]
        == OpenInferenceSpanKindValues.CHAIN.value
        for s in recall_spans
    )

    rerank_spans = [s for s in spans if s.name == "rerank"]
    assert all(s.attributes["reranker.reranked"] is True for s in rerank_spans)
    assert all(
        s.attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND]
        == OpenInferenceSpanKindValues.RERANKER.value
        for s in rerank_spans
    )


_UNSET = object()


def _collecting_pipeline(
    events: list[tuple[str, dict]],
    *,
    embedding_client=_UNSET,  # noqa: ANN001 - sentinel so explicit None means "no embedder"
    tracer=None,  # noqa: ANN001
) -> RetrievalPipeline:
    return RetrievalPipeline(
        embedding_client=(
            FakeEmbeddingClient({_Q: _QVEC}) if embedding_client is _UNSET else embedding_client
        ),
        reranker=FakeRerankerClient(),
        vector_index=_index(),
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
        observer=lambda e, p: events.append((e, p)),
        tracer=tracer,
    )


async def test_progress_events_are_shape_only_start_then_counts() -> None:
    events: list[tuple[str, dict]] = []
    await _collecting_pipeline(events).retrieve(
        question=_Q, column_scope=frozenset(), user_id=None
    )

    # L1: a start signal fires BEFORE the completion counts event.
    names = [e for e, _ in events]
    assert names.index("retrieval_start") < names.index("retrieval")

    start_payload = next(p for e, p in events if e == "retrieval_start")
    assert start_payload == {}  # pure start signal, no values

    payload = next(p for e, p in events if e == "retrieval")
    # Counts ONLY — the question is never in the payload (D25).
    assert set(payload) == {"blueprints", "knowledge"}
    assert _Q not in str(payload)

    # The public progress translation renders shape-only, question-free steps.
    assert to_progress_event("retrieval_start", start_payload).step == (  # type: ignore[union-attr]
        "searching for a matching blueprint…"
    )
    progress = to_progress_event("retrieval", payload)
    assert progress is not None
    assert set(progress.shape) == {"blueprints", "knowledge"}
    assert _Q not in str(progress.shape)


async def test_embedder_degrade_is_not_silent() -> None:
    # H1: with tracer + observer wired, a no-embedder / EmbeddingError degrade
    # must still emit a shape-only degrade span AND the retrieval progress events
    # (start + a {0, 0} completion) — never a silent early return.
    provider, exporter = _provider()
    for embedder in (None, FakeEmbeddingClient(fail=True)):
        exporter.clear()
        events: list[tuple[str, dict]] = []
        await _collecting_pipeline(
            events, embedding_client=embedder, tracer=tracing.get_tracer(provider)
        ).retrieve(question=_Q, column_scope=frozenset(), user_id=None)

        # Progress: start fired, and a {0, 0} completion event (H1).
        assert ("retrieval_start", {}) in events
        assert ("retrieval", {"blueprints": 0, "knowledge": 0}) in events

        # Span: a dedicated shape-only degrade span, question-free.
        degrade_spans = [s for s in exporter.get_finished_spans() if s.name == "retrieval.degraded"]
        assert len(degrade_spans) == 1
        assert "retrieval.degraded" in degrade_spans[0].attributes
        for value in degrade_spans[0].attributes.values():
            assert _Q not in str(value)
