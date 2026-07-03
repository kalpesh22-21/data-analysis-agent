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

**Entity-strip on promotion (D17).** `approve` moves a candidate toward a global,
retrievable state, so before it is stamped `validated` the entity-bearing leakage
`span`s are stripped from `entity_scan` — the invariant "no entity span crosses into
a global store" is enforced at this status boundary, not left to the physical
promoter.

Transitions are guarded: `approve`/`reject` require the current status to be
`in_review`; `retract` requires `validated`. An illegal transition raises
`InboxTransitionError` (fail-loud — a mis-routed action never silently mutates a
candidate).
"""

from __future__ import annotations

from dataclasses import replace

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.store import CandidateStore
from ..candidate.verdicts import EntityHit, LeakageVerdict
from .models import InboxItem


class InboxTransitionError(Exception):
    """Raised when a human transition is requested from an illegal current status."""


def _strip_entity_spans(env: CandidateEnvelope) -> CandidateEnvelope:
    """Return a copy with entity-bearing leakage `span`s blanked (D17). No-op when
    `entity_scan` is unsettled or carries no hits."""
    scan = env.entity_scan
    if not LeakageVerdict.is_settled(scan):
        return env
    verdict = LeakageVerdict.from_doc(scan)
    if not verdict.hits:
        return env
    stripped = LeakageVerdict(
        result=verdict.result,
        hits=tuple(EntityHit(field=h.field, kind=h.kind, span="") for h in verdict.hits),
        scanned_fields=verdict.scanned_fields,
        scanner=verdict.scanner,
    )
    return replace(env, entity_scan=stripped.to_doc())


class ReviewInbox:
    """The `in_review` projection over a `CandidateStore` + the human transitions."""

    def __init__(self, store: CandidateStore) -> None:
        self._store = store

    async def list(self, *, limit: int = 100) -> list[InboxItem]:
        """The current inbox: every `in_review` candidate as a reviewer view."""
        envelopes = await self._store.list_by_status(CandidateStatus.IN_REVIEW, limit=limit)
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
        """Human approve: `in_review → validated`. Strips entity spans first (D17)."""
        env = await self._require(candidate_id, CandidateStatus.IN_REVIEW)
        promoted = replace(_strip_entity_spans(env), status=CandidateStatus.VALIDATED)
        await self._store.put(promoted)
        return promoted

    async def reject(self, candidate_id: str) -> CandidateEnvelope:
        """Human reject: `in_review → rejected`. A NEGATIVE signal, NOT a delete —
        the row is retained for the S9 learner (D29)."""
        env = await self._require(candidate_id, CandidateStatus.IN_REVIEW)
        rejected = replace(env, status=CandidateStatus.REJECTED)
        await self._store.put(rejected)
        return rejected

    async def retract(self, candidate_id: str) -> CandidateEnvelope:
        """Retract a promoted artifact: `validated → retired` (a leak/drift pull).
        The physical index removal + D25 exposure trace are deferred to S10 (§11.4)."""
        env = await self._require(candidate_id, CandidateStatus.VALIDATED)
        retired = replace(env, status=CandidateStatus.RETIRED)
        await self._store.put(retired)
        return retired
