"""D30-dead-letter-after-n (design §4, matrix row 7, task item 9).

A job redelivered/reclaimed more than N=5 times lands on the dead-letter path and
its session CAS-transitions to `dead_letter`; the work stream is unblocked (the
poison entry is XACKed off), and the dead-letter delivery is NOT re-processed.
"""

from __future__ import annotations

from data_agent.learning import state_machine
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.learning.queue import DeliveredJob

from .conftest import make_message

# --- Queue-level: reclaim accumulates deliveries, dead-letters past N --------


async def test_queue_dead_letters_after_n_reclaims(store, queue, clock, seed_session):
    doc = seed_session(store, "poison", messages=[make_message(0, "user", "hi")])
    job = LearningJob.from_doc(doc, content_hash=compute_content_hash(doc))
    await queue.enqueue(job)

    delivered = await queue.consume(count=10, block_ms=0)
    assert delivered[0].delivery_count == 1  # first delivery
    assert queue.pending_count() == 1

    last = None
    for _ in range(5):  # 2,3,4,5,6 -> the 6th delivery crosses N=5
        clock.advance(10)
        reclaimed = await queue.reclaim_stale(min_idle_ms=1000, max_deliveries=5)
        assert len(reclaimed) == 1
        last = reclaimed[0]

    assert last.dead_lettered is True
    assert last.delivery_count == 6
    # New contract (MEDIUM-3 reorder): reclaim REPORTS the poison as dead_lettered
    # but leaves it in the PEL — the queue-side move (dead stream + XACK) is done
    # by `finalize_dead_letter`, which the consumer calls only AFTER CAS-marking
    # the session `dead_letter` (no irreversible XACK before the state CAS).
    assert queue.pending_count() == 1
    assert queue.stream_length() == 1
    assert queue.dead_letters() == []
    await queue.finalize_dead_letter(last)
    assert queue.pending_count() == 0          # off the PEL
    assert queue.stream_length() == 0          # off the work stream (unblocked)
    assert len(queue.dead_letters()) == 1
    assert queue.dead_letters()[0].session_id == "poison"


async def test_queue_does_not_dead_letter_before_n(store, queue, clock, seed_session):
    doc = seed_session(store, "sess")
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))
    await queue.consume(count=10, block_ms=0)
    for _ in range(3):  # deliveries 2,3,4 — all <= N=5
        clock.advance(10)
        reclaimed = await queue.reclaim_stale(min_idle_ms=1000, max_deliveries=5)
        assert reclaimed[0].dead_lettered is False
    assert queue.dead_letters() == []
    assert queue.pending_count() == 1


# --- End-to-end poison through the consumer ---------------------------------


async def test_wedged_session_is_dead_lettered_end_to_end(store, queue, settings, clock, seed_session):
    """A session wedged in `processing` (a prior consumer crashed mid-work) fails
    every `queued -> processing` assert on redelivery, so the job never ACKs and
    keeps accumulating deliveries until the queue dead-letters it and the consumer
    CAS-marks the session `dead_letter` (transition #5)."""
    doc = seed_session(store, "wedged", learning_status=LearningStatus.PROCESSING,
                       messages=[make_message(0, "user", "hi")])
    job = LearningJob.from_doc(doc, content_hash=compute_content_hash(doc))
    await queue.enqueue(job)
    # Model a crashed consumer: the message is delivered (dc=1) but never acked.
    await queue.consume(count=10, block_ms=0)

    consumer = LearningConsumer(store, queue, settings)
    outcome = None
    for _ in range(10):
        clock.advance(10)
        outcome = await consumer.run_once()
        if store._docs["wedged"].learning_status == LearningStatus.DEAD_LETTER:
            break

    assert store._docs["wedged"].learning_status == LearningStatus.DEAD_LETTER
    assert outcome.dead_letters == 1
    assert len(queue.dead_letters()) == 1
    assert queue.pending_count() == 0
    assert queue.stream_length() == 0


# --- Consumer handling of an already-dead-lettered delivery -----------------


class _DeadLetterOnceQueue:
    """A queue stub whose `reclaim_stale` reports exactly one over-threshold job
    (`dead_lettered=True`), still in the PEL (NOT yet XACKed — new MEDIUM-3
    contract), then nothing. Records `finalize_dead_letter`/`ack` calls so tests
    can assert the ORDERING: the session-state CAS happens before any queue-side
    finalize. `consume` is always empty."""

    def __init__(self, job: LearningJob):
        self._job = job
        self._served = False
        self.finalized: list[str] = []
        self.acked: list[str] = []

    async def reclaim_stale(self, *, min_idle_ms, max_deliveries):
        if self._served:
            return []
        self._served = True
        return [DeliveredJob(message_id="9-0", job=self._job, delivery_count=6, dead_lettered=True)]

    async def consume(self, *, count, block_ms):
        return []

    async def ack(self, message_id):
        self.acked.append(message_id)

    async def finalize_dead_letter(self, delivered):
        self.finalized.append(delivered.message_id)


