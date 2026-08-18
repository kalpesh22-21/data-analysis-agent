"""ParameterizationCompleter — the human half of fail-to-review.

A `needs_parameterization` candidate is a form with holes in it: the judge said the work was
worth extracting, and the model could not classify every literal predicate of the accepted
SQL. A reviewer supplies the missing entries and the candidate re-enters the pipeline it fell
out of.

NO BYPASS, and that is the whole design: the completed payload runs the SAME `to_candidate`
validation the corrective turn ran — totality walk included — and then the SAME write-router
stages any extracted candidate runs. The one thing a human is trusted with is CONTENT, never
the checks. NO SESSION STORE either: everything the re-validation reads was snapshotted onto
the envelope at decline time, so a review item does not quietly stop being completable when
the session's TTL expires. FAILURE IS A RESULT, not an exception — a still-incomplete form is
the expected outcome, and exceptions are reserved for operations that could not be attempted
at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from ..candidate.decline import QUOTE_WITHHELD, DeclineBlock
from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.store import CandidateStore
from ..candidate.verdicts import LeakageVerdict
from ..extractor.grounding import RuleIndex
from ..extractor.models import Decline, ExtractedCandidate
from ..extractor.validation import to_candidate
from ..leakage import settle_entity_scan
from ..stage import CandidateStage, StageContext, run_pipeline
from ..triage import TriageVerdict

_logger = logging.getLogger(__name__)

# The triage verdict the re-run stages are given. NOTHING in the pipeline reads
# `ctx.verdict` today (the stages read `ctx.summary` only), so this is documentation
# with a type: it says, in the one place a future stage would look, how this candidate
# got here. `keep` because the original triage kept it — a completion never revisits
# that decision.
COMPLETION_VERDICT = TriageVerdict(
    decision="keep",
    reason="human_completed_parameterization",
    target_hints=("blueprint",),
)


class CompletionUnavailableError(RuntimeError):
    """The completion could not be ATTEMPTED — no validation plane wired, or the
    candidate carries no re-validation snapshot. Distinct from a re-validation that ran
    and declined, which is an ordinary result."""


class CompletionInputError(ValueError):
    """The reviewer-supplied entries are not a parameterization array at all."""


class CompletionRaceError(RuntimeError):
    """The candidate stopped being a form while this completion was running.

    Its own type because the CALLER must be able to tell it from a wrong-status request: the
    request was legal when it arrived, so the honest answer is "somebody moved it". Both map to
    409; only this one means the reviewer should re-read the row before deciding anything.
    """


@dataclass(frozen=True)
class CompletionResult:
    """What one completion attempt did.

    `outcome="declined"` carries the FRESH decline — subject to the same withholding rule as the
    wire projection — and the envelope is still `needs_parameterization`. `outcome="completed"`
    carries the envelope as the pipeline left it, which may be `candidate` or `in_review`; this
    path asserts nothing about which, because that is the router's decision.
    """

    outcome: Literal["completed", "declined"]
    envelope: CandidateEnvelope
    decline: DeclineBlock | None = None


def _merged_parameterization(
    payload: dict[str, Any], entries: list[Any], *, replace_all: bool
) -> list[Any]:
    """The parameterization array the re-validation will walk.

    APPEND by default, REPLACE on request, because they answer two different reviewer tasks:
    `totality_violation` means entries are MISSING, so appending leaves the model's already-valid
    classifications untouched, while `rule_predicate_mismatch` means an entry is WRONG, which no
    amount of appending fixes. Neither mode is trusted — whatever comes out goes through the same
    readers and the same D97 totality walk as model output.
    """
    if replace_all:
        return list(entries)
    existing = payload.get("parameterization")
    return [*(existing if isinstance(existing, list) else []), *entries]


def _raw_candidate(env: CandidateEnvelope, payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the raw candidate envelope `to_candidate` reads.

    The evidence QUOTES are not the candidate store's to hold (D51), so each citation is rebuilt
    from its snapshotted pointer with an explicit marker in the quote's place — satisfying D31's
    structural gate honestly, since the citations are the model's own and the reviewer supplies
    none, while keeping the entity-bearing text where it belongs.
    """
    snapshot = env.revalidation
    assert snapshot is not None  # guarded by the caller
    return {
        "type": env.type,
        "confidence": env.confidence,
        "evidence": [
            {
                "turn_ref": pointer.turn_ref,
                "tool_call_ref": pointer.tool_call_ref,
                "quote": QUOTE_WITHHELD,
            }
            for pointer in snapshot.evidence
        ],
        "rationale": env.extractor_rationale,
        "proposed_action": env.proposed_action,
        "entity_self_check": _entity_self_check(env),
        "depends_on": list(env.depends_on),
        "payload": payload,
    }


