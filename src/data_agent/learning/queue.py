"""The `LearningQueue` port seam (D96 §9 / design §9).

A thin Protocol lets Layer-1 tests drive an in-memory fake (`memory_queue`)
through the exact same enqueue/consume/ack/reclaim paths the real Redis Streams
impl (`redis_queue`) uses, so the state-machine and dead-letter logic are
exercised identically at both layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import LearningJob


@dataclass(frozen=True)
class DeliveredJob:
    """One message delivered off the stream: the Redis entry id, the decoded
    reference envelope, and how many times it has now been delivered (the PEL
    delivery count that drives the dead-letter threshold).

    `dead_lettered` is set only by `reclaim_stale` for an entry that crossed the
    delivery threshold: the queue has ALREADY moved it to the dead stream and
    XACKed it off the work stream, so the consumer must NOT process/ack it — it
    only CAS-marks the session `dead_letter` (transition #5). A normal delivery
    (from `consume`, or a still-live reclaim) has `dead_lettered=False`.

    `reclaimed` marks the DELIVERY PATH: True ⟺ this delivery came from
    `reclaim_stale` (XAUTOCLAIM of an entry idle past `min_idle_ms`), False ⟺ it
    came from `consume` (XREADGROUP `>`, a first delivery). The consumer needs the
    distinction to decide whether a session sitting in `processing` is a crashed
    owner's work it may continue (`consumer._claim_decision`), and it is carried
    EXPLICITLY rather than derived from `delivery_count > 1` because that
    equivalence is a property of the transport (`>` only ever yields
    `times_delivered == 1`), not of this port — a queue impl that redelivered
    without XAUTOCLAIM would silently change the meaning of the derived form.
    NOTE what it does NOT mean: a reclaim proves the entry was IDLE for min-idle,
    NOT that its previous owner is dead (Redis resets idle time on delivery, not on
    the owner's progress). The CAS is what makes re-entry safe; this flag only says
    the transport handed us the message.
    """

    message_id: str
    job: LearningJob
    delivery_count: int
    dead_lettered: bool = False
    reclaimed: bool = False


class LearningQueue(Protocol):
    """Transport port for the learning loop (Redis Streams in production)."""

    async def ensure_group(self) -> None:
        """Idempotently create the stream + consumer group (XGROUP CREATE
        MKSTREAM; a pre-existing group is not an error)."""
        ...

    async def enqueue(self, job: LearningJob) -> str:
        """XADD the reference envelope; return the assigned message id."""
        ...

    async def consume(self, *, count: int, block_ms: int) -> list[DeliveredJob]:
        """XREADGROUP up to *count* NEW ('>') messages, blocking up to
        *block_ms*. Delivered messages enter the PEL until `ack`ed."""
        ...

    async def ack(self, message_id: str) -> None:
        """XACK a fully-processed message off the PEL."""
        ...

    async def reclaim_stale(
        self, *, min_idle_ms: int, max_deliveries: int
    ) -> list[DeliveredJob]:
        """XAUTOCLAIM entries idle past *min_idle_ms* (a crashed/slow consumer's
        un-ACKed work) and re-deliver them. An entry whose delivery count exceeds
        *max_deliveries* is REPORTED with `dead_lettered=True` but is NOT yet
        XACKed or moved to the dead stream — it stays in the PEL. The consumer
        must CAS the session to `dead_letter` FIRST and only THEN call
        `finalize_dead_letter` (MEDIUM-3 ordering invariant: no irreversible XACK
        before the session-state CAS the message represents). A live (still under
        threshold) reclaim comes back `dead_lettered=False` for normal
        processing."""
        ...

    async def finalize_dead_letter(self, delivered: DeliveredJob) -> None:
        """Move a poison message to the dead-letter stream (XADD) and XACK it off
        the work stream — called by the consumer ONLY AFTER the session has been
        CAS-marked `dead_letter`. Idempotent-safe to skip: a crash before this
        leaves the message in the PEL to be reclaimed again, at which point the
        now-terminal session short-circuits it to a plain ack (no re-dead-letter,
        MEDIUM-4)."""
        ...
