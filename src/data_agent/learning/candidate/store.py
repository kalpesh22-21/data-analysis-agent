"""CandidateStore — the `learning_candidates` port (D101).

Mirrors the `AuditStore`/`SessionStore` port pattern (Protocol + Couchbase impl +
in-memory fake). Unlike the KV-only `learning_audit` store, candidates are
QUERYABLE BY `status` (N1QL) so the Slice-7 review inbox and the Slice-9 promotion
scheduler can read them later — so the Couchbase impl provisions a primary index
(see `scripts/learning-candidates-init.sh`).
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

        `order_by` picks the SORT KEY; `order` picks the direction.

        `order_by="created_at"` (default) is the human-facing arrival order: `"asc"`
        is the review-queue view (a small self-draining set), `"desc"` is newest-first
        for the durable, unbounded rejected archive, so the `limit` cap trims OLD
        history, not present rejects (ui-inbox-type-archive contract §Retention).

        `order_by="last_scanned_at"` is the S9 cron's ROTATION read — least-recently-
        examined first, with NEVER-examined (MISSING/None) candidates FIRST, and
        `candidate_id` as a SECONDARY key. This is load-bearing, not cosmetic:
        `created_at ASC` gave the bounded `scan_limit` window to the OLDEST rows
        permanently, so once more than `scan_limit` candidates were stuck in a hold the
        scheduler could never see a newly extracted one again — silent head-of-line
        starvation with no error. Sorting on the scan cursor makes the window
        round-robin: examining a candidate pushes it to the back, and a brand-new
        candidate (no cursor) jumps to the front.

        The `candidate_id` tiebreak makes that ordering TOTAL, so both impls return the
        same window for a tied set instead of each falling back to its own accident (the
        fake to dict insertion order, the GSI to its implicit trailing doc key). Be
        precise about what it buys, though: totality, not fairness. FAIRNESS comes from
        the cursor ADVANCING, which confines ties to rows stamped inside a single clock
        tick — any clock that moves between cycles rotates fine, including a coarse one.
        A clock frozen across cycles (only ever a test double; `_now_iso` has microsecond
        resolution) leaves every row permanently tied and the same prefix comes back
        every time. That is a property of ordering-by-cursor, not something a tiebreak
        could fix, and a test with a pinned clock cannot detect a rotation stall.

        `created_at` keeps its single sort key so the inbox/archive statement stays
        byte-identical; its tie order remains impl-defined and nothing depends on it. In
        both modes DESC is the exact reverse of ASC (every key takes the same direction),
        which is what lets the fake implement it as a stable sort followed by a
        reverse."""
        ...

    async def touch_scanned(self, candidate_id: str, at: str) -> None:
        """Stamp `last_scanned_at = at` on ONE candidate — the S9 scan cursor write.

        A deliberately NARROW, single-field write, NOT a full-envelope `put`, for three
        reasons that are all load-bearing:

          1. **It cannot clobber.** The cron scan reads a snapshot at the top of the
             cycle and may act on a stale copy; re-`put`ting that copy just to record
             "I looked at it" would revert any field a concurrent writer (the S7 inbox
             approve/verify/retract path) changed in between. A single-path write
             touches only the scheduler-owned cursor and leaves every other field alone.
          2. **It preserves the retention clock.** `put` sets the candidate TTL fresh on
             every write; stamping the cursor through `put` would make a permanently-held
             candidate immortal (its 90-day TTL renewed every 5 minutes forever).
          3. It is cheap enough to run on EVERY scanned candidate every cycle, which is
             what makes the rotation total rather than best-effort.

        Idempotent and fail-quiet for an unknown / already-expired `candidate_id` (a
        no-op) — a candidate that vanished between the scan read and this write needs
        no cursor."""
        ...

    async def stamp_drift(self, candidate_id: str, drift: DriftStamp) -> None:
        """Stamp `drift` on ONE candidate — the S9 bookkeeping verdict write.

        The narrow sibling of `touch_scanned`, for the same three reasons and one more.
        S9 records a golden-replay verdict on candidates it is NOT transitioning (a
        blueprint parked below the hit threshold), purely so the next cycle can reuse it
        instead of paying for the probe again. That is bookkeeping, and routing it
        through `put` would have three costs a single-path write avoids:

          1. **Clobber.** `put` rewrites the WHOLE envelope from the cycle-start
             snapshot, including fields S9 does not own.
          2. **Resurrection.** `supersede` may have DELETED the document between the scan
             read and the write (a redelivered session re-extracting); a full `put` would
             recreate it, resurrecting a candidate that was deliberately dropped. A
             sub-document write on a missing document is a no-op instead.
          3. **Immortality.** `put` sets the candidate TTL fresh on every write, so a
             verdict re-stamped twice a day would renew a parked candidate's 90-day
             retention clock for ever. This write preserves the existing expiry.

        Genuine lifecycle writes (promote / demote / approve / retire) still go through
        `put` and DO renew the TTL — those are events, not bookkeeping.

        Idempotent and fail-quiet for an unknown / already-expired / superseded
        `candidate_id` (a no-op)."""
        ...

    async def supersede(self, content_hash: str) -> None:
        """Remove ALL candidates previously written for *content_hash* (the
        `candidate::<content_hash>::*` family). Called before persisting a fresh
        extraction so a re-run (redelivery re-invokes the LLM, which may emit a
        different count/order) never leaves a MIXED set from two attempts —
        MEDIUM-3. Idempotent: a no-op when none exist."""
        ...
