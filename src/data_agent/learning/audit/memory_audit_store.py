"""InMemoryAuditStore — the Layer-1 `AuditStore` fake (§4.2).

Dict-backed, same KV semantics as `CouchbaseAuditStore` (mint / snapshot / read, plus
the judge's record_judgement / read_judgement) for Layer-1 wiring tests — including the
S2 invariant test that the consumer path performs ZERO `snapshot` calls (this fake
counts them so QA can assert it).

`fail_judgements` exists because the judge's write is the ONE audit write whose failure
changes behaviour (a record that did not land must not be followed by a drop). A fake
that can only succeed cannot exercise that branch, and that branch is the mitigation.
"""

from __future__ import annotations

from .judgement import JudgeRecord
from .models import EvidenceSnapshot
from .store import mint_evidence_ref


class InMemoryAuditStore:
    """Dict-backed `AuditStore` fake — no I/O, deterministic, Layer-1 only."""

    def __init__(
        self, *, fail_judgements: bool = False, fail_judgement_reads: bool = False
    ) -> None:
        self._snapshots: dict[str, EvidenceSnapshot] = {}
        self._judgements: dict[str, JudgeRecord] = {}
        # Call counters so wiring tests can assert "provisioned, not yet wired"
        # (§4.3 A5): the S2 consumer path must never call `snapshot`.
        self.snapshot_calls = 0
        self.mint_calls = 0
        self._fail_judgements = fail_judgements
        self._fail_judgement_reads = fail_judgement_reads

    def mint_evidence_ref(self, session_id: str) -> str:
        self.mint_calls += 1
        return mint_evidence_ref(session_id)

    async def snapshot(self, ref: str, snapshot: EvidenceSnapshot) -> None:
        self.snapshot_calls += 1
        self._snapshots[ref] = snapshot

    async def read(self, ref: str) -> EvidenceSnapshot | None:
        return self._snapshots.get(ref)

    async def record_judgement(self, record: JudgeRecord) -> None:
        if self._fail_judgements:
            raise RuntimeError("in-memory audit store scripted to fail judgement writes")
        self._judgements[record.judgement_ref] = record

    async def read_judgement(self, ref: str) -> JudgeRecord | None:
        if self._fail_judgement_reads:
            raise RuntimeError("in-memory audit store scripted to fail judgement reads")
        return self._judgements.get(ref)

    @property
    def judgements(self) -> tuple[JudgeRecord, ...]:
        """Every judgement written, in insertion order — the assertion surface for
        "a drop left a durable record"."""
        return tuple(self._judgements.values())
