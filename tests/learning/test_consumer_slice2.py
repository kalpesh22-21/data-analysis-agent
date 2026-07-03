"""S2 consumer integration (Layer 1) — load → triage → skip/keep → done, the
zero-snapshot invariant, kill-switch precedence, idempotency, D72 read-only, and
the no-false-done-on-loader-exception guarantee (matrix rows C1–C5, C7 + A5).
"""

from __future__ import annotations

import copy

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.runtime.session.models import ResultPreview

from .conftest import make_message, make_trail_entry


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    exporter._provider = provider
    return exporter


@pytest.fixture
def tracer(span_exporter):
    return span_exporter._provider.get_tracer("learning-loop-test")


def _span_names(exporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def _span_by_name(exporter, name):
    return next(s for s in exporter.get_finished_spans() if s.name == name)


async def _enqueue_queued(store, queue, seed_session, sid, *, messages, tool_trail=()):
    doc = seed_session(store, sid, learning_status=LearningStatus.QUEUED,
                       messages=messages, tool_trail=list(tool_trail))
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))
    return doc


# --- C1: load → triage → SKIP -----------------------------------------------


async def test_skip_path_marks_done_and_traces_skip(store, queue, settings, seed_session,
                                                     span_exporter, tracer):
    audit = InMemoryAuditStore()
    # A chat-only session → triage skip_no_tool_calls.
    await _enqueue_queued(store, queue, seed_session, "sess-skip",
                          messages=[make_message(0, "user", "hi"),
                                    make_message(0, "assistant", "hello!")])
    consumer = LearningConsumer(store, queue, settings, tracer=tracer, audit=audit)

    result = await consumer.run_once()

    assert result.done == 1
    assert store._docs["sess-skip"].learning_status == LearningStatus.DONE
    names = _span_names(span_exporter)
    triage_span = _span_by_name(span_exporter, "learning.triage")
    assert triage_span.attributes["learning.triage.decision"] == "skip"
    assert triage_span.attributes["learning.triage.reason"] == "skip_no_tool_calls"
    # Nothing enqueued downstream: no extract span on a skip.
    assert "learning.extract" not in names
    # INVARIANT (A5 / row 6): S2 writes NO evidence.
    assert audit.snapshot_calls == 0


# --- C2: load → triage → KEEP → stub extractor ------------------------------


async def test_keep_path_hits_stub_and_traces_would_extract(store, queue, settings, seed_session,
                                                             span_exporter, tracer):
    audit = InMemoryAuditStore()
    # An accepted successful query → K1 keep.
    await _enqueue_queued(store, queue, seed_session, "sess-keep",
                          messages=[make_message(0, "user", "show overtime"),
                                    make_message(0, "assistant", "Sales paid $12k.")],
                          tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                                       args={"sql": "SELECT 1"}, status="ok")])
    consumer = LearningConsumer(store, queue, settings, tracer=tracer, audit=audit)

    result = await consumer.run_once()

    assert result.done == 1
    assert store._docs["sess-keep"].learning_status == LearningStatus.DONE
    triage_span = _span_by_name(span_exporter, "learning.triage")
    assert triage_span.attributes["learning.triage.decision"] == "keep"
    assert triage_span.attributes["learning.triage.reason"] == "K1"
    extract_span = _span_by_name(span_exporter, "learning.extract")
    assert extract_span.attributes["learning.extract.outcome"] == "would_extract"
    # INVARIANT (A5 / row 6): the stub writes NO evidence even on keep.
    assert audit.snapshot_calls == 0


# --- Row 6 / A5: S2 writes NO evidence on EITHER path ------------------------


async def test_zero_snapshot_across_skip_and_keep(store, queue, settings, seed_session):
    audit = InMemoryAuditStore()
    await _enqueue_queued(store, queue, seed_session, "sess-skip",
                          messages=[make_message(0, "user", "hi")])
    await _enqueue_queued(store, queue, seed_session, "sess-keep",
                          messages=[make_message(0, "user", "q"),
                                    make_message(0, "assistant", "a")],
                          tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                                       args={"sql": "SELECT 1"}, status="ok")])
    consumer = LearningConsumer(store, queue, settings, audit=audit)

    result = await consumer.run_once()

    assert result.done == 2
    # The audit client is injected but DORMANT in S2 (§4.3): zero snapshot calls.
    assert audit.snapshot_calls == 0


# --- C3: kill-switch halts BEFORE any loader/triage work --------------------


async def test_kill_switch_halts_before_loader(store, queue, settings, seed_session, monkeypatch):
    calls = {"n": 0}

    async def spy_loader(doc, store_arg, *, job):  # pragma: no cover - must NOT run
        calls["n"] += 1
        raise AssertionError("loader must not run while disabled")

    await _enqueue_queued(store, queue, seed_session, "sess-1",
                          messages=[make_message(0, "user", "q"),
                                    make_message(0, "assistant", "a")],
                          tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                                       args={"sql": "SELECT 1"}, status="ok")])
    monkeypatch.setenv("LEARNING_ENABLED", "false")
    consumer = LearningConsumer(store, queue, settings, summary_loader=spy_loader)

    result = await consumer.run_once()

    assert result.disabled is True
    assert calls["n"] == 0                                  # loader never invoked
    assert store._docs["sess-1"].learning_status == LearningStatus.QUEUED
    assert queue.new_count() == 1                           # work waits in the stream


