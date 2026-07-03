"""D30-sweeper-idle-detection + two-step-claim crash recovery + no-double-enqueue
(design §3/§6, matrix rows 6 & 3, task items 2-4).
"""

from __future__ import annotations

import pytest

from data_agent.learning.models import (
    LearningJob,
    LearningStatus,
    compute_content_hash,
)
from data_agent.learning.sweeper import LearningSweeper

from .conftest import make_message


@pytest.fixture
def sweeper(store, queue, settings) -> LearningSweeper:
    return LearningSweeper(store, queue, settings)


# --- Row 6: sweeper picks ONLY idle active/pending sessions -----------------


async def test_idle_active_session_is_claimed_and_enqueued(store, queue, sweeper, seed_session):
    seed_session(store, "idle-1", messages=[make_message(0, "user", "hi")])
    result = await sweeper.run_once()

    assert result.scanned == 1
    assert result.claimed == 1
    assert result.enqueued == 1
    assert store._docs["idle-1"].learning_status == LearningStatus.QUEUED
    assert queue.stream_length() == 1


async def test_fresh_recent_session_is_untouched(store, queue, sweeper, seed_session):
    from datetime import UTC, datetime

    fresh_ts = datetime.now(UTC).isoformat()
    seed_session(store, "fresh-1", last_activity=fresh_ts)
    result = await sweeper.run_once()

    assert result.scanned == 0
    assert result.enqueued == 0
    assert store._docs["fresh-1"].learning_status == LearningStatus.ACTIVE
    assert queue.stream_length() == 0


@pytest.mark.parametrize("terminal", [LearningStatus.DONE, LearningStatus.PROCESSING,
                                       LearningStatus.QUEUED, LearningStatus.DEAD_LETTER])
async def test_non_sweepable_status_is_not_reswept(store, queue, sweeper, seed_session, terminal):
    # Even though it is idle, a session already handed to a consumer (or terminal)
    # is NOT in SWEEPABLE_STATUSES, so it is never re-claimed/re-enqueued.
    seed_session(store, "busy-1", learning_status=terminal)
    result = await sweeper.run_once()

    assert result.scanned == 0
    assert result.enqueued == 0
    assert store._docs["busy-1"].learning_status == terminal
    assert queue.stream_length() == 0


async def test_only_idle_ones_among_mixed_are_swept(store, queue, sweeper, seed_session):
    from datetime import UTC, datetime

    seed_session(store, "idle-a")                       # idle active   -> swept
    seed_session(store, "idle-b", learning_status=LearningStatus.PENDING)  # idle pending -> swept
    seed_session(store, "fresh", last_activity=datetime.now(UTC).isoformat())  # recent -> skip
    seed_session(store, "done", learning_status=LearningStatus.DONE)      # terminal -> skip

    result = await sweeper.run_once()

    assert result.scanned == 2
    assert result.enqueued == 2
    assert queue.stream_length() == 2
    assert store._docs["idle-a"].learning_status == LearningStatus.QUEUED
    assert store._docs["idle-b"].learning_status == LearningStatus.QUEUED
    assert store._docs["fresh"].learning_status == LearningStatus.ACTIVE
    assert store._docs["done"].learning_status == LearningStatus.DONE


# --- Row 3: two-step claim + crash recovery ---------------------------------


async def test_pending_with_no_stream_entry_is_recovered(store, queue, sweeper, seed_session):
    """A sweeper crash AFTER `active -> pending` but BEFORE XADD leaves a
    `pending` doc with NO stream entry. The next sweep re-detects it (pending is
    sweepable), skips the claim (already pending), and enqueues -> queued."""
    seed_session(store, "crashed-1", learning_status=LearningStatus.PENDING,
                 messages=[make_message(0, "user", "hi")])
    assert queue.stream_length() == 0

    result = await sweeper.run_once()

    assert result.scanned == 1
    assert result.claimed == 0  # already pending: no active->pending claim
    assert result.enqueued == 1
    assert queue.stream_length() == 1
    assert store._docs["crashed-1"].learning_status == LearningStatus.QUEUED


async def test_crash_after_xadd_does_not_double_enqueue(store, queue, sweeper, seed_session):
    """A sweeper crash AFTER XADD but BEFORE the `pending -> queued` CAS: the
    stream ALREADY holds the entry. The re-sweep re-enqueues, but the content-hash
    dedup key makes XADD idempotent, so no second entry is added."""
    doc = seed_session(store, "crashed-2", learning_status=LearningStatus.PENDING,
                       messages=[make_message(0, "user", "hi")])
    content_hash = compute_content_hash(doc)
    # Simulate the XADD that happened before the crash.
    first_id = await queue.enqueue(LearningJob.from_doc(doc, content_hash=content_hash))
    assert queue.stream_length() == 1

    result = await sweeper.run_once()

    assert result.enqueued == 1
    assert queue.stream_length() == 1  # NO double-enqueue
    # The recovered job carries the SAME message id (dedup returned the original).
    assert queue._dedup[content_hash] == first_id
    assert store._docs["crashed-2"].learning_status == LearningStatus.QUEUED


# --- Row 3/4: no double-enqueue across cycles -------------------------------


async def test_resweep_of_queued_session_adds_nothing(store, queue, sweeper, seed_session):
    seed_session(store, "sess-1", messages=[make_message(0, "user", "hi")])
    await sweeper.run_once()
    assert queue.stream_length() == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.QUEUED

    # Second cycle: the session is now `queued` (not sweepable) -> nothing happens.
    result2 = await sweeper.run_once()
    assert result2.scanned == 0
    assert result2.enqueued == 0
    assert queue.stream_length() == 1


async def test_enqueue_is_idempotent_by_content_hash(store, queue, seed_session):
    """Direct dedup-key proof: two enqueues of the identical content-hash job add
    exactly one stream entry and return the same message id."""
    doc = seed_session(store, "sess-dup", messages=[make_message(0, "user", "hi")])
    h = compute_content_hash(doc)
    id1 = await queue.enqueue(LearningJob.from_doc(doc, content_hash=h))
    id2 = await queue.enqueue(LearningJob.from_doc(doc, content_hash=h))
    assert id1 == id2
    assert queue.stream_length() == 1


async def test_scan_limit_is_respected(store, queue, seed_session):
    from data_agent.learning.config import LearningSettings

    for i in range(5):
        seed_session(store, f"idle-{i}")
    limited = LearningSweeper(store, queue, LearningSettings(_env_file=None, learning_scan_limit=2))
    result = await limited.run_once()
    assert result.scanned == 2
    assert queue.stream_length() == 2
