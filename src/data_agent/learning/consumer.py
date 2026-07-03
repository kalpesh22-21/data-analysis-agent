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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from data_agent.runtime.session.models import SessionDoc
from data_agent.runtime.session.store import CASMismatchError, SessionStore

from . import state_machine
from .audit import AuditStore, InMemoryAuditStore
from .audit.models import EvidenceSnapshot
from .candidate import (
    CandidateEnvelope,
    CandidateStore,
    InMemoryCandidateStore,
    build_envelope,
    mint_candidate_id,
)
from .config import LearningSettings, learning_enabled
from .extractor import ExtractedCandidate, LearningExtractor
from .models import LearningStatus, compute_content_hash
from .observability import (
    consume_span,
    disabled_span,
    extract_span,
    extract_stub_span,
    triage_span,
)
from .queue import DeliveredJob, LearningQueue
from .stage import CandidateStage, StageContext
from .summary import SessionSummary, load_session_summary
from .triage import TriageVerdict
from .triage import triage as _default_triage

_logger = logging.getLogger(__name__)

# The S2 collaborators, typed for DI (all defaulted so Layer-1 fakes drop in).
SummaryLoader = Callable[..., Awaitable[SessionSummary]]
Triage = Callable[[SessionSummary], TriageVerdict]


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
        summary_loader: SummaryLoader = load_session_summary,
        triage: Triage = _default_triage,
        audit: AuditStore | None = None,
        extractor: LearningExtractor | None = None,
        candidates: CandidateStore | None = None,
        stages: tuple[CandidateStage, ...] = (),
    ) -> None:
        self._store = store
        self._queue = queue
        self._settings = settings
        self._tracer = tracer
        # S2 DI (design §5.3): the loader + triage are pure/deterministic.
        self._summary_loader = summary_loader
        self._triage = triage
        # S3 DI: the audit client now writes real evidence snapshots; the
        # extractor + candidate store are injected. When `extractor is None`
        # (e.g. unconfigured deployment, or the S2-parity tests) the KEEP path
        # falls back to the S2 `would_extract` stub — additive, nothing breaks.
        self._audit: AuditStore = audit if audit is not None else InMemoryAuditStore()
        self._extractor = extractor
        self._candidates: CandidateStore = (
            candidates if candidates is not None else InMemoryCandidateStore()
        )
        # Wave-0 seam (D102 §7.1): the ordered write-router pipeline
        # (generalize → leakage → dedup → writer). Defaulted EMPTY ⇒ the S3
        # behavior is behaviorally identical (no stage runs ⇒ no extra puts,
        # additive keys only). Builders register their stage here at the
        # composition root; the consumer never hard-codes one.
        self._stages = stages

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

        # --- Slice-2 work: load → triage → skip/keep (§5.1). Operates on the
        # already-loaded `doc` (keeps the fresh-hash source consistent with
        # MEDIUM-3) and is strictly READ-ONLY (D72) — the only writes are the two
        # lifecycle CAS transitions bracketing this call. ---
        await self._do_work(doc, delivered)

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

    async def _do_work(self, doc: SessionDoc, delivered: DeliveredJob) -> None:
        """Slice-2/3 seam (§5.1): load the `SessionSummary` (READ-ONLY, D72), run
        the deterministic triage gate, and on KEEP run the grounded extractor
        (S3). Either way the outer `_process` CAS-marks the session `done` —
        "processed" == "triaged". This method NEVER mutates the request-path
        session (D72); the only writes are to the LEARNING plane (audit +
        candidate stores)."""
        summary = await self._summary_loader(doc, self._store, job=delivered.job)
        verdict = self._triage(summary)
        self._emit_triage(summary.session_id, verdict)
        if verdict.decision != "keep":
            return
        if self._extractor is None:
            # Back-compat: no extractor wired ⇒ the S2 `would_extract` stub.
            self._emit_extract_stub(summary.session_id, verdict.target_hints)
            return
        await self._run_extractor(summary, verdict)

    async def _run_extractor(self, summary: SessionSummary, verdict: TriageVerdict) -> None:
        """S3: extract candidates → snapshot each candidate's evidence into
        `learning_audit` (the FIRST real evidence writes) → persist the candidate
        envelope (carrying only `evidence_ref`s) at `status=extracted`. A candidate
        with no evidence never reaches here (rejected at emit, D31)."""
        result = await self._extractor.extract(summary, verdict)
        # MEDIUM-3: drop any candidates a PRIOR attempt (redelivery before `done`)
        # wrote for this session, so the store never holds a mixed set from two
        # LLM runs that emitted a different count/order.
        await self._candidates.supersede(summary.content_hash)
        for ordinal, candidate in enumerate(result.candidates):
            evidence_refs = await self._snapshot_evidence(candidate, summary)
            envelope = build_envelope(
                candidate,
                summary,
                candidate_id=mint_candidate_id(summary.content_hash, ordinal),
                evidence_refs=evidence_refs,
            )
            await self._candidates.put(envelope)
            if await self._run_stages(envelope, summary, verdict) == "halt":
                break
        decline_reasons = tuple(d.reason for d in result.declines)
        self._emit_extract(
            summary.session_id,
            candidate_count=len(result.candidates),
            decline_reasons=decline_reasons,
            target_hints=verdict.target_hints,
        )

    async def _run_stages(
        self,
        envelope: CandidateEnvelope,
        summary: SessionSummary,
        verdict: TriageVerdict,
    ) -> Literal["continue", "halt"]:
        """Run the injected write-router pipeline over one freshly-`extracted`
        envelope (D102 §7.1). Returns `"halt"` if a stage asked to stop the whole
        extraction pipeline, else `"continue"`.

        EMPTY tuple ⇒ behaviorally identical S3 behavior (no extra `put`; the
        candidate was already persisted at `extracted`): the loop never runs. Only
        a WIRED stage triggers the final persist of its enriched envelope. Control
        semantics (see `stage.StageControl`): `continue` → next stage; `route_inbox`
        → stop + persist; `drop` → stop, do NOT persist; `halt` → stop + persist,
        then skip the remaining candidates. An UNKNOWN control string is a
        programming error and raises (never a silent route_inbox)."""
        if not self._stages:
            return "continue"
        ctx = StageContext(summary=summary, verdict=verdict)
        env = envelope
        persist = True
        control: str = "continue"
        for stage in self._stages:
            outcome = await stage.process(env, ctx)
            env = outcome.envelope
            control = outcome.control
            if control == "continue":
                continue
            if control in ("route_inbox", "halt"):
                persist = True
            elif control == "drop":
                # The stage committed the candidate elsewhere (or discarded it);
                # do not persist the enriched envelope here.
                persist = False
            else:
                raise ValueError(
                    f"stage {getattr(stage, 'stage_id', stage)!r} returned an "
                    f"unknown control {control!r} (expected one of continue, "
                    f"route_inbox, drop, halt)"
                )
            break
        if persist:
            await self._candidates.put(env)
        return "halt" if control == "halt" else "continue"

    async def _snapshot_evidence(
        self, candidate: ExtractedCandidate, summary: SessionSummary
    ) -> tuple[str, ...]:
        """Snapshot each cited evidence quote into `learning_audit` (D51/D95) and
        return the minted `evidence_ref`s. The entity-bearing quote lives ONLY in
        the audit store; the candidate carries only the refs (D17)."""
        refs: list[str] = []
        for ev in candidate.header.evidence:
            ref = self._audit.mint_evidence_ref(summary.session_id)
            await self._audit.snapshot(
                ref,
                EvidenceSnapshot(
                    evidence_ref=ref,
                    session_id=summary.session_id,
                    trace_id=summary.trace_id,
                    turn_ref=ev.turn_ref,
                    tool_call_ref=ev.tool_call_ref,
                    quote=ev.quote,
                    snapshotted_at=datetime.now(UTC).isoformat(),
                ),
            )
            refs.append(ref)
        return tuple(refs)

    def _emit_triage(self, session_id: str, verdict: TriageVerdict) -> None:
        if self._tracer is not None:
            with triage_span(
                self._tracer,
                session_id=session_id,
                decision=verdict.decision,
                reason=verdict.reason,
                target_hints=verdict.target_hints,
            ):
                pass

    def _emit_extract_stub(self, session_id: str, target_hints: tuple[str, ...]) -> None:
        if self._tracer is not None:
            with extract_stub_span(
                self._tracer, session_id=session_id, target_hints=target_hints
            ):
                pass

    def _emit_extract(
        self,
        session_id: str,
        *,
        candidate_count: int,
        decline_reasons: tuple[str, ...],
        target_hints: tuple[str, ...],
    ) -> None:
        if self._tracer is not None:
            with extract_span(
                self._tracer,
                session_id=session_id,
                candidate_count=candidate_count,
                decline_count=len(decline_reasons),
                decline_reasons=decline_reasons,
                target_hints=target_hints,
            ):
                pass

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
