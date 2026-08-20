"""UserKnowledgeStore — the per-user knowledge port (S8, D17; mirrors D95/D101).

A dedicated, access-controlled store with its OWN KEYSPACE (bucket + scope + collection) and
an RBAC user scoped to THAT keyspace only. Unlike the entity-free global stores this one
holds entity-BEARING per-user facts, so its RBAC boundary is load-bearing: the store may
touch only its own keyspace, and a read is always scoped to a single `user_id`.

The grant is a KEYSPACE, not a bucket, because the deployment may put every store in ONE
bucket separated by named scopes (`pcm_iwant`.`user`.`knowledge` next to
`pcm_iwant`.`learning`.`audit`). Comparing bucket NAMES there is vacuous — it would admit
the audit, candidate and corpus keyspaces, which is exactly the boundary this guard exists
to defend — so the comparison is on all three parts. A bucket-per-store deployment sets
scope and collection to `_default` and gets the identical behaviour it always had.
"""

from __future__ import annotations

from typing import Protocol

from .models import UserKnowledgeRecord


class UserKnowledgeAccessError(PermissionError):
    """The store's RBAC role was asked to touch a keyspace outside its grant.

    The Layer-1 stand-in for the Couchbase RBAC boundary (D95): a real cross-keyspace access
    would fail at the cluster, and the fake fails here so a wiring test can assert the
    boundary without infra.
    """


class UserKnowledgeStore(Protocol):
    def keyspace(self) -> str:
        """The single ``bucket.scope.collection`` this store's RBAC role is granted."""
        ...

    def open_keyspace(self, keyspace: str) -> UserKnowledgeStore:
        """Return this store IFF *keyspace* is the granted keyspace, else raise
        `UserKnowledgeAccessError` — models the scoped RBAC role (D95).

        Compares all THREE parts: in a shared bucket a bucket-name compare would pass for
        every other store's keyspace and guard nothing.
        """
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
