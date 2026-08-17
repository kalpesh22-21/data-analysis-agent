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

CONNECT (2026-08-11): shares `CouchbaseConnectGate` with every other
Couchbase-backed store — `acouchbase` refuses all ops until `on_connect()` has
been awaited, and a sync `__init__` cannot do that, so each public coroutine
gates itself. The consumer/scheduler/inbox daemons build this store and never
connected it; nothing here had ever run from a daemon entrypoint either. See
`runtime/couchbase_connect.py`.

CONSTRUCTION (2026-08-17): also shared, via `CouchbaseStoreBase` — the SDK guard,
the `Cluster`/bucket/collection graph, and the TTL are built there from this
store's own settings, and (with no `cluster=` injected) not until the first
`_ensure_connected()`. `__init__` does no I/O.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.couchbase_connect import (
    CouchbaseStoreBase,
    couchbase_ttl,
    get_or_none,
)

from ..config import LearningSettings
from .judgement import JudgeRecord
from .models import EvidenceSnapshot
from .store import mint_evidence_ref

# The availability flag + the shared SDK symbols live in `couchbase_connect`; this
# is the extra type only this store writes with.
try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    from couchbase.options import UpsertOptions
except ImportError:  # pragma: no cover
    pass


class CouchbaseAuditStore(CouchbaseStoreBase):
    """Real `AuditStore` backed by the dedicated `learning_audit` bucket."""

    def __init__(self, settings: LearningSettings, cluster: Any = None) -> None:
        self._settings = settings
        # KV-only, default scope/collection (no GSI needed — §4.1) = the base's
        # default `_bind_collections`. No I/O here: with no injected cluster the
        # handles are built by the first `_ensure_connected()`.
        self._init_couchbase_store(
            cluster=cluster,
            connection_string=settings.learning_audit_connection_string,
            username=settings.learning_audit_username,
            password=settings.learning_audit_password,
            bucket=settings.learning_audit_bucket,
            ttl_seconds=settings.learning_audit_ttl_seconds,
        )
        # A SEPARATE clock for judge verdicts (plan §3b). Same bucket, different
        # question: an evidence quote is entity-bearing and SHOULD expire on the D95
        # 90-day audit floor, while a verdict row is scalars plus one capped reason and
        # exists to be aggregated over quarters. Inheriting the evidence retention would
        # have erased the dataset roughly as fast as the skew signal it carries
        # accumulates — the composable-blueprint question is answered by months of rows,
        # not by 90 days of them.
        self._judgement_ttl = couchbase_ttl(settings.learning_judge_record_ttl_seconds)

    def mint_evidence_ref(self, session_id: str) -> str:
        return mint_evidence_ref(session_id)

    async def snapshot(self, ref: str, snapshot: EvidenceSnapshot) -> None:
        # Audit TTL is set FRESH on every write — the audit retention clock is
        # independent by design (D95 floor: audit_TTL ≥ max_candidate_lifetime).
        await self._ensure_connected()
        await self._collection.upsert(ref, snapshot.to_doc(), UpsertOptions(expiry=self._ttl))

    async def read(self, ref: str) -> EvidenceSnapshot | None:
        await self._ensure_connected()
        result = await get_or_none(self._collection, ref)
        return None if result is None else EvidenceSnapshot.from_doc(result.content_as[dict])

    async def record_judgement(self, record: JudgeRecord) -> None:
        """Upsert one coverage judgement (plan §3b) under its content-derived key.

        Same collection and same RBAC boundary as the evidence snapshots — splitting
        them across buckets would mean a second user for no gain, and the `record_type`
        discriminator keeps the two families apart in N1QL — but its OWN, much longer
        TTL. See `__init__`.

        DELIBERATELY NOT swallowed: the caller drops a candidate only if this returns.
        Note what that does and does not guarantee — `store.py::AuditStore.
        record_judgement` states plainly that a plain upsert acks from the managed cache
        and that persistence is asynchronous.
        """
        await self._ensure_connected()
        await self._collection.upsert(
            record.judgement_ref,
            record.to_doc(),
            UpsertOptions(expiry=self._judgement_ttl),
        )

    async def read_judgement(self, ref: str) -> JudgeRecord | None:
        await self._ensure_connected()
        result = await get_or_none(self._collection, ref)
        return None if result is None else JudgeRecord.from_doc(result.content_as[dict])
