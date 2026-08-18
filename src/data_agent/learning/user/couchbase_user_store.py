"""CouchbaseUserKnowledgeStore — the real per-user store (S8, D17; mirrors D95).

Its OWN `Cluster`, authenticated as `user_knowledge_writer` against the dedicated
`user_knowledge` bucket — a separate RBAC boundary from the session, audit and candidate
stores. This is the ONE store that holds entity-bearing facts, so the scoped role is
load-bearing: `open_bucket` refuses any bucket but its grant, and `list_for_user` is a
`user_id`-parameterized N1QL query with no cross-user surface.

Import-guarded (imports without the SDK; constructing raises), gated per public coroutine by
`CouchbaseConnectGate`, and built through `CouchbaseStoreBase` so `__init__` does no I/O.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.couchbase_connect import (
    CouchbaseStoreBase,
    get_or_none,
)

from .config import UserKnowledgeStoreConfig
from .models import UserKnowledgeRecord
from .store import UserKnowledgeAccessError

# The availability flag + the shared SDK symbols live in `couchbase_connect`; these
# are the extra types only this store reads/writes with.
try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    from couchbase.options import QueryOptions, UpsertOptions
except ImportError:  # pragma: no cover
    pass


class CouchbaseUserKnowledgeStore(CouchbaseStoreBase):
    """Real `UserKnowledgeStore` backed by the dedicated `user_knowledge` bucket."""

    def __init__(self, config: UserKnowledgeStoreConfig, cluster: Any = None) -> None:
        self._config = config
        # KV-only default collection = the base's default `_bind_collections`. A
        # non-positive TTL means NO expiry here (`ttl_none_when_not_positive`), which
        # the writes below turn into options built without `expiry=`.
        self._init_couchbase_store(
            cluster=cluster,
            connection_string=config.user_knowledge_connection_string,
            username=config.user_knowledge_username,
            password=config.user_knowledge_password,
            bucket=config.user_knowledge_bucket,
            ttl_seconds=config.user_knowledge_ttl_seconds,
            ttl_none_when_not_positive=True,
        )

    def bucket(self) -> str:
        return self._bucket_name

    def open_bucket(self, bucket: str) -> CouchbaseUserKnowledgeStore:
        if bucket != self._bucket_name:
            raise UserKnowledgeAccessError(
                f"user_knowledge_writer is scoped to {self._bucket_name!r}; "
                f"access to {bucket!r} is denied"
            )
        return self

    async def commit(self, record: UserKnowledgeRecord) -> None:
        await self._ensure_connected()
        options = UpsertOptions(expiry=self._ttl) if self._ttl is not None else UpsertOptions()
        await self._collection.upsert(record.record_id, record.to_doc(), options)

    async def get(self, record_id: str) -> UserKnowledgeRecord | None:
        await self._ensure_connected()
        result = await get_or_none(self._collection, record_id)
        return None if result is None else UserKnowledgeRecord.from_doc(result.content_as[dict])

    async def list_for_user(
        self, user_id: str, *, limit: int = 100
    ) -> list[UserKnowledgeRecord]:
        await self._ensure_connected()
        statement = (
            f"SELECT r.* FROM `{self._bucket_name}` r "
            "WHERE r.user_id = $user_id "
            "ORDER BY r.committed_at ASC LIMIT $limit"
        )
        result = self._cluster.query(
            statement,
            QueryOptions(named_parameters={"user_id": user_id, "limit": int(limit)}),
        )
        out: list[UserKnowledgeRecord] = []
        async for row in result:
            out.append(UserKnowledgeRecord.from_doc(row))
        return out
