"""UserKnowledgeStore — the per-user knowledge port (S8, D17; mirrors D95/D101).

A dedicated, access-controlled store with its OWN bucket and an RBAC user scoped to THAT
bucket only. Unlike the entity-free global stores this one holds entity-BEARING per-user
facts, so its RBAC boundary is load-bearing: the store may touch only its own bucket, and a
read is always scoped to a single `user_id`.
"""

from __future__ import annotations

from typing import Protocol

from .models import UserKnowledgeRecord


class UserKnowledgeAccessError(PermissionError):
    """The store's RBAC role was asked to touch a bucket outside its grant.

    The Layer-1 stand-in for the Couchbase RBAC boundary (D95): a real cross-bucket access would
    fail at the cluster, and the fake fails here so a wiring test can assert the boundary without
    infra.
    """


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
