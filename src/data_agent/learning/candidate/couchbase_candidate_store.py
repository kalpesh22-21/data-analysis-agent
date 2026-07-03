"""CouchbaseCandidateStore — the real `CandidateStore` (D101).

Its OWN `Cluster`, authenticated as `learning_candidates_writer` against the
dedicated `learning_candidates` bucket (`_default._default`) — a separate RBAC
boundary from the session/audit stores (D101, mirroring D95). `put` is a KV
upsert with the candidate TTL; `list_by_status` is a parameterized N1QL query
(needs the primary index provisioned by `scripts/learning-candidates-init.sh`).

Import-guarded exactly like `couchbase_store` / `couchbase_audit_store`: imports
with or without the SDK; constructing without it raises.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..config import LearningSettings
from .models import CandidateEnvelope

try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import DocumentNotFoundException
    from couchbase.options import ClusterOptions, GetOptions, QueryOptions, UpsertOptions

    COUCHBASE_AVAILABLE = True
except ImportError:  # pragma: no cover
    COUCHBASE_AVAILABLE = False


class CouchbaseCandidateStore:
    """Real `CandidateStore` backed by the dedicated `learning_candidates` bucket."""

    def __init__(self, settings: LearningSettings, cluster: Any = None) -> None:
        if not COUCHBASE_AVAILABLE:
            raise RuntimeError(
                "The 'couchbase' package is not installed. "
                "Install it (see pyproject.toml) to use CouchbaseCandidateStore."
            )
        self._settings = settings
        self._cluster = cluster or Cluster(
            settings.learning_candidates_connection_string,
            ClusterOptions(
                PasswordAuthenticator(
                    settings.learning_candidates_username,
                    settings.learning_candidates_password,
                )
            ),
        )
        self._bucket_name = settings.learning_candidates_bucket
        bucket = self._cluster.bucket(self._bucket_name)
        self._collection = bucket.default_collection()
        self._ttl = timedelta(seconds=settings.learning_candidates_ttl_seconds)

    async def put(self, envelope: CandidateEnvelope) -> None:
        await self._collection.upsert(
            envelope.candidate_id, envelope.to_doc(), UpsertOptions(expiry=self._ttl)
        )

    async def get(self, candidate_id: str) -> CandidateEnvelope | None:
        try:
            result = await self._collection.get(candidate_id, GetOptions())
        except DocumentNotFoundException:
            return None
        return CandidateEnvelope.from_doc(result.content_as[dict])

    async def list_by_status(self, status: str, *, limit: int = 100) -> list[CandidateEnvelope]:
        statement = (
            f"SELECT c.* FROM `{self._bucket_name}` c "
            "WHERE c.status = $status "
            "ORDER BY c.created_at ASC LIMIT $limit"
        )
        result = self._cluster.query(
            statement,
            QueryOptions(named_parameters={"status": status, "limit": int(limit)}),
        )
        out: list[CandidateEnvelope] = []
        async for row in result:
            out.append(CandidateEnvelope.from_doc(row))
        return out

    async def supersede(self, content_hash: str) -> None:
        # N1QL-SELECT the stale candidate ids (query_select), then KV-remove each
        # (data_writer) — avoids needing query_delete on the writer role. A doc
        # already gone (concurrent removal) is a tolerated no-op.
        statement = (
            f"SELECT META(c).id AS id FROM `{self._bucket_name}` c "
            "WHERE c.content_hash = $content_hash"
        )
        result = self._cluster.query(
            statement, QueryOptions(named_parameters={"content_hash": content_hash})
        )
        ids = [row["id"] async for row in result]
        for candidate_id in ids:
            try:
                await self._collection.remove(candidate_id)
            except DocumentNotFoundException:
                continue
