"""ReviewInbox — the `in_review` projection + the human transitions (§4).

A PROJECTION over `learning_candidates` (D101) adding NO second store: `list()` reads by
status and maps each envelope to an `InboxItem`, and a human's action advances `status`.

    approve  → in_review → validated
    reject   → in_review | needs_parameterization → rejected   (archived as a NEGATIVE
                                                                training signal, NOT a delete)
    complete → needs_parameterization → extracted → (the write router decides)
    retract  → validated → retired          (a post-promotion pull-from-index)
    verify   → validated → validated        (Phase-3: flip `verified` on node + envelope)
    promote  → validated → promoted         (Phase-3: emit the MCP YAML for a MANUAL PR)

ONE APPROVE IMPLEMENTATION (R4): the inbox does NOT re-implement the invariants — the D17
entity strip, the `depends_on` guard, the static/replay guards — it guards the current status
fail-loud and DELEGATES to `PromotionScheduler.apply_human_decision`. An illegal transition
raises `InboxTransitionError`, so a mis-routed action never silently mutates a candidate.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Literal

from data_agent.timeutil import now_iso

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.store import CandidateStore
from ..candidate.verdicts import (
    LeakageAttestation,
    LeakageVerdict,
    leakage_fingerprint,
)
from ..promotion.mcp_export import PromotionEmit, build_promotion_emit
from ..promotion.models import ProbeResult, PromotionPolicy
from ..promotion.scheduler import PromotionScheduler
from ..revise import BlueprintReviser, ReviseProposal, ReviserUnavailableError
from .completion import CompletionResult, ParameterizationCompleter
from .models import InboxItem, _leakage_cleared
from .ranking import rank_key

_logger = logging.getLogger(__name__)


class InboxTransitionError(Exception):
    """Raised when a human transition is requested from an illegal current status."""


class _NoOpProbe:
    """A no-op warehouse probe for an UNWIRED inbox (no scheduler injected).

    Only reached if an approve replays a blueprint template; a production inbox injects the real
    scheduler and probe.
    """

    async def run(
        self,
        sql: str,
        *,
        grain_columns: tuple[str, ...],
        column_scope: tuple[str, ...] = (),
    ) -> ProbeResult:
        return ProbeResult(row_count=0, distinct_grain_count=None, columns=())


class _ZeroHitCounts:
    async def hit_count(self, canonical_key: str) -> int:
        return 0


# The statuses a parameterization revision may be proposed for and applied to.
#
# `needs_parameterization` is the form: entries are MISSING and the decline names which.
# `in_review` is a candidate a human is adjudicating and can see is wrong — a slot that should
# be frozen, a `why` that explains nothing. Both re-run the SAME `to_candidate` re-validation
# and the SAME write-router stages, so neither is a way around a check; the second one was
# impossible until kept candidates started carrying a `ValidationSnapshot`
# (`build_candidate_envelope`), because there was no accepted SQL to re-validate against.
#
# ⚠ NOT `validated` or `promoted`. Those have passed golden replay and may already be landed,
# so editing one means re-verifying and re-landing an artifact the corpus is serving — a
# different operation with a different blast radius, and `retract` is how that is expressed.
REVISABLE_STATUSES: tuple[str, ...] = (
    CandidateStatus.NEEDS_PARAMETERIZATION,
    CandidateStatus.IN_REVIEW,
)


class ReviewInbox:
    """The `in_review` projection over a `CandidateStore` + the human transitions."""

    def __init__(
        self,
        store: CandidateStore,
        *,
        scheduler: PromotionScheduler | None = None,
        policy: PromotionPolicy | None = None,
        completer: ParameterizationCompleter | None = None,
        reviser: BlueprintReviser | None = None,
    ) -> None:
        self._store = store
        # The fail-to-review completion plane. OPTIONAL and default-absent, so an inbox
        # built without one behaves exactly as it did before the slice — except that a
        # completion attempt is refused LOUDLY (503) instead of silently doing nothing.
        # It is not folded into the scheduler: the scheduler owns transitions of a
        # candidate that already passed validation, and this one owns re-running the
        # validation itself.
        self._completer = completer
        # The LLM typing aid for that same form (design §C). OPTIONAL and default-absent,
        # like the completer — and, unlike the completer, absent changes NOTHING about what a
        # reviewer can accomplish: the raw-entries form still works. It writes nothing; see
        # `propose_revision`.
        self._reviser = reviser
        # The SINGLE approve/reject implementation (R4). Defaulted for an unwired
        # inbox; production injects the wired scheduler.
        self._scheduler = scheduler or PromotionScheduler(
            store, probe=_NoOpProbe(), hit_counts=_ZeroHitCounts(),
        )
        # Plan §4: the inbox needs exactly ONE knob off the promotion policy
        # (`review_score_cutoff`).
        #
        # It defaults to the SCHEDULER's policy, never to a fresh `PromotionPolicy()`.
        # The fresh-default version re-created, one level down, the exact defect this
        # slice was written to remove: a caller who built a correctly-configured
        # scheduler and passed it here would silently get cutoff 0.0 — a knob turned in
        # the environment and ignored at the surface it governs. `build_promotion_plane`
        # passes it explicitly, but a direct `ReviewInbox(store, scheduler=...)` is a
        # supported construction (both demo scripts and every test in this suite use it),
        # and correctness must not depend on the caller remembering.
        #
        # `self._scheduler` is always set by the line above, and an unwired inbox's
        # default scheduler carries a default policy — so this changes nothing for the
        # unwired case and removes the split for the wired one.
        self._policy = policy if policy is not None else self._scheduler.policy

    async def list(
        self,
        *,
        status: str = CandidateStatus.IN_REVIEW,
        limit: int = 100,
        order: Literal["asc", "desc"] = "asc",
    ) -> list[InboxItem]:
        """The inbox projection for *status* (default `in_review`, the review queue).

        The archive view passes `status=rejected` so the SAME projection serves the Archived tab,
        with `order="desc"` chosen explicitly by the caller — never inferred from the status string
        inside the store — so the LIMIT trims OLD history rather than present rejects.

        ONLY the review queue is RANKED (plan §4), by `novelty × groundedness² × session-quality`
        descending, MEASURED rows first, arrival order breaking ties, filtered by
        `review_score_cutoff`. The other listings are left alone on purpose: `rejected` is an ARCHIVE
        whose newest-first contract the LIMIT depends on; `validated` is the verify/promote worklist,
        whose rows have already been through a human once; `needs_parameterization` is a WORK list
        whose entry condition already answered "is this worth extracting".

        THE LIMIT IS APPLIED BY THE STORE, BEFORE THE RANKING — a real limitation rather than an
        oversight: the ranking inputs live inside the candidate document, so ranking the whole
        `in_review` population would mean fetching it all. Past 100 rows the caller ranks the oldest
        100, not the best 100; fixing it properly means ranking server-side, which needs the score
        materialized.
        """
        envelopes = await self._store.list_by_status(status, limit=limit, order=order)
        if status != CandidateStatus.IN_REVIEW:
            return [InboxItem.from_envelope(env) for env in envelopes]
        # Rank the ENVELOPES, then project — rather than projecting and then sorting the
        # items by a re-derived key. Two reasons, and the second is the one that bites:
        # the order and the projected score then come from ONE computation and cannot
        # disagree, and there is no id-keyed map to join the two lists back together (a
        # `candidate_id` read off a rehydrated doc with no type check need not even be
        # hashable, and building that map would have been the crash site).
        ranked = sorted(envelopes, key=rank_key)
        items = [InboxItem.from_envelope(env) for env in ranked]
        return self._apply_cutoff(items)

    def _apply_cutoff(self, items: list[InboxItem]) -> list[InboxItem]:
        """Drop review-queue rows below `review_score_cutoff` — MEASURED rows only.

        A row whose novelty or groundedness could not be measured carries a NEUTRAL 1.0 on that axis,
        and novelty's measured ceiling against a real corpus is ~0.47 — so filtering both groups on
        one number would hide the candidates we know most about and keep the ones we know nothing
        about, the knob doing the exact opposite of what its name says. A cutoff is a judgement about
        a score, and an unmeasured row has no score to judge; it is never in the way either, because
        the sort has already put every unmeasured row below every measured one.

        The consequence, stated rather than hidden: a queue dominated by unmeasured rows cannot be
        trimmed with this knob. That is a signal, not a defect — the fix is to wire the prior-art
        index. An operator who empties their own inbox is TOLD.
        """
        cutoff = self._policy.review_score_cutoff
        if cutoff <= 0.0 or not items:
            return items
        kept = [
            item for item in items if item.score.measured is False or item.score.score >= cutoff
        ]
        if not kept:
            _logger.warning(
                "review_score_cutoff=%.3f hid ALL %d row(s) of a non-empty review queue. "
                "The score is an ORDERING, not a percentage, and it does not use the top "
                "of its range: sentence-embedding cosines floor around 0.53, so novelty "
                "lives in roughly [0, 0.47] and a PERFECT candidate scores about 0.26. A "
                "moderate-looking cutoff hides everything. Set it from the scores you "
                "actually observe (they are on every item as `score`), or back to 0.0.",
                cutoff,
                len(items),
            )
        return kept

    async def _require(self, candidate_id: str, expected: str) -> CandidateEnvelope:
        env = await self._store.get(candidate_id)
        if env is None:
            raise InboxTransitionError(f"candidate {candidate_id!r} not found")
        if env.status != expected:
            raise InboxTransitionError(
                f"candidate {candidate_id!r} is {env.status!r}, expected {expected!r}"
            )
        return env

    async def approve(self, candidate_id: str) -> CandidateEnvelope:
        """Human approve: `in_review → validated`, delegating to `apply_human_decision` (D17/R4).

        A guard that HOLDS — an unresolved `depends_on`, a missing generalization, a failed replay —
        leaves the candidate `in_review`. That is NOT a success, so a held approve surfaces as an
        `InboxTransitionError` carrying the hold reason rather than as an unchanged envelope.
        """
        await self._require(candidate_id, CandidateStatus.IN_REVIEW)
        env = await self._store.get(candidate_id)
        decision = await self._scheduler.apply_human_decision(env, "approve")
        if decision.action != "approve":
            raise InboxTransitionError(
                f"approve held for {candidate_id}: {decision.reason}"
            )
        return await self._store.get(candidate_id)

    async def reject(self, candidate_id: str) -> CandidateEnvelope:
        """Human reject: `in_review | needs_parameterization → rejected`. A NEGATIVE signal, not a delete.

        The row is retained for the S9 learner (D29). `needs_parameterization` is accepted because
        reject writes no content, so gating it would leave a row with NO terminal action at all —
        completable only by a human who may have decided the form has no honest answer, and otherwise
        clearable only by waiting out a 90-day TTL.

        RACE, stated because it is real and unclosed here: the guard reads the envelope and
        `apply_human_decision` writes it back, so a completion finishing in between is overwritten by
        this reject. That direction is the SAFE one — the human's "no" wins and nothing lands — where
        the opposite is refused by `completion.py::_guarded_put`. Both windows close properly only
        with a CAS on the candidate store.
        """
        env = await self._require_one_of(
            candidate_id,
            (CandidateStatus.IN_REVIEW, CandidateStatus.NEEDS_PARAMETERIZATION),
        )
        await self._scheduler.apply_human_decision(env, "reject")
        return await self._store.get(candidate_id)

    async def _require_one_of(
        self, candidate_id: str, expected: tuple[str, ...]
    ) -> CandidateEnvelope:
        env = await self._store.get(candidate_id)
        if env is None:
            raise InboxTransitionError(f"candidate {candidate_id!r} not found")
        if env.status not in expected:
            raise InboxTransitionError(
                f"candidate {candidate_id!r} is {env.status!r}, expected one of "
                f"{sorted(expected)!r}"
            )
        return env

    async def complete_parameterization(
        self,
        candidate_id: str,
        *,
        entries: list,
        replace_all: bool = False,
    ) -> CompletionResult:
        """FILL IN the form of a `needs_parameterization` candidate and re-run the pipeline.

        Guarded on `needs_parameterization` specifically — NOT on `in_review` — so this can never be
        used as a second, unvalidated route into an ordinary review item's payload; approve is
        guarded the other way round, so the two surfaces cannot be crossed. Returns the
        `CompletionResult` rather than an envelope, because "the form is still incomplete" is an
        outcome the caller must be able to SHOW, not an error to map to a status code.

        ⚠ The APPEND mode is why this guard stays an equality. `_merged_parameterization`
        concatenates, so a second submission — a double-click, a stale tab, a redelivery —
        would double the entries of a candidate that has already moved on. The status is what
        makes that unreachable. Editing an `in_review` candidate is a DIFFERENT operation with
        a different safety argument: see `apply_revision`, which is replace-only and therefore
        idempotent.
        """
        env = await self._require(candidate_id, CandidateStatus.NEEDS_PARAMETERIZATION)
        if self._completer is None:
            raise InboxTransitionError(
                f"completion_unavailable: no validation plane is wired for "
                f"{candidate_id!r}, so the completed parameterization cannot be "
                "re-validated against the accepted SQL"
            )
        return await self._completer.complete(
            env, entries=entries, replace_all=replace_all
        )

    async def propose_revision(
        self, candidate_id: str, *, feedback: str
    ) -> ReviseProposal:
        """Ask the reviser for parameterization entries. WRITES NOTHING.

        Guarded on `REVISABLE_STATUSES` — the SAME guard as `complete_parameterization`, so the
        assistant is never offered toward an operation the completer would then refuse.

        Both statuses became reachable once `build_candidate_envelope` started stamping a
        `ValidationSnapshot` on kept candidates. Before that, `in_review` was structurally
        impossible rather than merely disallowed: the snapshot was written at DECLINE time
        only, so such a candidate had no accepted SQL to propose against. That was design
        §C.4's deferral, and stamping it is what §C.4 named as the unlock.

        ⚠ A candidate carrying no snapshot at all (extracted before that change) still degrades
        cleanly — `BlueprintReviser.propose` returns a proposal with a reason saying so, rather
        than raising.

        The proposal is returned, not applied. The reviewer applies it through `complete`,
        which is and remains the only write path into a candidate's payload.
        """
        env = await self._require_one_of(candidate_id, REVISABLE_STATUSES)
        if self._reviser is None:
            raise ReviserUnavailableError(
                f"revise_unavailable: no reviser is wired for {candidate_id!r}; the "
                "parameterization entries can still be supplied directly"
            )
        # ⚠ THE SAME WITHHOLDING RULE THE DECLINE DETAIL OBEYS (`InboxItem.decline_view`).
        #
        # A proposal is entity-BEARING BY CONSTRUCTION and cannot be redacted the way the
        # payload view and the judge verdict are: every entry carries a `locator.value` lifted
        # verbatim from the accepted SQL, the diff interpolates those values into its rows, and
        # the rationale is model prose about the unredacted query. Redacting them would also
        # destroy them — an entry whose value is `[redacted]` matches no literal and applies to
        # nothing.
        #
        # So this surface REFUSES where the others redact. Without it, a candidate whose scan
        # settled `quarantine` renders a card that withholds its decline detail and then hands
        # back the same literals the moment the reviewer clicks the assistant — through a route
        # added to make that form easier, past the one rule the form is careful about. An
        # UNSETTLED scan refuses for the stronger reason: nobody looked.
        if not _leakage_cleared(env):
            scan = env.entity_scan if isinstance(env.entity_scan, dict) else {}
            flagged = ", ".join(
                sorted(
                    {
                        f"{hit.get('field')} ({hit.get('kind')})"
                        for hit in (scan.get("hits") or [])
                        if isinstance(hit, dict) and hit.get("field")
                    }
                )
            )
            # ⚠ SAY ONLY WHAT IS TRUE AND ACTIONABLE. The first version of this message ended
            # "or clear the scan first", which named an operation that DOES NOT EXIST anywhere
            # in the plane — there is no re-scan or override action on any surface. It also
            # said "supply the entries directly", which is only possible on the FORM: a review
            # queue card has no entries box, so on `in_review` it advised a control that is not
            # on the page either. Both readings sent a reviewer looking for a button.
            #
            # What is true: the scan verdict is the gate, a reviewer cannot change it, and the
            # only way it is re-evaluated is a re-extraction of the session. Naming the flagged
            # FIELD AND KIND (never the span — that is the value being withheld) is what turns
            # this from a refusal into something a human can judge: `sql_template (person)` on
            # a query full of enum literals reads as the false positive it usually is.
            supply = (
                " You can still type the entries into the form below."
                if env.status == CandidateStatus.NEEDS_PARAMETERIZATION
                else ""
            )
            return self._reviser.refuse_withheld(
                env,
                reason=(
                    "the assistant is unavailable for this candidate: its entity scan settled "
                    f"{scan.get('result') or 'unsettled'}"
                    + (f" on {flagged}" if flagged else "")
                    + ". A proposal necessarily quotes the accepted SQL's literal values — the "
                    "same ones this row is withholding — so it is refused rather than redacted "
                    "(a redacted literal matches no predicate). The verdict is not something "
                    "this surface can change; it is re-evaluated only when the session is "
                    "re-extracted." + supply
                ),
            )
        return await self._reviser.propose(env, feedback=feedback)

    async def apply_revision(
        self, candidate_id: str, *, entries: list
    ) -> CompletionResult:
        """Apply a revision to a candidate ALREADY under review (`in_review`).

        A SEPARATE verb from `complete_parameterization`, and the split is the safety argument
        rather than tidiness:

          * `complete` fills in a FORM. Its entries APPEND, because the reviewer is supplying
            what is missing and must not have to retype the model's valid classifications. That
            makes it non-idempotent, which is exactly why its guard is an equality against
            `needs_parameterization` — a double-click on a row that has moved on would double
            the entries.
          * this REPLACES the parameterization of a candidate whose form is already complete.
            The reviewer is correcting a role, not filling a gap, so `replace_all=True` is both
            the right semantics AND idempotent: applying the same entries twice yields the same
            payload. A stale tab or a double-click costs one redundant re-validation, never a
            corrupted array.

        `replace_all` is therefore NOT a parameter. Offering it would reintroduce the append
        mode on the one status whose guard cannot protect it.

        Everything else is the completer's usual path — `to_candidate` (totality walk included)
        then the full write-router stages — so the revision is RE-ADJUDICATED, not admitted: a
        payload whose leakage scan still fails routes back to review, and one whose dedup
        verdict changes re-routes on the writer's rules. Nothing here moves a candidate
        FORWARD; `approve` remains the only thing that does, and it stays `in_review`-only.
        """
        env = await self._require(candidate_id, CandidateStatus.IN_REVIEW)
        if self._completer is None:
            raise InboxTransitionError(
                f"completion_unavailable: no validation plane is wired for "
                f"{candidate_id!r}, so the revised parameterization cannot be "
                "re-validated against the accepted SQL"
            )
        return await self._completer.complete(env, entries=entries, replace_all=True)

    async def attest_scan(self, candidate_id: str, *, note: str) -> CandidateEnvelope:
        """Record a reviewer's statement that a leakage finding is a FALSE POSITIVE.

        Motivated by a real case: an NER scanner flagged an 8-character leave-type enum inside
        `event_type = '<...> Request'` as a `person`, quarantining a time-off blueprint with no
        way to unblock it. Nothing in the plane could clear a verdict, so a false positive
        blocked the assistant permanently.

        WHAT IT DOES NOT DO, and each is deliberate:

          * it does NOT rewrite `entity_scan`. The scanner's finding is the record of what a
            machine saw; a human disagreeing is a second fact stored beside it, so both survive
            and the disagreement itself is queryable;
          * it does NOT make the candidate auto-promotable. `_entity_scan_is_clean` — the
            AUTOMATIC edge, which exists for the path where "nobody is looking there" — never
            consults it;
          * it does NOT stop redaction. The strip costs nothing if the attestation is right.

        The one gate it clears is `inbox/models.py::_leakage_cleared`: the assistant and the
        decline-detail display, both of which a human is looking at when it happens.

        REQUIRES A SETTLED, NON-PASS VERDICT. Attesting to an unsettled scan would be a human
        vouching for content NOBODY has looked at, which is the opposite of the point; a clean
        pass has nothing to attest to. And the attestation binds to THESE findings, so it
        lapses the moment the scan re-settles differently.
        """
        env = await self._require_one_of(candidate_id, REVISABLE_STATUSES)
        scan = env.entity_scan if isinstance(env.entity_scan, dict) else {}
        if not LeakageVerdict.is_settled(scan):
            raise InboxTransitionError(
                f"candidate {candidate_id!r} has no SETTLED leakage verdict to attest to — "
                "attesting to an unscanned candidate would vouch for content nobody has "
                "looked at"
            )
        if scan.get("result") == "pass":
            raise InboxTransitionError(
                f"candidate {candidate_id!r} already has a clean leakage pass; there is "
                "nothing to override"
            )
        hits = scan.get("hits") if isinstance(scan.get("hits"), list) else []
        attestation = LeakageAttestation(
            scan_fingerprint=leakage_fingerprint(scan),
            attested_at=now_iso(),
            note=note,
            hit_count=len(hits),
        )
        _logger.warning(
            "leakage OVERRIDE on %s: a reviewer attested %d finding(s) (%s) are false "
            "positives — reason: %s. The verdict itself is unchanged and the candidate is "
            "still not auto-promotable.",
            candidate_id,
            len(hits),
            ", ".join(
                sorted({f"{h.get('field')}/{h.get('kind')}" for h in hits if isinstance(h, dict)})
            )
            or "none",
            note,
        )
        await self._store.put(replace(env, leakage_attestation=attestation))
        return await self._store.get(candidate_id)

    async def retract(self, candidate_id: str) -> CandidateEnvelope:
        """Retract a promoted artifact: `validated → retired` (a leak/drift pull).

        DELEGATES to the single `apply_retract` path, so the leak PULL stamps the landed neo4j node
        `retired` (fail-open) BEFORE the store retire and the recall filter excludes it immediately.
        The physical index removal and the D25 exposure trace remain S10; the STAMP is what closes
        the recall exposure here and now.
        """
        env = await self._require(candidate_id, CandidateStatus.VALIDATED)
        await self._scheduler.apply_retract(env)
        return await self._store.get(candidate_id)

    async def verify(self, candidate_id: str) -> tuple[CandidateEnvelope, bool]:
        """VERIFY an auto-landed learning node (Phase-3): flip `verified` on node AND envelope.

        Requires the current status to be `validated` — every validated candidate in the store is
        `source='learning'` by construction. DELEGATES to `apply_verify`. Returns
        `(env, node_stamped)`, where `node_stamped` is False when the neo4j write did not land (no
        writer, never landed, or a fail-open error) so the caller can prompt a re-verify; the
        envelope reads `verified=true` regardless, and a re-verify converges the node.
        """
        env = await self._require(candidate_id, CandidateStatus.VALIDATED)
        _decision, node_stamped = await self._scheduler.apply_verify(env)
        return await self._store.get(candidate_id), node_stamped

    async def promote(
        self,
        candidate_id: str,
        *,
        doc_id: str | None = None,
        title: str | None = None,
    ) -> PromotionEmit:
        """PROMOTE a verified learning node (Phase-3): emit the MCP-format YAML for a MANUAL PR.

        The FIRST promote (from `validated`, requiring `verified`) also moves the candidate to
        `promoted`; a re-promote RE-EMITS the same YAML with NO status move, so an abandoned or lost
        PR can always be regenerated. Any other status is a fail-loud transition error. The YAML `id`
        is the landing id VERBATIM so a later reseed flips THAT SAME node instead of duplicating it;
        `doc_id`/`title` are optional knowledge refinements and `id` can NEVER be overridden. The
        move is OPTIMISTIC: the actual `learning → mcp` reseed happens only when the human MERGES,
        and until then the node stays excluded from recall.
        """
        env = await self._store.get(candidate_id)
        if env is None:
            raise InboxTransitionError(f"candidate {candidate_id!r} not found")
        # Idempotent re-emit for an already-promoted candidate: regenerate the YAML with
        # NO status move (pure build), so a lost/abandoned PR can always be recreated.
        if env.status == CandidateStatus.PROMOTED:
            return build_promotion_emit(env, doc_id=doc_id, title=title)
        if env.status != CandidateStatus.VALIDATED:
            raise InboxTransitionError(
                f"candidate {candidate_id!r} is {env.status!r}, "
                "expected 'validated' or 'promoted'"
            )
        if not env.verified:
            raise InboxTransitionError(
                f"candidate {candidate_id!r} is not verified; verify it before promoting"
            )
        # Emit FIRST (pure — raises on a malformed/non-landable candidate before any
        # store move), then move to the terminal `promoted` state via the single writer.
        emit = build_promotion_emit(env, doc_id=doc_id, title=title)
        decision = await self._scheduler.apply_promote(env)
        if decision.action != "promote_emit":
            # A held promote (e.g. a racing status change) must NOT return 200 + YAML with
            # the candidate left validated — surface the hold reason fail-loud (mirrors
            # `approve`).
            raise InboxTransitionError(
                f"promote held for {candidate_id}: {decision.reason}"
            )
        return emit