async def test_consumer_cas_marks_session_dead_letter(store, settings, seed_session):
    doc = seed_session(store, "sess-1", learning_status=LearningStatus.QUEUED,
                       messages=[make_message(0, "user", "hi")])
    job = LearningJob.from_doc(doc, content_hash=compute_content_hash(doc))
    queue = _DeadLetterOnceQueue(job)
    consumer = LearningConsumer(store, queue, settings)

    result = await consumer.run_once()

    assert result.dead_letters == 1
    assert result.done == 0
    assert store._docs["sess-1"].learning_status == LearningStatus.DEAD_LETTER
    # Ordering (MEDIUM-3): session CAS'd terminal, THEN the queue-side move —
    # never a bare ack for a non-terminal dead-letter.
    assert queue.finalized == ["9-0"]
    assert queue.acked == []


async def test_consumer_skips_dead_letter_for_already_done_session(store, settings, seed_session):
    """A dead-letter delivery for a session that ALREADY reached `done` is a plain
    ACK + skip (terminal states are not force-transitioned, and NOT re-dead-
    lettered — MEDIUM-4 kills that noise)."""
    seed_session(store, "sess-1", learning_status=LearningStatus.DONE,
                 learning_content_hash="h", messages=[make_message(0, "user", "hi")])
    job = LearningJob(session_id="sess-1", couchbase_doc_id="session::sess-1", content_hash="h")
    queue = _DeadLetterOnceQueue(job)
    consumer = LearningConsumer(store, queue, settings)

    result = await consumer.run_once()

    assert result.dead_letters == 0
    assert result.skipped == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.DONE
    # Terminal → plain ack off the stream, no re-dead-letter.
    assert queue.acked == ["9-0"]
    assert queue.finalized == []


async def test_consumer_does_not_redeadletter_already_dead_letter_session(store, settings, seed_session):
    """MEDIUM-4: a poison reclaim of a session that is ALREADY `dead_letter` (a
    crash between a prior CAS and its finalize, or a duplicate poison delivery) is
    a plain ACK + skip — it is NOT dead-lettered a SECOND time (no spurious
    dead-stream noise, no redundant CAS)."""
    seed_session(store, "sess-1", learning_status=LearningStatus.DEAD_LETTER,
                 messages=[make_message(0, "user", "hi")])
    job = LearningJob(session_id="sess-1", couchbase_doc_id="session::sess-1", content_hash="h")
    queue = _DeadLetterOnceQueue(job)
    consumer = LearningConsumer(store, queue, settings)

    result = await consumer.run_once()

    assert result.dead_letters == 0
    assert result.skipped == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.DEAD_LETTER
    assert queue.acked == ["9-0"]  # plain ack off the PEL
    assert queue.finalized == []   # NO second dead-letter move


async def test_crash_between_cas_and_finalize_leaves_message_reclaimable(store, queue, settings, clock, seed_session):
    """MEDIUM-3 ordering crash-safety: the consumer CAS-marks the session
    `dead_letter` BEFORE `finalize_dead_letter`. If the process crashes in that
    window, the message is STILL in the PEL (finalize never ran) and is therefore
    reclaimable; the next reclaim sees a terminal session and plain-ACKs it
    (`ack_terminal`) — never a lost message, never a double dead-letter."""
    doc = seed_session(store, "sess-1", learning_status=LearningStatus.PROCESSING,
                       messages=[make_message(0, "user", "hi")])
    job = LearningJob.from_doc(doc, content_hash=compute_content_hash(doc))
    await queue.enqueue(job)
    await queue.consume(count=10, block_ms=0)  # dc=1, in the PEL

    # Simulate the CRASH: the session was CAS'd to `dead_letter` but the process
    # died before `finalize_dead_letter`, so the message is still pending.
    _, cas = await store.get_session_with_cas("sess-1")
    await state_machine.transition(
        store, "sess-1", LearningStatus.PROCESSING, LearningStatus.DEAD_LETTER,
        cas, assert_from=False,
    )
    assert queue.pending_count() == 1  # message survived the crash — reclaimable

    # RECOVERY: a later consumer cycle reclaims the stuck entry; the session is
    # already terminal, so it is plain-ACKed, NOT dead-lettered a second time.
    consumer = LearningConsumer(store, queue, settings)
    for _ in range(10):
        clock.advance(10)
        result = await consumer.run_once()
        if queue.pending_count() == 0:
            break

    assert queue.pending_count() == 0        # recovered off the PEL
    assert queue.dead_letters() == []        # never a second dead-letter move
    assert result.dead_letters == 0
    assert store._docs["sess-1"].learning_status == LearningStatus.DEAD_LETTER
