"""LearningConsumer — the no-op Slice-1 consumer (D96 §5/§7, design §2).

Every cycle: read the kill-switch FRESH; if enabled, first reclaim stale PEL
entries (dead-lettering any past N deliveries), then XREADGROUP new work. For
each delivered job:

  idempotency check (already-`done` + same `content_hash` → ACK + skip)
    → CAS `queued → processing`
    → **no-op work + trace event**   ← Slice 2 replaces this middle with the
                                        loader + triage + extractor
    → CAS `processing → done`  (records a FRESHLY computed content_hash)
    → XACK

Ordering invariant (MEDIUM-3): NO irreversible XACK (or dead-letter move) happens
before the session-state CAS the message represents. A CAS loss / crash at either
transition is a skip WITHOUT ack (the message stays in the PEL and is reclaimed
later), so no work is silently dropped and a crash mid-transition is
reclaim-recoverable via the `done`+same-hash dedup. The consumer is read-only
w.r.t. request-path data (D72): the only writes are the two lifecycle
transitions (+ `learning_content_hash` recorded on `done`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from data_agent.runtime.session.store import CASMismatchError, SessionStore

from . import state_machine
from .config import LearningSettings, learning_enabled
from .models import LearningStatus, compute_content_hash
from .observability import consume_span, disabled_span
from .queue import DeliveredJob, LearningQueue

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConsumeResult:
    """Outcome tallies for one `run_once` cycle."""

    done: int = 0
    dedup_skips: int = 0
    dead_letters: int = 0
    skipped: int = 0
    disabled: bool = False


class LearningConsumer:
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

    async def run_once(self) -> ConsumeResult:
        # Kill-switch gate FIRST (design §7): disabled ⇒ do NOT XREADGROUP or
        # process; work simply waits in the stream (no loss). Read per cycle.
        if not learning_enabled():
            if self._tracer is not None:
                with disabled_span(self._tracer, process="consumer"):
                    pass
            return ConsumeResult(disabled=True)

        tally = _Tally()

        # Reclaim + dead-letter first so a poison job never head-of-lines.
        reclaimed = await self._queue.reclaim_stale(
            min_idle_ms=self._settings.learning_reclaim_min_idle_seconds * 1000,
            max_deliveries=self._settings.learning_max_deliveries,
        )
        for delivered in reclaimed:
            await self._dispatch(delivered, tally)

        # New deliveries.
        delivered_batch = await self._queue.consume(
            count=self._settings.learning_batch_size,
            block_ms=self._settings.learning_block_ms,
        )
        for delivered in delivered_batch:
            await self._dispatch(delivered, tally)

        return ConsumeResult(
            done=tally.done,
            dedup_skips=tally.dedup,
            dead_letters=tally.dead,
            skipped=tally.skipped,
        )

    async def _dispatch(self, delivered: DeliveredJob, tally: _Tally) -> None:
        """Route one delivery, isolating transient failures (MEDIUM-1): a bad
        message logs and is left in the PEL for a later reclaim rather than
        killing the batch/daemon."""
        try:
            if delivered.dead_lettered:
                outcome = await self._handle_dead_letter(delivered)
            else:
                outcome = await self._process(delivered)
        except CASMismatchError:
            tally.skipped += 1
            return
        except Exception:  # noqa: BLE001 - MEDIUM-1: isolate transient store/queue
            # errors; the un-ACKed message is reclaimed and retried next cycle.
            _logger.exception(
                "learning consume failed for message %s; leaving it for reclaim",
                delivered.message_id,
            )
            tally.skipped += 1
            return
        tally.record(outcome)

    async def _process(self, delivered: DeliveredJob) -> str:
        """Idempotent process of one delivery. Returns the outcome label
        (`done` | `dedup_skip` | `skip`)."""
        job = delivered.job
        doc, cas = await self._store.get_session_with_cas(job.session_id)

        # Idempotency (D96 §5.2): a re-delivery of an already-processed job
        # (session already `done` with the SAME recorded hash) → ACK + skip.
        if (
            doc.learning_status == LearningStatus.DONE
            and doc.learning_content_hash == job.content_hash
        ):
            await self._queue.ack(delivered.message_id)
            self._emit_consume(job.session_id, "dedup_skip", delivered.delivery_count)
            return "dedup_skip"

        try:
            cas = await state_machine.transition(
                self._store, job.session_id, LearningStatus.QUEUED,
                LearningStatus.PROCESSING, cas,
            )
        except CASMismatchError:
            # A peer is handling it, or the state isn't `queued` — do NOT ack;
            # leave the message for the owner / a later reclaim.
            return "skip"

        # --- no-op work (Slice 1). Slice 2 replaces this with the D27
        # loader → normalizer → cheap-LLM triage → grounded extractor. ---
        await self._do_work(delivered)

        # MEDIUM-3: record a FRESHLY computed hash from the loaded doc (not the
        # stale message hash) so the Slice-2 dedup sees the true content hash.
        fresh_hash = compute_content_hash(doc)
        try:
            await state_machine.transition(
                self._store, job.session_id, LearningStatus.PROCESSING,
                LearningStatus.DONE, cas, content_hash=fresh_hash,
            )
        except CASMismatchError:
            # Crash/lost race before XACK is safe: the message stays in the PEL,
            # is reclaimed, and the `done`+same-hash dedup ACKs it next time.
            return "skip"

        await self._queue.ack(delivered.message_id)
        self._emit_consume(job.session_id, "done", delivered.delivery_count)
        return "done"

    async def _handle_dead_letter(self, delivered: DeliveredJob) -> str:
        """A message past N deliveries. Ordering (MEDIUM-3/4): CAS the session to
        `dead_letter` FIRST, then `finalize_dead_letter` (XADD-dead + XACK).
        Returns `dead_letter` | `ack_terminal` | `skip`."""
        job = delivered.job
        doc, cas = await self._store.get_session_with_cas(job.session_id)

        # Already terminal (crash between a prior CAS and finalize, OR a poison
        # reclaim of an already-`done` session) → plain ACK + skip, NOT a
        # re-dead-letter (MEDIUM-4: kills the spurious dead-letter noise).
        if doc.learning_status in (LearningStatus.DONE, LearningStatus.DEAD_LETTER):
            await self._queue.ack(delivered.message_id)
            return "ack_terminal"

        try:
            await state_machine.transition(
                self._store, job.session_id, doc.learning_status,
                LearningStatus.DEAD_LETTER, cas, assert_from=False,
            )
        except CASMismatchError:
            # Peer advanced it — leave in the PEL for a later reclaim.
            return "skip"

        # Session is terminal now; complete the queue-side move. A crash before
        # this leaves the message in the PEL → reclaimed → terminal → ack_terminal.
        await self._queue.finalize_dead_letter(delivered)
        self._emit_consume(job.session_id, "dead_letter", delivered.delivery_count)
        return "dead_letter"

    async def _do_work(self, _delivered: DeliveredJob) -> None:
        """Slice-1 no-op stand-in for the Slice-2 loader/triage/extractor."""
        return None

    def _emit_consume(self, session_id: str, outcome: str, delivery_count: int) -> None:
        if self._tracer is not None:
            with consume_span(
                self._tracer,
                session_id=session_id,
                outcome=outcome,
                delivery_count=delivery_count,
            ):
                pass

    async def run_forever(self, *, sleep) -> None:
        """Blocking loop (used by the entrypoint). Ensures the group exists, then
        consumes batches forever. A transient error logs + continues (MEDIUM-1);
        when disabled, `run_once` returns immediately (no blocking XREADGROUP), so
        *sleep* paces the re-check on the consumer's OWN idle interval."""
        await self._queue.ensure_group()
        while True:
            try:
                result = await self.run_once()
            except Exception:  # noqa: BLE001 - MEDIUM-1: never die on a transient blip.
                _logger.exception("learning consume cycle failed; retrying")
                await sleep(self._settings.learning_consumer_idle_sleep_seconds)
                continue
            if result.disabled:
                await sleep(self._settings.learning_consumer_idle_sleep_seconds)


@dataclass
class _Tally:
    done: int = 0
    dedup: int = 0
    dead: int = 0
    skipped: int = 0

    def record(self, outcome: str) -> None:
        if outcome == "done":
            self.done += 1
        elif outcome == "dedup_skip":
            self.dedup += 1
        elif outcome == "dead_letter":
            self.dead += 1
        else:  # "skip" | "ack_terminal"
            self.skipped += 1
