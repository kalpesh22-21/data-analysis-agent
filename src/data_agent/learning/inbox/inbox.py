"""ReviewInbox — the `in_review` projection + the human transitions (§4).

The inbox is a PROJECTION over `learning_candidates` (D101): `list()` reads the
store by `status == "in_review"` and maps each envelope to an `InboxItem`. It adds
NO second store; it only advances `status` on the human's action — the ONLY
caller-driven transitions in the write router:

    approve  → in_review → validated   (knowledge becomes retrievable; blueprint
                                         confirmed; schema_edit opens the D53 PR — the
                                         human MERGE is the real gate, out of scope here)
    reject   → in_review → rejected     (archived as a NEGATIVE training signal — D29;
                                         NOT a delete: the row stays for the S9 learner)
    retract  → validated → retired      (a post-promotion pull-from-index; the physical
                                         index removal + D25 exposure trace are S10, §11.4)
    verify   → validated → validated    (Phase-3: flip `verified=true` on the landed node
                                         + envelope so a human vouches for a learning node)
    promote  → validated → promoted     (Phase-3: emit the MCP-format YAML for a MANUAL PR;
                                         requires `verified`; terminal `promoted` state)

**One approve implementation (R4).** `approve`/`reject` are the caller-driven
promotion transitions. To guarantee EVERY approve enforces the same invariants (the
D17 entity strip, the `depends_on` guard, and the static/replay guards for a
replayable blueprint), the inbox does NOT re-implement them — it fetches + guards
the current status (fail-loud) and DELEGATES the transition to the single
`PromotionScheduler.apply_human_decision` implementation. A production inbox injects
the wired scheduler; an unwired inbox builds a default one (its guards are guard
functions of the envelope + injected collaborators, so an unwired approve of a
non-replayable candidate still strips + validates).

Transitions are guarded: `approve`/`reject` require the current status to be
`in_review`; `retract` requires `validated`. An illegal transition raises
`InboxTransitionError` (fail-loud — a mis-routed action never silently mutates a
candidate).
"""

from __future__ import annotations

import logging
from typing import Literal

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.store import CandidateStore
from ..promotion.mcp_export import PromotionEmit, build_promotion_emit
from ..promotion.models import ProbeResult, PromotionPolicy
from ..promotion.scheduler import PromotionScheduler
from .models import InboxItem
from .ranking import rank_key

_logger = logging.getLogger(__name__)


class InboxTransitionError(Exception):
    """Raised when a human transition is requested from an illegal current status."""


