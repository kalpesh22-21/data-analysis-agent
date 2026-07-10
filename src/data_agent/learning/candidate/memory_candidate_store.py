"""InMemoryCandidateStore — the Layer-1 `CandidateStore` fake (D101).

Dict-backed, same semantics as `CouchbaseCandidateStore` (put / get /
list_by_status) for Layer-1 wiring tests, including the S3 consumer-integration
test that asserts a KEEP session persists candidates at `status=extracted`.
"""

from __future__ import annotations

from typing import Literal

from .models import CandidateEnvelope


class InMemoryCandidateStore:
    """Dict-backed `CandidateStore` fake — no I/O, deterministic, Layer-1 only."""

    def __init__(self) -> None:
        self._by_id: dict[str, CandidateEnvelope] = {}
        self.put_calls = 0

    async def put(self, envelope: CandidateEnvelope) -> None:
        self.put_calls += 1
        self._by_id[envelope.candidate_id] = envelope

    async def get(self, candidate_id: str) -> CandidateEnvelope | None:
        return self._by_id.get(candidate_id)

    async def list_by_status(
        self, status: str, *, limit: int = 100, order: Literal["asc", "desc"] = "asc"
    ) -> list[CandidateEnvelope]:
        matches = [c for c in self._by_id.values() if c.status == status]
        matches.sort(key=lambda c: c.created_at)
        # DESC is the exact reverse of the deterministic ASC order — ties on
        # created_at keep their (reversed) stable-sort order, so it stays
        # deterministic. `desc` serves the newest-first rejected archive.
        if order == "desc":
            matches.reverse()
        return matches[:limit]

    async def supersede(self, content_hash: str) -> None:
        stale = [cid for cid, c in self._by_id.items() if c.content_hash == content_hash]
        for cid in stale:
            del self._by_id[cid]

    # Read-only inspection helper for Layer-1 tests.
    def all_candidates(self) -> list[CandidateEnvelope]:
        return list(self._by_id.values())
