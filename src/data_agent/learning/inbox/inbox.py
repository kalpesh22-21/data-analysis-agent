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

from typing import Literal

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.store import CandidateStore
from ..promotion.models import ProbeResult
from ..promotion.scheduler import PromotionScheduler
from .models import InboxItem


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
        self, store: CandidateStore, *, scheduler: PromotionScheduler | None = None
    ) -> None:
        self._store = store
        # The SINGLE approve/reject implementation (R4). Defaulted for an unwired
        # inbox; production injects the wired scheduler.
        self._scheduler = scheduler or PromotionScheduler(
            store, probe=_NoOpProbe(), hit_counts=_ZeroHitCounts(),
        )

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
        The default (`status=in_review`, `order=asc`) is unchanged, so every existing
        caller keeps the byte-identical review-queue view. The caller passes
        `order="desc"` for the archive so the LIMIT trims OLD history, not present
        rejects — the ordering is chosen explicitly here, never inferred from the
        status string inside the store."""
        envelopes = await self._store.list_by_status(status, limit=limit, order=order)
        return [InboxItem.from_envelope(env) for env in envelopes]

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
