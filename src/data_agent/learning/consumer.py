"""LearningConsumer — the no-op Slice-1 consumer (D96 §5/§7, design §2).

Every cycle: read the kill-switch FRESH; if enabled, first reclaim stale PEL
entries (dead-lettering any past N deliveries), then XREADGROUP new work. For
each delivered job:

  idempotency check (already-`done` + same `content_hash` → ACK + skip)
    → CLAIM decision (`_claim_decision`: the delivered session's CURRENT status ×
      the delivery path → forward / recover / refuse / terminal)
    → CAS `queued → processing`   (or a `processing`/`done` recovery re-entry)
    → **no-op work + trace event**   ← Slice 2 replaces this middle with the
                                        loader + triage + extractor
    → CAS `processing → done`  (records a FRESHLY computed content_hash)
    → XACK

Every non-`done` outcome LOGS what it saw and emits a `learning.consume` span
carrying the state it refused from (`learning.session_status`) and a reason code
(`learning.skip_reason`). This is not decoration: the claim used to be a bare
`queued → processing` CAS whose failure returned `"skip"` with no ack, no log, no
span and no metric, so a session in any other state produced a message that was
reclaimed and re-refused until it dead-lettered by attrition — invisibly. See
`_claim_decision` for the table and for which states are recoverable.

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
from dataclasses import dataclass, replace
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
    build_declined_envelope,
    build_envelope,
    mint_candidate_id,
    mint_review_candidate_id,
)
from .config import LearningSettings, learning_enabled
from .extractor import ExtractedCandidate, LearningExtractor
from .extractor.models import BlueprintPayload, Decline, EvidenceRef
from .extractor.validation import REASON_RULE_MISMATCH, REASON_TOTALITY, read_evidence
from .judge import OUTCOME_PROCEEDED, CoverageJudge, JudgeOutcomeResult
from .leakage import settle_entity_scan
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
from .stage import CandidateStage, StageContext, run_pipeline
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

# Decline reason codes whose detail is known to carry a value that did NOT come from a
# path/requirement/type — a literal out of the analyst's SQL, or a string the model
# authored from an entity-bearing session. Maintained as documentation, NOT as a filter
# — see `_decline_details` for why filtering by this list would be the wrong guard.
#
#   `totality_violation`      the predicates it names carry `pred.value`, lifted from
#                             the accepted SQL (`validation.py::_predicate_hint`).
#   `rule_predicate_mismatch` same, plus the catalog rule's own predicate text
#                             (which is authored, not session-derived).
#   `missing_rule_hinted`     the rule id the MODEL wrote, which a model that read an
#                             entity-bearing session can have derived from it
#                             (`validation.py::_rule_hint`). The terminal `missing_rule`
#                             interpolates the same field and is listed for the same
#                             reason — this classification is about the field, not about
#                             which branch printed it.
#
# All three are rendered through `validation.py::_quoted`/`_flattened` at their build
# site, so what lands here is single-line and bounded — that is a safety property of the
# ATTRIBUTE, not a substitute for this classification.
#
# **THE SPAN IS NO LONGER THE ONLY LANDING SURFACE, and this is the note that says so.**
# Since the fail-to-review slice, the detail of a `totality_violation` /
# `rule_predicate_mismatch` decline on a MERIT-PASSED candidate is also DURABLY PERSISTED
# — onto `CandidateEnvelope.decline` in the `learning_candidates` store — because the
# text IS the reviewer's task (`docs/decisions/learning-declined-candidate-review.md`).
# Three things make that a decided posture rather than a drift:
#
#   * the leakage scan runs over the envelope BEFORE it is persisted, so a decline is not
#     a side door around the entity gate (`_persist_declined_for_review`);
#   * `learning_candidates` is the access-controlled store (D101) that already holds the
#     candidate payload's raw locator values — this adds no new class of content to it;
#   * the WIRE layer withholds the detail unless the persisted leakage verdict is a clean
#     `pass` (`inbox/models.py::InboxItem.decline_view`), so the browser-facing surface is
#     narrower than the stored one, not equal to it.
#
# The two reasons that do NOT route to review (`missing_rule_hinted`, `missing_rule`)
# keep the span as their only surface.
ENTITY_BEARING_DECLINE_REASONS: frozenset[str] = frozenset(
    {"totality_violation", "rule_predicate_mismatch", "missing_rule_hinted", "missing_rule"}
)

# The decline reasons that route a MERIT-PASSED candidate to a human instead of the bin
# (`docs/decisions/learning-declined-candidate-review.md`).
#
# Both members say the same thing about the candidate: the IDEA survived every judgement
# of its content and the FORM could not be filled in. `totality_violation` means a
# predicate of the accepted SQL has no entry; `rule_predicate_mismatch` means an entry
# cites a rule the catalog says is a different filter. A human fixes either in seconds.
#
# WHAT IS DELIBERATELY ABSENT, because the temptation is to add it:
#
#   * `missing_rule` — terminal, and its whole value is as the §7 signal that a human
#     must ADD a rule to the catalog. Routing it here would answer it with the wrong
#     action (complete a form that has no legal completion) and blur the count the
#     pairing work is prioritized from. The decision doc leaves it open; it stays out
#     until that count question is settled.
#   * every merit-failed reason (`no_evidence`, `no_acceptance`, `unrewritable_sql`,
#     `role_inconsistent`, `malformed_candidate`) — these are supposed to die, and a
#     review queue that fills with them stops being read.
REVIEW_ROUTED_DECLINE_REASONS: frozenset[str] = frozenset(
    {REASON_TOTALITY, REASON_RULE_MISMATCH}
)


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
    """The accepted SQL: the LAST query the answer designated (`answerWithTable`),
    else the last successful runQuery. Entity-bearing — verbose-gated.

    The designation wins because the span answers "what did this session accept?" and
    that is what the user was shown — which since Release 1 need never have been
    dispatched as a runQuery at all, so reading only ok calls left the one query that
    mattered out of the trace and put an intermediate probe in its place."""
    if summary.answer_sqls:
        return summary.answer_sqls[-1].sql
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
      * this note used to say the RIGHT fix was at the producer — drop the value and
        name the path like the message's neighbours. That is no longer the plan, and the
        reversal is deliberate: `totality_violation` is now a CORRECTABLE decline whose
        message is fed back to the model, and the predicate it names (column, operator,
        literal) is the entire content of the fix. Dropping the value would make the
        message unusable for the thing it now exists to do, and it would buy nothing —
        the literal comes from the accepted SQL, which the extractor's own prompt
        carries in full and which this same span carries in `learning.accepted_sql`.
        `validation.py::_correctable` states the rule the message obeys instead.

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


# What one delivery may do with the session it names, given that session's CURRENT
# `learning_status` and the DELIVERY PATH the message arrived on. The four claim kinds
# map onto the outcome taxonomy: `forward`/`recover` run the work (→ `done`), `refuse`
# leaves the message in the PEL (→ `skip`), `terminal` ACKs it away (→ `ack_terminal`).
ClaimKind = Literal["forward", "recover", "refuse", "terminal"]


def _claim_decision(status: str, *, reclaimed: bool) -> tuple[ClaimKind, str]:
    """THE skip-path table: `(learning_status, delivery path) → (claim, reason)`.

    Pure and module-level so it can be read and tested as a table rather than inferred
    from control flow. `done` + the SAME `content_hash` never reaches here — `_process`
    ACKs that as `dedup_skip` before asking.

      status         fresh delivery                    reclaimed delivery
      -------------- --------------------------------- --------------------------------
      queued         forward   (queued)                 forward   (queued)
      processing     refuse    (owner_may_be_live)      recover   (reclaimed_processing)
      done (≠hash)   recover   (content_changed)        recover   (content_changed)
      active         refuse    (not_yet_claimable)      refuse    (not_yet_claimable)
      pending        refuse    (not_yet_claimable)      refuse    (not_yet_claimable)
      dead_letter    terminal  (dead_letter)            terminal  (dead_letter)
      <anything else> terminal (unknown_status)         terminal  (unknown_status)

    WHY each row:

    `processing` — a session left `processing` by an owner that crashed mid-run can be
    finished by nobody unless a later delivery is allowed to re-enter it, and XAUTOCLAIM
    re-assigning the message IS the transport saying "this is now yours". On a FRESH
    delivery the same state means something else entirely: a peer picked the message up
    seconds ago and is working. Hence the delivery path, not the state, decides.

    THE DISCRIMINATOR, and why it is the delivery path and not `delivery_count > 1`:
    the two are equivalent under both shipped queues (XREADGROUP `>` only yields
    never-before-delivered entries, so a fresh delivery is always `delivery_count == 1`),
    but the equivalence is a property of Redis Streams, not of the `LearningQueue` port
    — a queue that redelivered by any other route would silently flip the meaning of the
    derived form. `DeliveredJob.reclaimed` states the fact directly.

    AND WHY NEITHER IS A STALENESS PROOF: min-idle (`LEARNING_RECLAIM_MIN_IDLE_SECONDS`,
    300s) does NOT guarantee the previous owner is dead — Redis resets a PEL entry's
    idle clock on DELIVERY, not on the owner's progress, so a consumer legitimately
    grinding through a long extraction has its own message reclaimed out from under it.
    What makes the re-entry safe is the CAS: the token comes from a fresh read taken
    microseconds earlier, so if the live owner writes (its `processing → done`) either
    it or this re-entry loses, and the loser takes the ordinary `CASMismatchError` skip.
    The cost of the rare double-run is one duplicated extraction, absorbed by
    `CandidateStore.supersede(content_hash)`; the cost of NOT re-entering is a session
    that no delivery can ever finish.

    `done` with a DIFFERENT hash — the session was processed and then gained turns, so
    what was recorded at `done` no longer describes the transcript. This is the LIVE
    signature behind the silent stall: the consumer records a FRESHLY computed hash at
    `done` (MEDIUM-3), so a session that changed between enqueue and consume ends up
    `done` with a hash the in-flight message never carried; a redelivery of that message
    then matched neither the dedup check nor the `queued` gate and skipped forever.

    `active`/`pending` — REFUSE, and deliberately WITHOUT an ack. Both are states the
    sweeper still owns (`SWEEPABLE_STATUSES`): a `pending` session is re-detected and
    driven to `queued` on a later sweep, at which point a reclaim of this very message
    processes it. Acking here would drop work the sweeper is about to make claimable.

    `dead_letter` and any UNRECOGNIZED status — TERMINAL, ack. Neither can become
    `queued` again: `dead_letter` is terminal by design, and an unknown status string is
    outside the state machine entirely (nothing in this package writes one — it means a
    hand-edited doc or a schema skew) so no sweeper will ever pick it up. Leaving the
    message in the PEL just re-runs this decision every reclaim until the delivery count
    exhausts and it dead-letters by attrition — which is the CURRENT behaviour and the
    least informative possible outcome. The ack is paired with a WARNING log and an
    `ack_terminal` span so the discard is loud and countable rather than a slow silent
    burn. (The alternative — CAS the session to `dead_letter` and XADD the message to
    the dead stream — was rejected for the unknown-status case: it would overwrite the
    very evidence an operator needs, and for `dead_letter` the session is already there.)
    """
    if status == LearningStatus.QUEUED:
        return "forward", "queued"
    if status == LearningStatus.PROCESSING:
        if reclaimed:
            return "recover", "reclaimed_processing"
        return "refuse", "processing_owner_may_be_live"
    if status == LearningStatus.DONE:
        # Same-hash was ACKed as `dedup_skip` upstream; reaching here means the
        # session's content changed after it was processed.
        return "recover", "done_content_changed"
    if status in (LearningStatus.ACTIVE, LearningStatus.PENDING):
        return "refuse", "not_yet_claimable"
    if status == LearningStatus.DEAD_LETTER:
        return "terminal", "dead_letter"
    return "terminal", "unknown_status"


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
            # The residual arm: `_process` handles its OWN CAS losses (logged + spanned
            # there), so what lands here is a loss from the dead-letter path or a
            # `get_session_with_cas` on a session that no longer exists (TTL expiry).
            # Logged because it was the second silent skip in this file.
            _logger.info(
                "learning consume lost a CAS race for message %s (session %s, "
                "delivery %d); leaving it for reclaim",
                delivered.message_id, delivered.job.session_id, delivered.delivery_count,
            )
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
        (`done` | `dedup_skip` | `skip` | `ack_terminal`)."""
        job = delivered.job
        # Rehydrate the enqueue-propagated trace context so this consume (and the
        # triage/extract spans nested under it) join the session's ONE trace. A
        # missing/malformed traceparent ⇒ None ⇒ a normal root span (fail-open).
        parent_ctx = context_from_traceparent(job.traceparent)
        doc, cas = await self._store.get_session_with_cas(job.session_id)
        status = doc.learning_status

        # Idempotency (D96 §5.2): a re-delivery of an already-processed job
        # (session already `done` with the SAME recorded hash) → ACK + skip.
        if status == LearningStatus.DONE and doc.learning_content_hash == job.content_hash:
            await self._queue.ack(delivered.message_id)
            self._emit_consume(
                job.session_id,
                "dedup_skip",
                delivered.delivery_count,
                context=parent_ctx,
                session_status=status,
                reclaimed=delivered.reclaimed,
            )
            return "dedup_skip"

        # May this delivery claim the session? (`_claim_decision` — the whole
        # state × delivery-path table lives there, with the log/span vocabulary.)
        claim, reason = _claim_decision(status, reclaimed=delivered.reclaimed)

        if claim == "terminal":
            # The state can never become `queued` again, so no future redelivery can
            # help: ACK, or the message rides the PEL until it dead-letters by
            # attrition (which is exactly what used to happen, silently).
            _logger.warning(
                "learning consume ACKing a terminal delivery for session %s: message %s "
                "(delivery %d, reclaimed=%s) names a session in learning_status %r "
                "(reason=%s) — nothing can move it to `queued`, so the message is "
                "discarded rather than redelivered until it dead-letters",
                job.session_id, delivered.message_id, delivered.delivery_count,
                delivered.reclaimed, status, reason,
            )
            await self._queue.ack(delivered.message_id)
            self._emit_consume(
                job.session_id,
                "ack_terminal",
                delivered.delivery_count,
                context=parent_ctx,
                session_status=status,
                skip_reason=reason,
                reclaimed=delivered.reclaimed,
            )
            return "ack_terminal"

        if claim == "refuse":
            self._log_skip(job.session_id, delivered, status, reason)
            self._emit_consume(
                job.session_id,
                "skip",
                delivered.delivery_count,
                context=parent_ctx,
                session_status=status,
                skip_reason=reason,
                reclaimed=delivered.reclaimed,
            )
            return "skip"

        try:
            if claim == "forward":
                cas = await state_machine.transition(
                    self._store, job.session_id, LearningStatus.QUEUED,
                    LearningStatus.PROCESSING, cas,
                )
            else:  # "recover" — `processing`/`done` re-entry (models.RECOVERY_TRANSITIONS)
                cas = await state_machine.recover_to_processing(
                    self._store, job.session_id, status, cas
                )
        except CASMismatchError:
            # A peer wrote since our read (it owns the claim, or the session was
            # advanced under us) — do NOT ack; leave the message for the owner / a
            # later reclaim. This is the arm that keeps the `processing` re-entry
            # above safe against a STILL-LIVE owner: its write bumps the CAS and this
            # transition loses.
            self._log_skip(job.session_id, delivered, status, "cas_lost")
            self._emit_consume(
                job.session_id,
                "skip",
                delivered.delivery_count,
                context=parent_ctx,
                session_status=status,
                skip_reason="cas_lost",
                reclaimed=delivered.reclaimed,
            )
            return "skip"

        # Open the consume span (session-trace continuation, under the propagated
        # context) as the PARENT of the triage/extract spans, so a session's whole
        # journey reads as ONE trace top-to-bottom. No tracer ⇒ nullcontext (no-op).
        with self._consume_scope(
            job.session_id,
            delivered.delivery_count,
            parent_ctx,
            session_status=status,
            reclaimed=delivered.reclaimed,
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
                self._log_skip(job.session_id, delivered, status, "cas_lost_at_done")
                if consume is not None:
                    consume.set_attribute("learning.outcome", "skip")
                    consume.set_attribute("learning.skip_reason", "cas_lost_at_done")
                return "skip"

            await self._queue.ack(delivered.message_id)
            self._set_verbose_consume(consume, summary)
        return "done"

    def _log_skip(
        self, session_id: str, delivered: DeliveredJob, status: str, reason: str
    ) -> None:
        """EVERY non-processing outcome says what it saw, at INFO.

        A skip is an ordinary, expected event under concurrency, so it is not a
        warning — but it was previously not ANYTHING: no log, no span, no metric, while
        the message sat in the PEL being reclaimed toward a dead letter. The three facts
        here are the ones that were being discarded: the session, the state the claim
        was refused FROM, and how many deliveries it has burned (the distance to
        `LEARNING_MAX_DELIVERIES`, i.e. how close this is to dead-lettering)."""
        _logger.info(
            "learning consume SKIPPED session %s: message %s (delivery %d, "
            "reclaimed=%s) found learning_status=%r, reason=%s — not ACKed, left in "
            "the PEL for the owner or a later reclaim",
            session_id, delivered.message_id, delivered.delivery_count,
            delivered.reclaimed, status, reason,
        )

    def _consume_scope(
        self,
        session_id: str,
        delivery_count: int,
        parent_ctx,
        *,
        session_status: str,
        reclaimed: bool,
    ):
        """The consume span (default `outcome=done`, overridden to `skip` on the rare
        DONE-CAS race) OR a `nullcontext(None)` when no tracer is wired, so the
        no-op-tracer behavior is byte-identical.

        `session_status` is the state the claim was made FROM — `queued` on the ordinary
        path, `processing`/`done` on a recovery re-entry — so a Phoenix filter can tell
        the two apart without reading logs."""
        if self._tracer is None:
            return nullcontext(None)
        return consume_span(
            self._tracer,
            session_id=session_id,
            outcome="done",
            delivery_count=delivery_count,
            context=parent_ctx,
            session_status=session_status,
            reclaimed=reclaimed,
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
            self._emit_consume(
                job.session_id,
                "ack_terminal",
                delivered.delivery_count,
                context=parent_ctx,
                session_status=doc.learning_status,
                skip_reason="already_terminal",
                reclaimed=delivered.reclaimed,
            )
            return "ack_terminal"

        try:
            await state_machine.transition(
                self._store, job.session_id, doc.learning_status,
                LearningStatus.DEAD_LETTER, cas, assert_from=False,
            )
        except CASMismatchError:
            # Peer advanced it — leave in the PEL for a later reclaim.
            self._log_skip(
                job.session_id, delivered, doc.learning_status, "cas_lost_at_dead_letter"
            )
            self._emit_consume(
                job.session_id,
                "skip",
                delivered.delivery_count,
                context=parent_ctx,
                session_status=doc.learning_status,
                skip_reason="cas_lost_at_dead_letter",
                reclaimed=delivered.reclaimed,
            )
            return "skip"

        # Session is terminal now; complete the queue-side move. A crash before
        # this leaves the message in the PEL → reclaimed → terminal → ack_terminal.
        await self._queue.finalize_dead_letter(delivered)
        self._emit_consume(
            job.session_id,
            "dead_letter",
            delivered.delivery_count,
            context=parent_ctx,
            session_status=doc.learning_status,
            reclaimed=delivered.reclaimed,
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
        judged = await self._judge_session(summary)
        if judged.drop:
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
            return summary
        await self._run_extractor(summary, verdict, judged)
        return summary

    async def _judge_session(self, summary: SessionSummary) -> JudgeOutcomeResult:
        """Ask the coverage judge whether this session's work already exists (plan
        §3b). `drop=True` ⇒ the caller must SKIP extraction entirely.

        **Returns the whole result, not a bool, and the difference is a slice.** The
        `outcome` label is the only record of WHY a judgement did not drop — it proceeded
        on merit, or it never ran (no cards, below the floor, unavailable, failed) — and
        the fail-to-review route is allowed for exactly one of those values
        (`proceeded`). Collapsing them to `drop=False` at this boundary, which is what
        this method used to do, would leave the extractor's declines with no way to tell
        "a model said this work is genuinely new" from "nobody was asked", and a review
        queue that cannot tell them apart is a queue of unscreened declines.

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
        # `not_judged` — the default — is the honest label for BOTH no-judge postures
        # (none wired, or one that raised): nobody screened this session, which is
        # exactly what the fail-to-review route must not mistake for a positive verdict.
        if self._judge is None:
            return JudgeOutcomeResult()
        try:
            return await self._judge.screen_session(summary)
        except Exception:  # noqa: BLE001 - the judge may never cost a session
            _logger.warning(
                "learning: the coverage judge raised for session %s — extracting "
                "anyway (fail-open). This is a judge bug: every internal failure path "
                "is supposed to be handled inside it.",
                summary.session_id,
                exc_info=True,
            )
            return JudgeOutcomeResult()

    async def _run_extractor(
        self,
        summary: SessionSummary,
        verdict: TriageVerdict,
        judged: JudgeOutcomeResult | None = None,
    ) -> None:
        """S3: extract candidates → snapshot each candidate's evidence into
        `learning_audit` (the FIRST real evidence writes) → persist the candidate
        envelope (carrying only `evidence_ref`s) at `status=extracted`. A candidate
        with no evidence never reaches here (rejected at emit, D31).

        *judged* is the PRE-extraction judgement (`_judge_session`), carried in because it
        is the only thing that can say a decline belongs in front of a human rather than
        in the bin — see `_persist_declined_for_review`. Defaulted `None` so the S3-era
        callers (and every test that drives this method directly) keep the pre-slice
        behaviour: no judgement, no review route."""
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
        review_count = await self._persist_declined_for_review(
            result.declines, summary, verdict, judged, traceparent
        )
        decline_reasons = tuple(d.reason for d in result.declines)
        self._emit_extract(
            summary,
            candidate_count=len(result.candidates),
            decline_reasons=decline_reasons,
            target_hints=verdict.target_hints,
            correction_count=result.corrections,
            review_count=review_count,
            intent=intent,
            slots=slots,
            rationale=rationale,
            decline_details=_decline_details(result.declines),
        )

    async def _persist_declined_for_review(
        self,
        declines: tuple[Decline, ...],
        summary: SessionSummary,
        verdict: TriageVerdict,
        judged: JudgeOutcomeResult | None,
        traceparent: str | None,
    ) -> int:
        """Persist a MERIT-PASSED, form-failed decline for a human to complete. Returns
        how many were written (0 or 1).

        THE BUG THIS CLOSES is silent loss, and it is worth stating precisely because the
        code reads like an addition rather than a fix. `validation.py`'s stated doctrine
        is that a decline "routes to review, never a bad landing" — but a terminal decline
        after corrections wrote NOTHING durable: no candidate, no inbox row, no audit
        record beyond a span. One traced session proved a blueprint the corpus wanted
        evaporating three times over two days
        (`docs/decisions/learning-declined-candidate-review.md`). Precision was never the
        problem; recall was being eaten by a form that sometimes has no fillable answer.

        TWO CONDITIONS, and both are necessary:

          * the pre-extraction judge said `proceeded` — a POSITIVE statement that a model
            looked at the corpus and found this work new. Every other outcome, including
            the `skipped_*` ones, means nobody screened it, and a review queue filled with
            unscreened declines is a queue that stops being read. `judged is None` (the S3
            callers, the tests that drive `_run_extractor` directly) is the same answer.
          * the reason is one a human can act on (`REVIEW_ROUTED_DECLINE_REASONS`).

        ONE PERSIST PER EXTRACTION, taking the LAST qualifying decline. The bound is the
        decision doc's (§6) and it is about the queue, not about storage: a session that
        produces several unfillable forms is a prompt problem, and putting each of them in
        front of a person is how the surface earns its own neglect. The count is on the
        span either way, and the LAST one is the model's final word.

        THE LEAKAGE SCAN RUNS FIRST, over the built envelope, using the SAME wired stage
        instance the extraction pipeline uses — never a private scanner, so the gate a
        declined candidate passes can never drift from the one a kept candidate passes.
        With no stage wired the envelope keeps its `pending` sentinel and every downstream
        surface fails closed on it (the wire withholds the detail, the approve guards
        refuse); that is a degraded review item, not a leak.

        THE EVIDENCE IS SNAPSHOTTED, exactly as it is for a kept candidate. This row is a
        durable candidate now, and a candidate whose citations resolve to nothing is one
        no reviewer can audit and no landing can be traced back — the D31 evidence
        contract does not become optional because the form was incomplete. The quotes go
        to `learning_audit` and only the minted refs travel on the envelope (D51/D17),
        which is the same split every other candidate obeys."""
        if judged is None or judged.outcome != OUTCOME_PROCEEDED:
            return 0
        eligible = [
            d
            for d in declines
            if d.reason in REVIEW_ROUTED_DECLINE_REASONS and d.raw_payload is not None
        ]
        if not eligible:
            return 0
        if len(eligible) > 1:
            _logger.info(
                "learning: session %s produced %d review-routable declines; persisting "
                "the last one only (one review item per extraction — see the decision "
                "doc §6). Reasons: %s",
                summary.session_id, len(eligible), ", ".join(d.reason for d in eligible),
            )
        decline = eligible[-1]
        envelope = build_declined_envelope(
            decline,
            summary,
            # A namespace of its own so a declined item can never collide with a kept
            # sibling minted from a different count over the same batch.
            candidate_id=mint_review_candidate_id(summary.content_hash, 0),
            evidence_refs=await self._snapshot_quotes(
                read_evidence(decline.raw_payload or {}), summary
            ),
            judge=judged.assessment,
            traceparent=traceparent,
        )
        envelope = await self._scan_declined(envelope, summary, verdict)
        await self._candidates.put(envelope)
        _logger.info(
            "learning: session %s declined %s after %d correction(s) but the judge "
            "passed it on merit — persisted %s at status=%s for a human to complete "
            "the parameterization",
            summary.session_id, decline.reason, decline.corrections_attempted,
            envelope.candidate_id, envelope.status,
        )
        return 1

    async def _scan_declined(
        self,
        envelope: CandidateEnvelope,
        summary: SessionSummary,
        verdict: TriageVerdict,
    ) -> CandidateEnvelope:
        """Stamp the settled leakage verdict onto a fail-to-review envelope.

        Runs the WIRED gate's SCAN — `leakage.settle_entity_scan`, which calls
        `LeakageGateStage.scan` rather than `process`, so what comes back is a verdict and
        nothing else. Two consequences are deliberately not taken:

          * the stage's ROUTING (a `reject` sets `status=rejected`) — written for a
            candidate flowing toward a landing, where this one is flowing toward a form.
            A quarantine must leave the row exactly where a reviewer will find it;
          * the stage's WRITES. `_apply` commits a per-user knowledge record on a
            `reroute`, and doing that here would take an entity out of a candidate that
            FAILED validation, may never be completed, and may be rejected outright, and
            commit it to a user's durable store with nothing to retract it. On the
            validated path that commit is the gate doing its job; here it would be the
            gate doing a job nobody asked for.

        No leakage stage wired ⇒ the `pending` sentinel survives and is persisted as-is.
        Logged at WARNING because it is invisible otherwise and it degrades a real
        reviewer surface: every consumer of an unsettled scan fails closed."""
        if not any(getattr(s, "stage_id", "") == "leakage" for s in self._stages):
            _logger.warning(
                "learning: no leakage stage wired — the fail-to-review candidate for "
                "session %s is persisted with an UNSETTLED entity scan; its decline "
                "detail is withheld at the wire and it cannot be approved until a "
                "deployment with the gate re-processes the session",
                summary.session_id,
            )
        entity_scan = await settle_entity_scan(
            self._stages, envelope, StageContext(summary=summary, verdict=verdict)
        )
        return replace(envelope, entity_scan=entity_scan)

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
        programming error and raises (never a silent route_inbox).

        The loop itself lives in `stage.run_pipeline`, shared with the inbox's
        parameterization-completion path — which re-runs the SAME pipeline over a
        candidate a human finished filling in, and must not be able to disagree with this
        one about what a control means. What stays here is the consumer's own half: the
        store to persist into, and the halt that skips the remaining candidates."""
        if not self._stages:
            return "continue"
        outcome = await run_pipeline(
            self._stages, envelope, StageContext(summary=summary, verdict=verdict)
        )
        if outcome.persist:
            await self._candidates.put(outcome.envelope)
        return "halt" if outcome.control == "halt" else "continue"

    async def _snapshot_evidence(
        self, candidate: ExtractedCandidate, summary: SessionSummary
    ) -> tuple[str, ...]:
        """Snapshot a VALIDATED candidate's cited quotes (D51/D95)."""
        return await self._snapshot_quotes(candidate.header.evidence, summary)

    async def _snapshot_quotes(
        self, evidence: tuple[EvidenceRef, ...], summary: SessionSummary
    ) -> tuple[str, ...]:
        """Snapshot each cited evidence quote into `learning_audit` (D51/D95) and
        return the minted `evidence_ref`s. The entity-bearing quote lives ONLY in
        the audit store; the candidate carries only the refs (D17).

        Takes the CITATIONS rather than the candidate, because a fail-to-review row has
        no `ExtractedCandidate` to take them off — it exists precisely because validation
        did not produce one — and its citations still have to be auditable. One
        implementation for both, so the audit record of a review item is the same record
        a kept candidate gets, minted the same way, keyed the same way."""
        refs: list[str] = []
        for ev in evidence:
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
        review_count: int = 0,
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
            review_count=review_count,
            verbose=verbose,
            accepted_sql=_accepted_sql(summary) if verbose else None,
            intent=intent if verbose else None,
            slots=slots if verbose else None,
            rationale=rationale if verbose else None,
            decline_details=decline_details if verbose else None,
        ):
            pass

    def _emit_consume(
        self,
        session_id: str,
        outcome: str,
        delivery_count: int,
        *,
        context=None,
        session_status: str | None = None,
        skip_reason: str | None = None,
        reclaimed: bool | None = None,
    ) -> None:
        if self._tracer is not None:
            with consume_span(
                self._tracer,
                session_id=session_id,
                outcome=outcome,
                delivery_count=delivery_count,
                context=context,
                session_status=session_status,
                skip_reason=skip_reason,
                reclaimed=reclaimed,
            ):
                pass

    async def run_forever(self, *, sleep) -> None:
        """Blocking loop (used by the entrypoint). Ensures the group exists, then
        consumes batches forever. A transient error logs + continues (MEDIUM-1);
        when disabled, `run_once` returns immediately (no blocking XREADGROUP), so
        *sleep* paces the re-check on the consumer's OWN idle interval.

        The kill-switch STATE CHANGE is logged — once per change, never per cycle. A
        consumer held off by `LEARNING_ENABLED` otherwise looks exactly like a healthy
        idle one (it emits a `learning.disabled` span, which is invisible unless OTLP
        is configured, and nothing else), so "the loop is running and doing nothing" had
        no readable cause. Per-cycle logging is refused: at the 5s idle interval it
        would be 17k lines a day."""
        await self._queue.ensure_group()
        disabled_logged = False
        while True:
            try:
                result = await self.run_once()
            except Exception:  # noqa: BLE001 - MEDIUM-1: never die on a transient blip.
                _logger.exception("learning consume cycle failed; retrying")
                await sleep(self._settings.learning_consumer_idle_sleep_seconds)
                continue
            if result.disabled and not disabled_logged:
                _logger.warning(
                    "learning consumer IDLE — the LEARNING_ENABLED kill-switch is OFF, "
                    "so nothing is being consumed (work waits in the stream; no loss). "
                    "NB an unrecognized value counts as OFF, fail-safe: check for a "
                    "typo in the env var or in .env. Re-checking every %ss.",
                    self._settings.learning_consumer_idle_sleep_seconds,
                )
                disabled_logged = True
            elif not result.disabled and disabled_logged:
                _logger.info("learning consumer RESUMED — LEARNING_ENABLED is back on")
                disabled_logged = False
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
