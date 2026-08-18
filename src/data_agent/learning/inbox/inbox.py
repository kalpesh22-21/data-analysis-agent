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
from typing import Literal

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.store import CandidateStore
from ..promotion.mcp_export import PromotionEmit, build_promotion_emit
from ..promotion.models import ProbeResult, PromotionPolicy
from ..promotion.scheduler import PromotionScheduler
from .completion import CompletionResult, ParameterizationCompleter
from .models import InboxItem
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


class ReviewInbox:
    """The `in_review` projection over a `CandidateStore` + the human transitions."""

    def __init__(
        self,
        store: CandidateStore,
        *,
        scheduler: PromotionScheduler | None = None,
        policy: PromotionPolicy | None = None,
        completer: ParameterizationCompleter | None = None,
    ) -> None:
        self._store = store
        # The fail-to-review completion plane. OPTIONAL and default-absent, so an inbox
        # built without one behaves exactly as it did before the slice — except that a
        # completion attempt is refused LOUDLY (503) instead of silently doing nothing.
        # It is not folded into the scheduler: the scheduler owns transitions of a
        # candidate that already passed validation, and this one owns re-running the
        # validation itself.
        self._completer = completer
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
