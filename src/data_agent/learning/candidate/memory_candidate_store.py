"""InMemoryCandidateStore — the Layer-1 `CandidateStore` fake (D101).

PARITY IS THE POINT: the whole unit suite drives the scheduler through THIS class, so
anywhere it diverges from the Couchbase impl is a place the tests prove nothing. What must
match exactly is (a) `list_by_status` ordering, including where a MISSING/None sort key
lands, (b) the S9 bookkeeping stamps being single-field writes against the CURRENTLY-STORED
envelope rather than whole-envelope puts of a possibly-stale copy, and (c) those stamps being
NO-OPS on a missing id — the real `mutate_in` replaces, it does not upsert.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from .models import CandidateEnvelope
from .verdicts import DriftStamp


def _sort_key(env: CandidateEnvelope, order_by: str) -> tuple:
    """The ASC sort key, shaped to reproduce the ordering the Couchbase impl gets from N1QL.

    The primary component is a `(rank, value)` TUPLE rather than the bare field, for two reasons:
    `last_scanned_at` is `None` on every never-scanned candidate and a bare `sorted()` over a mix
    of `None` and `str` RAISES — a crash in the cron's first read, not a mis-order — and N1QL
    sorts MISSING/NULL BEFORE every string, which is also what the rotation depends on, so
    brand-new work jumps ahead. The ROTATION key then appends `candidate_id`, matching the
    Couchbase `ORDER BY`; without it the two stores broke ties differently and "which rows the
    window contains" was impl-defined.

    CAVEAT: the tiebreak makes the order TOTAL, not FAIR. Fairness comes from the cursor
    advancing, which confines ties to rows stamped within one clock tick; a clock frozen across
    cycles (only ever a test double) leaves every row tied forever. The tuple is FLAT so element
    0 is always the collation rank, which the parity suite compares directly against the measured
    N1QL one.
    """
    if order_by == "created_at":
        raw = env.created_at
        return (1, raw) if isinstance(raw, str) else (0, "")
    raw = env.last_scanned_at
    if isinstance(raw, str):
        return (1, raw, env.candidate_id)
    return (0, "", env.candidate_id)


class InMemoryCandidateStore:
    """Dict-backed `CandidateStore` fake — no I/O, deterministic, Layer-1 only."""

    def __init__(self) -> None:
        self._by_id: dict[str, CandidateEnvelope] = {}
        self.put_calls = 0
        # Counted separately from `put_calls` on purpose: an S9 bookkeeping stamp is NOT
        # a put (it neither rewrites the envelope nor renews the TTL), and existing tests
        # assert exact `put_calls` totals that must stay unchanged by the rotation.
        self.touch_calls = 0
        self.drift_stamps = 0

    async def put(self, envelope: CandidateEnvelope) -> None:
        self.put_calls += 1
        self._by_id[envelope.candidate_id] = envelope

    async def get(self, candidate_id: str) -> CandidateEnvelope | None:
        return self._by_id.get(candidate_id)

    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 100,
        order: Literal["asc", "desc"] = "asc",
        order_by: Literal["created_at", "last_scanned_at"] = "created_at",
    ) -> list[CandidateEnvelope]:
        matches = [c for c in self._by_id.values() if c.status == status]
        matches.sort(key=lambda c: _sort_key(c, order_by))
        # DESC is the exact reverse of the deterministic ASC order — ties on the sort
        # key keep their (reversed) stable-sort order, so it stays deterministic.
        # `desc` serves the newest-first rejected archive.
        if order == "desc":
            matches.reverse()
        return matches[:limit]

    async def touch_scanned(self, candidate_id: str, at: str) -> None:
        """Single-field write of the S9 scan cursor, mirroring the Couchbase sub-document `mutate_in`.

        It re-reads the CURRENTLY-STORED envelope and changes only `last_scanned_at`. Writing the
        caller's (scanned, possibly stale) copy back instead would let the cron silently revert a
        concurrent inbox transition — invisible in the fake and fatal in production. An unknown id is
        a no-op.
        """
        current = self._by_id.get(candidate_id)
        if current is None:
            return
        self.touch_calls += 1
        self._by_id[candidate_id] = replace(current, last_scanned_at=at)

    async def stamp_drift(self, candidate_id: str, drift: DriftStamp) -> None:
        """Single-field write of the S9 drift verdict, mirroring `mutate_in` as `touch_scanned` does.

        The missing-id NO-OP is what matters most to reproduce: the real `mutate_in` uses REPLACE
        semantics, so a candidate `supersede` deleted between the scan read and this write stays
        deleted. A fake falling back to `put` would RESURRECT it, and the divergence would only ever
        show up in production.
        """
        current = self._by_id.get(candidate_id)
        if current is None:
            return
        self.drift_stamps += 1
        self._by_id[candidate_id] = replace(current, drift=drift)

    async def supersede(
        self, content_hash: str, *, keep_candidate_ids: tuple[str, ...] = ()
    ) -> None:
        keep = set(keep_candidate_ids)
        stale = [
            cid
            for cid, c in self._by_id.items()
            if c.content_hash == content_hash and cid not in keep
        ]
        for cid in stale:
            del self._by_id[cid]

    # Read-only inspection helper for Layer-1 tests.
    def all_candidates(self) -> list[CandidateEnvelope]:
        return list(self._by_id.values())
