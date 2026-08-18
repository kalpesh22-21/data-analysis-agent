"""InMemoryLearningQueue — the Layer-1 `LearningQueue` fake (design §9).

Dict-backed, no I/O, but with the SAME observable semantics as
`RedisStreamsLearningQueue`: a PEL, per-message redelivery counting, content-hash enqueue
idempotency, dead-letter after N deliveries. `reclaim_stale` staleness is driven by an
injectable `now_fn` (seconds), so tests advance a clock instead of sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .models import LearningJob
from .queue import DeliveredJob


@dataclass
class _PelEntry:
    job: LearningJob
    delivery_count: int
    last_delivered_at: float


class InMemoryLearningQueue:
    """Dict-backed `LearningQueue` fake — deterministic, Layer-1 only."""

    def __init__(self, *, now_fn: Callable[[], float] = time.monotonic) -> None:
        self._now = now_fn
        self._counter = 0
        # message_id -> job, for entries that exist on the stream (XADDed and not
        # yet dead-lettered/removed).
        self._entries: dict[str, LearningJob] = {}
        # Never-delivered entries available to `consume` ('>' semantics).
        self._new: list[str] = []
        # Pending Entries List: delivered-not-acked messages.
        self._pel: dict[str, _PelEntry] = {}
        # content_hash -> message_id (Redis SET-NX dedup-key analogue).
        self._dedup: dict[str, str] = {}
        # The dead-letter stream.
        self._dead: list[LearningJob] = []

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._counter}-0"

    async def ensure_group(self) -> None:
        # Nothing to create for the fake; matches the idempotent no-op shape.
        return None

    async def enqueue(self, job: LearningJob) -> str:
        # XADD-first, mark-dedup-second (BLOCKER parity with the real queue): a
        # SET dedup entry ⟹ the add happened; absent ⟹ safe to (re-)add. A
        # re-enqueue of an already-marked hash returns the original id without a
        # second entry.
        existing = self._dedup.get(job.content_hash)
        if existing is not None:
            return existing
        message_id = self._next_id()
        self._entries[message_id] = job
        self._new.append(message_id)
        self._dedup[job.content_hash] = message_id
        return message_id

    async def enqueue_without_dedup_mark(self, job: LearningJob) -> str:
        """TEST SEAM: add WITHOUT recording the dedup mark — a crash between the two.

        A subsequent `enqueue` of the same hash then produces a benign DUPLICATE (the consumer's
        idempotency absorbs it), proving no-strand. Never used in production.
        """
        message_id = self._next_id()
        self._entries[message_id] = job
        self._new.append(message_id)
        return message_id

    async def consume(self, *, count: int, block_ms: int) -> list[DeliveredJob]:
        delivered: list[DeliveredJob] = []
        while self._new and len(delivered) < count:
            message_id = self._new.pop(0)
            job = self._entries[message_id]
            self._pel[message_id] = _PelEntry(
                job=job, delivery_count=1, last_delivered_at=self._now()
            )
            delivered.append(DeliveredJob(message_id=message_id, job=job, delivery_count=1))
        return delivered

    async def ack(self, message_id: str) -> None:
        self._pel.pop(message_id, None)
        self._entries.pop(message_id, None)

    async def reclaim_stale(
        self, *, min_idle_ms: int, max_deliveries: int
    ) -> list[DeliveredJob]:
        now = self._now()
        delivered: list[DeliveredJob] = []
        for message_id in list(self._pel.keys()):
            entry = self._pel[message_id]
            idle_ms = (now - entry.last_delivered_at) * 1000.0
            if idle_ms < min_idle_ms:
                continue
            # Reclaiming re-delivers → increments the delivery count (mirrors
            # XAUTOCLAIM bumping XPENDING's times_delivered).
            entry.delivery_count += 1
            entry.last_delivered_at = now
            # Over-threshold → REPORT dead_lettered but LEAVE in the PEL; the
            # consumer CAS-marks the session then calls `finalize_dead_letter`
            # (MEDIUM-3). No removal / dead-stream write happens here.
            delivered.append(
                DeliveredJob(
                    message_id=message_id,
                    job=entry.job,
                    delivery_count=entry.delivery_count,
                    dead_lettered=entry.delivery_count > max_deliveries,
                    # Mirrors the real queue: EVERY entry that comes back from
                    # `reclaim_stale` is a reclaimed delivery (the consumer reads
                    # this to decide whether a `processing` session is a crashed
                    # owner's work it may continue).
                    reclaimed=True,
                )
            )
        return delivered

    async def finalize_dead_letter(self, delivered: DeliveredJob) -> None:
        # Move to the dead stream + remove from the PEL/stream. Idempotent-safe:
        # a message already finalized is simply absent from the PEL.
        if delivered.message_id not in self._pel:
            return
        self._dead.append(delivered.job)
        self._pel.pop(delivered.message_id, None)
        self._entries.pop(delivered.message_id, None)

    # --- Read-only inspection helpers for Layer-1 tests (not part of the port). ---

    def pending_count(self) -> int:
        """Un-ACKed messages currently in the PEL."""
        return len(self._pel)

    def new_count(self) -> int:
        """Never-delivered messages still awaiting a first `consume`."""
        return len(self._new)

    def stream_length(self) -> int:
        """Live entries on the work stream (new + in-flight)."""
        return len(self._entries)

    def dead_letters(self) -> list[LearningJob]:
        """Jobs parked on the dead-letter stream."""
        return list(self._dead)
