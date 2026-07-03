"""InMemoryAuditStore — the Layer-1 `AuditStore` fake (§4.2).

Dict-backed, same KV semantics as `CouchbaseAuditStore` (mint / snapshot / read)
for Layer-1 wiring tests — including the S2 invariant test that the consumer path
performs ZERO `snapshot` calls (this fake counts them so QA can assert it).
"""

from __future__ import annotations

from .models import EvidenceSnapshot
from .store import mint_evidence_ref


class InMemoryAuditStore:
    """Dict-backed `AuditStore` fake — no I/O, deterministic, Layer-1 only."""

    def __init__(self) -> None:
        self._snapshots: dict[str, EvidenceSnapshot] = {}
        # Call counters so wiring tests can assert "provisioned, not yet wired"
        # (§4.3 A5): the S2 consumer path must never call `snapshot`.
        self.snapshot_calls = 0
        self.mint_calls = 0

    def mint_evidence_ref(self, session_id: str) -> str:
        self.mint_calls += 1
        return mint_evidence_ref(session_id)

    async def snapshot(self, ref: str, snapshot: EvidenceSnapshot) -> None:
        self.snapshot_calls += 1
        self._snapshots[ref] = snapshot

    async def read(self, ref: str) -> EvidenceSnapshot | None:
        return self._snapshots.get(ref)
