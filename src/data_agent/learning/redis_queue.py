"""RedisStreamsLearningQueue — the real `LearningQueue` (D30, design §4/§9).

Backed by `redis.asyncio`. Topology:
  - work stream        `learning:jobs`       (XADD / XREADGROUP `>` / XACK)
  - consumer group     `learning-workers`    (idempotent XGROUP CREATE MKSTREAM)
  - dead-letter stream `learning:jobs:dead`  (terminal parking for poison jobs)

Two idempotency mechanisms cooperate (D30/D96 §5):
  1. Enqueue is idempotent by `content_hash` via a Redis `SET NX` dedup key, so a
     sweeper that crashed AFTER XADD but BEFORE the `pending → queued` CAS does
     not double-enqueue when the still-`pending` session is re-detected next
     cycle.
  2. Delivery is at-least-once; the CONSUMER makes it effectively exactly-once at
     the processing boundary (already-`done` + same hash → ACK + skip).

Dead-letter: `reclaim_stale` reclaims PEL entries idle past `min_idle_ms`
(XAUTOCLAIM) and, for any whose delivery count (XPENDING) exceeds
`max_deliveries`, XADDs them to the dead stream + XACKs them off the work stream,
reporting them so the consumer can CAS the session to `dead_letter`.

Import-guarded like `couchbase_store`: if `redis` is not installed the module
still imports (so the unit suite stays green with zero infra); constructing the
queue without the package raises.
"""

from __future__ import annotations

from typing import Any

from .config import LearningSettings
from .models import LearningJob
from .queue import DeliveredJob

try:  # pragma: no cover - exercised only when the redis package is installed
    import redis.asyncio as aioredis
    from redis.exceptions import ResponseError

    REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    REDIS_AVAILABLE = False


# Bound the work stream's memory with approximate trimming (design §4). Generous
# — the stream is a short transport hop, not a store.
_STREAM_MAXLEN = 100_000
# Dedup-key retention: comfortably longer than the enqueue→process window so a
# crash-recovery re-sweep is deduped, but self-expiring so the keyspace is bounded.
_DEDUP_TTL_SECONDS = 86_400