class _NoOpProbe:
    """A no-op warehouse probe for an UNWIRED inbox (no scheduler injected). Only
    reached if an approve replays a blueprint template; a production inbox injects
    the real scheduler + probe."""

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
    ) -> None:
        self._store = store
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
        # the environment and ignored at the surface it governs. `build_review_inbox`
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

        The archive view passes `status=rejected` (durable rejected rows, D29) so the
        SAME projection also serves the Archived tab (ui-inbox-type-archive contract).
        The caller passes `order="desc"` for the archive so the LIMIT trims OLD history,
        not present rejects — the ordering is chosen explicitly here, never inferred from
        the status string inside the store.

        **The REVIEW QUEUE — and only the review queue — is RANKED (plan §4)**, by
        `novelty × groundedness² × session-quality` descending, MEASURED rows first,
        arrival order breaking ties, and filtered by `review_score_cutoff`. The other two
        listings are left exactly as they were on purpose:

          * `rejected` is an ARCHIVE. Ranking history by how interesting it would have
            been is meaningless, and reordering it would break the newest-first contract
            the LIMIT depends on to trim old rows rather than present ones.
          * `validated` is the Phase-3 verify/promote worklist. Its rows have already been
            through a human once; "is this worth thirty seconds" is not the question being
            asked of them.

        **The LIMIT is applied by the store, BEFORE the ranking**, and that is a real
        limitation rather than an oversight: the ranking inputs live inside the candidate
        document, so ranking the whole `in_review` population would mean fetching it all.
        With `limit=100` and a queue smaller than that (every deployment today — the store
        is empty) the two are identical. Past 100 in_review rows the caller ranks the
        oldest 100, not the best 100. Fixing it properly means ranking server-side, which
        needs the score materialized; it is not worth doing before there is a queue.
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

        **The exemption is the second half of the unmeasured-neutral fix.** A row whose
        novelty or groundedness could not be measured carries a NEUTRAL 1.0 on that axis,
        and novelty's measured ceiling against a real corpus is ~0.47. Filtering both
        groups on one number would therefore hide the candidates we know most about and
        keep the ones we know nothing about — the knob doing the exact opposite of what
        its name says. A cutoff is a judgement about a score; an unmeasured row has no
        score to judge, so it is never hidden by one. It is also never in the way: the
        sort has already put every unmeasured row below every measured one.

        The consequence, stated rather than hidden: **a queue dominated by unmeasured rows
        cannot be trimmed with this knob.** That is a signal, not a defect — the fix is to
        wire the prior-art index (which is what makes novelty measurable at all), not to
        hide the rows that prove it is dark.

        An operator who empties their own inbox is TOLD. Silently returning zero rows to a
        reviewer who set a knob they misjudged is how a queue stops being read."""
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
        """Human approve: `in_review → validated`. Delegates to the single
        `apply_human_decision` path (strip + deps + static/replay guards; D17/R4).

        A guard that HOLDS (e.g. an unresolved `depends_on`, a missing generalization,
        a failed replay) leaves the candidate `in_review`. That is NOT a success — so
        a held approve is surfaced as an `InboxTransitionError` carrying the hold
        reason (nit: a held approve must be distinguishable from a validated one), not
        silently returned as an unchanged envelope."""
        await self._require(candidate_id, CandidateStatus.IN_REVIEW)
        env = await self._store.get(candidate_id)
        decision = await self._scheduler.apply_human_decision(env, "approve")
        if decision.action != "approve":
            raise InboxTransitionError(
                f"approve held for {candidate_id}: {decision.reason}"
            )
        return await self._store.get(candidate_id)

    async def reject(self, candidate_id: str) -> CandidateEnvelope:
        """Human reject: `in_review → rejected`. A NEGATIVE signal, NOT a delete —
        the row is retained for the S9 learner (D29). Delegates to the single path."""
        await self._require(candidate_id, CandidateStatus.IN_REVIEW)
        env = await self._store.get(candidate_id)
        await self._scheduler.apply_human_decision(env, "reject")
        return await self._store.get(candidate_id)

    async def retract(self, candidate_id: str) -> CandidateEnvelope:
        """Retract a promoted artifact: `validated → retired` (a leak/drift pull).

        DELEGATES to the single `apply_retract` path (like approve/reject) so the leak
        PULL stamps the landed neo4j node `retired` (fail-open) BEFORE the store retire —
        the recall filter then excludes it, so a leaked blueprint stops being recallable
        immediately (S9-activation Slice 3, review BLOCKER 2). The physical index removal
        + the D25 exposure trace remain S10 (§11.4); the STAMP is what closes the recall
        exposure here and now."""
        env = await self._require(candidate_id, CandidateStatus.VALIDATED)
        await self._scheduler.apply_retract(env)
        return await self._store.get(candidate_id)

    async def verify(self, candidate_id: str) -> tuple[CandidateEnvelope, bool]:
        """VERIFY an auto-landed learning node (Phase-3): flip `verified → true` on both
        the landed neo4j node and the candidate envelope. Requires the current status to
        be `validated` (a validated learning node is the verifiable set — all validated
        candidates in the store are `source='learning'` by construction). DELEGATES to
        the single `PromotionScheduler.apply_verify` path (fail-open node write-back +
        the authoritative envelope write).

        Returns `(env, node_stamped)`: `node_stamped` is False when the neo4j node write
        did not land (no writer wired, node never landed, or a fail-open write error), so
        the caller can prompt a re-verify — the envelope reads `verified=true` regardless
        (source-of-truth for the inbox), a re-verify converges the node."""
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
        """PROMOTE a verified learning node (Phase-3): emit the MCP-format YAML for a
        MANUAL PR into the MCP corpus repo. The FIRST promote (from `validated`) also
        moves the candidate `validated → promoted`; a re-promote (from `promoted`)
        RE-EMITS the same YAML with NO status move so an abandoned/revived/lost PR can
        always regenerate it (`build_promotion_emit` is pure).

        First promote requires `validated` AND `verified == True` — an unverified node is
        refused with a clear transition error (a human must VERIFY before PROMOTE). A
        `promoted` candidate was necessarily verified at its first promote (the only edge
        into `promoted`), so the re-emit needs no re-check. Any other status is a fail-loud
        transition error. The YAML `id` is the landing id VERBATIM so a later reseed flips
        THAT SAME node `learning → mcp` instead of duplicating it. `doc_id`/`title` are
        OPTIONAL human refinements for knowledge; `id` can NEVER be overridden.

        The move is OPTIMISTIC (abandoned-PR caveat): the emit + status move happen here,
        but the actual `learning → mcp` reseed only happens when the human MERGES the PR.
        If they never do, the neo4j node stays `source='learning'` (excluded from recall)
        and the candidate stays `promoted` — no recall exposure either way, and the
        idempotent re-emit above lets the PR be regenerated."""
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
