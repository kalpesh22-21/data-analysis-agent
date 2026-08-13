"""The consumer's CLAIM decision: `learning_status` x delivery path -> outcome.

THE BUG THIS FILE PINS. `_process` used to attempt exactly one transition —
`queued -> processing` — and swallow `CASMismatchError` with a bare `return "skip"`:
no ack, no log, no span, no metric. Every other state (a `processing` session whose
owner crashed, a `done` session whose content changed after it was learned, a
hand-edited status string) therefore produced a message that stayed in the PEL, was
reclaimed every `min-idle`, was re-refused, and dead-lettered by attrition — with
nothing anywhere saying what state had been refused. Live repro: a claimed message,
an idle event loop and zero log lines for 17+ minutes.

Covered here, per the table in `consumer._claim_decision`:

  queued      + any        -> forward   -> done
  processing  + reclaimed  -> recover   -> done      (the crashed owner's work continues)
  processing  + fresh      -> refuse    -> skip      (a peer may be live)
  done(!=hash)+ any        -> recover   -> done      (content changed after processing)
  done(==hash)+ any        -> dedup_skip            (unchanged, see test_consumer.py)
  active      + any        -> refuse    -> skip
  pending     + any        -> refuse    -> skip
  dead_letter + any        -> terminal  -> ack_terminal
  <unknown>   + any        -> terminal  -> ack_terminal

plus: EVERY skip logs the state it refused, and every skip/terminal emits a
countable `learning.consume` span carrying that state.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.consumer import LearningConsumer, _claim_decision
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.runtime.session.store import CASMismatchError

from .conftest import make_message


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


def consume_spans(span_exporter):
    return [s for s in span_exporter.get_finished_spans() if s.name == "learning.consume"]


async def deliver_fresh(store, queue, session_id: str, *, content_hash: str | None = None):
    """XADD + first ('>') delivery of a job for *session_id*, leaving it in the PEL
    exactly as `consume` would. `delivery_count == 1`, `reclaimed is False`."""
    doc = store._docs[session_id]
    job = LearningJob.from_doc(
        doc, content_hash=content_hash or compute_content_hash(doc)
    )
    await queue.enqueue(job)
    return job


# --- The decision table, as a table ------------------------------------------


@pytest.mark.parametrize(
    "status,reclaimed,expected",
    [
        (LearningStatus.QUEUED, False, ("forward", "queued")),
        (LearningStatus.QUEUED, True, ("forward", "queued")),
        (LearningStatus.PROCESSING, True, ("recover", "reclaimed_processing")),
        (LearningStatus.PROCESSING, False, ("refuse", "processing_owner_may_be_live")),
        (LearningStatus.DONE, False, ("recover", "done_content_changed")),
        (LearningStatus.DONE, True, ("recover", "done_content_changed")),
        (LearningStatus.ACTIVE, False, ("refuse", "not_yet_claimable")),
        (LearningStatus.PENDING, False, ("refuse", "not_yet_claimable")),
        (LearningStatus.PENDING, True, ("refuse", "not_yet_claimable")),
        (LearningStatus.DEAD_LETTER, False, ("terminal", "dead_letter")),
        (LearningStatus.DEAD_LETTER, True, ("terminal", "dead_letter")),
        ("banana", False, ("terminal", "unknown_status")),
        ("", True, ("terminal", "unknown_status")),
    ],
)
def test_claim_decision_table(status, reclaimed, expected):
    assert _claim_decision(status, reclaimed=reclaimed) == expected


# --- processing: the delivery path decides -----------------------------------


async def test_processing_reclaimed_delivery_is_recovered(
    store, queue, settings, clock, seed_session, span_exporter, tracer
):
    """A crashed owner left the session `processing`; XAUTOCLAIM re-assigns the
    message to us and that IS the recovery path — we re-enter `processing` and
    drive it to `done`, ACKing the message."""
    seed_session(store, "sess-1", learning_status=LearningStatus.PROCESSING,
                 messages=[make_message(0, "user", "hi")])
    await deliver_fresh(store, queue, "sess-1")
    await queue.consume(count=10, block_ms=0)  # the crashed owner's delivery (dc=1)

    consumer = LearningConsumer(store, queue, settings, tracer=tracer)
    clock.advance(10)  # past min-idle -> reclaimable
    result = await consumer.run_once()

    assert result.done == 1
    assert result.skipped == 0
    assert store._docs["sess-1"].learning_status == LearningStatus.DONE
    assert queue.pending_count() == 0
    span = consume_spans(span_exporter)[-1]
    assert span.attributes["learning.outcome"] == "done"
    # The state we claimed FROM is on the span — a recovery is distinguishable
    # from an ordinary `queued` claim without reading logs.
    assert span.attributes["learning.session_status"] == LearningStatus.PROCESSING
    assert span.attributes["learning.reclaimed"] is True


async def test_processing_fresh_delivery_is_skipped_not_acked(
    store, queue, settings, seed_session, span_exporter, tracer, caplog
):
    """A FIRST delivery of a `processing` session means a peer picked the message up
    moments ago and is working: refuse, do NOT ack, and say so. (The reclaim path
    above is the one that recovers it, once min-idle has elapsed.)"""
    seed_session(store, "sess-1", learning_status=LearningStatus.PROCESSING,
                 messages=[make_message(0, "user", "hi")])
    await deliver_fresh(store, queue, "sess-1")

    consumer = LearningConsumer(store, queue, settings, tracer=tracer)
    with caplog.at_level(logging.INFO, logger="data_agent.learning.consumer"):
        result = await consumer.run_once()

    assert result.done == 0
    assert result.skipped == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.PROCESSING
    assert queue.pending_count() == 1  # left for the owner / a later reclaim
    span = consume_spans(span_exporter)[-1]
    assert span.attributes["learning.outcome"] == "skip"
    assert span.attributes["learning.session_status"] == LearningStatus.PROCESSING
    assert span.attributes["learning.skip_reason"] == "processing_owner_may_be_live"
    assert span.attributes["learning.reclaimed"] is False
    assert "processing" in caplog.text and "sess-1" in caplog.text


async def test_processing_reentry_loses_to_a_live_peer(
    store, queue, settings, clock, seed_session, span_exporter, tracer, caplog
):
    """The `processing -> processing` re-entry is safe against a STILL-LIVE owner
    because of the CAS, not because of the state: min-idle proves the PEL entry was
    idle, NOT that the owner died (Redis resets idle time on delivery, not on the
    owner's progress). A peer writing between our read and our transition bumps the
    CAS, our re-entry loses, and the delivery takes the ordinary skip path."""
    seed_session(store, "sess-1", learning_status=LearningStatus.PROCESSING,
                 messages=[make_message(0, "user", "hi")])
    await deliver_fresh(store, queue, "sess-1")
    await queue.consume(count=10, block_ms=0)

    real_get = store.get_session_with_cas

    async def racing_get(session_id: str):
        doc, cas = await real_get(session_id)
        # The live owner writes RIGHT AFTER our read: the token we hold is stale.
        store._bump_version(session_id)
        return doc, cas

    store.get_session_with_cas = racing_get

    consumer = LearningConsumer(store, queue, settings, tracer=tracer)
    clock.advance(10)
    with caplog.at_level(logging.INFO, logger="data_agent.learning.consumer"):
        result = await consumer.run_once()

    assert result.done == 0
    assert result.skipped == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.PROCESSING
    assert queue.pending_count() == 1  # never acked — the owner keeps its message
    span = consume_spans(span_exporter)[-1]
    assert span.attributes["learning.outcome"] == "skip"
    assert span.attributes["learning.skip_reason"] == "cas_lost"
    assert span.attributes["learning.session_status"] == LearningStatus.PROCESSING
    assert "cas_lost" in caplog.text


# --- done: the hash decides ---------------------------------------------------


async def test_done_with_a_different_hash_is_reprocessed(
    store, queue, settings, seed_session, span_exporter, tracer
):
    """THE LIVE SIGNATURE. The consumer records a FRESHLY computed hash at `done`
    (MEDIUM-3), so a session that gained turns between enqueue and consume ends up
    `done` with a hash the in-flight message never carried. A redelivery of that
    message matched neither the dedup check (hashes differ) nor the `queued` gate,
    and used to skip in silence forever. It now re-enters `processing` and is
    re-learned against the CURRENT transcript."""
    doc = seed_session(store, "sess-1", learning_status=LearningStatus.DONE,
                       learning_content_hash="hash-recorded-at-done",
                       messages=[make_message(0, "user", "how much overtime?")])
    stale_hash = compute_content_hash(doc)
    assert stale_hash != "hash-recorded-at-done"
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=stale_hash))

    consumer = LearningConsumer(store, queue, settings, tracer=tracer)
    result = await consumer.run_once()

    assert result.done == 1
    assert result.dedup_skips == 0
    after = store._docs["sess-1"]
    assert after.learning_status == LearningStatus.DONE
    # Re-learned against the CURRENT content, and the message is off the PEL.
    assert after.learning_content_hash == compute_content_hash(after)
    assert queue.pending_count() == 0
    span = consume_spans(span_exporter)[-1]
    assert span.attributes["learning.outcome"] == "done"
    assert span.attributes["learning.session_status"] == LearningStatus.DONE


async def test_done_with_the_same_hash_is_still_dedup_skip(
    store, queue, settings, seed_session, span_exporter, tracer
):
    """UNCHANGED by the recovery edge: the idempotency check runs FIRST, so an
    ordinary at-least-once redelivery is still an ACK + `dedup_skip` and never
    re-enters `processing`."""
    doc = seed_session(store, "sess-1", learning_status=LearningStatus.DONE,
                       messages=[make_message(0, "user", "hi")])
    content_hash = compute_content_hash(doc)
    store._docs["sess-1"].learning_content_hash = content_hash
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=content_hash))
    version_before = store._versions["sess-1"]

    consumer = LearningConsumer(store, queue, settings, tracer=tracer)
    result = await consumer.run_once()

    assert result.dedup_skips == 1
    assert result.done == 0
    assert store._versions["sess-1"] == version_before  # no transition at all
    assert queue.pending_count() == 0
    assert consume_spans(span_exporter)[-1].attributes["learning.outcome"] == "dedup_skip"


# --- refused states: skip, WITH the state, and never an ack -------------------


@pytest.mark.parametrize("status", [LearningStatus.ACTIVE, LearningStatus.PENDING])
async def test_sweeper_owned_states_are_skipped_without_ack(
    store, queue, settings, seed_session, span_exporter, tracer, caplog, status
):
    """`active`/`pending` are states the SWEEPER still owns: a later sweep drives
    them to `queued`, at which point a reclaim of this very message processes it.
    Acking here would drop work the sweeper is about to make claimable."""
    seed_session(store, "sess-1", learning_status=status,
                 messages=[make_message(0, "user", "hi")])
    await deliver_fresh(store, queue, "sess-1")

    consumer = LearningConsumer(store, queue, settings, tracer=tracer)
    with caplog.at_level(logging.INFO, logger="data_agent.learning.consumer"):
        result = await consumer.run_once()

    assert result.skipped == 1
    assert store._docs["sess-1"].learning_status == status
    assert queue.pending_count() == 1  # NOT acked
    span = consume_spans(span_exporter)[-1]
    assert span.attributes["learning.outcome"] == "skip"
    assert span.attributes["learning.session_status"] == status
    assert span.attributes["learning.skip_reason"] == "not_yet_claimable"
    # THE fact that used to be discarded: the state the claim was refused FROM.
    assert status in caplog.text


# --- terminal states: ack, loudly --------------------------------------------


@pytest.mark.parametrize(
    "status,reason",
    [
        (LearningStatus.DEAD_LETTER, "dead_letter"),
        ("in_progres", "unknown_status"),  # a typo'd/hand-edited status
    ],
)
async def test_terminal_states_are_acked_and_logged(
    store, queue, settings, seed_session, span_exporter, tracer, caplog, status, reason
):
    """Neither state can ever become `queued` again, so redelivering the message
    only burns delivery counts until it dead-letters by attrition — the current
    silent failure. ACK it instead, with a WARNING and a countable span."""
    seed_session(store, "sess-1", learning_status=status,
                 messages=[make_message(0, "user", "hi")])
    await deliver_fresh(store, queue, "sess-1")

    consumer = LearningConsumer(store, queue, settings, tracer=tracer)
    with caplog.at_level(logging.INFO, logger="data_agent.learning.consumer"):
        result = await consumer.run_once()

    assert result.skipped == 1  # `ack_terminal` tallies as a skip
    assert result.done == 0
    assert store._docs["sess-1"].learning_status == status  # never rewritten
    assert queue.pending_count() == 0  # ACKed off the PEL — no attrition burn
    span = consume_spans(span_exporter)[-1]
    assert span.attributes["learning.outcome"] == "ack_terminal"
    assert span.attributes["learning.session_status"] == status
    assert span.attributes["learning.skip_reason"] == reason
    assert status in caplog.text
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


# --- the queued happy path is untouched --------------------------------------


async def test_queued_delivery_still_forwards(store, queue, settings, seed_session):
    seed_session(store, "sess-1", learning_status=LearningStatus.QUEUED,
                 messages=[make_message(0, "user", "hi")])
    await deliver_fresh(store, queue, "sess-1")

    result = await LearningConsumer(store, queue, settings).run_once()

    assert result.done == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.DONE
    assert queue.pending_count() == 0


async def test_queued_delivery_that_loses_the_cas_race_is_logged(
    store, queue, settings, seed_session, span_exporter, tracer, caplog
):
    """The ORIGINAL silent path: a peer consumer claimed the same `queued` session
    first. Still a skip, still un-ACKed — but no longer invisible."""
    seed_session(store, "sess-1", learning_status=LearningStatus.QUEUED,
                 messages=[make_message(0, "user", "hi")])
    await deliver_fresh(store, queue, "sess-1")

    real_get = store.get_session_with_cas

    async def racing_get(session_id: str):
        doc, cas = await real_get(session_id)
        store._bump_version(session_id)  # a peer wins the claim
        return doc, cas

    store.get_session_with_cas = racing_get

    consumer = LearningConsumer(store, queue, settings, tracer=tracer)
    with caplog.at_level(logging.INFO, logger="data_agent.learning.consumer"):
        result = await consumer.run_once()

    assert result.skipped == 1
    assert queue.pending_count() == 1
    span = consume_spans(span_exporter)[-1]
    assert span.attributes["learning.outcome"] == "skip"
    assert span.attributes["learning.skip_reason"] == "cas_lost"
    assert span.attributes["learning.session_status"] == LearningStatus.QUEUED
    assert "sess-1" in caplog.text


async def test_processing_to_done_cas_loss_is_logged(
    store, queue, settings, seed_session, span_exporter, tracer, caplog
):
    """The other end of the same window: the work ran, but the closing
    `processing -> done` CAS lost. The message stays in the PEL (correct — the
    `done`+same-hash dedup ACKs it on redelivery) and the skip now says why."""
    seed_session(store, "sess-1", learning_status=LearningStatus.QUEUED,
                 messages=[make_message(0, "user", "hi")])
    await deliver_fresh(store, queue, "sess-1")

    real_transition = store.transition_learning_status

    async def failing_done(session_id, expected_from, to, cas, **kw):
        if to == LearningStatus.DONE:
            raise CASMismatchError("peer advanced the session")
        return await real_transition(session_id, expected_from, to, cas, **kw)

    store.transition_learning_status = failing_done

    consumer = LearningConsumer(store, queue, settings, tracer=tracer)
    with caplog.at_level(logging.INFO, logger="data_agent.learning.consumer"):
        result = await consumer.run_once()

    assert result.done == 0
    assert result.skipped == 1
    assert queue.pending_count() == 1
    span = consume_spans(span_exporter)[-1]
    assert span.attributes["learning.outcome"] == "skip"
    assert span.attributes["learning.skip_reason"] == "cas_lost_at_done"
    assert "cas_lost_at_done" in caplog.text