# --- C4: idempotency unchanged — loader NOT run on a dedup re-delivery -------


async def test_dedup_redelivery_does_not_run_loader(store, queue, settings, seed_session):
    calls = {"n": 0}
    real_from = compute_content_hash

    doc = seed_session(store, "sess-1", learning_status=LearningStatus.DONE,
                       learning_content_hash=None,
                       messages=[make_message(0, "user", "q"), make_message(0, "assistant", "a")],
                       tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                                    args={"sql": "SELECT 1"}, status="ok")])
    # The session is already `done` with the recorded hash equal to the message's.
    done_hash = real_from(doc)
    store._docs["sess-1"].learning_content_hash = done_hash
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=done_hash))

    async def spy_loader(d, s, *, job):  # pragma: no cover - must NOT run on dedup
        calls["n"] += 1
        raise AssertionError("loader must not run on a dedup re-delivery")

    consumer = LearningConsumer(store, queue, settings, summary_loader=spy_loader)
    result = await consumer.run_once()

    assert result.dedup_skips == 1
    assert result.done == 0
    assert calls["n"] == 0                    # idempotency short-circuits BEFORE load
    assert queue.pending_count() == 0         # the re-delivery was ACKed


# --- C5: D72 end-to-end — only the lifecycle flag changes -------------------


async def test_full_consume_mutates_only_lifecycle_flag(store, queue, settings, seed_session):
    preview = ResultPreview(columns=["ot"], row_count=1, truncated=False, preview_rows=[[12000]])
    doc = seed_session(
        store, "sess-1", learning_status=LearningStatus.QUEUED,
        last_activity="2000-01-01T00:00:00+00:00",
        messages=[make_message(0, "user", "show overtime"),
                  make_message(0, "assistant", "Sales paid $12k."),
                  make_message(1, "user", "perfect")],
        tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                     args={"sql": "SELECT 1"}, status="ok",
                                     result_preview=preview, result_full_ref="result::abc")],
    )
    before_messages = copy.deepcopy(doc.messages)
    before_trail = copy.deepcopy(doc.tool_trail)
    before_last_activity = doc.last_activity
    expected_hash = compute_content_hash(doc)
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=expected_hash))

    consumer = LearningConsumer(store, queue, settings, audit=InMemoryAuditStore())
    await consumer.run_once()

    after = store._docs["sess-1"]
    assert after.learning_status == LearningStatus.DONE
    assert after.learning_content_hash == expected_hash
    assert after.messages == before_messages
    assert after.tool_trail == before_trail
    assert after.last_activity == before_last_activity  # NOT bumped


# --- C7: a loader/triage exception → NO false done --------------------------


async def test_loader_exception_does_not_mark_done(store, queue, settings, seed_session):
    async def boom_loader(doc, s, *, job):
        raise RuntimeError("loader blew up (e.g. transient store read)")

    await _enqueue_queued(store, queue, seed_session, "sess-1",
                          messages=[make_message(0, "user", "q"),
                                    make_message(0, "assistant", "a")],
                          tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                                       args={"sql": "SELECT 1"}, status="ok")])
    consumer = LearningConsumer(store, queue, settings, summary_loader=boom_loader)

    result = await consumer.run_once()

    # No FALSE done: the exception left the session mid-processing, un-acked.
    assert result.done == 0
    assert result.skipped == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.PROCESSING
    assert queue.pending_count() == 1        # message stays in the PEL for reclaim


async def test_persistent_loader_exception_dead_letters_after_n(store, queue, settings,
                                                                clock, seed_session):
    """C7 arc: a persistently-failing loader never ACKs; the message is reclaimed,
    the wedged `processing` session fails every re-`queued→processing` assert, and
    after N deliveries it dead-letters (D96 preserved) — never a false done."""
    async def boom_loader(doc, s, *, job):
        raise RuntimeError("loader keeps failing")

    await _enqueue_queued(store, queue, seed_session, "sess-1",
                          messages=[make_message(0, "user", "q"),
                                    make_message(0, "assistant", "a")],
                          tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                                       args={"sql": "SELECT 1"}, status="ok")])
    consumer = LearningConsumer(store, queue, settings, summary_loader=boom_loader)

    outcome = None
    for _ in range(10):
        outcome = await consumer.run_once()
        clock.advance(10)
        if store._docs["sess-1"].learning_status == LearningStatus.DEAD_LETTER:
            break

    assert store._docs["sess-1"].learning_status == LearningStatus.DEAD_LETTER
    assert store._docs["sess-1"].learning_status != LearningStatus.DONE
    assert outcome.dead_letters == 1
    assert queue.pending_count() == 0
