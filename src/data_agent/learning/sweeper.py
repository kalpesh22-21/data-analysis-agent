"""LearningSweeper — idle-detection + two-step claim + enqueue (D96 §6).

Every cycle: read the kill-switch FRESH; if enabled, scan idle sessions and, for
each, perform the two-step claim (`active → pending` CAS, then XADD, then
`pending → queued` CAS recording `content_hash`). A CAS loss (peer sweeper won,
or the session was resumed) is a skip, not a retry.

Read-only w.r.t. request-path data (D72): the ONLY writes are the two lifecycle
transitions and `learning_content_hash`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from data_agent.runtime.session.store import CASMismatchError, SessionStore

from . import state_machine
from .config import LearningSettings, learning_enabled
from .models import SWEEPABLE_STATUSES, LearningJob, LearningStatus, compute_content_hash
from .observability import (
    disabled_span,
    enqueue_span,
    inject_current_traceparent,
    sweep_span,
)
from .queue import LearningQueue

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepResult:
    """Outcome of one `run_once` cycle (also the sweep span's counters)."""

    scanned: int = 0
    claimed: int = 0
    enqueued: int = 0
    disabled: bool = False


class LearningSweeper:
    def __init__(
        self,
        store: SessionStore,
        queue: LearningQueue,
        settings: LearningSettings,
        *,
        tracer=None,
    ) -> None:
        self._store = store
        self._queue = queue
        self._settings = settings
        self._tracer = tracer

    def _cutoff(self) -> str:
        cutoff = datetime.now(UTC) - timedelta(
            seconds=self._settings.learning_idle_threshold_seconds
        )
        return cutoff.isoformat()

    async def run_once(self) -> SweepResult:
        # Kill-switch gate FIRST (design §7): disabled ⇒ no scan, no claim, no
        # XADD. Read uncached, per cycle.
        if not learning_enabled():
            if self._tracer is not None:
                with disabled_span(self._tracer, process="sweeper"):
                    pass
            return SweepResult(disabled=True)

        idle = await self._store.scan_idle_sessions(
            statuses=SWEEPABLE_STATUSES,
            last_activity_before=self._cutoff(),
            limit=self._settings.learning_scan_limit,
        )
        scanned = len(idle)
        claimed = 0
        enqueued = 0
        for doc, cas in idle:
            try:
                # Step 1 — claim (active → pending). A session already `pending`
                # (crash recovery) skips straight to re-enqueue with its scan cas.
                if doc.learning_status == LearningStatus.ACTIVE:
                    cas = await state_machine.transition(
                        self._store, doc.session_id, LearningStatus.ACTIVE,
                        LearningStatus.PENDING, cas,
                    )
                    claimed += 1
                content_hash = compute_content_hash(doc)
                # Step 2 — XADD (idempotent by content_hash), then pending → queued.
                # The enqueue span is the per-session trace ROOT: its `traceparent`
                # is injected onto the job so the consumer/scheduler spans chain into
                # ONE trace. No tracer wired ⇒ a plain enqueue with no traceparent.
                #
                # NOTE (intended, reviewer 4b): the enqueue span now closes BEFORE the
                # pending→queued CAS (the traceparent must be injected around the XADD).
                # So if that CAS then loses a race (CASMismatchError below), an enqueue
                # span exists where the old order produced none. This is CORRECT — the
                # XADD really happened (idempotent by content_hash; the consumer absorbs
                # the duplicate) — and the `enqueued` COUNTER is still bumped only AFTER
                # the CAS succeeds, so the sweep counters are unchanged.
                await self._enqueue(doc, cas, content_hash)
                await state_machine.transition(
                    self._store, doc.session_id, LearningStatus.PENDING,
                    LearningStatus.QUEUED, cas, content_hash=content_hash,
                )
                enqueued += 1
            except CASMismatchError:
                # A peer sweeper claimed it, or the session was resumed — skip.
                # A `pending` doc left un-enqueued by a crash here is re-detected
                # next cycle (design §3 crash-safety); XADD is idempotent.
                continue
            except Exception:  # noqa: BLE001 - MEDIUM-1: one bad session must not
                # abort the whole cycle. A transient store/queue error (e.g. the
                # `enqueue` XADD failing) leaves the session `pending` (the CAS to
                # `queued` runs ONLY after enqueue returns), so the next cycle
                # retries it. Log and move on.
                _logger.exception(
                    "learning sweep failed for session %s; leaving it for the next cycle",
                    doc.session_id,
                )
                continue

        if self._tracer is not None:
            with sweep_span(
                self._tracer, scanned=scanned, claimed=claimed, enqueued=enqueued
            ):
                pass
        return SweepResult(scanned=scanned, claimed=claimed, enqueued=enqueued)

    async def _enqueue(self, doc, cas, content_hash: str) -> str:
        """XADD the reference envelope, returning the message id. When a tracer is
        wired the enqueue runs INSIDE the `learning.enqueue` span (the per-session
        trace ROOT) and injects that span's `traceparent` onto the job so the
        consumer/scheduler spans join the SAME Phoenix trace; otherwise a plain
        XADD with no traceparent (no-op-tracer behavior preserved)."""
        if self._tracer is None:
            job = LearningJob.from_doc(doc, content_hash=content_hash, cas=cas)
            return await self._queue.enqueue(job)
        with enqueue_span(
            self._tracer, session_id=doc.session_id, content_hash=content_hash
        ) as enqueue:
            job = LearningJob.from_doc(
                doc,
                content_hash=content_hash,
                cas=cas,
                traceparent=inject_current_traceparent(),
            )
            message_id = await self._queue.enqueue(job)
            enqueue.set_attribute("learning.message_id", message_id)
            return message_id

    async def run_forever(self, *, sleep) -> None:
        """Periodic loop (used by the entrypoint). *sleep* is injected
        (`asyncio.sleep`) so it is unit-drivable. Ensures the consumer group
        exists once, then sweeps every `learning_sweep_interval_seconds`."""
        await self._queue.ensure_group()
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - MEDIUM-1: a transient scan/queue
                # error must not kill the daemon; log and retry next cycle. This
                # is also the retry arm that makes the enqueue-ordering fix safe.
                _logger.exception("learning sweep cycle failed; retrying next interval")
            await sleep(self._settings.learning_sweep_interval_seconds)