def _entity_self_check(env: CandidateEnvelope) -> dict[str, Any]:
    """Rebuild the candidate's entity attestation from wherever it now lives.

    TWO SOURCES, because the field MOVES: `build_declined_envelope` seeds `entity_scan` with the
    model's own self-check, and the S5 gate then overwrites that whole doc with its settled
    `LeakageVerdict`, which has no such key — so reading only the key returned `False` for every
    scanned row. The SETTLED verdict wins where it exists (a machine that looked beats a model
    that said it looked), with the self-check as the fallback for a row nobody scanned. Advisory
    either way, but it must not ASSERT the opposite of what is known.
    """
    scan = env.entity_scan if isinstance(env.entity_scan, dict) else {}
    if LeakageVerdict.is_settled(scan):
        # `found` stays EMPTY on purpose: the verdict's hits carry the raw `span` — the
        # entity value itself — and feeding those back into a candidate payload is the
        # one thing D17 forbids everywhere else in this file.
        return {"contains_entities": scan.get("result") != "pass", "found": []}
    return {
        "contains_entities": bool(scan.get("self_check_contains_entities", False)),
        "found": [],
    }


@dataclass(frozen=True)
class ParameterizationCompleter:
    """Re-validate a human-completed form and put it back through the pipeline.

    `known_rules`/`rule_index` MUST come from the same catalog the extractor was grounded
    against: a completer holding a different one would accept rule ids the extractor could not,
    or decline ones it would have taken. `stages` EMPTY is a legal but degraded wiring — the
    candidate re-validates and is persisted at `extracted` with no generalization and an
    unsettled scan, which every downstream guard refuses, so it can never be approved. Legal
    because an offline dev inbox has no write plane; logged, because it is not obvious from the
    200 the reviewer gets.
    """

    store: CandidateStore
    known_rules: frozenset[str] = frozenset()
    rule_index: RuleIndex | None = None
    stages: tuple[CandidateStage, ...] = field(default_factory=tuple)

    async def complete(
        self,
        env: CandidateEnvelope,
        *,
        entries: list[Any],
        replace_all: bool = False,
    ) -> CompletionResult:
        if env.revalidation is None:
            raise CompletionUnavailableError(
                f"completion_unavailable: candidate {env.candidate_id!r} carries no "
                "re-validation snapshot, so the completed form cannot be checked "
                "against the accepted SQL it must cover"
            )
        if not isinstance(entries, list) or not all(
            isinstance(item, dict) for item in entries
        ):
            raise CompletionInputError(
                "entries must be an ARRAY of parameterization objects, each with "
                '"locator", "role" and the field that role requires'
            )

        summary = env.revalidation.to_summary()
        payload = dict(env.payload)
        payload["parameterization"] = _merged_parameterization(
            payload, entries, replace_all=replace_all
        )
        outcome = to_candidate(
            _raw_candidate(env, payload),
            summary,
            known_rules=self.known_rules,
            rule_index=self.rule_index,
        )
        if isinstance(outcome, Decline):
            return await self._still_declined(env, payload, outcome, summary)
        return await self._completed(env, outcome, summary)

    async def _still_declined(
        self,
        env: CandidateEnvelope,
        payload: dict[str, Any],
        decline: Decline,
        summary,
    ) -> CompletionResult:
        """The form is still not complete: keep the row, keep the reviewer's work.

        The MERGED payload is persisted even though it failed, so the next attempt starts from what
        the reviewer already wrote rather than from the model's original. The correction COUNT is
        preserved from the original block: it records what the MODEL was asked, and a human's attempt
        is not a corrective turn.

        THE SCAN IS RE-SETTLED, because the payload CHANGED. The stored verdict was settled about
        different content, and it is exactly what the wire projection consults before showing the
        decline detail to a browser. For today's shapes the re-scan usually returns the same verdict
        (a completion merges `parameterization`, which the gate deliberately does not scan); what
        changes is that the verdict is MEASURED against what is being stored. With no gate wired it
        goes back to `pending`, which fails closed everywhere.
        """
        block = DeclineBlock(
            reason=decline.reason,
            detail=decline.detail,
            corrections_attempted=(
                env.decline.corrections_attempted if env.decline is not None else 0
            ),
            correction_history=(
                env.decline.correction_history if env.decline is not None else ()
            ),
        )
        updated = replace(env, payload=payload, decline=block)
        updated = replace(
            updated,
            entity_scan=await settle_entity_scan(
                self.stages, updated, StageContext(summary=summary, verdict=COMPLETION_VERDICT)
            ),
        )
        await self._guarded_put(env, updated)
        _logger.info(
            "learning inbox: completion of %s still declines %s — the candidate stays "
            "at %s with the fresh decline recorded",
            env.candidate_id, decline.reason, CandidateStatus.NEEDS_PARAMETERIZATION,
        )
        return CompletionResult(outcome="declined", envelope=updated, decline=block)

    async def _completed(
        self,
        env: CandidateEnvelope,
        candidate: ExtractedCandidate,
        summary,
    ) -> CompletionResult:
        """Re-validation passed: rebuild an ORDINARY candidate and run the pipeline.

        `replace` on the existing envelope rather than `build_envelope`, deliberately: the identity
        and the history are the ones already in the store — same `candidate_id` (so the review row
        transitions in place instead of forking), `content_hash`, `created_at` and `session_signals`,
        which `build_envelope` would recompute from a RECONSTRUCTED summary whose transcript is empty
        by design.

        THREE FIELDS ARE DELIBERATELY CLEARED: `decline`, because a block left here would keep the
        inbox rendering an outstanding task for ever; `revalidation`, whose only reader is this path;
        and `entity_scan`, back to the S3 `pending` sentinel because THE PAYLOAD CHANGED — carrying
        the old verdict forward would let text nobody scanned ride a `pass` settled about different
        content. The status returns to `extracted` for the same reason: it is what the pipeline
        expects to be handed, and the router decides where it goes from there.
        """
        rebuilt = replace(
            env,
            status=CandidateStatus.EXTRACTED,
            payload=candidate.payload_to_doc(),
            confidence=candidate.header.confidence,
            proposed_action=candidate.header.proposed_action,
            depends_on=candidate.header.depends_on,
            extractor_rationale=candidate.header.rationale,
            entity_scan={
                "result": "pending",
                "hits": [],
                "self_check_contains_entities": (
                    candidate.header.entity_self_check.contains_entities
                ),
            },
            decline=None,
            revalidation=None,
        )
        if not self.stages:
            _logger.warning(
                "learning inbox: completed %s with NO write-router pipeline wired — it "
                "is persisted at %s with no generalization and an unsettled entity "
                "scan, which every approve guard refuses. This is the offline dev "
                "posture; a deployment that means to land completions must wire the "
                "stages.",
                env.candidate_id, CandidateStatus.EXTRACTED,
            )
            await self._guarded_put(env, rebuilt)
            return CompletionResult(outcome="completed", envelope=rebuilt)

        outcome = await run_pipeline(
            self.stages,
            rebuilt,
            StageContext(summary=summary, verdict=COMPLETION_VERDICT),
        )
        landed = self._settled(outcome)
        await self._guarded_put(env, landed)
        _logger.info(
            "learning inbox: %s completed by a reviewer and re-ran the write-router "
            "pipeline → status=%s (control=%s)",
            env.candidate_id, landed.status, outcome.control,
        )
        return CompletionResult(outcome="completed", envelope=landed)

    @staticmethod
    def _settled(outcome) -> CandidateEnvelope:
        """The envelope this completion leaves in the store — INCLUDING when the pipeline said `drop`.

        `drop` means a stage handled the candidate elsewhere: S6 bumps the existing artifact's count
        and drops the duplicate, or drops it as redundant with the canon. On the EXTRACTION path "do
        not persist" is harmless, because the consumer already wrote the row at `extracted` before
        the stages ran. On THIS path there is a row already, and it says `needs_parameterization`:
        not persisting left it saying that FOR EVER while the response said "completed", so the row
        relisted and every re-completion bumped the same corpus counter again. Not a corner case
        either — a re-processed session mints a fresh review item for a blueprint that may have
        landed on the earlier run, and completing it is GUARANTEED to hit the hard key and drop.

        So the enriched envelope is persisted whatever the control said, at the status the pipeline
        left it. The one thing forced is that it is NOT the review status: a completed form must
        never be a form again.
        """
        env = outcome.envelope
        if env.status == CandidateStatus.NEEDS_PARAMETERIZATION:
            return replace(env, status=CandidateStatus.EXTRACTED)
        return env

    async def _guarded_put(
        self, before: CandidateEnvelope, updated: CandidateEnvelope
    ) -> None:
        """Persist *updated*, unless the row stopped being a form while we were working.

        BEST-EFFORT, and the honest name for it is a NARROWED window rather than a closed one:
        `CandidateStore` has no compare-and-swap, so between this re-read and the `put` a concurrent
        reject can still be overwritten. What it closes is the WIDE window — the whole re-validation,
        which parses SQL, rewrites a template, scans for entities and possibly embeds. The direction
        of the failure decides the posture: silently resurrecting a REJECTED candidate re-enters work
        a human deliberately removed (D29), while refusing a completion that raced costs one retry
        against a row the reviewer is about to re-read anyway.
        """
        current = await self.store.get(before.candidate_id)
        if current is None or current.status != before.status:
            raise CompletionRaceError(
                f"candidate {before.candidate_id!r} changed while the completion was "
                f"running (was {before.status!r}, now "
                f"{current.status if current is not None else 'absent'!r}); nothing was "
                "written — re-read the row before deciding"
            )
        await self.store.put(updated)


__all__ = [
    "COMPLETION_VERDICT",
    "CompletionInputError",
    "CompletionRaceError",
    "CompletionResult",
    "CompletionUnavailableError",
    "ParameterizationCompleter",
]
