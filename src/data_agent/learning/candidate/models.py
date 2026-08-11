"""CandidateEnvelope — the persisted candidate record (D101, 05 §Candidate envelope).

Held in the dedicated, access-controlled `learning_candidates` Couchbase store
(D101), NOT the entity-free neo4j / vector recall stores: a pre-leakage-gate
candidate may still be entity-bearing (in its payload) and is less-trusted until
validated, so it needs the audit-store access posture, not an entity-free store.
The candidate carries only `evidence_refs` (KV keys into `learning_audit`) — never
the entity-bearing evidence quotes (D51/D17). S3 persists at `status=extracted`;
nothing promotes it (that is Slice 9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..audit.judgement import CoverageAssessment
from ..extractor.models import ExtractedCandidate
from ..summary.models import SessionSummary
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
    # Phase-3 terminal state (governed-corpus inbox PROMOTE): a verified learning node
    # whose MCP-format YAML has been emitted for a manual PR into the MCP corpus repo.
    # Terminal — it drops out of the inbox validated listing. Caveat: if the human
    # never merges the PR, the neo4j node stays `source='learning'` (excluded from
    # recall) while the candidate stays `promoted`; the emit is optimistic.
    PROMOTED = "promoted"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def mint_candidate_id(content_hash: str, ordinal: int) -> str:
    """Deterministic candidate key derived from the session `content_hash` +
    ordinal, so a re-processed session (crash/redelivery before `done`) UPSERTS
    the same candidate docs rather than duplicating them — best-effort idempotency
    until the D48 `canonical_key` dedup lands in Slice 6."""
    return f"candidate::{content_hash}::{ordinal}"


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
    # Additive, defaults None, emitted only when set so a pre-slice candidate doc
    # round-trips byte-identically (mirrors `traceparent`).
    judge: CoverageAssessment | None = None
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
        # Additive + OPTIONAL, same rule as the four above: an absent key means "this
        # stamp was never written", which the ranking treats differently from a stamp
        # whose values happen to be zero.
        if self.session_signals is not None:
            doc["session_signals"] = self.session_signals.to_doc()
        if self.novelty is not None:
            doc["novelty"] = self.novelty.to_doc()
        if self.route_reason is not None:
            doc["route_reason"] = self.route_reason
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
        )


def build_envelope(
    candidate: ExtractedCandidate,
    summary: SessionSummary,
    *,
    candidate_id: str,
    evidence_refs: tuple[str, ...],
    traceparent: str | None = None,
) -> CandidateEnvelope:
    """Assemble the persisted envelope from an `ExtractedCandidate` + the minted
    `evidence_refs` (the quotes are already snapshotted to `learning_audit`).
    `entity_scan.result` is `pending` — the Slice-5 leakage gate is authoritative
    (D58); S3 only records the extractor's preliminary self-check. *traceparent* (the
    extracting consume span's W3C context) is carried forward so the scheduler's
    promote/land spans continue the SAME session trace.

    THIS is the only place `session_signals` can be stamped (plan §4): the summary is an
    in-process value that is dropped as soon as the extraction finishes, so the
    session-quality axis of the inbox ranking is derivable here and nowhere later. Only
    counts, one bool and one enum member are read — the stamp is entity-free by
    construction, which it must be, because it travels to the review UI."""
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
    )
