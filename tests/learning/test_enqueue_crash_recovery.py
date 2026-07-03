"""BLOCKER regression — enqueue is XADD-first / mark-dedup-second, so a crash
between the XADD and the dedup mark degrades to a BENIGN DUPLICATE (absorbed by
the consumer's `done`+same-hash idempotency), NEVER a strand.

The data-loss BLOCKER: the OLD mark-FIRST ordering could set the dedup key,
crash before XADD, then on re-sweep `enqueue()` would SEE the key, skip the XADD,
and STILL advance the session `pending -> queued` — leaving it `queued` with ZERO
messages on the stream: never consumed, never learned = silent data loss. The
matrix couldn't reach this because the old fake's `enqueue` was atomic; the
`enqueue_without_dedup_mark()` test seam exposes exactly that crash window.

Invariant proved here: a session that is advanced to `queued` ALWAYS has (or had)
a real stream message backing it, and the transport is effectively exactly-once
(the duplicate is ACK+skipped, the no-op work runs once).
"""

from __future__ import annotations

from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.models import (
    LearningJob,
    LearningStatus,
    compute_content_hash,
)
from data_agent.learning.queue import DeliveredJob
from data_agent.learning.sweeper import LearningSweeper

from .conftest import make_message


class _WorkCountingConsumer(LearningConsumer):
    """Consumer that counts how many times the no-op work body actually runs, so
    a duplicate that is ACK+skipped (never processed) is provably not double-worked."""

    work_calls = 0

    async def _do_work(self, doc, delivered: DeliveredJob) -> None:
        # Slice-2 signature: `_do_work(doc, delivered)` (design §5.1). This override
        # keeps the Slice-1 "count the work body" behaviour for the crash-recovery
        # invariant, ignoring the loaded doc.
        self.work_calls += 1
        return None


async def test_crash_between_xadd_and_mark_is_benign_duplicate_not_strand(
    store, queue, settings, seed_session
):
    # A session that was CLAIMED (active -> pending) but not yet enqueued.
    doc = seed_session(
        store, "sess-crash", learning_status=LearningStatus.PENDING,
        messages=[make_message(0, "user", "how much overtime?")],
    )
    content_hash = compute_content_hash(doc)

    # CRASH SIMULATION: the XADD landed, but the process died before writing the
    # dedup mark. One entry is on the stream; NO dedup key exists.
    crash_id = await queue.enqueue_without_dedup_mark(
        LearningJob.from_doc(doc, content_hash=content_hash)
    )
    assert queue.stream_length() == 1
    assert content_hash not in queue._dedup  # the mark never happened

    # RE-SWEEP the still-`pending` session. `enqueue()` finds NO dedup key, so it
    # re-XADDs (a benign DUPLICATE), then advances pending -> queued.
    sweeper = LearningSweeper(store, queue, settings)
    result = await sweeper.run_once()

    assert result.enqueued == 1
    assert store._docs["sess-crash"].learning_status == LearningStatus.QUEUED
    # INVARIANT: the advanced (`queued`) session is backed by a REAL stream
    # message — never a strand. Here the crash-duplicate + the re-add = 2 entries.
    assert queue.stream_length() == 2
    resweep_id = queue._dedup[content_hash]
    assert resweep_id != crash_id  # a genuine second entry, not the skipped one

    # CONSUME: the first message drives the session to `done`; the duplicate hits
    # the `done`+same-hash idempotency -> ACK + skip (no re-processing).
    consumer = _WorkCountingConsumer(store, queue, settings)
    consume = await consumer.run_once()

    # NOT stranded: the session was actually learned.
    assert store._docs["sess-crash"].learning_status == LearningStatus.DONE
    assert consume.done == 1
    assert consume.dedup_skips == 1
    assert consumer.work_calls == 1          # the no-op work ran EXACTLY once
    # The stream is fully drained — zero PEL leak, both entries ACKed off.
    assert queue.pending_count() == 0
    assert queue.stream_length() == 0


async def test_re_enqueue_after_successful_mark_does_not_duplicate(
    store, queue, settings, seed_session
):
    """The counterpart: once the dedup mark DID land (no crash), a re-sweep of a
    still-`pending` session is deduped to the SAME id — no duplicate at all."""
    doc = seed_session(
        store, "sess-ok", learning_status=LearningStatus.PENDING,
        messages=[make_message(0, "user", "hi")],
    )
    content_hash = compute_content_hash(doc)
    first_id = await queue.enqueue(LearningJob.from_doc(doc, content_hash=content_hash))
    assert queue.stream_length() == 1
    assert content_hash in queue._dedup  # the mark landed

    sweeper = LearningSweeper(store, queue, settings)
    await sweeper.run_once()

    assert queue.stream_length() == 1              # NO duplicate
    assert queue._dedup[content_hash] == first_id  # same backing message
    assert store._docs["sess-ok"].learning_status == LearningStatus.QUEUED
