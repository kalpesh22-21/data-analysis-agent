"""CouchbaseAuditStore — the real `AuditStore` (D95, §4.2).

Its OWN `Cluster`, authenticated as `learning_audit_writer` against the dedicated
`learning_audit` bucket — a separate RBAC boundary and retention clock from the session store.
KV-only: `snapshot` upserts with the audit TTL set FRESH on every write (the audit clock is
independent, unlike the session transitions that `preserve_expiry`), and `read` returns `None`
on a missing document.

Import-guarded (imports without the SDK; constructing raises), gated per public coroutine by
`CouchbaseConnectGate`, and built through `CouchbaseStoreBase` so `__init__` does no I/O.
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
        """Upsert one coverage judgement under its content-derived key.

        Same collection and RBAC boundary as the evidence snapshots — the `record_type`
        discriminator keeps the two families apart in N1QL — but its OWN, much longer TTL.
        DELIBERATELY NOT swallowed: the caller drops a candidate only if this returns. Note what that
        does not guarantee — a plain upsert acks from the managed cache, and persistence is
        asynchronous.
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
