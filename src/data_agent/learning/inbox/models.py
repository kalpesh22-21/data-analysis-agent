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

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from ..candidate.models import CandidateEnvelope
from ..candidate.redaction import entity_free_payload_view, entity_spans, redact_payload
from ..candidate.verdicts import DedupVerdict, LeakageVerdict
from ..writer.routing import derive_inbox_reason
from .ranking import RankedScore, review_score

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
    status: str  # the envelope's lifecycle status (in_review | rejected — archive view)
    reason: str  # one of InboxReason — re-derived, never stored
    summary: str  # entity-free one-liner
    payload_view: dict[str, Any]  # entity-free-where-required payload for review
    evidence_refs: tuple[str, ...]  # KV keys into learning_audit (reviewer fetches quotes)
    entity_scan: LeakageVerdict  # what S5 found (drives reviewer attention)
    dedup: DedupVerdict | None  # what it collided with, if anything
    created_at: str
    # Phase-3 human-approval flag (mirrors the landed node's `verified`): lets the UI
    # tell a human-VERIFIED validated learning node (promotable) from an auto-landed one.
    # False for every pre-Phase-3 / auto-landed candidate.
    verified: bool = False
    # WHY the S9 scheduler routed this candidate, when it knew something the reviewer
    # cannot otherwise see. `"user_corrected"` or `None` today.
    #
    # DISTINCT FROM `reason`, and the pair of names is unfortunate but the distinction is
    # real: `reason` is the routing CATEGORY, re-derived on every projection from the
    # writer's own rules so it can never drift from them. `route_reason` is a fact only
    # the scheduler held, at one moment, and that nothing can reconstruct afterwards — the
    # user-correction drift stamp is overwritten by the very next replay.
    #
    # It is the difference between a reviewer approving a corrected blueprint knowingly
    # and approving it blind: the approve path re-runs static validation and the golden
    # replay, and NEITHER can see the value error a user reported.
    route_reason: str | None = None
    # The plan-§4 review score + its three axes. DERIVED, never stored — recomputed on
    # every projection from the two durable stamps (`session_signals`, `novelty`) and the
    # payload, exactly like `reason` is, and for the same reason: a score persisted next
    # to the weights that produced it drifts from them the moment either changes, and
    # nothing would notice.
    #
    # Non-optional with a neutral default rather than `None`, because every consumer
    # (sort key, cutoff, wire projection) would otherwise need the same three-line
    # None-guard. `RankedScore.measured` is what says whether the numbers mean anything.
    score: RankedScore = field(
        default_factory=lambda: RankedScore(
            score=0.0,
            novelty=0.0,
            groundedness=0.0,
            session_quality=0.0,
            novelty_measured=False,
            quality_measured=False,
            groundedness_measured=False,
        )
    )

    @classmethod
    def from_envelope(cls, env: CandidateEnvelope) -> InboxItem:
        return cls(
            candidate_id=env.candidate_id,
            type=env.type,
            status=env.status,
            reason=derive_inbox_reason(env),
            summary=_summary_of(env),
            payload_view=_entity_free_payload_view(env),
            evidence_refs=env.evidence_refs,
            entity_scan=_leakage_view(env),
            dedup=env.dedup,
            created_at=env.created_at,
            verified=env.verified,
            route_reason=env.route_reason,
            score=review_score(env),
        )
