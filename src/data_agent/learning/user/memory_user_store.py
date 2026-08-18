"""InMemoryUserKnowledgeStore — the Layer-1 `UserKnowledgeStore` fake (S8).

Dict-backed, same semantics as the Couchbase impl. Two invariants it lets QA assert without
infra: the RBAC boundary (`open_bucket` raises for any bucket but the granted one) and
per-user scoping (`list_for_user` returns only that user's rows).
"""

from __future__ import annotations

from .models import UserKnowledgeRecord
from .store import UserKnowledgeAccessError

_DEFAULT_BUCKET = "user_knowledge"


class InMemoryUserKnowledgeStore:
    """Dict-backed `UserKnowledgeStore` fake — no I/O, deterministic, Layer-1."""

    def __init__(self, *, bucket: str = _DEFAULT_BUCKET) -> None:
        self._bucket = bucket
        self._by_id: dict[str, UserKnowledgeRecord] = {}
        self.commit_calls = 0

    def bucket(self) -> str:
        return self._bucket

    def open_bucket(self, bucket: str) -> InMemoryUserKnowledgeStore:
        if bucket != self._bucket:
            raise UserKnowledgeAccessError(
                f"user-knowledge RBAC role is scoped to {self._bucket!r}; "
                f"access to {bucket!r} is denied"
            )
        return self

    async def commit(self, record: UserKnowledgeRecord) -> None:
        self.commit_calls += 1
        self._by_id[record.record_id] = record

    async def get(self, record_id: str) -> UserKnowledgeRecord | None:
        return self._by_id.get(record_id)

    async def list_for_user(
        self, user_id: str, *, limit: int = 100
    ) -> list[UserKnowledgeRecord]:
        matches = [r for r in self._by_id.values() if r.user_id == user_id]
        matches.sort(key=lambda r: r.committed_at)
        return matches[:limit]

    # Read-only inspection helper for Layer-1 tests.
    def all_records(self) -> list[UserKnowledgeRecord]:
        return list(self._by_id.values())
