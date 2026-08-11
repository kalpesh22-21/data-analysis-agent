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
from contextlib import nullcontext
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
from .extractor.models import BlueprintPayload, Decline
from .judge import CoverageJudge
from .models import LearningStatus, compute_content_hash
from .observability import (
    consume_span,
    context_from_traceparent,
    disabled_span,
    extract_span,
    extract_stub_span,
    inject_current_traceparent,
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

# The verbose transcript-preview length cap (D25-gated; entity-bearing).
_TRANSCRIPT_PREVIEW_LIMIT = 500

# Caps for the verbose `decline_details` attribute. Per-decline first, then a total:
# `Decline.detail` interpolates MODEL-authored strings (a slot name, a role, a type the
# model invented), so one pathological candidate must not be able to inflate a span, and
# neither must fifty ordinary ones.
_DECLINE_DETAIL_LIMIT = 300
_DECLINE_DETAILS_TOTAL_LIMIT = 2000

# Decline reason codes whose detail is known to carry a value lifted from the analyst's
# SQL rather than only a path/requirement/type. Maintained as documentation, NOT as a
# filter — see `_decline_details` for why filtering by this list would be the wrong
# guard. Today: `totality_violation`, whose message interpolates `pred.value`
# (`validation.py::_validate_totality`).
ENTITY_BEARING_DECLINE_REASONS: frozenset[str] = frozenset({"totality_violation"})


def _session_question(summary: SessionSummary) -> str | None:
    """The user's originating question (the first turn's NL). Entity-bearing —
    only ever surfaced on a span behind the verbose gate."""
    for turn in summary.turns:
        if turn.user_nl:
            return turn.user_nl
    return None


def _transcript_preview(summary: SessionSummary) -> str | None:
    """A short human-readable preview of what the chat was about (user/assistant
    lines, truncated). Entity-bearing — verbose-gated."""
    parts: list[str] = []
    for turn in summary.turns:
        if turn.user_nl:
            parts.append(f"user: {turn.user_nl}")
        if turn.assistant_text:
            parts.append(f"assistant: {turn.assistant_text}")
    if not parts:
        return None
    return " | ".join(parts)[:_TRANSCRIPT_PREVIEW_LIMIT]


def _accepted_sql(summary: SessionSummary) -> str | None:
    """The accepted SQL (the last successful runQuery). Entity-bearing —
    verbose-gated."""
    sql: str | None = None
    for tc in summary.tool_calls:
        if tc.tool_name == "runQuery" and tc.status == "ok" and tc.sql:
            sql = tc.sql
    return sql


def _blueprint_verbose(payload: BlueprintPayload) -> tuple[str | None, str | None]:
    """The learned (intent, slot-plan) of a BLUEPRINT payload for the verbose extract
    span. `slots` renders as `name→binds_to; ...` — with the slot TYPE in place of
    `binds_to` for a windowed slot, which legitimately declares none (it consumes no
    column domain; see `extractor/models.py::WINDOWED_SLOT_TYPES`). An f-string would
    have rendered the literal `None` there. Entity-bearing — verbose-gated.

    Takes the PAYLOAD, not the candidate, and no longer returns the rationale. It used to
    take a candidate of any type and return `(None, None, rationale)` for the three
    non-blueprint targets — which meant the caller's `isinstance(payload,
    BlueprintPayload)` guard skipped it entirely for a knowledge candidate and the
    rationale went with it. A session that extracted only knowledge therefore emitted a
    verbose extract span with NO human-readable attribute at all. The rationale lives on
    `CandidateHeader` for EVERY target, so the caller reads it there and this function
    answers only the question its name asks."""
    slots = (
        "; ".join(
            f"{p.slot.name}→{p.slot.binds_to or f'<{p.slot.type}>'}"
            for p in payload.parameterization
            if p.role == "slot" and p.slot is not None
        )
        or None
    )
    return payload.intent or None, slots


def _decline_details(declines: tuple[Decline, ...]) -> str | None:
    """The verbose extract span's `decline_details` — one `reason: detail` line per
    decline, or `None` when nothing was declined or no decline carried a detail.

    THE POINT: `decline_reasons` (shape-only) says `bad_role`; this says "slot pay_period
    has no binds_to (only a ['as_of_quarter', 'period'] slot may omit it)". A session that
    produced nothing and offered no readable why is the hole this closes, and a reason
    code alone does not close it — the code names a class, the detail names the fix.

    BOUNDED and FLATTENED, and not as a formality. `Decline.detail` interpolates
    MODEL-authored strings (`f"unknown role {p.role!r}"`, a slot name, a candidate type),
    which are neither a closed vocabulary nor a leakage-scanned surface: a newline in one
    would smear the attribute across the Phoenix UI and an unbounded one would put an
    arbitrary generation on a span. Same posture, and the same reason, as
    `judge/schema.py::_clean` on the judge's `reason`.

    **ENTITY-BEARING, and one detail is measurably so.** Most messages are derived from
    `(path, requirement, arrived_json_type)` and name no value. `totality_violation` is
    the exception: it interpolates `pred.value` — a literal lifted from a real analyst's
    accepted SQL (`validation.py::_validate_totality`). That is a deliberate accepted
    consequence of the D25 gate, not an oversight, and it is recorded here so the next
    reader does not have to rediscover it:

      * it changes no CLASS of content. This same span already carries
        `learning.accepted_sql` — the whole accepted query WITH its literals — under the
        SAME gate, and `pred.value` is by construction a literal out of a cited source
        query. The span was entity-bearing before this attribute existed;
      * it is therefore governed by the posture already stated on
        `observability.py::judge_span` and the module docstring: with verbose ON the
        `learning-loop` Phoenix project holds session content and MUST be
        access-controlled like `learning_audit` and the session store (D51);
      * the RIGHT fix is at the producer — that message should name the path and the
        requirement like its neighbours and drop the value, which no human debugging a
        totality violation needs. That belongs in `validation.py`, where it also cleans
        the log line the same string reaches.

    **Not filtered by reason code here, deliberately.** Excluding `totality_violation`
    from this attribute would be a guard keyed on a NAME — precisely the shape that has
    already missed this class of bug repeatedly in this package, because the next
    entity-bearing message added upstream inherits the exemption silently. It would also
    fail closed on the one thing this attribute exists for: a decline that shows no
    reason. `ENTITY_BEARING_DECLINE_REASONS` above records what is known, so a reader can
    audit it; it is documentation, not a filter.
    """
    lines = [
        f"{d.reason}: {' '.join(d.detail.split())[:_DECLINE_DETAIL_LIMIT]}"
        for d in declines
        if d.detail
    ]
    if not lines:
        return None
    return " | ".join(lines)[:_DECLINE_DETAILS_TOTAL_LIMIT]


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
        judge: CoverageJudge | None = None,
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
        # Plan §3b: the PRE-extraction coverage judge. Optional and default-absent, so a
        # deployment without one behaves exactly as it did before the slice. It sits
        # between triage and the extractor because that is the only place a drop saves
        # the extractor's call, which carries the whole session transcript.
        self._judge = judge

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
        # Rehydrate the enqueue-propagated trace context so this consume (and the
        # triage/extract spans nested under it) join the session's ONE trace. A
        # missing/malformed traceparent ⇒ None ⇒ a normal root span (fail-open).
        parent_ctx = context_from_traceparent(job.traceparent)
        doc, cas = await self._store.get_session_with_cas(job.session_id)

        # Idempotency (D96 §5.2): a re-delivery of an already-processed job
        # (session already `done` with the SAME recorded hash) → ACK + skip.
        if (
            doc.learning_status == LearningStatus.DONE
            and doc.learning_content_hash == job.content_hash
        ):
            await self._queue.ack(delivered.message_id)
            self._emit_consume(
                job.session_id, "dedup_skip", delivered.delivery_count, context=parent_ctx
            )
            return "dedup_skip"

        try:
            cas = await state_machine.transition(
                self._store, job.session_id, LearningStatus.QUEUED,
                LearningStatus.PROCESSING, cas,
            )
        except CASMismatchError:
            # A peer is handling it, or the state isn't `queued` — do NOT ack;
            # leave the message for the owner / a later reclaim. No consume span
            # (nothing was processed).
            return "skip"

        # Open the consume span (session-trace continuation, under the propagated
        # context) as the PARENT of the triage/extract spans, so a session's whole
        # journey reads as ONE trace top-to-bottom. No tracer ⇒ nullcontext (no-op).
        with self._consume_scope(
            job.session_id, delivered.delivery_count, parent_ctx
        ) as consume:
            # --- Slice-2 work: load → triage → skip/keep (§5.1). Operates on the
            # already-loaded `doc` (fresh-hash source consistent with MEDIUM-3) and
            # is strictly READ-ONLY (D72) — the only writes are the two lifecycle
            # CAS transitions bracketing this call. ---
            summary = await self._do_work(doc, delivered)

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
                if consume is not None:
                    consume.set_attribute("learning.outcome", "skip")
                return "skip"

            await self._queue.ack(delivered.message_id)
            self._set_verbose_consume(consume, summary)
        return "done"

    def _consume_scope(self, session_id: str, delivery_count: int, parent_ctx):
        """The consume span (default `outcome=done`, overridden to `skip` on the rare
        DONE-CAS race) OR a `nullcontext(None)` when no tracer is wired, so the
        no-op-tracer behavior is byte-identical."""
        if self._tracer is None:
            return nullcontext(None)
        return consume_span(
            self._tracer,
            session_id=session_id,
            outcome="done",
            delivery_count=delivery_count,
            context=parent_ctx,
        )

    def _set_verbose_consume(self, consume, summary: SessionSummary) -> None:
        """D25-gated: attach the user question + a transcript preview to the consume
        span ONLY when verbose is on (entity-bearing — see observability docstring)."""
        if consume is None or not self._settings.learning_trace_verbose:
            return
        question = _session_question(summary)
        if question is not None:
            consume.set_attribute("learning.question", question)
        preview = _transcript_preview(summary)
        if preview is not None:
            consume.set_attribute("learning.transcript_preview", preview)

    async def _handle_dead_letter(self, delivered: DeliveredJob) -> str:
        """A message past N deliveries. Ordering (MEDIUM-3/4): CAS the session to
        `dead_letter` FIRST, then `finalize_dead_letter` (XADD-dead + XACK).
        Returns `dead_letter` | `ack_terminal` | `skip`."""
        job = delivered.job
        parent_ctx = context_from_traceparent(job.traceparent)
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
        self._emit_consume(
            job.session_id, "dead_letter", delivered.delivery_count, context=parent_ctx
        )
        return "dead_letter"

    async def _do_work(self, doc: SessionDoc, delivered: DeliveredJob) -> SessionSummary:
        """Slice-2/3 seam (§5.1): load the `SessionSummary` (READ-ONLY, D72), run
        the deterministic triage gate, and on KEEP run the grounded extractor
        (S3). Either way the outer `_process` CAS-marks the session `done` —
        "processed" == "triaged". This method NEVER mutates the request-path
        session (D72); the only writes are to the LEARNING plane (audit +
        candidate stores). Returns the loaded summary so `_process` can attach the
        verbose consume attrs."""
        summary = await self._summary_loader(doc, self._store, job=delivered.job)
        verdict = self._triage(summary)
        self._emit_triage(summary, verdict)
        if verdict.decision != "keep":
            return summary
        if self._extractor is None:
            # Back-compat: no extractor wired ⇒ the S2 `would_extract` stub.
            self._emit_extract_stub(summary.session_id, verdict.target_hints)
            return summary
        if await self._judged_covered(summary):
            return summary
        await self._run_extractor(summary, verdict)
        return summary

    async def _judged_covered(self, summary: SessionSummary) -> bool:
        """Ask the coverage judge whether this session's work already exists (plan
        §3b). `True` ⇒ SKIP extraction entirely.

        Placed after the extractor-present check on purpose: with no extractor there is
        no call to cancel, so paying a judge to cancel nothing would be pure cost — and
        would drop a session on the ONE path that never spends money anyway.

        NEVER raises. The judge is documented fail-open at every internal boundary, but
        this call site is what makes that a guarantee rather than an intention: an
        unforeseen escape (a Protocol violation by an injected audit store, a bug in the
        judge itself) must degrade to "extract as usual", not dead-letter the session
        through the consumer's blanket handler. The direction matters — a wrong keep
        costs one extraction and lands in a review queue; a wrong drop is invisible.
        """
        if self._judge is None:
            return False
        try:
            outcome = await self._judge.screen_session(summary)
        except Exception:  # noqa: BLE001 - the judge may never cost a session
            _logger.warning(
                "learning: the coverage judge raised for session %s — extracting "
                "anyway (fail-open). This is a judge bug: every internal failure path "
                "is supposed to be handled inside it.",
                summary.session_id,
                exc_info=True,
            )
            return False
        if not outcome.drop:
            return False
        # The judge has already written the durable audit record (it refuses to drop
        # without one) and logged the drop with its reason and covered-by ref. The
        # extract span is emitted with zero candidates and a machine-readable decline
        # reason so the loop's own telemetry shows a session that produced nothing AND
        # why, rather than a silent gap between triage and nothing.
        self._emit_extract(
            summary,
            candidate_count=0,
            decline_reasons=("judge_prior_art_covered",),
            target_hints=(),
        )
        return True

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
        # Capture the CURRENT trace context (the open consume span) as a W3C
        # traceparent and stamp it onto each envelope, so the cron scheduler's
        # promote/land spans continue this SAME session trace. None when no tracer.
        traceparent = inject_current_traceparent() if self._tracer is not None else None
        intent = slots = rationale = None
        for ordinal, candidate in enumerate(result.candidates):
            evidence_refs = await self._snapshot_evidence(candidate, summary)
            envelope = build_envelope(
                candidate,
                summary,
                candidate_id=mint_candidate_id(summary.content_hash, ordinal),
                evidence_refs=evidence_refs,
                traceparent=traceparent,
            )
            await self._candidates.put(envelope)
            # The rationale comes off the HEADER, which every target has — see
            # `_blueprint_verbose`. Reading it only inside the blueprint branch left a
            # knowledge-only extraction with a verbose span carrying nothing readable.
            if rationale is None:
                rationale = candidate.header.rationale or None
            if intent is None and isinstance(candidate.payload, BlueprintPayload):
                intent, slots = _blueprint_verbose(candidate.payload)
            if await self._run_stages(envelope, summary, verdict) == "halt":
                break
        decline_reasons = tuple(d.reason for d in result.declines)
        self._emit_extract(
            summary,
            candidate_count=len(result.candidates),
            decline_reasons=decline_reasons,
            target_hints=verdict.target_hints,
            correction_count=result.corrections,
            intent=intent,
            slots=slots,
            rationale=rationale,
            decline_details=_decline_details(result.declines),
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

    def _emit_triage(self, summary: SessionSummary, verdict: TriageVerdict) -> None:
        if self._tracer is None:
            return
        verbose = self._settings.learning_trace_verbose
        with triage_span(
            self._tracer,
            session_id=summary.session_id,
            decision=verdict.decision,
            reason=verdict.reason,
            target_hints=verdict.target_hints,
            verbose=verbose,
            question=_session_question(summary) if verbose else None,
            transcript_preview=_transcript_preview(summary) if verbose else None,
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
        summary: SessionSummary,
        *,
        candidate_count: int,
        decline_reasons: tuple[str, ...],
        target_hints: tuple[str, ...],
        correction_count: int = 0,
        intent: str | None = None,
        slots: str | None = None,
        rationale: str | None = None,
        decline_details: str | None = None,
    ) -> None:
        if self._tracer is None:
            return
        verbose = self._settings.learning_trace_verbose
        with extract_span(
            self._tracer,
            session_id=summary.session_id,
            candidate_count=candidate_count,
            decline_count=len(decline_reasons),
            decline_reasons=decline_reasons,
            target_hints=target_hints,
            correction_count=correction_count,
            verbose=verbose,
            accepted_sql=_accepted_sql(summary) if verbose else None,
            intent=intent if verbose else None,
            slots=slots if verbose else None,
            rationale=rationale if verbose else None,
            decline_details=decline_details if verbose else None,
        ):
            pass

    def _emit_consume(
        self, session_id: str, outcome: str, delivery_count: int, *, context=None
    ) -> None:
        if self._tracer is not None:
            with consume_span(
                self._tracer,
                session_id=session_id,
                outcome=outcome,
                delivery_count=delivery_count,
                context=context,
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
