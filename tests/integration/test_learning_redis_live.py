"""Layer-2 integration — `RedisStreamsLearningQueue` against a LIVE Redis
(`l2-redis` in `docker-compose.integration.yml`). Matrix rows 3/7/9/10 (design
§4/§9, D30/D96).

The proofs, end-to-end against real Redis Streams:
  1. `ensure_group` is idempotent (a second call swallows BUSYGROUP);
  2. XADD + XREADGROUP `>` deliver the reference envelope; ACK drains the PEL to
     zero (no leak);
  3. content-hash dedup: a re-enqueue of the same hash does NOT add a second
     stream entry (crash-recovery idempotency);
  4. message-is-reference: the raw stream entry carries ONLY the D30 reference
     fields — no transcript, no raw JWT/scope;
  5. redelivery via XAUTOCLAIM re-delivers an un-ACKed entry and bumps its
     delivery count;
  6. dead-letter after N: a poison entry reclaimed past N=5 deliveries is XADDed
     to `learning:jobs:dead`, XACKed off the work stream (unblocking it), and
     reported with `dead_lettered=True`.

Skip-guarded on LEARNING_REDIS_TEST_URL; `uv run pytest` with no live Redis stays
fully green. Run with the stack up:

    docker compose -f docker-compose.integration.yml up -d --wait l2-redis
    LEARNING_REDIS_TEST_URL=redis://localhost:6379/0 \
        uv run pytest tests/integration/test_learning_redis_live.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest

from data_agent.learning.models import LearningJob
from data_agent.learning.redis_queue import REDIS_AVAILABLE, RedisStreamsLearningQueue

_REDIS_URL = os.environ.get("LEARNING_REDIS_TEST_URL")

pytestmark = pytest.mark.skipif(
    not REDIS_AVAILABLE or not _REDIS_URL,
    reason="Requires the 'redis' package AND a live Redis "
    "(set LEARNING_REDIS_TEST_URL=redis://localhost:6379/0 with l2-redis up).",
)


def _job(session_id: str, content_hash: str) -> LearningJob:
    return LearningJob(
        session_id=session_id,
        couchbase_doc_id=f"session::{session_id}",
        content_hash=content_hash,
        cas="42",
        scope_ref="scope-abc",
        trace_id="trace-1",
        session_closed_at="2026-07-01T00:00:00+00:00",
    )


@pytest.fixture
async def redis_client():
    import redis.asyncio as aioredis

    client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def queue(redis_client):
    """A queue over UNIQUE per-test stream names so parallel/repeat runs never
    cross-contaminate; the streams are deleted on teardown."""
    suffix = uuid.uuid4().hex[:12]
    stream = f"test:learning:jobs:{suffix}"
    dead = f"test:learning:jobs:dead:{suffix}"
    q = RedisStreamsLearningQueue(
        redis_client,
        stream=stream,
        group="learning-workers",
        consumer_name=f"worker-test-{suffix}",
        dead_letter_stream=dead,
    )
    yield q
    await redis_client.delete(stream, dead)
    # Dedup keys are per content-hash; scan+delete the test namespace.
    async for key in redis_client.scan_iter(match=f"{stream}:enqueued:*"):
        await redis_client.delete(key)


async def test_ensure_group_is_idempotent(queue):
    await queue.ensure_group()
    await queue.ensure_group()  # BUSYGROUP swallowed — no raise


async def test_enqueue_consume_ack_zero_pel(queue, redis_client):
    await queue.ensure_group()
    msg_id = await queue.enqueue(_job("sess-1", "hash-1"))
    assert msg_id

    delivered = await queue.consume(count=10, block_ms=100)
    assert len(delivered) == 1
    assert delivered[0].job.session_id == "sess-1"
    assert delivered[0].job.content_hash == "hash-1"

    # Un-ACKed => one pending entry.
    pending = await redis_client.xpending(queue._stream, queue._group)
    assert pending["pending"] == 1

    await queue.ack(delivered[0].message_id)
    pending_after = await redis_client.xpending(queue._stream, queue._group)
    assert pending_after["pending"] == 0  # zero PEL leak


async def test_content_hash_dedup_no_double_enqueue(queue, redis_client):
    await queue.ensure_group()
    id1 = await queue.enqueue(_job("sess-1", "same-hash"))
    id2 = await queue.enqueue(_job("sess-1", "same-hash"))
    assert id1 == id2
    length = await redis_client.xlen(queue._stream)
    assert length == 1  # only ONE entry despite two enqueues


async def test_crash_between_xadd_and_mark_re_enqueues_not_strands(queue, redis_client):
    """BLOCKER fix on real Redis: `enqueue_without_dedup_mark` models a crash that
    XADDed but died before the dedup SET. A following `enqueue` finds NO dedup key
    and re-XADDs (a benign DUPLICATE) rather than skipping the add — so the session
    is never advanced with zero backing messages (no strand)."""
    await queue.ensure_group()
    crash_id = await queue.enqueue_without_dedup_mark(_job("sess-1", "hash-1"))
    assert await redis_client.xlen(queue._stream) == 1
    assert await redis_client.get(queue._dedup_key("hash-1")) is None  # mark never landed

    resweep_id = await queue.enqueue(_job("sess-1", "hash-1"))
    assert resweep_id != crash_id
    assert await redis_client.xlen(queue._stream) == 2  # benign duplicate, not a strand
    # The mark now points at a REAL message id, so a further re-sweep is deduped.
    assert await redis_client.get(queue._dedup_key("hash-1")) == resweep_id
    assert await queue.enqueue(_job("sess-1", "hash-1")) == resweep_id
    assert await redis_client.xlen(queue._stream) == 2


async def test_message_is_reference_only(queue, redis_client):
    await queue.ensure_group()
    await queue.enqueue(_job("sess-ref", "hash-ref"))

    entries = await redis_client.xrange(queue._stream)
    assert len(entries) == 1
    _id, fields = entries[0]
    allowed = {"session_id", "couchbase_doc_id", "content_hash", "cas",
               "user_id", "scope_ref", "trace_id", "session_closed_at"}
    assert set(fields.keys()) <= allowed
    # No transcript / secret keys.
    for forbidden in ("messages", "tool_trail", "content", "args", "jwt",
                      "token", "column_scope", "result_full_ref"):
        assert forbidden not in fields


async def test_redelivery_via_reclaim_bumps_delivery_count(queue):
    await queue.ensure_group()
    await queue.enqueue(_job("sess-1", "hash-1"))
    delivered = await queue.consume(count=10, block_ms=100)
    assert delivered[0].delivery_count == 1

    # min_idle_ms=0 forces an immediate reclaim of the un-ACKed entry.
    reclaimed = await queue.reclaim_stale(min_idle_ms=0, max_deliveries=5)
    assert len(reclaimed) == 1
    assert reclaimed[0].delivery_count == 2
    assert reclaimed[0].dead_lettered is False


async def test_dead_letter_after_n(queue, redis_client):
    await queue.ensure_group()
    await queue.enqueue(_job("poison", "hash-poison"))
    await queue.consume(count=10, block_ms=100)  # delivery 1

    last = None
    for _ in range(5):  # 2,3,4,5,6 -> crosses N=5 on the 6th
        last = await queue.reclaim_stale(min_idle_ms=0, max_deliveries=5)
    assert len(last) == 1
    assert last[0].dead_lettered is True
    assert last[0].delivery_count == 6

    # New contract (MEDIUM-3): reclaim only REPORTS the poison as dead_lettered;
    # it is STILL pending and the dead stream is empty until `finalize_dead_letter`
    # runs (the consumer calls it only AFTER CAS-marking the session `dead_letter`,
    # so no irreversible XACK precedes the state CAS).
    pending_reported = await redis_client.xpending(queue._stream, queue._group)
    assert pending_reported["pending"] == 1
    assert await redis_client.xlen(queue._dead_stream) == 0

    await queue.finalize_dead_letter(last[0])

    # Now unblocked: XACKed off the PEL (no longer deliverable). NOTE the physical
    # work-stream entry is NOT deleted by XACK (that is XDEL / MAXLEN-trim); the
    # "unblocked / no head-of-line" invariant is that nothing is PENDING.
    pending_after = await redis_client.xpending(queue._stream, queue._group)
    assert pending_after["pending"] == 0
    dead_entries = await redis_client.xrange(queue._dead_stream)
    assert len(dead_entries) == 1
    _id, dead_fields = dead_entries[0]
    assert dead_fields["session_id"] == "poison"
    assert dead_fields["dead_letter_delivery_count"] == "6"
