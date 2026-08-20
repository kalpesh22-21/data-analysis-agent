"""CouchbaseUserKnowledgeStore — the real per-user store (S8, D17; mirrors D95).

Its OWN `Cluster`, authenticated as `user_knowledge_writer` against the configured
user-knowledge KEYSPACE — a separate RBAC boundary from the session, audit and candidate
stores. That keyspace is `user_knowledge`.`_default`.`_default` by default (the
bucket-per-store layout) or a named scope in a shared bucket (`pcm_iwant`.`user`.`knowledge`);
only configuration differs.

This is the ONE store that holds entity-bearing facts, so the scoped role is load-bearing:
`open_keyspace` refuses any keyspace but its grant, and `list_for_user` is a
`user_id`-parameterized N1QL query — against the THREE-part keyspace, so it cannot read the
other stores' scopes when they share a bucket — with no cross-user surface.

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
    """Real `UserKnowledgeStore` backed by the dedicated user-knowledge keyspace."""

    def __init__(self, config: UserKnowledgeStoreConfig, cluster: Any = None) -> None:
        # Set BEFORE `_init_couchbase_store`: with an injected cluster that call binds
        # handles EAGERLY, and `_bind_collections` below reads `self._config`.
        self._config = config
        # KV reads/writes plus the one per-user N1QL scan — see `_bind_collections` for
        # the KV handle and `keyspace()` for the query side. A non-positive TTL means NO
        # expiry here (`ttl_none_when_not_positive`), which the writes below turn into
        # options built without `expiry=`.
        self._init_couchbase_store(
            cluster=cluster,
            connection_string=config.user_knowledge_connection_string,
            username=config.user_knowledge_username,
            password=config.user_knowledge_password,
            bucket=config.user_knowledge_bucket,
            ttl_seconds=config.user_knowledge_ttl_seconds,
            ttl_none_when_not_positive=True,
        )

    def _bind_collections(self, bucket: Any) -> None:
        """The ONE knowledge collection, from the configured scope + collection.

        Overrides the base's `bucket.default_collection()`. The defaults are
        `_default`/`_default`, which yields the IDENTICAL handle the base built, so a
        bucket-per-store deployment is unaffected; a shared-bucket deployment points this
        at e.g. `pcm_iwant`.`user`.`knowledge` with no code change.
        """
        scope = bucket.scope(self._config.user_knowledge_scope)
        self._collection = scope.collection(self._config.user_knowledge_collection)

    def keyspace(self) -> str:
        """The single THREE-part keyspace this store's RBAC role is granted.

        Backtick-quoted so it can be interpolated straight into N1QL and compared as one
        string by `open_keyspace`. ONE definition, so the guard and the query can never
        disagree about what this store is allowed to touch.
        """
        return (
            f"`{self._bucket_name}`"
            f".`{self._config.user_knowledge_scope}`"
            f".`{self._config.user_knowledge_collection}`"
        )

    def open_keyspace(self, keyspace: str) -> CouchbaseUserKnowledgeStore:
        if keyspace != self.keyspace():
            raise UserKnowledgeAccessError(
                f"user_knowledge_writer is scoped to {self.keyspace()}; "
                f"access to {keyspace} is denied"
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
        # THREE-part keyspace, never the bucket alone. A one-part `` `bucket` `` means
        # "every scope and collection in this bucket": in a SHARED bucket this scan would
        # read the audit, candidate and corpus scopes — and the session scope — and hand
        # whatever it found to `UserKnowledgeRecord.from_doc`. That is precisely the
        # cross-store surface the scoped RBAC role exists to remove (D17), so the guard
        # and the statement are built from the SAME `keyspace()`.
        statement = (
            f"SELECT r.* FROM {self.keyspace()} r "
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
