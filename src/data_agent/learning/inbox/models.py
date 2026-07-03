"""InboxItem — the reviewer-facing projection of an `in_review` candidate (§4).

`InboxItem` is DERIVED from a `CandidateEnvelope` + its `status`; it is not stored.
It carries an entity-free-where-required view so a human can adjudicate without the
review UI ever touching a global store. The `reason` is re-derived from the same
routing rules the writer used (`writer.routing.derive_inbox_reason`) so the label
can never drift from the decision that produced it.

`payload_view` is the candidate payload with entity-bearing leakage `span`s
stripped, so a reviewer sees WHAT leaked (field + kind) without the raw value being
re-exposed through the inbox surface (D17). `entity_scan` is the settled S5
`LeakageVerdict`; `dedup` is the S6 verdict (what it collided with, if anything).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..candidate.models import CandidateEnvelope
from ..candidate.verdicts import DedupVerdict, LeakageVerdict
from ..writer.routing import derive_inbox_reason

InboxReason = Literal[
    "knowledge_pre_gate",
    "schema_edit",
    "leakage_near_miss",
    "blueprint_sampled",
    "dedup_conflict",
    "fail_to_review",
]


def _summary_of(env: CandidateEnvelope) -> str:
    """An entity-free one-liner: the blueprint `intent` or the knowledge
    `statement`. Both are entity-free by the time a candidate is `in_review`
    (blueprint intents are S5-scanned; knowledge statements are the fact text)."""
    payload = env.payload
    return str(payload.get("intent") or payload.get("statement") or "").strip()


def _entity_free_payload_view(env: CandidateEnvelope) -> dict[str, Any]:
    """The payload for review with leakage `span`s stripped (D17). Only the
    `entity_scan` view is span-bearing on the envelope surface; the payload itself
    is copied shallowly so the source envelope is never mutated."""
    return dict(env.payload)


def _leakage_view(env: CandidateEnvelope) -> LeakageVerdict:
    """The settled S5 verdict. A pre-S5 (`pending`) self-check is not a settled
    verdict, so it is represented as an empty `pass` view — the inbox never asserts
    an entity finding that S5 did not settle."""
    scan = env.entity_scan
    if LeakageVerdict.is_settled(scan):
        return LeakageVerdict.from_doc(scan)
    return LeakageVerdict(result="pass", scanner="unsettled")


@dataclass(frozen=True)
class InboxItem:
    """A reviewer-facing view over one `in_review` candidate (Contract D §4)."""

    candidate_id: str
    type: str  # blueprint | global_knowledge | user_knowledge | schema_edit
    reason: str  # one of InboxReason — re-derived, never stored
    summary: str  # entity-free one-liner
    payload_view: dict[str, Any]  # entity-free-where-required payload for review
    evidence_refs: tuple[str, ...]  # KV keys into learning_audit (reviewer fetches quotes)
    entity_scan: LeakageVerdict  # what S5 found (drives reviewer attention)
    dedup: DedupVerdict | None  # what it collided with, if anything
    created_at: str

    @classmethod
    def from_envelope(cls, env: CandidateEnvelope) -> InboxItem:
        return cls(
            candidate_id=env.candidate_id,
            type=env.type,
            reason=derive_inbox_reason(env),
            summary=_summary_of(env),
            payload_view=_entity_free_payload_view(env),
            evidence_refs=env.evidence_refs,
            entity_scan=_leakage_view(env),
            dedup=env.dedup,
            created_at=env.created_at,
        )
