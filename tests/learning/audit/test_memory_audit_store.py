"""S2-audit-store-provisioned (Layer-1 fake) — mint uniqueness/shape, snapshot→
read round-trip, read(missing)→None (matrix row A5's fake + client semantics).

The real-Couchbase mint/snapshot/read/TTL/RBAC proofs are Layer 2
(`tests/integration/test_learning_audit_store_live.py`).
"""

from __future__ import annotations

from data_agent.learning.audit import EvidenceSnapshot, InMemoryAuditStore, mint_evidence_ref


def _snapshot(ref: str, *, session_id="sess-1") -> EvidenceSnapshot:
    return EvidenceSnapshot(
        evidence_ref=ref, session_id=session_id, trace_id="trace-1",
        turn_ref=2, tool_call_ref="call_7", quote="Jane Doe earns $85,000",
        snapshotted_at="2026-07-03T00:00:00+00:00",
    )


def test_mint_shape_and_uniqueness():
    refs = {mint_evidence_ref("sess-1") for _ in range(200)}
    assert len(refs) == 200  # uuid uniqueness
    for ref in refs:
        assert ref.startswith("evidence::sess-1::")
        # evidence::<session_id>::<uuid4> — three ::-delimited segments.
        assert ref.count("::") == 2


def test_mint_is_pure_no_state_on_module_function():
    a = mint_evidence_ref("s")
    b = mint_evidence_ref("s")
    assert a != b  # fresh uuid each call


async def test_snapshot_then_read_round_trips():
    store = InMemoryAuditStore()
    ref = store.mint_evidence_ref("sess-1")
    snap = _snapshot(ref)
    await store.snapshot(ref, snap)
    got = await store.read(ref)
    assert got == snap


async def test_read_missing_returns_none():
    store = InMemoryAuditStore()
    assert await store.read("evidence::nope::123") is None


async def test_fake_counts_mint_and_snapshot_calls():
    store = InMemoryAuditStore()
    assert store.mint_calls == 0
    assert store.snapshot_calls == 0
    ref = store.mint_evidence_ref("sess-1")
    assert store.mint_calls == 1
    await store.snapshot(ref, _snapshot(ref))
    assert store.snapshot_calls == 1


def test_evidence_snapshot_doc_round_trips():
    snap = _snapshot("evidence::sess-1::abc")
    assert EvidenceSnapshot.from_doc(snap.to_doc()) == snap
