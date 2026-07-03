"""D96-cas-single-writer-transition (design §3, matrix row 5, task item 7).

The CAS on the session doc is the single-writer-per-session guarantee: two
workers that both read the same snapshot and both try the SAME transition -> one
wins, the loser gets `CASMismatchError` and SKIPS (never crashes, never forces).
Covered at both contended transitions: the sweeper's `active -> pending` claim
and the consumer's `queued -> processing`.
"""

from __future__ import annotations

import pytest

from data_agent.learning import state_machine
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.learning.sweeper import LearningSweeper
from data_agent.runtime.session.store import CASMismatchError

from .conftest import make_message

# --- Sweeper claim: active -> pending ---------------------------------------


async def test_two_sweeper_claims_from_same_snapshot_one_wins(store, seed_session):
    seed_session(store, "sess-1")
    # Two sweepers each scan and observe the SAME cas snapshot.
    _, cas_a = await store.get_session_with_cas("sess-1")
    _, cas_b = await store.get_session_with_cas("sess-1")
    assert cas_a == cas_b

    new_cas = await state_machine.transition(
        store, "sess-1", LearningStatus.ACTIVE, LearningStatus.PENDING, cas_a
    )
    assert store._docs["sess-1"].learning_status == LearningStatus.PENDING

    # The loser uses the now-stale snapshot cas -> CAS mismatch, NOT a crash.
    with pytest.raises(CASMismatchError):
        await state_machine.transition(
            store, "sess-1", LearningStatus.ACTIVE, LearningStatus.PENDING, cas_b
        )
    # State advanced exactly once.
    assert store._docs["sess-1"].learning_status == LearningStatus.PENDING
    assert new_cas == store._versions["sess-1"]


async def test_sweeper_loop_treats_cas_loss_as_skip(store, queue, settings, seed_session):
    """A store whose claim always loses the CAS race must not crash the sweep —
    the loop swallows `CASMismatchError` and moves on (0 claimed, 0 enqueued)."""
    seed_session(store, "sess-1")

    class LosingStore:
        def __init__(self, inner):
            self._inner = inner

        async def scan_idle_sessions(self, **kw):
            return await self._inner.scan_idle_sessions(**kw)

        async def transition_learning_status(self, *a, **kw):
            raise CASMismatchError("a peer won the claim")

    losing = LosingStore(store)
    sweeper = LearningSweeper(losing, queue, settings)
    result = await sweeper.run_once()

    assert result.scanned == 1
    assert result.claimed == 0
    assert result.enqueued == 0
    assert queue.stream_length() == 0  # nothing enqueued on a lost claim


# --- Consumer: queued -> processing -----------------------------------------


async def test_two_processing_transitions_from_same_snapshot_one_wins(store, seed_session):
    seed_session(store, "sess-1", learning_status=LearningStatus.QUEUED,
                 learning_content_hash="h")
    _, cas_a = await store.get_session_with_cas("sess-1")
    _, cas_b = await store.get_session_with_cas("sess-1")

    await state_machine.transition(
        store, "sess-1", LearningStatus.QUEUED, LearningStatus.PROCESSING, cas_a
    )
    with pytest.raises(CASMismatchError):
        await state_machine.transition(
            store, "sess-1", LearningStatus.QUEUED, LearningStatus.PROCESSING, cas_b
        )
    assert store._docs["sess-1"].learning_status == LearningStatus.PROCESSING


async def test_consumer_skips_when_peer_already_took_queued(store, queue, settings, seed_session):
    """A peer advances `queued -> processing` between the consumer's read and its
    own CAS: the from-state assertion fails -> `_process` returns 'skip', the
    message is NOT acked (left for the owner/reclaim), no crash."""
    doc = seed_session(store, "sess-1", learning_status=LearningStatus.QUEUED,
                       messages=[make_message(0, "user", "hi")])
    content_hash = compute_content_hash(doc)
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=content_hash))

    # A peer claims processing first.
    _, cas = await store.get_session_with_cas("sess-1")
    await state_machine.transition(
        store, "sess-1", LearningStatus.QUEUED, LearningStatus.PROCESSING, cas
    )

    consumer = LearningConsumer(store, queue, settings)
    result = await consumer.run_once()

    assert result.done == 0
    assert result.skipped == 1
    # The message stays in the PEL (un-acked) for the owner / a later reclaim.
    assert queue.pending_count() == 1
    # The session is still where the peer left it — the consumer forced nothing.
    assert store._docs["sess-1"].learning_status == LearningStatus.PROCESSING
