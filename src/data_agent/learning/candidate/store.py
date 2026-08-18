"""CandidateStore — the `learning_candidates` port (D101).

Mirrors the `AuditStore`/`SessionStore` port pattern (Protocol + Couchbase impl + in-memory
fake). Unlike the KV-only `learning_audit` store, candidates are QUERYABLE BY `status` (N1QL)
so the S7 inbox and the S9 scheduler can read them, which is why the Couchbase impl needs a
provisioned primary index.
"""

from __future__ import annotations

from typing import Literal, Protocol

from .models import CandidateEnvelope
from .verdicts import DriftStamp


class CandidateStore(Protocol):
    async def put(self, envelope: CandidateEnvelope) -> None:
        """Upsert *envelope* keyed by its `candidate_id` (idempotent on a
        content-hash-derived key)."""
        ...

    async def get(self, candidate_id: str) -> CandidateEnvelope | None:
        """Read one candidate by id, or `None` if absent."""
        ...

    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 100,
        order: Literal["asc", "desc"] = "asc",
        order_by: Literal["created_at", "last_scanned_at"] = "created_at",
    ) -> list[CandidateEnvelope]:
        """Return candidates in *status* (for the S7 inbox / S9 scheduler).

        `order_by="created_at"` (default) is the human-facing arrival order: `"asc"` for the review
        queue, `"desc"` for the unbounded rejected archive so the `limit` trims OLD history rather
        than present rejects.

        `order_by="last_scanned_at"` is the S9 cron's ROTATION read — least-recently-examined first,
        NEVER-examined (MISSING/None) FIRST, with `candidate_id` as a SECONDARY key. Load-bearing:
        `created_at ASC` gave the bounded `scan_limit` window to the OLDEST rows permanently, so once
        more than `scan_limit` candidates were stuck in a hold the scheduler could never see a newly
        extracted one again — silent head-of-line starvation with no error.

        The `candidate_id` tiebreak makes the ordering TOTAL, so both impls return the same window
        for a tied set. Be precise about what it buys: totality, not FAIRNESS. Fairness comes from
        the cursor ADVANCING, which confines ties to rows stamped inside one clock tick; a clock
        frozen across cycles (only ever a test double) leaves every row tied and returns the same
        prefix, which no tiebreak could fix. DESC is the exact reverse of ASC in both modes, which is
        what lets the fake implement it as a stable sort plus a reverse.
        """
        ...

    async def touch_scanned(self, candidate_id: str, at: str) -> None:
        """Stamp `last_scanned_at = at` on ONE candidate — the S9 scan cursor write.

        A deliberately NARROW single-field write, NOT a full-envelope `put`, for three load-bearing
        reasons. IT CANNOT CLOBBER: the cron acts on a cycle-start snapshot, and re-putting that copy
        just to record "I looked" would revert any field a concurrent inbox transition changed. IT
        PRESERVES THE RETENTION CLOCK: `put` sets the TTL fresh, so stamping through it would make a
        permanently-held candidate immortal. And it is cheap enough to run on EVERY scanned candidate
        every cycle, which is what makes the rotation total rather than best-effort. A vanished
        candidate is a no-op.
        """
        ...

    async def stamp_drift(self, candidate_id: str, drift: DriftStamp) -> None:
        """Stamp `drift` on ONE candidate — the S9 bookkeeping verdict write.

        The narrow sibling of `touch_scanned`, for the same three reasons and one more. S9 records a
        golden-replay verdict on candidates it is NOT transitioning, purely so the next cycle can
        reuse it; routing that through `put` would CLOBBER fields S9 does not own, RESURRECT a
        document `supersede` deleted between the scan read and the write (a sub-document write on a
        missing document is a no-op instead), and make a parked candidate IMMORTAL by renewing its
        TTL. Genuine lifecycle writes still go through `put` and DO renew the TTL — those are events,
        not bookkeeping.
        """
        ...

    async def supersede(self, content_hash: str) -> None:
        """Remove ALL candidates previously written for *content_hash*.

        Called before persisting a fresh extraction so a re-run — a redelivery re-invokes the LLM,
        which may emit a different count or order — never leaves a MIXED set from two attempts.
        Idempotent.
        """
        ...
