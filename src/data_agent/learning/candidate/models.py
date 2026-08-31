"""CandidateEnvelope — the persisted candidate record (D101).

Held in the dedicated, access-controlled `learning_candidates` bucket, NOT the entity-free
neo4j / vector stores: a pre-leakage-gate candidate may still be entity-bearing in its payload
and is less-trusted until validated, so it needs the audit-store access posture. The candidate
carries only `evidence_refs` (KV keys into `learning_audit`) — never the entity-bearing quotes
(D51/D17).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from data_agent.timeutil import now_iso as _now

from ..audit.judgement import CoverageAssessment, ParamAssessment
from ..extractor.models import Decline, ExtractedCandidate
from ..extractor.shape import ShapeError, as_int, as_object, as_text, require
from ..summary.models import SessionSummary
from .decline import DeclineBlock, EvidencePointer, ValidationSnapshot
from .signals import NoveltyStamp, SessionSignals
from .verdicts import DedupVerdict, DriftStamp


class CandidateStatus:
    """Lifecycle states (05 §Candidate envelope). S3 only ever writes `EXTRACTED`."""

    EXTRACTED = "extracted"
    CANDIDATE = "candidate"
    IN_REVIEW = "in_review"
    VALIDATED = "validated"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    RETIRED = "retired"
    # FAIL-TO-REVIEW (`docs/decisions/learning-declined-candidate-review.md`): the judge
    # said the work was worth extracting and the parameterization form could not be
    # filled in. SEPARATE from `in_review` because the reviewer's task is a different
    # one: complete the form, not judge the idea — and because the count is the metric
    # that measures how often the form is unfillable, which folding it into `in_review`
    # would destroy.
    NEEDS_PARAMETERIZATION = "needs_parameterization"
    # Phase-3 terminal state (governed-corpus inbox PROMOTE): a verified learning node
    # whose MCP-format YAML has been emitted for a manual PR into the MCP corpus repo.
    # Terminal — it drops out of the inbox validated listing. Caveat: if the human
    # never merges the PR, the neo4j node stays `source='learning'` (excluded from
    # recall) while the candidate stays `promoted`; the emit is optimistic.
    PROMOTED = "promoted"


def mint_candidate_id(content_hash: str, ordinal: int) -> str:
    """Deterministic candidate key from the session `content_hash` + ordinal.

    A re-processed session (a crash or redelivery before `done`) therefore UPSERTS the same
    candidate docs rather than duplicating them.
    """
    return f"candidate::{content_hash}::{ordinal}"


def mint_review_candidate_id(content_hash: str, ordinal: int) -> str:
    """The same deterministic key for a DECLINED candidate routed to review.

    A SEPARATE ordinal namespace, and load-bearing rather than tidy: kept candidates are minted
    from `enumerate(result.candidates)` while a decline's natural ordinal is its index in the RAW
    emitted array, so without the prefix one key could name both and the later `put` would
    silently overwrite the earlier. Supersede is unaffected — both sweep on the `content_hash`
    FIELD, not the key.
    """
    return f"candidate::{content_hash}::review-{ordinal}"


@dataclass(frozen=True)
class CandidateEnvelope:
    candidate_id: str
    type: str
    status: str
    payload: dict[str, Any]
    source_session: str
    source_trace: str
    evidence_refs: tuple[str, ...]  # KV keys into learning_audit — never the quotes
    extractor_rationale: str
    entity_scan: dict[str, Any]  # LeakageVerdict shape — "pending" pre-leakage-gate (S5 authoritative)
    confidence: float
    proposed_action: str
    depends_on: tuple[str, ...]
    content_hash: str  # idempotency key (the session content hash)
    created_at: str = field(default_factory=_now)
    # --- additive Wave-0 verdict fields (D102). Each is stamped by exactly one
    # downstream stage and defaults so a pre-stage envelope is a VALID doc. S3
    # never populates these; the S3 spine above is untouched. ---
    dedup: DedupVerdict | None = None  # S6 WRITES (Contract C); None pre-S6
    drift: DriftStamp = field(default_factory=DriftStamp)  # S9 WRITES (Contract E); unchecked pre-S9
    # W3C `traceparent` of the extracting consume span (the session's learning
    # trace). Stamped at extraction so the cron scheduler's promote/land spans
    # CONTINUE the SAME Phoenix trace as the enqueue → consume → extract that
    # produced this candidate. Additive, defaults None, round-trips through
    # to_doc/from_doc; a missing value ⇒ the scheduler starts a normal root span.
    traceparent: str | None = None
    # Phase-3 human-approval flag, MIRRORING the `verified` property on the landed
    # neo4j node. The node is the source of truth for recall; this envelope copy lets
    # the inbox distinguish a human-VERIFIED landing (promotable) from an auto-landed
    # one WITHOUT a neo4j read. The scheduler stamps it to match on land (True for a
    # human-approve, False for auto) and the VERIFY action flips it True. Additive,
    # defaults False, round-trips through to_doc/from_doc (emitted only when True so a
    # pre-existing candidate doc stays byte-identical, mirroring `traceparent`).
    verified: bool = False
    # SCAN-ROTATION cursor (S9). "The promotion scheduler EXAMINED this envelope at T"
    # — a bookkeeping timestamp with NO verdict semantics whatsoever. Deliberately NOT
    # `drift.last_drift_check_at`: that field means "the D43 drift probes RAN", and the
    # scheduler stamps this one on holds where NO probe ran (a human-gated target, an
    # unsettled entity_scan, an unresolved `depends_on`), so overloading drift would
    # claim a check that never happened and feed a fabricated freshness to
    # `silent_eligible`. Load-bearing for scan FAIRNESS: `list_by_status(...,
    # order_by="last_scanned_at")` sorts on it so the bounded `scan_limit` window
    # rotates instead of pinning the same permanently-held candidates forever
    # (head-of-line starvation — a never-scanned candidate has no value here and MUST
    # sort FIRST: MISSING/NULL precede every string in both the N1QL collation and the
    # in-memory fake's sort key). Additive, defaults None, emitted only when set so a
    # pre-existing candidate doc round-trips byte-identically (mirrors `traceparent`).
    last_scanned_at: str | None = None
    # The POST-extraction coverage verdict (plan §3b), stamped by the dedup stage when
    # the soft-layer cosine landed in the ambiguous band. `None` means the judge did not
    # run — no judge wired, the score outside the band, or a fail-open path — and is
    # NOT the same as a `new` verdict, which is a positive statement a model made.
    #
    # It is here for two reasons, one weaker than it looks. The strong one: a human
    # opening this candidate in the review inbox can see that the machine already had an
    # opinion about its novelty, and the field is part of the queryable dataset that
    # decides whether composable blueprints are worth building. The weaker one: it lets
    # a re-run of the pipeline over an EXISTING envelope skip the model call. It is NOT
    # what makes a redelivery idempotent — a redelivery re-extracts and mints a fresh
    # envelope with this field unset; the deterministic `learning_audit` key
    # (`post_extraction_ref`) is what actually prevents the second model call. Do not
    # read this field as the idempotency mechanism.
    #
    # ONE EXCEPTION, and it is the reason this comment is not simply "the post-extraction
    # verdict": a fail-to-review candidate (`build_declined_envelope`) stamps the
    # PRE-extraction assessment here — the judgement that said the work was worth
    # extracting at all, which is precisely what distinguishes this row from a decline
    # that was supposed to die. Same shape, same question ("is this already covered?"),
    # asked one stage earlier; a reader who needs to tell them apart has `status` and
    # `decline`.
    #
    # Additive, defaults None, emitted only when set so a pre-slice candidate doc
    # round-trips byte-identically (mirrors `traceparent`).
    judge: CoverageAssessment | None = None

    # The S4 PARAMETERIZATION judge's verdict (design §D), or `None` when it did not run —
    # not wired, not a blueprint, a candidate that already failed static validation, or a
    # fail-open path. ADDITIVE AND INERT: no routing rule, static check or dedup key reads
    # it. It is carried so the reviewer card can show what the judge said about the very
    # candidate in front of the reviewer, which is half of the phase-D-1 measurement.
    param_judge: ParamAssessment | None = None
    # --- inbox-ranking inputs (plan §4). Two separate stamps because two different
    # stages own them and neither can compute the other's:
    #   * `session_signals` is stamped ONCE at `build_envelope` from the in-memory
    #     `SessionSummary`, which is dropped immediately afterwards. Nothing downstream
    #     can recover it.
    #   * `novelty` is stamped by S6 dedup, the only stage that has already embedded this
    #     candidate's intent and queried the graph.
    # Both default `None` = "nobody looked", which every reader must keep distinct from a
    # zero-valued measurement (see `candidate/signals.py`). Additive + emitted only when
    # set, so a pre-slice candidate doc round-trips byte-identically.
    session_signals: SessionSignals | None = None
    novelty: NoveltyStamp | None = None
    # WHY the S9 scheduler routed this candidate to `in_review`, when it knew something a
    # reader of the routed envelope cannot reconstruct. Exactly ONE value today,
    # `USER_CORRECTED`, and this is deliberately NOT a general-purpose slot — see
    # `RouteReason`.
    #
    # STICKY: the scheduler only ever SETS it, never clears it. A negative signal that a
    # later clean cycle can erase is not a signal; erasing it is the class of bug the
    # correction fix itself was about.
    route_reason: str | None = None
    # --- fail-to-review (status `needs_parameterization`) -------------------------
    # Set together or not at all: `decline` is WHY the form could not be filled in, and
    # `revalidation` is WHAT the completed form is re-checked against. Both are absent on
    # every other candidate in the store, which is what keeps them a marker as well as a
    # payload — `writer/routing.py::derive_inbox_reason` reads the presence of `decline`,
    # and the completion path refuses without `revalidation` rather than re-validating
    # against a session shape it invented. See `candidate/decline.py`.
    #
    # CLEARED on successful completion: a completed candidate is an ordinary one, and a
    # decline block left on it would keep the inbox rendering a form that has already
    # been filled in.
    decline: DeclineBlock | None = None
    revalidation: ValidationSnapshot | None = None

    def to_doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "_id": self.candidate_id,
            "candidate_id": self.candidate_id,
            "type": self.type,
            "status": self.status,
            "payload": self.payload,
            "provenance": {
                "source_session": self.source_session,
                "source_trace": self.source_trace,
                "evidence_ref": list(self.evidence_refs),
                "extractor_rationale": self.extractor_rationale,
            },
            "entity_scan": self.entity_scan,
            "dedup": self.dedup.to_doc() if self.dedup is not None else None,
            "drift": self.drift.to_doc(),
            "confidence": self.confidence,
            "proposed_action": self.proposed_action,
            "depends_on": list(self.depends_on),
            "content_hash": self.content_hash,
            "created_at": self.created_at,
        }
        # Additive + OPTIONAL: emit the trace-chaining carrier only when present, so a
        # pre-existing candidate doc (no traceparent) round-trips byte-identically.
        if self.traceparent is not None:
            doc["traceparent"] = self.traceparent
        # Additive + OPTIONAL: emit `verified` only when True, so a pre-Phase-3
        # candidate doc (no `verified`) round-trips byte-identically (mirrors
        # `traceparent`). A missing key reads back as the False default.
        if self.verified:
            doc["verified"] = True
        # Additive + OPTIONAL: emit the scan cursor only once the scheduler has
        # actually looked at this candidate, so (a) a pre-S9 doc round-trips
        # byte-identically and (b) a never-scanned candidate leaves the key MISSING —
        # which is what makes it sort FIRST in the rotation query (MISSING precedes
        # NULL precedes every string in the N1QL collation order), i.e. brand-new work
        # jumps the queue ahead of everything already examined.
        if self.last_scanned_at is not None:
            doc["last_scanned_at"] = self.last_scanned_at
        # Additive + OPTIONAL, mirroring the three above: absent means "the judge did
        # not run", which must stay distinguishable from a stored `new` verdict.
        if self.judge is not None:
            doc["judge"] = self.judge.to_doc()
        # Same additive+optional posture as `judge` above: absent means the S4 parameterization
        # judge did not run, which is the shape of every candidate written before it existed
        # and of every deployment with it switched off.
        if self.param_judge is not None:
            doc["param_judge"] = self.param_judge.to_doc()
        # Additive + OPTIONAL, same rule as the four above: an absent key means "this
        # stamp was never written", which the ranking treats differently from a stamp
        # whose values happen to be zero.
        if self.session_signals is not None:
            doc["session_signals"] = self.session_signals.to_doc()
        if self.novelty is not None:
            doc["novelty"] = self.novelty.to_doc()
        if self.route_reason is not None:
            doc["route_reason"] = self.route_reason
        # Additive + OPTIONAL, same rule as every field above: absent means this is not a
        # fail-to-review item, which is exactly what every candidate written before this
        # slice is.
        if self.decline is not None:
            doc["decline"] = self.decline.to_doc()
        if self.revalidation is not None:
            doc["revalidation"] = self.revalidation.to_doc()
        return doc

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> CandidateEnvelope:
        prov = doc.get("provenance", {})
        return cls(
            candidate_id=doc["candidate_id"],
            type=doc["type"],
            status=doc["status"],
            payload=dict(doc.get("payload", {})),
            source_session=prov.get("source_session", ""),
            source_trace=prov.get("source_trace", ""),
            evidence_refs=tuple(prov.get("evidence_ref", []) or []),
            extractor_rationale=prov.get("extractor_rationale", ""),
            entity_scan=dict(doc.get("entity_scan", {})),
            dedup=(
                DedupVerdict.from_doc(doc["dedup"])
                if doc.get("dedup") is not None
                else None
            ),
            drift=DriftStamp.from_doc(doc.get("drift", {}) or {}),
            confidence=float(doc.get("confidence", 0.0)),
            proposed_action=doc.get("proposed_action", "new"),
            depends_on=tuple(doc.get("depends_on", []) or []),
            content_hash=doc.get("content_hash", ""),
            created_at=doc.get("created_at", _now()),
            traceparent=doc.get("traceparent"),
            verified=bool(doc.get("verified", False)),
            # NORMALIZE, do not trust: this doc is rehydrated JSON from a store other
            # code (and humans, via cbq) can write. Every consumer treats the value as
            # an ISO-8601 STRING (`datetime.fromisoformat`, a `str`-vs-None sort key),
            # and `fromisoformat` raises TypeError — not ValueError — on an int/dict,
            # so a non-string here would be a crash site, not a wrong answer. Coerce
            # anything that is not a `str` to None ("never scanned").
            #
            # This normalization is IN-PROCESS ONLY, and it does NOT make a corrupt
            # cursor self-healing — an earlier revision of this comment claimed it did,
            # which was wrong and was disproved against live Couchbase. `ORDER BY` runs
            # SERVER-SIDE on the RAW stored value, which this coercion never sees. In
            # the N1QL collation a number sorts before strings (so a numeric cursor
            # sorts early and does self-heal on the next stamp), but an ARRAY or OBJECT
            # sorts AFTER every string — so a corrupt cursor of that shape sorts LAST
            # and, past `scan_limit`, is never examined again. The in-memory fake ranks
            # every non-string FIRST, so the two stores report exact opposites here and
            # no unit test can catch it. Writing a non-string cursor requires a
            # hand-edited or foreign-written document; S9 only ever writes
            # `_now_iso()`. Repair is manual (fix or delete the document).
            #
            # The same ordering-without-a-cutoff design has one other permanent-loss
            # case, accepted deliberately: a cursor stamped far in the FUTURE (clock
            # skew, or a hand edit) sorts last for as long as it stays in the future and
            # that row is starved. The alternative — a freshness cutoff in the WHERE —
            # trades this for a strictly worse failure, since a cutoff DROPS rows rather
            # than merely mis-ordering them (see `list_by_status`).
            last_scanned_at=(
                doc["last_scanned_at"]
                if isinstance(doc.get("last_scanned_at"), str)
                else None
            ),
            # A non-dict `judge` (a hand edit, a foreign writer) reads back as "the
            # judge did not run" rather than raising inside a queue worker — the same
            # normalize-do-not-trust posture as `last_scanned_at` above.
            judge=(
                CoverageAssessment.from_doc(doc["judge"])
                if isinstance(doc.get("judge"), dict)
                else None
            ),
            # A non-dict `param_judge` (a hand edit, a foreign writer, a doc written
            # before this field existed) reads back as "the judge did not run" rather
            # than raising inside a queue worker — the same posture as `judge` above.
            param_judge=(
                ParamAssessment.from_doc(doc["param_judge"])
                if isinstance(doc.get("param_judge"), dict)
                else None
            ),
            # Same normalize-do-not-trust posture as `last_scanned_at`/`judge`: a
            # non-dict stamp (hand edit, foreign writer) reads back as "nobody looked"
            # rather than raising inside the cron scan or the inbox projection. The
            # per-field coercion inside `from_doc` handles a dict with junk MEMBERS.
            session_signals=(
                SessionSignals.from_doc(doc["session_signals"])
                if isinstance(doc.get("session_signals"), dict)
                else None
            ),
            novelty=(
                NoveltyStamp.from_doc(doc["novelty"])
                if isinstance(doc.get("novelty"), dict)
                else None
            ),
            # A non-str reads as absent. The downstream read is a string RENDERED into a
            # reviewer-facing wire field, so a dict or a list here would reach the review
            # UI as a repr — and a foreign writer must not be able to put arbitrary
            # structure in front of a human through this path.
            route_reason=(
                doc["route_reason"] if isinstance(doc.get("route_reason"), str) else None
            ),
            # Both own their own normalize-do-not-trust rules (a bad shape reads as
            # ABSENT, never raises) — see `candidate/decline.py`, which states why the
            # snapshot in particular must read as missing rather than as empty.
            decline=DeclineBlock.from_doc(doc.get("decline")),
            revalidation=ValidationSnapshot.from_doc(doc.get("revalidation")),
        )


def build_envelope(
    candidate: ExtractedCandidate,
    summary: SessionSummary,
    *,
    candidate_id: str,
    evidence_refs: tuple[str, ...],
    traceparent: str | None = None,
) -> CandidateEnvelope:
    """Assemble the persisted envelope from an `ExtractedCandidate` + the minted `evidence_refs`.

    The quotes are already snapshotted to `learning_audit`. `entity_scan.result` is `pending` —
    the S5 leakage gate is authoritative and this only records the extractor's self-check.
    *traceparent* is carried forward so the scheduler's promote/land spans continue the same
    session trace.

    THIS is the only place `session_signals` can be stamped: the summary is an in-process value
    dropped as soon as extraction finishes, so the session-quality ranking axis is derivable here
    and nowhere later. Only counts, one bool and one enum member are read, so the stamp is
    entity-free by construction — which it must be, because it travels to the review UI.
    """
    header = candidate.header
    return CandidateEnvelope(
        candidate_id=candidate_id,
        type=header.type,
        status=CandidateStatus.EXTRACTED,
        payload=candidate.payload_to_doc(),
        source_session=summary.session_id,
        source_trace=summary.trace_id,
        evidence_refs=evidence_refs,
        extractor_rationale=header.rationale,
        entity_scan={
            "result": "pending",
            "hits": list(header.entity_self_check.found),
            "self_check_contains_entities": header.entity_self_check.contains_entities,
        },
        confidence=header.confidence,
        proposed_action=header.proposed_action,
        depends_on=header.depends_on,
        content_hash=summary.content_hash,
        traceparent=traceparent,
        session_signals=SessionSignals.from_summary(summary),
        # The SAME argument the `session_signals` note above makes, applied to the other
        # thing only derivable here: the summary is an in-process value dropped when
        # extraction ends, so a candidate that does not carry its own validation context
        # can never be RE-validated afterwards.
        #
        # It used to be stamped by `build_declined_envelope` alone, which made the LLM
        # revise surface (design §C) structurally impossible on anything but the
        # fail-to-review queue — an `in_review` candidate had no accepted SQL to propose
        # against and nothing to re-check a revision with. Stamping it here is what §C.4
        # deferred and named as the unlock.
        #
        # ⚠ ENTITY-BEARING (`sql_by_ref` holds the query's literals), and that is not a new
        # boundary: it is the same field, in the same access-controlled candidate store
        # (D101), that every declined candidate has always carried. It is never projected to
        # the wire — `InboxItem` does not read it — and `strip_entity_bearing` governs the
        # promotion boundary as before.
        revalidation=ValidationSnapshot.from_summary(
            summary,
            # REAL pointers, not `()`. `ValidationSnapshot.from_doc` REFUSES a snapshot with
            # no citations — "an empty one read back is damage, not a legitimate shape" — so an
            # empty tuple round-trips to `None` and the field is silently useless. `header.evidence`
            # is mandatory and non-empty by D31, and carries exactly the two pointer fields; the
            # QUOTES stay out (D51), which is the same projection `build_declined_envelope` makes.
            evidence=tuple(
                EvidencePointer(turn_ref=ref.turn_ref, tool_call_ref=ref.tool_call_ref)
                for ref in header.evidence
            ),
        ),
    )


def _payload_of(raw: dict[str, Any]) -> dict[str, Any]:
    payload = raw.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _confidence_of(raw: dict[str, Any]) -> float:
    value = raw.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _depends_on_of(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("depends_on")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _evidence_pointers(raw: dict[str, Any]) -> tuple[EvidencePointer, ...]:
    """The citations, as (turn_ref, tool_call_ref) pairs — never the quotes (D51).

    Reads the RAW array because there is no typed model to read: this candidate never became an
    `ExtractedCandidate`. An unreadable item is skipped, matching `validation.py::_evidence`'s
    partial tolerance. THROUGH THE SAME READERS the validator uses, not hand-written isinstance
    checks: `as_int` accepts the `"0"` a real model emits, so a stricter reader here would drop
    citations validation had already accepted, and the completion path would then fail D31's
    evidence gate for a reason nobody could see.
    """
    items = raw.get("evidence")
    if not isinstance(items, list):
        return ()
    out: list[EvidencePointer] = []
    for idx, item in enumerate(items):
        at = f"candidate.evidence[{idx}]"
        try:
            obj = as_object(item, at=at, requirement="an evidence object")
            out.append(
                EvidencePointer(
                    turn_ref=require(
                        obj, "turn_ref", as_int, at=at, requirement="the turn index"
                    ),
                    tool_call_ref=require(
                        obj, "tool_call_ref", as_text, at=at, requirement="the tool call id"
                    ),
                )
            )
        except ShapeError:
            continue
    return tuple(out)


def _self_check(raw: dict[str, Any]) -> tuple[bool, list[str]]:
    """The model's OWN entity attestation, as it wrote it.

    Read from the raw candidate rather than defaulted, because a hardcoded `False` is not neutral
    here — it is an assertion, in the field a reviewer and the S5 gate both read as "the extractor
    looked and found nothing". Preliminary either way: the S5 gate is authoritative and overwrites
    this whole doc with its settled verdict.
    """
    esc = raw.get("entity_self_check")
    if not isinstance(esc, dict):
        return False, []
    found = esc.get("found")
    return (
        bool(esc.get("contains_entities", False)),
        [item for item in found if isinstance(item, str)] if isinstance(found, list) else [],
    )


def build_declined_envelope(
    decline: Decline,
    summary: SessionSummary,
    *,
    candidate_id: str,
    evidence_refs: tuple[str, ...] = (),
    judge: CoverageAssessment | None = None,
    traceparent: str | None = None,
) -> CandidateEnvelope:
    """Assemble the FAIL-TO-REVIEW envelope for a candidate that died on the parameterization form.

    A SIBLING of `build_envelope`, deliberately not a mode of it: that one takes an
    `ExtractedCandidate`, a value that exists only because every validation passed, and this one
    exists precisely because they did not, so its input is the raw model JSON and every field is
    read defensively. Contorting one constructor to serve both would put "was this validated?"
    behind a parameter.

    `entity_scan` is left at the `pending` self-check because THIS FUNCTION IS NOT THE GATE — the
    caller runs the real leakage stage and stamps the settled verdict before persisting — so a
    caller that forgets fails CLOSED: an unsettled scan withholds the decline detail at the wire
    and blocks every approve path. `evidence_refs` are the MINTED audit keys, exactly as for a
    kept candidate, defaulting to `()` for envelopes written before that was wired.
    """
    raw = decline.raw_payload or {}
    contains_entities, found = _self_check(raw)
    return CandidateEnvelope(
        candidate_id=candidate_id,
        type=decline.type,
        status=CandidateStatus.NEEDS_PARAMETERIZATION,
        payload=_payload_of(raw),
        source_session=summary.session_id,
        source_trace=summary.trace_id,
        evidence_refs=evidence_refs,
        extractor_rationale=str(raw.get("rationale", "")),
        entity_scan={
            "result": "pending",
            "hits": found,
            "self_check_contains_entities": contains_entities,
        },
        confidence=_confidence_of(raw),
        proposed_action=str(raw.get("proposed_action", "new")),
        depends_on=_depends_on_of(raw),
        content_hash=summary.content_hash,
        traceparent=traceparent,
        judge=judge,
        session_signals=SessionSignals.from_summary(summary),
        decline=DeclineBlock(
            reason=decline.reason,
            detail=decline.detail,
            corrections_attempted=decline.corrections_attempted,
            correction_history=decline.correction_history,
        ),
        revalidation=ValidationSnapshot.from_summary(
            summary, evidence=_evidence_pointers(raw)
        ),
    )
