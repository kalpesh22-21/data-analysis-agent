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

from ..extractor.models import ExtractedCandidate
from ..summary.models import SessionSummary
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
    promote/land spans continue the SAME session trace."""
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
    )
