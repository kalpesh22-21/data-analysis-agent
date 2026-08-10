"""InMemoryCandidateStore — the Layer-1 `CandidateStore` fake (D101).

Dict-backed, same semantics as `CouchbaseCandidateStore` (put / get /
list_by_status / touch_scanned / stamp_drift) for Layer-1 wiring tests, including
the S3 consumer-integration test that asserts a KEEP session persists candidates at
`status=extracted`.

PARITY IS THE POINT: the whole unit suite drives the scheduler through THIS class,
so any place it diverges from the Couchbase impl is a place the tests prove
nothing. The behaviours that must match exactly are (a) `list_by_status` ordering,
including where a MISSING/None sort key lands, (b) the S9 bookkeeping stamps being
single-field writes against the CURRENTLY-STORED envelope rather than whole-envelope
puts of a possibly-stale copy, and (c) those stamps being NO-OPS on a missing id —
the real `mutate_in` replaces, it does not upsert, so a superseded candidate must not
be resurrected by a stamp.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from .models import CandidateEnvelope
from .verdicts import DriftStamp


def _sort_key(env: CandidateEnvelope, order_by: str) -> tuple[int, str]:
    """The ASC sort key, shaped to reproduce the N1QL collation the Couchbase impl
    gets for free.

    Returns a `(rank, value)` TUPLE rather than the bare field for two reasons:

      * `last_scanned_at` is `None` on every never-scanned candidate, and a bare
        `sorted()` over a mix of `None` and `str` raises `TypeError: '<' not
        supported between instances of 'str' and 'NoneType'` — a crash in the cron's
        very first read, not a mis-order. The rank makes the comparison total.
      * N1QL sorts MISSING/NULL BEFORE every string, so a never-scanned candidate must
        sort FIRST here too (rank 0). That is also the behaviour the rotation depends
        on: brand-new work jumps ahead of everything already examined.

    Anything that is not a `str` is treated as absent (rank 0). `from_doc` already
    normalizes rehydrated non-strings to None, but envelopes also arrive constructed
    in-process (the dataclass validates nothing), and the fail-safe direction for a
    malformed cursor is "scan it now" — one wasted scan, then a well-formed stamp."""
    value = env.created_at if order_by == "created_at" else env.last_scanned_at
    return (1, value) if isinstance(value, str) else (0, "")


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
        """Single-field write of the S9 scan cursor, mirroring the Couchbase
        sub-document `mutate_in`: it re-reads the CURRENTLY-STORED envelope and
        changes only `last_scanned_at`. Writing the caller's (scanned, possibly
        stale) copy back instead would let the cron silently revert a concurrent
        inbox transition — the divergence would be invisible in the fake and fatal
        in production. An unknown id is a no-op (the doc expired / was superseded)."""
        current = self._by_id.get(candidate_id)
        if current is None:
            return
        self.touch_calls += 1
        self._by_id[candidate_id] = replace(current, last_scanned_at=at)

    async def stamp_drift(self, candidate_id: str, drift: DriftStamp) -> None:
        """Single-field write of the S9 drift verdict, mirroring the Couchbase
        sub-document `mutate_in` exactly as `touch_scanned` does.

        The missing-id NO-OP is the behaviour that matters most to reproduce: the real
        `mutate_in` uses REPLACE semantics, so a candidate `supersede` deleted between
        the cron's scan read and this write stays deleted. A fake that fell back to
        `put` would RESURRECT it, and the divergence would only ever show up in
        production."""
        current = self._by_id.get(candidate_id)
        if current is None:
            return
        self.drift_stamps += 1
        self._by_id[candidate_id] = replace(current, drift=drift)

    async def supersede(self, content_hash: str) -> None:
        stale = [cid for cid, c in self._by_id.items() if c.content_hash == content_hash]
        for cid in stale:
            del self._by_id[cid]

    # Read-only inspection helper for Layer-1 tests.
    def all_candidates(self) -> list[CandidateEnvelope]:
        return list(self._by_id.values())
