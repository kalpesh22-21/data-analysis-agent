"""Consumer happy-path + content-hash idempotency (design §5, matrix rows 3 & 10,
task items 5-6).
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.learning.sweeper import LearningSweeper

from .conftest import make_message, make_trail_entry


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


async def test_consumer_happy_path(store, queue, settings, seed_session, span_exporter, tracer):
    """queued -> processing -> done, message fully ACKed (zero PEL leak), the
    no-op work ran, and a `learning.consume` trace event with outcome=done is
    emitted."""
    seed_session(
        store, "sess-1",
        messages=[make_message(0, "user", "hi"), make_message(0, "assistant", "hello")],
        tool_trail=[make_trail_entry()],
    )
    sweeper = LearningSweeper(store, queue, settings)
    await sweeper.run_once()
    assert store._docs["sess-1"].learning_status == LearningStatus.QUEUED

    consumer = LearningConsumer(store, queue, settings, tracer=tracer)
    result = await consumer.run_once()

    assert result.done == 1
    assert result.dedup_skips == 0
    assert result.skipped == 0
    # done + recorded hash:
    doc = store._docs["sess-1"]
    assert doc.learning_status == LearningStatus.DONE
    assert doc.learning_content_hash == compute_content_hash(doc)
    # zero PEL leak / stream drained:
    assert queue.pending_count() == 0
    assert queue.stream_length() == 0
    # trace event emitted with the right outcome:
    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    assert "learning.consume" in spans
    assert spans["learning.consume"].attributes["learning.outcome"] == "done"
    assert spans["learning.consume"].attributes["session.id"] == "sess-1"


async def test_consumer_redelivery_of_done_job_is_dedup_skip(store, queue, settings, seed_session):
    """A redelivery of an already-`done` session with the SAME content_hash is
    ACKed + skipped — no re-processing, no state change, no error (D96 §5.2)."""
    doc = seed_session(store, "sess-2", messages=[make_message(0, "user", "hi")])
    content_hash = compute_content_hash(doc)

    # First pass: enqueue + consume to `done`.
    sweeper = LearningSweeper(store, queue, settings)
    await sweeper.run_once()
    consumer = LearningConsumer(store, queue, settings)
    first = await consumer.run_once()
    assert first.done == 1
    assert store._docs["sess-2"].learning_status == LearningStatus.DONE
    version_after_done = store._versions["sess-2"]

    # Simulate an at-least-once REDELIVERY of the same job (stream already drained,
    # so we re-inject the identical envelope directly).
    job = LearningJob.from_doc(doc, content_hash=content_hash)
    msg_id = await queue.enqueue(job)
    # `enqueue` deduped to the original id but the stream is empty; force a fresh
    # live entry to model Redis re-delivering the same message id post-crash.
    queue._entries[msg_id] = job
    queue._new.append(msg_id)

    second = await consumer.run_once()

    assert second.dedup_skips == 1
    assert second.done == 0
    assert second.skipped == 0
    # No re-processing: status unchanged, doc version NOT bumped by a transition.
    assert store._docs["sess-2"].learning_status == LearningStatus.DONE
    assert store._versions["sess-2"] == version_after_done
    # The redelivered message was ACKed off the PEL (no leak).
    assert queue.pending_count() == 0


async def test_consumer_no_work_when_stream_empty(store, queue, settings):
    consumer = LearningConsumer(store, queue, settings)
    result = await consumer.run_once()
    assert result.done == 0
    assert result.dedup_skips == 0
    assert result.dead_letters == 0
    assert result.skipped == 0
    assert result.disabled is False


async def test_consumer_records_fresh_hash_not_stale_message_hash(store, queue, settings, seed_session):
    """MEDIUM-4: if the session's content CHANGES between enqueue and consume (a
    new turn arrives while it sat `queued`), the consumer must record a FRESHLY
    computed `content_hash` at `done` — computed from the doc it actually loaded —
    NOT the now-stale hash carried on the message. Otherwise the Slice-2 dedup
    would key off content that no longer matches the session."""
    doc = seed_session(store, "sess-1", learning_status=LearningStatus.QUEUED,
                       messages=[make_message(0, "user", "how much overtime?")])
    stale_hash = compute_content_hash(doc)  # the hash at sweep/enqueue time
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=stale_hash))

    # The session gains a NEW turn while queued (content changed) — the message's
    # hash is now stale w.r.t. the live doc.
    store._docs["sess-1"].messages.append(make_message(1, "user", "and for marketing?"))
    store._bump_version("sess-1")
    fresh_hash = compute_content_hash(store._docs["sess-1"])
    assert fresh_hash != stale_hash  # precondition: content really did change

    consumer = LearningConsumer(store, queue, settings)
    result = await consumer.run_once()

    assert result.done == 1
    after = store._docs["sess-1"]
    assert after.learning_status == LearningStatus.DONE
    # The recorded hash reflects the ACTUAL loaded content, not the message's.
    assert after.learning_content_hash == fresh_hash
    assert after.learning_content_hash != stale_hash
