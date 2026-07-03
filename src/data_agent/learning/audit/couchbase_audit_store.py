"""CouchbaseAuditStore — the real `AuditStore` (D95, §4.2).

Its OWN `Cluster`, authenticated as `learning_audit_writer` against the dedicated
`learning_audit` bucket (`_default._default`) — a separate RBAC boundary and
retention clock from the session store (D95). KV-only: `snapshot` is a
`collection.upsert` with the audit TTL set FRESH on every write (the audit clock
is independent — unlike the session-lifecycle transitions that `preserve_expiry`);
`read` is a `collection.get`, `None` on `DocumentNotFoundException`.

Import-guarded exactly like `runtime/session/couchbase_store.py`: the module
imports with or without the `couchbase` SDK (so the unit suite stays green with
zero infra); constructing the store without the SDK raises.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..config import LearningSettings
from .models import EvidenceSnapshot
from .store import mint_evidence_ref

try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import DocumentNotFoundException
    from couchbase.options import ClusterOptions, GetOptions, UpsertOptions

    COUCHBASE_AVAILABLE = True
except ImportError:  # pragma: no cover
    COUCHBASE_AVAILABLE = False


class CouchbaseAuditStore:
    """Real `AuditStore` backed by the dedicated `learning_audit` bucket."""

    def __init__(self, settings: LearningSettings, cluster: Any = None) -> None:
        if not COUCHBASE_AVAILABLE:
            raise RuntimeError(
                "The 'couchbase' package is not installed. "
                "Install it (see pyproject.toml) to use CouchbaseAuditStore."
            )
        self._settings = settings
        self._cluster = cluster or Cluster(
            settings.learning_audit_connection_string,
            ClusterOptions(
                PasswordAuthenticator(
                    settings.learning_audit_username, settings.learning_audit_password
                )
            ),
        )
        bucket = self._cluster.bucket(settings.learning_audit_bucket)
        # KV-only, default scope/collection (no GSI needed — §4.1).
        self._collection = bucket.default_collection()
        self._ttl = timedelta(seconds=settings.learning_audit_ttl_seconds)

    def mint_evidence_ref(self, session_id: str) -> str:
        return mint_evidence_ref(session_id)

    async def snapshot(self, ref: str, snapshot: EvidenceSnapshot) -> None:
        # Audit TTL is set FRESH on every write — the audit retention clock is
        # independent by design (D95 floor: audit_TTL ≥ max_candidate_lifetime).
        await self._collection.upsert(ref, snapshot.to_doc(), UpsertOptions(expiry=self._ttl))

    async def read(self, ref: str) -> EvidenceSnapshot | None:
        try:
            result = await self._collection.get(ref, GetOptions())
        except DocumentNotFoundException:
            return None
        return EvidenceSnapshot.from_doc(result.content_as[dict])
