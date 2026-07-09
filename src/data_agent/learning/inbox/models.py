"""InboxItem — the reviewer-facing projection of an `in_review` candidate (§4).

`InboxItem` is DERIVED from a `CandidateEnvelope` + its `status`; it is not stored.
It carries an entity-free-where-required view so a human can adjudicate without the
review UI ever touching a global store. The `reason` is re-derived from the same
routing rules the writer used (`writer.routing.derive_inbox_reason`) so the label
can never drift from the decision that produced it.

`payload_view` is the candidate payload with entity-bearing leakage spans REDACTED
(the entity value replaced), so a reviewer sees WHAT leaked (field + kind, via
`entity_scan`) without the raw value being re-exposed through the inbox surface
(D17/QA-Q7). `entity_scan` is the settled S5 `LeakageVerdict`; `dedup` is the S6
verdict (what it collided with, if anything).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from ..candidate.models import CandidateEnvelope
from ..candidate.redaction import entity_free_payload_view, entity_spans, redact_payload
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
    """An entity-free one-liner: the blueprint `intent` or the knowledge `statement`,
    with any settled entity spans REDACTED.

    A near-miss candidate can carry the entity IN its `intent` (the very value
    `payload_view` redacts) — surfacing the RAW intent here would bypass that redaction
    and re-leak it through the summary. So the one-liner runs through the SAME
    `redact_payload` strip (keyed off the settled S5 spans) the payload view uses, so a
    reviewer sees `[redacted]` wherever a value was, never the raw span (D17/QA-Q7)."""
    payload = env.payload
    raw = str(payload.get("intent") or payload.get("statement") or "").strip()
    if not raw:
        return raw
    return redact_payload({"summary": raw}, entity_spans(env))["summary"]


def _entity_free_payload_view(env: CandidateEnvelope) -> dict[str, Any]:
    """The payload for review with the settled leakage spans REDACTED (D17/QA-Q7).
    Delegates to the shared redaction so the reviewer surface can never re-expose a
    raw entity value the S5 verdict flagged. The source envelope is never mutated."""
    return entity_free_payload_view(env)


def _leakage_view(env: CandidateEnvelope) -> LeakageVerdict:
    """The settled S5 verdict with hit `span`s BLANKED (field/kind kept) — the
    reviewer sees WHAT leaked and WHERE, never the raw value.

    An `in_review` envelope still stores the raw spans (they are only blanked at the
    promotion boundary by `blank_scan_spans`), so projecting the verdict verbatim would
    ship the exact entity value the payload redaction withholds (contract §2a requires
    `span == ""`). Mirror `redaction.blank_scan_spans` here so the inbox surface is
    span-free regardless of status. A pre-S5 (`pending`) self-check is not a settled
    verdict → an empty `pass` view (the inbox never asserts a finding S5 did not
    settle)."""
    scan = env.entity_scan
    if LeakageVerdict.is_settled(scan):
        verdict = LeakageVerdict.from_doc(scan)
        return replace(verdict, hits=tuple(replace(h, span="") for h in verdict.hits))
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
