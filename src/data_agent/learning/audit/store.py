"""AuditStore — the `evidence_ref` KV port (D95, §4.2).

Mirrors the `SessionStore` port pattern: a Protocol + a real Couchbase impl
(`couchbase_audit_store`) + an in-memory fake (`memory_audit_store`). KV-only
(get/put by `evidence_ref`), so no N1QL/GSI is involved. S2 stands the client up
and Layer-2-validates it, but writes NO evidence (§4.3) — the first `snapshot`
call is S3's.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from .models import EvidenceSnapshot


def mint_evidence_ref(session_id: str) -> str:
    """Pure key mint (no I/O): `evidence::<session_id>::<uuid4>`.

    The ref is non-entity-bearing (a session id is non-PII per D25 + an opaque
    uuid), so it is safe to carry on the entity-free global candidate (D95). S3
    can mint → attach → snapshot deterministically because minting is side-effect
    free."""
    return f"evidence::{session_id}::{uuid.uuid4()}"


class AuditStore(Protocol):
    def mint_evidence_ref(self, session_id: str) -> str:
        """Mint a fresh `evidence_ref` KV key (pure — no I/O)."""
        ...

    async def snapshot(self, ref: str, snapshot: EvidenceSnapshot) -> None:
        """KV-upsert *snapshot* at *ref* with the audit TTL (retention is the
        audit clock, set fresh on every write — §4.2)."""
        ...

    async def read(self, ref: str) -> EvidenceSnapshot | None:
        """Read the snapshot at *ref* (for the S7 review UI), or `None` if
        absent/expired."""
        ...
