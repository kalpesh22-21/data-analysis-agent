"""InMemoryUserKnowledgeStore — the Layer-1 `UserKnowledgeStore` fake (S8).

Dict-backed, same semantics as the Couchbase impl. Two invariants it lets QA assert without
infra: the RBAC boundary (`open_keyspace` raises for any keyspace but the granted one) and
per-user scoping (`list_for_user` returns only that user's rows).
"""

from __future__ import annotations

from .models import UserKnowledgeRecord
from .store import UserKnowledgeAccessError

# The bucket-per-store layout's keyspace, spelled the way the real store spells it
# (backtick-quoted, three parts). `_default`/`_default` is the default scope/collection —
# see `learning/user/config.py`. A shared-bucket deployment would pass e.g.
# "`pcm_iwant`.`user`.`knowledge`".
_DEFAULT_KEYSPACE = "`user_knowledge`.`_default`.`_default`"


class InMemoryUserKnowledgeStore:
    """Dict-backed `UserKnowledgeStore` fake — no I/O, deterministic, Layer-1."""

    def __init__(self, *, keyspace: str = _DEFAULT_KEYSPACE) -> None:
        self._keyspace = keyspace
        self._by_id: dict[str, UserKnowledgeRecord] = {}
        self.commit_calls = 0

    def keyspace(self) -> str:
        return self._keyspace

    def open_keyspace(self, keyspace: str) -> InMemoryUserKnowledgeStore:
        # THREE-part compare, matching the real store: in a shared bucket, comparing
        # bucket names alone would admit every sibling store's keyspace (D17).
        if keyspace != self._keyspace:
            raise UserKnowledgeAccessError(
                f"user-knowledge RBAC role is scoped to {self._keyspace}; "
                f"access to {keyspace} is denied"
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
