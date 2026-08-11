"""Layer-2 integration — CouchbaseAuditStore against a LIVE Couchbase
`learning_audit` bucket (D95, §4.2; matrix rows A1–A4).

The proofs, end-to-end against real Couchbase:
  A1. `mint_evidence_ref` yields unique `evidence::<session_id>::<uuid>` keys, pure (no I/O);
  A2. `snapshot` → `read` round-trips an `EvidenceSnapshot` faithfully;
  A3. retention — a snapshot written with a short TTL is gone after expiry;
  A4. RBAC boundary (D17/D51) — `learning_audit_writer` can read/write `learning_audit`
      but is DENIED on `agent_sessions` (isolation), and its scope is the audit bucket only.

Provision first (idempotent):
    docker compose -f docker-compose.integration.yml up -d --wait couchbase
    ./scripts/couchbase-init.sh && ./scripts/learning-audit-init.sh
Run:
    RUN_COUCHBASE_TESTS=1 \
    LEARNING_AUDIT_CONNECTION_STRING=couchbase://localhost \
    LEARNING_AUDIT_USERNAME=learning_audit_writer LEARNING_AUDIT_PASSWORD=audit-writer-pass \
        uv run pytest tests/integration/test_learning_audit_store_live.py -v

`uv run pytest` with no live stack stays fully green (skip-guarded).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from data_agent.learning.audit.couchbase_audit_store import COUCHBASE_AVAILABLE
from data_agent.learning.audit.models import EvidenceSnapshot
from data_agent.learning.config import LearningSettings

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE or not os.environ.get("RUN_COUCHBASE_TESTS"),
    reason="Requires the 'couchbase' package AND a live Couchbase cluster with the "
    "learning_audit bucket + learning_audit_writer RBAC user "
    "(set RUN_COUCHBASE_TESTS=1; run scripts/learning-audit-init.sh).",
)


def _settings(**overrides) -> LearningSettings:
    return LearningSettings(_env_file=None, **overrides)


def _snapshot(ref: str, *, session_id: str) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        evidence_ref=ref, session_id=session_id, trace_id="trace-live-1",
        turn_ref=3, tool_call_ref="call_live_9",
        quote="Jane Doe earns $85,000 in the sales department",
        snapshotted_at="2026-07-03T00:00:00+00:00",
    )


@pytest.fixture
async def audit_store():
    from data_agent.learning.audit.couchbase_audit_store import CouchbaseAuditStore

    store = CouchbaseAuditStore(_settings())
    await store.connect()
    created: list[str] = []
    store._created_refs = created  # type: ignore[attr-defined]
    yield store
    from couchbase.exceptions import DocumentNotFoundException
    for ref in created:
        try:
            await store._collection.remove(ref)
        except DocumentNotFoundException:
            pass


# --- A1: mint uniqueness/shape ----------------------------------------------


async def test_mint_is_unique_and_shaped(audit_store):
    sid = f"sess-{uuid.uuid4().hex[:8]}"
    refs = {audit_store.mint_evidence_ref(sid) for _ in range(100)}
    assert len(refs) == 100
    for ref in refs:
        assert ref.startswith(f"evidence::{sid}::")
        assert ref.count("::") == 2


# --- A2: snapshot → read round-trip -----------------------------------------


async def test_snapshot_then_read_round_trips_live(audit_store):
    sid = f"sess-{uuid.uuid4().hex[:8]}"
    ref = audit_store.mint_evidence_ref(sid)
    snap = _snapshot(ref, session_id=sid)
    await audit_store.snapshot(ref, snap)
    audit_store._created_refs.append(ref)

    got = await audit_store.read(ref)
    assert got == snap


async def test_read_missing_ref_returns_none_live(audit_store):
    assert await audit_store.read(f"evidence::nope::{uuid.uuid4()}") is None


# --- A3: retention / TTL-on-write -------------------------------------------


async def test_short_ttl_snapshot_expires(audit_store):
    # A dedicated store with a 2s TTL proves the audit clock is honored on write.
    short = type(audit_store)(_settings(learning_audit_ttl_seconds=2))
    await short.connect()
    sid = f"sess-{uuid.uuid4().hex[:8]}"
    ref = short.mint_evidence_ref(sid)
    await short.snapshot(ref, _snapshot(ref, session_id=sid))

    assert await short.read(ref) is not None  # present immediately

    # Poll until the KV TTL purges it (Couchbase expiry granularity is seconds).
    deadline = asyncio.get_event_loop().time() + 20.0
    while asyncio.get_event_loop().time() < deadline:
        if await short.read(ref) is None:
            break
        await asyncio.sleep(1.0)
    assert await short.read(ref) is None  # gone after expiry


# --- A4: RBAC boundary (D17/D51) --------------------------------------------


async def test_writer_denied_on_agent_sessions_bucket(audit_store):
    """The `learning_audit_writer` is scoped to `learning_audit` ONLY: it can
    read/write the audit bucket (positive control) but is DENIED any access to
    `agent_sessions` — the isolation D95 requires. A write is used for the denial
    probe so a plain not-found can never masquerade as access."""
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import DocumentNotFoundException
    from couchbase.options import ClusterOptions

    settings = _settings()
    cluster = Cluster(
        settings.learning_audit_connection_string,
        ClusterOptions(
            PasswordAuthenticator(
                settings.learning_audit_username, settings.learning_audit_password
            )
        ),
    )
    await cluster.on_connect()
    try:
        # Positive control: the writer CAN write+read its own bucket.
        audit_coll = cluster.bucket(settings.learning_audit_bucket).default_collection()
        sid = f"sess-{uuid.uuid4().hex[:8]}"
        probe_ref = f"evidence::{sid}::rbac-probe"
        await audit_coll.upsert(probe_ref, {"ok": True})
        assert (await audit_coll.get(probe_ref)).content_as[dict] == {"ok": True}
        await audit_coll.remove(probe_ref)

        # Denial: a WRITE to agent_sessions must be rejected (no data_writer grant).
        sessions_coll = (
            cluster.bucket("agent_sessions")
            .scope("_default")
            .collection("sessions")
        )
        with pytest.raises(Exception) as exc_info:  # noqa: PT011 - SDK maps authz to varied types
            await sessions_coll.upsert(
                f"session::rbac-leak-probe-{uuid.uuid4().hex[:8]}", {"leak": True}
            )
        # A denial must NOT be a mere not-found (which would imply access).
        assert not isinstance(exc_info.value, DocumentNotFoundException)
    finally:
        await cluster.close()
