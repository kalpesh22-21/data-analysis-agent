"""InboxItem — the reviewer-facing projection of an `in_review` candidate (§4).

DERIVED from a `CandidateEnvelope` plus its `status`; never stored. The `reason` is
re-derived from the same routing rules the writer used, so the label can never drift from the
decision that produced it. `payload_view` is the candidate payload with entity-bearing
leakage spans REDACTED, so a reviewer sees WHAT leaked (field + kind, via `entity_scan`)
without the raw value being re-exposed through the inbox surface (D17/QA-Q7).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from data_agent.runtime.blueprint.template import SLOT_TOKEN

from ..audit.judgement import ParamAssessment
from ..candidate.decline import DeclineBlock
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
    # The judge passed it on merit and the parameterization form could not be filled in.
    # The reviewer's task is to COMPLETE it, not to adjudicate it — see
    # `docs/decisions/learning-declined-candidate-review.md`.
    "needs_parameterization",
]


# What a reviewer sees in place of content nobody has scanned. One constant, shared by
# the payload view and the summary, so the two cannot disagree about whether a row is
# being withheld — and phrased as the reason rather than as a blank, because a row that
# renders empty reads as a row with nothing in it, which is the failure this whole slice
# is about.
UNSCANNED_NOTICE = (
    "withheld — the entity scan never settled on this candidate, so nothing in its "
    "payload has been cleared for display"
)


def _withheld_for_unsettled_scan(env: CandidateEnvelope) -> bool:
    """Must this row's CONTENT be withheld from the wire entirely?

    Only for a FAIL-TO-REVIEW row whose scan never settled, and both halves matter. The redaction
    machinery is keyed off SETTLED spans, so on an unsettled row it removes nothing and
    `payload_view` would ship the raw payload verbatim — a combination no other listable status
    can reach, because the write router settles a verdict on the way. And a decline-bearing row
    CAN arrive here unscanned by design, since the consumer persists it with the `pending`
    sentinel when no leakage stage is wired; withholding the decline detail while shipping the
    same session's literals inside `payload_view` would be an elaborate way of leaking exactly
    what the other rule protects. The row itself still LISTS, with its shape, decline reason and
    counts — only the unscanned CONTENT is held back.
    """
    return env.decline is not None and not LeakageVerdict.is_settled(env.entity_scan)


def _summary_of(env: CandidateEnvelope) -> str:
    """An entity-free one-liner: the blueprint `intent` or the knowledge `statement`, redacted.

    A near-miss candidate can carry the entity IN its `intent` — the very value `payload_view`
    redacts — so surfacing the raw intent would bypass that redaction. The one-liner runs through
    the SAME `redact_payload` strip, keyed off the settled S5 spans (D17/QA-Q7).
    """
    if _withheld_for_unsettled_scan(env):
        return UNSCANNED_NOTICE
    payload = env.payload
    raw = str(payload.get("intent") or payload.get("statement") or "").strip()
    if not raw:
        return raw
    return redact_payload({"summary": raw}, entity_spans(env))["summary"]


def _entity_free_payload_view(env: CandidateEnvelope) -> dict[str, Any]:
    """The payload for review with the settled leakage spans REDACTED (D17/QA-Q7).

    Delegates to the shared redaction so the reviewer surface can never re-expose a raw entity
    value the S5 verdict flagged; the source envelope is never mutated. An UNSCANNED
    fail-to-review row ships the notice instead of the payload — the redaction has nothing to key
    off, so "redacted" would mean "unmodified".
    """
    if _withheld_for_unsettled_scan(env):
        return {"withheld": UNSCANNED_NOTICE}
    return entity_free_payload_view(env)


def _template_parts(payload_view: dict[str, Any]) -> tuple[dict[str, str], ...]:
    """The generalized SQL template split into literal text and `{slot}` tokens, for the card.

    ⚠ TAKES THE REDACTED VIEW, NEVER `env.payload`, and the argument type is the guard: a
    template is not slots-only — every `role="inline"` predicate keeps its LITERAL VALUE in the
    template body (`record_type = 'EARNING'`), and an inline literal can be entity-bearing.
    Tokenizing the raw payload would ship those literals to a browser through the one field added
    to make the surface safer, bypassing the redaction the `payload_view` beside it respects. The
    general shape of the mistake: a DERIVED FIELD INHERITS THE TRUST LEVEL OF ITS SOURCE, not of
    its sibling.

    Split rather than a regex in the browser because the token grammar is
    `runtime/blueprint/template.SLOT_TOKEN` and a second spelling of it in JS would fork a
    definition two layers already depend on. The browser then does no parsing at all, which is
    also what keeps the page's `textContent`-only posture cheap to hold: alternating text and slot
    nodes are appended, never concatenated into markup.

    EMPTY is the honest answer for everything without a template — a knowledge candidate, a
    withheld payload, and a `fail_to_review` blueprint (whose generalization carries no
    `sql_template` at all) are all legitimately here, and the card renders the section only when
    this is non-empty.
    """
    generalization = payload_view.get("generalization")
    if not isinstance(generalization, dict):
        return ()
    template = generalization.get("sql_template")
    if not isinstance(template, str) or not template:
        return ()
    parts: list[dict[str, str]] = []
    cursor = 0
    for match in SLOT_TOKEN.finditer(template):
        if match.start() > cursor:
            parts.append({"text": template[cursor : match.start()]})
        parts.append({"slot": match.group(1)})
        cursor = match.end()
    if cursor < len(template):
        parts.append({"text": template[cursor:]})
    return tuple(parts)


def _param_judge_view(env: CandidateEnvelope) -> ParamAssessment | None:
    """The parameterization judge's verdict as it may cross to a browser.

    ⚠ REDACTED, not carried verbatim. The judge's `feedback` and its findings' `note` are model
    prose written about a payload the judge was shown UNREDACTED — so a verdict about a leaked
    entity can quote it ("employee = 'E12345' should be inline"), and shipping it raw would
    walk the exact value `payload_view` withholds past the same reviewer, in a field added to
    explain the withholding. Same rule, same spans, same pass as the payload.

    Withheld entirely on an unscanned fail-to-review row, for the reason `_entity_free_payload_view`
    gives: with nothing settled there is nothing to key a redaction off, so "redacted" would mean
    "unmodified".
    """
    if env.param_judge is None or _withheld_for_unsettled_scan(env):
        return None
    spans = entity_spans(env)
    if not spans:
        return env.param_judge
    return ParamAssessment.from_doc(redact_payload(env.param_judge.to_doc(), spans))


def _leakage_view(env: CandidateEnvelope) -> LeakageVerdict:
    """The settled S5 verdict with hit `span`s BLANKED — field and kind kept, never the raw value.

    An `in_review` envelope still stores the raw spans (they are only blanked at the promotion
    boundary), so projecting the verdict verbatim would ship the exact entity value the payload
    redaction withholds. A pre-S5 (`pending`) self-check is not a settled verdict and renders as
    an empty `pass`: the inbox never asserts a finding S5 did not settle.
    """
    scan = env.entity_scan
    if LeakageVerdict.is_settled(scan):
        verdict = LeakageVerdict.from_doc(scan)
        return replace(verdict, hits=tuple(replace(h, span="") for h in verdict.hits))
    return LeakageVerdict(result="pass", scanner="unsettled")


def _leakage_cleared(env: CandidateEnvelope) -> bool:
    """Did the S5 gate SETTLE a clean `pass` on this envelope?

    Reads the stored `entity_scan`, deliberately NOT `_leakage_view`, which renders an UNSETTLED
    scan as `pass` so the reviewer sees no phantom finding. That rendering is right for its own
    purpose and catastrophic for this one: "nobody scanned" would read as "cleared", and the one
    surface that must fail closed would open on the exact case where nothing is known.
    """
    scan = env.entity_scan
    return LeakageVerdict.is_settled(scan) and scan.get("result") == "pass"


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
    # The fail-to-review stamps (`needs_parameterization` rows only; None everywhere
    # else). Carried VERBATIM — the withholding rule lives in `decline_view`, which is
    # the only thing that may cross to the wire.
    decline: DeclineBlock | None = None
    # The judge's verdict + the artifact it named, for a fail-to-review row: the reason
    # this candidate is in front of a person rather than in the bin. Two fields, not the
    # whole assessment, and the two left out are the point — `reason` is model prose
    # ABOUT THE SESSION (entity-bearing by nature) and `confidence` is a number that
    # invites a reviewer to argue with a judgement that has already been made. A verdict
    # from a closed vocabulary and a corpus artifact id are entity-free by construction.
    judge_verdict: str = ""
    judge_covered_by: str = ""
    # Did the persisted leakage verdict come back a clean `pass`? Computed here, from the
    # envelope, because this is the last place the stored scan is in hand.
    decline_detail_cleared: bool = False
    # The `sql_template` pre-split into text / `{slot}` parts for the reviewer card. DERIVED
    # FROM `payload_view` (the redacted one) — see `_template_parts`, which owns that rule and
    # says why it is not merely a preference. Empty for everything without a template.
    template_parts: tuple[dict[str, str], ...] = ()
    # The S4 parameterization judge's verdict (design §D), REDACTED and conditionally withheld
    # by `_param_judge_view` — which owns the rule and states why. Not carried verbatim: the
    # judge is shown the UNREDACTED payload, so its `feedback` and its findings' `note` can
    # quote the very entity span the payload view withholds.
    param_judge: ParamAssessment | None = None

    def decline_view(self) -> dict[str, Any] | None:
        """The fail-to-review block AS IT MAY CROSS TO A BROWSER, or `None` for a non-decline row.

        THE DETAIL IS WITHHELD unless the leakage scan settled a clean `pass`. The text names
        predicates and their literal values, lifted from the analyst's accepted SQL, which is why it
        lives in the access-controlled candidate store (D101) and why this projection is narrower. A
        `quarantine` or `reroute` verdict means the scanners found something they could not clear; an
        UNSETTLED scan withholds for the stronger reason that nobody looked. `correction_history` is
        withheld by the same rule — it is the hint text, one round earlier. What survives is the
        SHAPE: the reason code, whether the model was re-asked, and the flag saying a detail exists
        and is being held back, so a reviewer knows to go look in the store rather than concluding
        the row has nothing to say.
        """
        if self.decline is None:
            return None
        cleared = self.decline_detail_cleared
        return {
            "reason": self.decline.reason,
            "detail": self.decline.detail if cleared else "",
            "detail_withheld": not cleared,
            "corrections_attempted": self.decline.corrections_attempted,
            "correction_history": (
                list(self.decline.correction_history) if cleared else []
            ),
            # UNGATED, unlike the detail: a verdict word and a corpus artifact id say
            # nothing about the session, and they are what tells the reviewer this row
            # was screened rather than merely dropped here.
            "judge_verdict": self.judge_verdict,
            "judge_covered_by": self.judge_covered_by,
        }

    @classmethod
    def from_envelope(cls, env: CandidateEnvelope) -> InboxItem:
        # ONE view, computed once and used for BOTH the payload and everything derived from
        # it. Not a micro-optimization: a second `_entity_free_payload_view(env)` call for the
        # template would be a second place that could later be given the raw payload instead,
        # which is exactly the leak `_template_parts` exists to refuse.
        payload_view = _entity_free_payload_view(env)
        return cls(
            candidate_id=env.candidate_id,
            type=env.type,
            status=env.status,
            reason=derive_inbox_reason(env),
            summary=_summary_of(env),
            payload_view=payload_view,
            evidence_refs=env.evidence_refs,
            entity_scan=_leakage_view(env),
            dedup=env.dedup,
            created_at=env.created_at,
            verified=env.verified,
            route_reason=env.route_reason,
            score=review_score(env),
            decline=env.decline,
            decline_detail_cleared=_leakage_cleared(env),
            judge_verdict=env.judge.verdict if env.judge is not None else "",
            judge_covered_by=env.judge.covered_by if env.judge is not None else "",
            template_parts=_template_parts(payload_view),
            param_judge=_param_judge_view(env),
        )
