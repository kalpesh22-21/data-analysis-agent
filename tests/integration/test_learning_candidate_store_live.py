"""Layer-2 integration — CouchbaseCandidateStore against a LIVE Couchbase
`learning_candidates` bucket (D101).

The proofs, end-to-end against real Couchbase:
  - put → get round-trips a `CandidateEnvelope` (KV);
  - list_by_status returns `status=extracted` candidates (N1QL, needs the index);
  - RBAC boundary (D101/D17): `learning_candidates_writer` can read/write its own
    bucket but is DENIED on BOTH `agent_sessions` AND `learning_audit` (isolation);
  - end-to-end: the entity-bearing evidence quote is written ONLY to
    `learning_audit`; the persisted candidate carries only the `evidence_ref`.

Provision first (idempotent):
    docker compose -f docker-compose.integration.yml up -d --wait couchbase
    ./scripts/couchbase-init.sh && ./scripts/learning-audit-init.sh \
        && ./scripts/learning-candidates-init.sh
Run:
    RUN_COUCHBASE_TESTS=1 \
    LEARNING_CANDIDATES_CONNECTION_STRING=couchbase://localhost \
    LEARNING_CANDIDATES_USERNAME=learning_candidates_writer \
    LEARNING_CANDIDATES_PASSWORD=candidates-writer-pass \
    LEARNING_AUDIT_CONNECTION_STRING=couchbase://localhost \
    LEARNING_AUDIT_USERNAME=learning_audit_writer LEARNING_AUDIT_PASSWORD=audit-writer-pass \
        uv run pytest tests/integration/test_learning_candidate_store_live.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from data_agent.learning.candidate.couchbase_candidate_store import COUCHBASE_AVAILABLE
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.config import LearningSettings

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE or not os.environ.get("RUN_COUCHBASE_TESTS"),
    reason="Requires the 'couchbase' package AND a live Couchbase with the "
    "learning_candidates bucket + RBAC user (run scripts/learning-candidates-init.sh; "
    "set RUN_COUCHBASE_TESTS=1).",
)

_SECRET = "SSN-777-00-1234-Jane-Doe"


def _settings(**overrides) -> LearningSettings:
    return LearningSettings(_env_file=None, **overrides)


def _envelope(candidate_id: str, *, status: str = CandidateStatus.EXTRACTED,
              evidence_refs=("evidence::sess-1::abc",)) -> CandidateEnvelope:
    return CandidateEnvelope(
        candidate_id=candidate_id, type="blueprint", status=status,
        payload={"intent": "total earnings for a department in a year", "kind": "single"},
        source_session="sess-1", source_trace="trace-1",
        evidence_refs=tuple(evidence_refs), extractor_rationale="reusable report",
        entity_scan={"result": "pending", "hits": []}, confidence=0.9,
        proposed_action="new", depends_on=(), content_hash="hash-live-1",
    )


@pytest.fixture
async def candidate_store():
    from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore

    settings = _settings()
    store = CouchbaseCandidateStore(settings)
    await store._cluster.on_connect()
    # NOTE: list_by_status needs a primary/status index, but the writer is
    # deliberately scoped WITHOUT query_manage_index (D101 RBAC) — the index is
    # provisioned by scripts/learning-candidates-init.sh as admin, not here.
    created: list[str] = []
    store._created = created  # type: ignore[attr-defined]
    yield store
    from couchbase.exceptions import DocumentNotFoundException
    for cid in created:
        try:
            await store._collection.remove(cid)
        except DocumentNotFoundException:
            pass


# --- put / get / list_by_status ---------------------------------------------


async def test_put_get_round_trips_live(candidate_store):
    cid = f"candidate::hash-live::{uuid.uuid4().hex[:8]}"
    env = _envelope(cid)
    await candidate_store.put(env)
    candidate_store._created.append(cid)

    got = await candidate_store.get(cid)
    assert got is not None
    assert got.candidate_id == cid
    assert got.status == CandidateStatus.EXTRACTED
    assert got.evidence_refs == ("evidence::sess-1::abc",)


async def test_get_missing_returns_none_live(candidate_store):
    assert await candidate_store.get(f"candidate::nope::{uuid.uuid4().hex}") is None


async def test_list_by_status_returns_extracted_live(candidate_store):
    tag = uuid.uuid4().hex[:8]
    cid = f"candidate::hash-live::{tag}"
    await candidate_store.put(_envelope(cid))
    candidate_store._created.append(cid)

    # N1QL is eventually consistent w.r.t. the KV write — poll until it appears.
    deadline = asyncio.get_event_loop().time() + 15.0
    found = []
    while asyncio.get_event_loop().time() < deadline:
        rows = await candidate_store.list_by_status(CandidateStatus.EXTRACTED, limit=500)
        found = [c for c in rows if c.candidate_id == cid]
        if found:
            break
        await asyncio.sleep(0.5)
    assert len(found) == 1
    assert all(c.status == CandidateStatus.EXTRACTED for c in
               await candidate_store.list_by_status(CandidateStatus.EXTRACTED, limit=500))


# --- MEDIUM-3: supersede live -----------------------------------------------


async def test_supersede_removes_prior_attempt_live(candidate_store):
    """A 2nd extraction attempt for the same session content supersedes the 1st:
    after `supersede(content_hash)`, none of the prior candidates remain."""
    ch = f"hash-super-{uuid.uuid4().hex[:8]}"
    ids = [f"candidate::{ch}::{i}" for i in range(3)]
    for cid in ids:
        env = _envelope(cid)
        # override the content_hash so supersede can find them by it
        env = CandidateEnvelope.from_doc({**env.to_doc(), "content_hash": ch})
        await candidate_store.put(env)
        candidate_store._created.append(cid)
    for cid in ids:
        assert await candidate_store.get(cid) is not None

    # supersede SELECTs by content_hash (N1QL, eventually consistent w.r.t. the KV
    # puts) then KV-removes — poll until the index has caught up and all are gone.
    async def _all_gone() -> bool:
        for cid in ids:
            if await candidate_store.get(cid) is not None:
                return False
        return True

    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        await candidate_store.supersede(ch)
        if await _all_gone():
            break
        await asyncio.sleep(0.5)

    assert await _all_gone()


# --- RBAC boundary (D101/D17) -----------------------------------------------


async def test_candidates_writer_denied_on_sessions_and_audit(candidate_store):
    """`learning_candidates_writer` is scoped to `learning_candidates` ONLY: it
    can write its own bucket (positive control) but is DENIED a write to BOTH
    `agent_sessions` and `learning_audit`. A write probe is used so a plain
    not-found can never masquerade as access."""
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import DocumentNotFoundException
    from couchbase.options import ClusterOptions

    settings = _settings()
    cluster = Cluster(
        settings.learning_candidates_connection_string,
        ClusterOptions(
            PasswordAuthenticator(
                settings.learning_candidates_username, settings.learning_candidates_password
            )
        ),
    )
    await cluster.on_connect()
    try:
        # Positive control: writer CAN write+read its own bucket.
        own = cluster.bucket(settings.learning_candidates_bucket).default_collection()
        probe = f"candidate::rbac-probe::{uuid.uuid4().hex[:8]}"
        await own.upsert(probe, {"ok": True})
        assert (await own.get(probe)).content_as[dict] == {"ok": True}
        await own.remove(probe)

        # Denial on agent_sessions (write probe).
        sessions = cluster.bucket("agent_sessions").scope("_default").collection("sessions")
        with pytest.raises(Exception) as sess_exc:  # noqa: PT011 - SDK maps authz to varied types
            await sessions.upsert(f"session::leak-{uuid.uuid4().hex[:8]}", {"leak": True})
        assert not isinstance(sess_exc.value, DocumentNotFoundException)

        # Denial on learning_audit (write probe) — a sibling store, still off-limits.
        audit = cluster.bucket("learning_audit").default_collection()
        with pytest.raises(Exception) as audit_exc:  # noqa: PT011
            await audit.upsert(f"evidence::leak-{uuid.uuid4().hex[:8]}", {"leak": True})
        assert not isinstance(audit_exc.value, DocumentNotFoundException)
    finally:
        await cluster.close()


# --- end-to-end: real evidence write + ref-only candidate persistence -------


async def test_evidence_in_audit_candidate_is_ref_only_live(candidate_store):
    """The entity-bearing quote is written ONLY to `learning_audit`; the persisted
    candidate in `learning_candidates` carries only the `evidence_ref`, never the
    quote (D17/D51/D95)."""
    from data_agent.learning.audit.couchbase_audit_store import CouchbaseAuditStore
    from data_agent.learning.audit.models import EvidenceSnapshot

    audit = CouchbaseAuditStore(_settings())
    await audit._cluster.on_connect()

    sid = f"sess-{uuid.uuid4().hex[:8]}"
    ref = audit.mint_evidence_ref(sid)
    await audit.snapshot(ref, EvidenceSnapshot(
        evidence_ref=ref, session_id=sid, trace_id="trace-1", turn_ref=0,
        tool_call_ref="tc1", quote=_SECRET, snapshotted_at="2026-07-03T00:00:00+00:00",
    ))
    try:
        cid = f"candidate::hash-live::{uuid.uuid4().hex[:8]}"
        await candidate_store.put(_envelope(cid, evidence_refs=(ref,)))
        candidate_store._created.append(cid)

        # The audit store holds the secret quote ...
        audit_snap = await audit.read(ref)
        assert audit_snap is not None
        assert audit_snap.quote == _SECRET

        # ... the persisted candidate carries only the ref, never the quote.
        got = await candidate_store.get(cid)
        assert got is not None
        assert ref in got.evidence_refs
        import json
        assert _SECRET not in json.dumps(got.to_doc())
    finally:
        await audit._collection.remove(ref)
        await audit._cluster.close()