class RedisStreamsLearningQueue:
    """Real `LearningQueue` backed by a Redis Streams consumer group."""

    def __init__(
        self,
        redis: Any,
        *,
        stream: str,
        group: str,
        consumer_name: str,
        dead_letter_stream: str,
        maxlen: int = _STREAM_MAXLEN,
        dedup_ttl_seconds: int = _DEDUP_TTL_SECONDS,
    ) -> None:
        if not REDIS_AVAILABLE:
            raise RuntimeError(
                "The 'redis' package is not installed. "
                "Install it (see pyproject.toml) to use RedisStreamsLearningQueue."
            )
        self._redis = redis
        self._stream = stream
        self._group = group
        self._consumer = consumer_name
        self._dead_stream = dead_letter_stream
        self._maxlen = maxlen
        self._dedup_ttl = dedup_ttl_seconds

    @classmethod
    def from_settings(cls, settings: LearningSettings) -> RedisStreamsLearningQueue:
        """Build the queue + its Redis client from `LearningSettings`.
        `decode_responses=True` so stream fields/ids come back as `str` (the
        `LearningJob` (de)serialization assumes text, not bytes)."""
        if not REDIS_AVAILABLE:
            raise RuntimeError("The 'redis' package is not installed.")
        client = aioredis.from_url(settings.learning_redis_url, decode_responses=True)
        return cls(
            client,
            stream=settings.learning_jobs_stream,
            group=settings.learning_consumer_group,
            consumer_name=settings.learning_consumer_name,
            dead_letter_stream=settings.learning_dead_letter_stream,
        )

    def _dedup_key(self, content_hash: str) -> str:
        return f"{self._stream}:enqueued:{content_hash}"

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except ResponseError as exc:  # pragma: no cover - trivial branch
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, job: LearningJob) -> str:
        """XADD-first, mark-dedup-second (BLOCKER fix). The dedup key is a
        duplicate-VOLUME reducer, NEVER a correctness gate — correctness is the
        consumer's `done`+same-hash idempotency.

        Invariant: a SET dedup key ⟹ XADD definitely happened (it holds the real
        message id); an ABSENT key ⟹ safe to (re-)XADD. So a crash AFTER XADD but
        BEFORE the mark degrades to a benign DUPLICATE on the next re-sweep (the
        consumer absorbs it), never a strand — whereas the old mark-first order
        could strand a `pending`→`queued` session with zero messages on the
        stream.
        """
        key = self._dedup_key(job.content_hash)
        existing = await self._redis.get(key)
        if existing:  # a confirmed prior enqueue (holds a real id) → skip re-XADD
            return existing
        message_id = await self._redis.xadd(
            self._stream, job.to_fields(), maxlen=self._maxlen, approximate=True
        )
        # Mark AFTER the XADD is confirmed; a crash here just re-XADDs next sweep.
        await self._redis.set(key, message_id, ex=self._dedup_ttl)
        return message_id

    async def enqueue_without_dedup_mark(self, job: LearningJob) -> str:
        """TEST SEAM (Layer 1/2): XADD WITHOUT recording the dedup key —
        simulating a crash that XADDed but died before the mark. Lets QA prove
        the re-sweep produces a benign DUPLICATE (absorbed by consumer
        idempotency), never a strand. Never called in production."""
        return await self._redis.xadd(
            self._stream, job.to_fields(), maxlen=self._maxlen, approximate=True
        )

    async def consume(self, *, count: int, block_ms: int) -> list[DeliveredJob]:
        response = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: ">"},
            count=count,
            block=block_ms,
        )
        return self._decode_stream_response(response, delivery_count=1)

    async def ack(self, message_id: str) -> None:
        await self._redis.xack(self._stream, self._group, message_id)

    async def reclaim_stale(
        self, *, min_idle_ms: int, max_deliveries: int
    ) -> list[DeliveredJob]:
        response = await self._redis.xautoclaim(
            self._stream,
            self._group,
            self._consumer,
            min_idle_time=min_idle_ms,
            start_id="0-0",
        )
        # redis-py returns [next_cursor, [(id, {fields}), ...], [deleted_ids]].
        claimed = response[1] if len(response) > 1 else []
        delivered: list[DeliveredJob] = []
        for message_id, fields in claimed:
            if not fields:  # a deleted/tombstoned entry surfaced by XAUTOCLAIM
                continue
            job = LearningJob.from_fields(fields)
            delivery_count = await self._delivery_count(message_id)
            # Over-threshold entries are REPORTED as dead_lettered but left in
            # the PEL (no XADD-dead / XACK yet): the consumer CAS-marks the
            # session `dead_letter` FIRST, then calls `finalize_dead_letter`
            # (MEDIUM-3 no-irreversible-XACK-before-CAS invariant).
            delivered.append(
                DeliveredJob(
                    message_id=message_id,
                    job=job,
                    delivery_count=delivery_count,
                    dead_lettered=delivery_count > max_deliveries,
                )
            )
        return delivered

    async def finalize_dead_letter(self, delivered: DeliveredJob) -> None:
        fields = delivered.job.to_fields()
        fields["dead_letter_delivery_count"] = str(delivered.delivery_count)
        fields["dead_letter_origin_id"] = delivered.message_id
        await self._redis.xadd(self._dead_stream, fields)
        await self._redis.xack(self._stream, self._group, delivered.message_id)

    async def _delivery_count(self, message_id: str) -> int:
        pending = await self._redis.xpending_range(
            self._stream, self._group, min=message_id, max=message_id, count=1
        )
        if not pending:
            return 1
        return int(pending[0]["times_delivered"])

    @staticmethod
    def _decode_stream_response(response: Any, *, delivery_count: int) -> list[DeliveredJob]:
        delivered: list[DeliveredJob] = []
        for _stream_name, entries in response or []:
            for message_id, fields in entries:
                if not fields:
                    continue
                delivered.append(
                    DeliveredJob(
                        message_id=message_id,
                        job=LearningJob.from_fields(fields),
                        delivery_count=delivery_count,
                    )
                )
        return delivered
