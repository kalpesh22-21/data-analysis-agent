"""CouchbaseUserKnowledgeStore — the real per-user store (S8, D17; mirrors D95).

Its OWN `Cluster`, authenticated as `user_knowledge_writer` against the dedicated
`user_knowledge` bucket — a separate RBAC boundary from the session / audit /
candidate stores. This is the ONE store that holds entity-bearing facts, so the
scoped role is load-bearing: `open_bucket` refuses any bucket but its grant, and
`list_for_user` is a `user_id`-parameterized N1QL query (no cross-user surface).

Import-guarded exactly like `couchbase_audit_store` / `couchbase_candidate_store`:
imports with or without the SDK; constructing without it raises.

CONNECT (2026-08-11): shares `CouchbaseConnectGate` with every other
Couchbase-backed store — `acouchbase` refuses all ops until `on_connect()` has
been awaited, which a sync `__init__` cannot do, so each public coroutine gates
itself. The consumer builds this store and never connected it. See
`runtime/couchbase_connect.py`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from data_agent.runtime.couchbase_connect import CouchbaseConnectGate

from .config import UserKnowledgeStoreConfig
from .models import UserKnowledgeRecord
from .store import UserKnowledgeAccessError

try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import DocumentNotFoundException
    from couchbase.options import ClusterOptions, GetOptions, QueryOptions, UpsertOptions

    COUCHBASE_AVAILABLE = True
except ImportError:  # pragma: no cover
    COUCHBASE_AVAILABLE = False


class CouchbaseUserKnowledgeStore(CouchbaseConnectGate):
    """Real `UserKnowledgeStore` backed by the dedicated `user_knowledge` bucket."""

    def __init__(self, config: UserKnowledgeStoreConfig, cluster: Any = None) -> None:
        if not COUCHBASE_AVAILABLE:
            raise RuntimeError(
                "The 'couchbase' package is not installed. "
                "Install it (see pyproject.toml) to use CouchbaseUserKnowledgeStore."
            )
        self._config = config
        self._cluster = cluster or Cluster(
            config.user_knowledge_connection_string,
            ClusterOptions(
                PasswordAuthenticator(
                    config.user_knowledge_username, config.user_knowledge_password
                )
            ),
        )
        self._bucket_name = config.user_knowledge_bucket
        bucket = self._cluster.bucket(self._bucket_name)
        self._collection = bucket.default_collection()
        # The connect itself is async; every public coroutine awaits the gate.
        self._init_connect_gate(self._cluster, bucket)
        self._ttl = (
            timedelta(seconds=config.user_knowledge_ttl_seconds)
            if config.user_knowledge_ttl_seconds > 0
            else None
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
        try:
            result = await self._collection.get(record_id, GetOptions())
        except DocumentNotFoundException:
            return None
        return UserKnowledgeRecord.from_doc(result.content_as[dict])

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
