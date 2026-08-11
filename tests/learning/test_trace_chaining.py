"""Trace chaining + the D25 verbose gate (Track-B learning-loop observability).

Two guarantees, proven over the REAL sweeper/consumer/scheduler with an in-memory
span exporter (no Phoenix, no infra):

  * `traces-chained-one-trace-per-session` — a session's learning journey reads as
    ONE trace: the sweeper's `learning.enqueue` is the per-session ROOT; its W3C
    `traceparent` rides on the `LearningJob` and, extracted at consume, makes
    `learning.consume` a CHILD of enqueue with `triage`/`extract` nested UNDER
    consume — all sharing ONE traceId. The same `traceparent`, carried on the
    `CandidateEnvelope`, makes the scheduler's `promote`/`land` continue the trace.

  * `traces-verbose-off-is-d25-shape-only` — with `learning_trace_verbose` OFF
    (now the opt-out — verbose is the default since the 2026-07-15 D25 amendment), NO
    human-readable attribute (question, transcript, SQL, intent) is ever set on any
    learning span (the D25 shape-only posture is preserved); ON (the default), they
    appear (the entity-bearing diagnostic posture).
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus, build_envelope
from data_agent.learning.config import LearningSettings
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.models import LearningJob, LearningStatus
from data_agent.learning.observability import (
    consume_span,
    context_from_traceparent,
    inject_current_traceparent,
    land_span,
)
from data_agent.learning.promotion import PromotionScheduler
from data_agent.learning.promotion.landing import landing_id
from data_agent.learning.summary.models import TurnSummary
from data_agent.learning.sweeper import LearningSweeper

from .conftest import make_message, make_trail_entry
from .extractor.helpers import (
    KEEP_VERDICT,
    PAYROLL_SQL,
    blueprint_raw,
    emit_extractor,
    make_summary,
    make_tool_call,
)
from .promotion.helpers import (
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
)

_KEY = "sha256:single-bp"


# --- shared span-exporter fixtures -------------------------------------------


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    exporter._provider = provider  # keep the provider alive for the test
    return exporter


@pytest.fixture
def tracer(span_exporter):
    return span_exporter._provider.get_tracer("learning-loop-test")


def _by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


def _keep_loader(summary):
    async def loader(doc, store, *, job):
        return summary

    return loader


def _keep_consumer(store, queue, settings, *, tracer, verbose: bool):
    """A consumer wired for the KEEP extraction path (scripted extractor, in-memory
    audit + candidate stores) with a fixed summary carrying a question + SQL."""
    summary = replace(
        make_summary(
            session_id="sess-chain",
            content_hash="hash-chain",
            tool_calls=(make_tool_call(sql=PAYROLL_SQL, status="ok"),),
        ),
        turns=(
            TurnSummary(
                turn_index=0,
                user_nl="total earnings for a department",
                assistant_text="147000",
                tool_call_refs=("tc1",),
            ),
        ),
    )
    return LearningConsumer(
        store,
        queue,
        settings.model_copy(update={"learning_trace_verbose": verbose}),
        tracer=tracer,
        summary_loader=_keep_loader(summary),
        triage=lambda _s: KEEP_VERDICT,
        audit=InMemoryAuditStore(),
        candidates=InMemoryCandidateStore(),
        extractor=emit_extractor([blueprint_raw()]),
        stages=(),
    )


# --- traces-chained-one-trace-per-session ------------------------------------


async def test_enqueue_to_extract_share_one_trace(
    store, queue, settings, seed_session, span_exporter, tracer
):
    """[traces-chained-one-trace-per-session] The sweeper's enqueue span is the
    per-session ROOT; consume is its child (via the propagated traceparent) and
    triage + extract nest UNDER consume — one traceId, correct parentage."""
    seed_session(
        store,
        "sess-chain",
        messages=[
            make_message(0, "user", "total earnings for a department"),
            make_message(0, "assistant", "147000"),
        ],
        tool_trail=[make_trail_entry(tool_name="runQuery", args={"sql": PAYROLL_SQL})],
    )

    # Sweep (enqueue span = the session trace root; injects the traceparent).
    sweeper = LearningSweeper(store, queue, settings, tracer=tracer)
    await sweeper.run_once()
    assert store._docs["sess-chain"].learning_status == LearningStatus.QUEUED

    # The job carried a W3C traceparent forward on the wire. Re-decode from the
    # wire fields to prove the round-trip (not just the in-memory object).
    (job,) = queue._entries.values()
    wire = LearningJob.from_fields(job.to_fields())
    assert wire.traceparent is not None and wire.traceparent.startswith("00-")

    # Consume (extracts the traceparent → consume nests under enqueue).
    consumer = _keep_consumer(store, queue, settings, tracer=tracer, verbose=False)
    result = await consumer.run_once()
    assert result.done == 1

    spans = _by_name(span_exporter)
    for name in ("learning.enqueue", "learning.consume", "learning.triage", "learning.extract"):
        assert name in spans, f"missing {name}"

    enqueue = spans["learning.enqueue"]
    consume = spans["learning.consume"]
    triage = spans["learning.triage"]
    extract = spans["learning.extract"]

    # ONE trace end-to-end.
    trace_ids = {s.context.trace_id for s in (enqueue, consume, triage, extract)}
    assert len(trace_ids) == 1, f"expected one trace, got {trace_ids}"

    # enqueue is the ROOT; consume is ITS child; triage + extract are consume's children.
    assert enqueue.parent is None
    assert consume.parent is not None and consume.parent.span_id == enqueue.context.span_id
    assert triage.parent is not None and triage.parent.span_id == consume.context.span_id
    assert extract.parent is not None and extract.parent.span_id == consume.context.span_id
    assert extract.attributes["learning.extract.outcome"] == "extracted"


def _traced_scheduler(store, *, tracer, trace_verbose: bool):
    return PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({_KEY: 5}),
        policy=promotion_policy(),
        landing_writer=FakeLandingWriter(),
        require_landing=True,
        clock=lambda: "2026-07-05T00:00:00+00:00",
        tracer=tracer,
        trace_verbose=trace_verbose,
    )


async def test_candidate_carries_traceparent_and_scheduler_continues_trace(span_exporter, tracer):
    """[traces-chained-one-trace-per-session] The extracting consume span's
    traceparent is stamped on the CandidateEnvelope (round-trips through to_doc)
    and the scheduler's promote/land spans CONTINUE that same trace.

    Driven through the HUMAN-APPROVE edge since plan §4: that is the edge that lands, so
    it is the only one that emits a `learning.land` span. The cron's own edge is covered
    by `test_the_route_edge_continues_the_session_trace_too` below — both matter, because
    the whole point of the traceparent is that a session's learning story stays in ONE
    trace no matter which edge advances it."""
    store = InMemoryCandidateStore()

    # Capture a traceparent from a root span, as the consumer does at extraction.
    with tracer.start_as_current_span("consume-root"):
        traceparent = inject_current_traceparent()
    assert traceparent is not None
    parent_trace_id = context_from_traceparent(traceparent)  # sanity: extractable

    env = replace(
        make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=_KEY),
        traceparent=traceparent,
    )
    # Round-trips through the persisted doc.
    assert CandidateEnvelope.from_doc(env.to_doc()).traceparent == traceparent
    await store.put(env)

    sched = _traced_scheduler(store, tracer=tracer, trace_verbose=False)

    decision = await sched.apply_human_decision(env, "approve")
    assert decision.action == "approve"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED

    spans = _by_name(span_exporter)
    assert "learning.promote" in spans and "learning.land" in spans
    root_trace_id = spans["consume-root"].context.trace_id
    assert spans["learning.promote"].context.trace_id == root_trace_id
    assert spans["learning.land"].context.trace_id == root_trace_id
    # land is nested under promote.
    assert spans["learning.land"].parent.span_id == spans["learning.promote"].context.span_id
    assert parent_trace_id is not None  # extraction produced a usable parent context


async def test_the_route_edge_continues_the_session_trace_too(span_exporter, tracer):
    """[traces-chained-one-trace-per-session] The plan-§4 cron edge (`candidate →
    in_review`) is the transition that now happens to EVERY candidate, so if it did not
    continue the session's trace, the trace would end at extraction for almost everything.
    It emits a `learning.promote` span with `action=route` and NO land span (it lands
    nothing)."""
    store = InMemoryCandidateStore()
    with tracer.start_as_current_span("consume-root"):
        traceparent = inject_current_traceparent()
    env = replace(
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=_KEY),
        traceparent=traceparent,
    )
    await store.put(env)
    sched = _traced_scheduler(store, tracer=tracer, trace_verbose=False)

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    spans = _by_name(span_exporter)
    assert spans["learning.promote"].attributes["learning.promote.action"] == "route"
    assert spans["learning.promote"].context.trace_id == spans["consume-root"].context.trace_id
    assert "learning.land" not in spans


async def test_job_and_envelope_traceparent_round_trip(span_exporter, tracer):
    """[traces-chained-one-trace-per-session] The traceparent survives BOTH wire
    hops: LearningJob.to_fields/from_fields and CandidateEnvelope.to_doc/from_doc."""
    with tracer.start_as_current_span("root"):
        tp = inject_current_traceparent()

    job = LearningJob(
        session_id="s", couchbase_doc_id="session::s", content_hash="h", traceparent=tp
    )
    assert LearningJob.from_fields(job.to_fields()).traceparent == tp

    summary = make_summary(session_id="s", content_hash="h")
    result = emit_extractor([blueprint_raw()])
    extracted = await result.extract(summary, KEEP_VERDICT)
    env = build_envelope(
        extracted.candidates[0], summary, candidate_id="candidate::s::0",
        evidence_refs=("ref-1",), traceparent=tp,
    )
    assert env.traceparent == tp
    assert CandidateEnvelope.from_doc(env.to_doc()).traceparent == tp


# --- traces-verbose-off-is-d25-shape-only ------------------------------------

# Every human-readable (entity-bearing) attribute the verbose gate can add.
_VERBOSE_ATTRS = {
    "learning.question",
    "learning.transcript_preview",
    "learning.accepted_sql",
    "learning.extract.intent",
    "learning.extract.slots",
    "learning.extract.rationale",
    "learning.extract.decline_details",
    "learning.blueprint_id",
    "learning.blueprint.intent",
    "learning.canonical_key",
    # The judge's own gated attrs (2026-08-10). Listed HERE and not only in
    # `judge/test_judge_span_verbose.py` because this is the suite that walks EVERY span
    # of a real session with the gate shut — a judge span leaking under a global opt-out
    # is a different failure from the judge span's own gate being wrong.
    "learning.judge.reason",
    "learning.judge.covered_by",
    "learning.judge.prior_art",
}


async def test_verbose_off_sets_no_human_readable_attrs(
    store, queue, settings, seed_session, span_exporter, tracer
):
    """[traces-verbose-off-is-d25-shape-only] With verbose OFF (now the opt-out) NO
    learning span carries any entity-bearing attribute — the D25 shape-only posture
    holds."""
    seed_session(
        store, "sess-chain",
        messages=[make_message(0, "user", "secret question about Jane Doe"),
                  make_message(0, "assistant", "answer")],
        tool_trail=[make_trail_entry(tool_name="runQuery", args={"sql": PAYROLL_SQL})],
    )
    sweeper = LearningSweeper(store, queue, settings, tracer=tracer)
    await sweeper.run_once()
    consumer = _keep_consumer(store, queue, settings, tracer=tracer, verbose=False)
    await consumer.run_once()

    for span in span_exporter.get_finished_spans():
        leaked = set(span.attributes) & _VERBOSE_ATTRS
        assert not leaked, f"{span.name} leaked verbose attrs {leaked} with verbose OFF"


async def test_verbose_on_sets_human_readable_attrs(
    store, queue, settings, seed_session, span_exporter, tracer
):
    """[traces-verbose-off-is-d25-shape-only] (positive control) With verbose ON the
    triage/consume/extract spans DO carry the question + accepted SQL + learned intent."""
    seed_session(
        store, "sess-chain",
        messages=[make_message(0, "user", "total earnings for a department"),
                  make_message(0, "assistant", "147000")],
        tool_trail=[make_trail_entry(tool_name="runQuery", args={"sql": PAYROLL_SQL})],
    )
    sweeper = LearningSweeper(store, queue, settings, tracer=tracer)
    await sweeper.run_once()
    consumer = _keep_consumer(store, queue, settings, tracer=tracer, verbose=True)
    await consumer.run_once()

    spans = _by_name(span_exporter)
    triage = spans["learning.triage"]
    extract = spans["learning.extract"]
    assert triage.attributes["learning.question"]  # the user's question is present
    assert extract.attributes["learning.accepted_sql"] == PAYROLL_SQL
    assert extract.attributes["learning.extract.intent"]
    # The consume span (parent) also carries the question under verbose.
    assert spans["learning.consume"].attributes["learning.question"]


async def test_verbose_off_scheduler_promote_land_shape_only(span_exporter, tracer):
    """[traces-verbose-off-is-d25-shape-only] The scheduler's promote/land spans — the
    thinnest gate — carry NO entity-bearing attribute with verbose OFF (the
    consumer-only guard above never exercises these span types). Driven through the
    human-approve edge, the only one that emits a land span since plan §4."""
    store = InMemoryCandidateStore()
    with tracer.start_as_current_span("consume-root"):
        traceparent = inject_current_traceparent()
    env = replace(
        make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=_KEY),
        traceparent=traceparent,
    )
    await store.put(env)
    sched = _traced_scheduler(store, tracer=tracer, trace_verbose=False)
    await sched.apply_human_decision(env, "approve")

    spans = _by_name(span_exporter)
    for name in ("learning.promote", "learning.land"):
        leaked = set(spans[name].attributes) & _VERBOSE_ATTRS
        assert not leaked, f"{name} leaked verbose attrs {leaked} with verbose OFF"


async def test_verbose_on_scheduler_promote_land_carry_blueprint_attrs(span_exporter, tracer):
    """[traces-verbose-off-is-d25-shape-only] (positive control) With verbose ON the
    promote/land spans DO carry the blueprint id + intent + canonical_key."""
    store = InMemoryCandidateStore()
    with tracer.start_as_current_span("consume-root"):
        traceparent = inject_current_traceparent()
    env = replace(
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=_KEY),
        traceparent=traceparent,
    )
    await store.put(env)
    sched = _traced_scheduler(store, tracer=tracer, trace_verbose=True)
    await sched.run_once()

    promote = _by_name(span_exporter)["learning.promote"]
    assert promote.attributes["learning.blueprint_id"] == landing_id(env)
    assert promote.attributes["learning.canonical_key"] == _KEY
    assert promote.attributes["learning.blueprint.intent"]


def test_learning_span_records_no_exception_event_on_raise(span_exporter, tracer):
    """[traces-verbose-off-is-d25-shape-only] BLOCKER guard: an exception raised INSIDE
    a learning span (which now wraps real work — the LLM extractor / landing writer)
    must NOT attach an `exception` event (message/stacktrace can echo an OpenAI response
    body or the entity-bearing `forbidden_spans`) EVEN with verbose OFF. The ERROR
    status is still set, so failures stay visible in traces."""
    secret = "openai response body: SSN-424-11-9090 for Jane Doe"
    with pytest.raises(RuntimeError):  # noqa: PT012 - the raise is the point
        with consume_span(tracer, session_id="s", outcome="done", delivery_count=1):
            raise RuntimeError(secret)
    # And the same for a land span (its errors reference forbidden_spans).
    with pytest.raises(RuntimeError):  # noqa: PT012
        with land_span(tracer, session_id="s", candidate_id="c"):
            raise RuntimeError(secret)

    for sp in span_exporter.get_finished_spans():
        assert all(e.name != "exception" for e in sp.events), (
            f"{sp.name} recorded an exception event (D25 leak surface)"
        )
        # No attribute anywhere echoes the secret payload.
        assert all(secret not in str(v) for v in sp.attributes.values())
        # The failure is STILL visible: the span status is ERROR.
        assert sp.status.status_code == StatusCode.ERROR


def test_verbose_flag_defaults_on():
    """The D25 gate now defaults ON — verbose by default (amended 2026-07-15). The
    learning-loop/learning-sessions Phoenix projects are entity-bearing by default;
    set LEARNING_TRACE_VERBOSE=false to restore the shape-only telemetry posture."""
    assert LearningSettings(_env_file=None).learning_trace_verbose is True
