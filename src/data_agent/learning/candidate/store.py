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


class CandidateStore(Protocol):
    async def put(self, envelope: CandidateEnvelope) -> None:
        """Upsert *envelope* keyed by its `candidate_id` (idempotent on a
        content-hash-derived key)."""
        ...

    async def get(self, candidate_id: str) -> CandidateEnvelope | None:
        """Read one candidate by id, or `None` if absent."""
        ...

    async def list_by_status(
        self, status: str, *, limit: int = 100, order: Literal["asc", "desc"] = "asc"
    ) -> list[CandidateEnvelope]:
        """Return candidates in *status* (for the S7 inbox / S9 scheduler).

        `order` sorts by `created_at`: `"asc"` (default — the review-queue view, a
        small self-draining set) or `"desc"` (newest-first — the durable, unbounded
        rejected archive, so the `limit` cap trims OLD history, not present rejects;
        ui-inbox-type-archive contract §Retention). Neither direction adds a secondary
        tiebreak, so tie order on equal `created_at` is impl-defined: the in-memory
        store is deterministic (DESC is the exact reverse of ASC — a stable sort then
        reverse), while the Couchbase impl does not guarantee tie order in EITHER
        direction (pre-existing for asc; desc is symmetric). Nothing depends on
        Couchbase tie order, and the asc N1QL is intentionally byte-identical."""
        ...

    async def supersede(self, content_hash: str) -> None:
        """Remove ALL candidates previously written for *content_hash* (the
        `candidate::<content_hash>::*` family). Called before persisting a fresh
        extraction so a re-run (redelivery re-invokes the LLM, which may emit a
        different count/order) never leaves a MIXED set from two attempts —
        MEDIUM-3. Idempotent: a no-op when none exist."""
        ...
