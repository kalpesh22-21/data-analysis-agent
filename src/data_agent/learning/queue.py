"""The `LearningQueue` port seam (D96 §9 / design §9).

A Protocol so Layer-1 tests drive the in-memory fake (`memory_queue`) through the exact
same enqueue/consume/ack/reclaim paths the real Redis Streams impl (`redis_queue`) uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import LearningJob


@dataclass(frozen=True)
class DeliveredJob:
    """One message delivered off the stream: entry id, decoded envelope, delivery count.

    `dead_lettered` (only ever from `reclaim_stale`) means the queue ALREADY moved the entry to
    the dead stream and XACKed it — the consumer must not process/ack it, only CAS-mark the
    session. `reclaimed` marks the delivery path, and proves the entry was IDLE past min-idle,
    NOT that its previous owner is dead.
    """

    message_id: str
    job: LearningJob
    delivery_count: int
    dead_lettered: bool = False
    reclaimed: bool = False


class LearningQueue(Protocol):
    """Transport port for the learning loop (Redis Streams in production)."""

    async def ensure_group(self) -> None:
        """Idempotently create the stream + consumer group (XGROUP CREATE MKSTREAM)."""
        ...

    async def enqueue(self, job: LearningJob) -> str:
        """XADD the reference envelope; return the assigned message id."""
        ...

    async def consume(self, *, count: int, block_ms: int) -> list[DeliveredJob]:
        """XREADGROUP up to *count* NEW ('>') messages, blocking *block_ms*; they enter the PEL."""
        ...

    async def ack(self, message_id: str) -> None:
        """XACK a fully-processed message off the PEL."""
        ...

    async def reclaim_stale(
        self, *, min_idle_ms: int, max_deliveries: int
    ) -> list[DeliveredJob]:
        """XAUTOCLAIM entries idle past *min_idle_ms* and re-deliver them.

        An entry over *max_deliveries* is reported with `dead_lettered=True` but is NOT yet XACKed
        or moved: the consumer must CAS the session to `dead_letter` FIRST and only then call
        `finalize_dead_letter` — no irreversible XACK before the state CAS it represents.
        """
        ...

    async def finalize_dead_letter(self, delivered: DeliveredJob) -> None:
        """Move a poison message to the dead-letter stream and XACK it off the work stream.

        Called ONLY AFTER the session has been CAS-marked `dead_letter`. Safe to skip: a crash
        before this leaves the message in the PEL, and the now-terminal session short-circuits the
        next reclaim to a plain ack.
        """
        ...
