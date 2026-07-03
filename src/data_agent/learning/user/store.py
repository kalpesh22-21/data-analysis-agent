"""UserKnowledgeStore — the per-user knowledge port (S8, D17; mirrors D95/D101).

A dedicated, access-controlled store — its OWN Couchbase bucket + an RBAC user
scoped to THAT bucket only (the D95 `learning_audit` / D101 `learning_candidates`
posture). Unlike the entity-free global stores, this one holds entity-BEARING
per-user facts, so its RBAC boundary is load-bearing: the store may touch only its
own bucket, and a read is always scoped to a single `user_id` (no cross-user
surface). Protocol + in-memory fake (`memory_user_store`) + Couchbase impl
(`couchbase_user_store`), same shape as the audit/candidate ports.
"""

from __future__ import annotations

from typing import Protocol

from .models import UserKnowledgeRecord


class UserKnowledgeAccessError(PermissionError):
    """Raised when the store's RBAC role is asked to touch a bucket outside its
    grant — the Layer-1 stand-in for the Couchbase RBAC boundary (D95). A real
    cross-bucket access would fail at the cluster; the fake fails here so a wiring
    test can assert the boundary without infra."""


class UserKnowledgeStore(Protocol):
    def bucket(self) -> str:
        """The single bucket this store's RBAC role is granted."""
        ...

    def open_bucket(self, bucket: str) -> UserKnowledgeStore:
        """Return this store IFF *bucket* is the granted bucket, else raise
        `UserKnowledgeAccessError` — models the scoped RBAC role (D95)."""
        ...

    async def commit(self, record: UserKnowledgeRecord) -> None:
        """Auto-commit (D17) *record*, keyed by its `record_id` (idempotent
        upsert). Scoped to `record.user_id`."""
        ...

    async def get(self, record_id: str) -> UserKnowledgeRecord | None:
        """Read one record by id, or `None` if absent."""
        ...

    async def list_for_user(
        self, user_id: str, *, limit: int = 100
    ) -> list[UserKnowledgeRecord]:
        """Return ONLY *user_id*'s records — the per-user surface (no cross-user
        leakage)."""
        ...
