"""Layer-2 integration — CouchbaseUserKnowledgeStore against a LIVE Couchbase
`user_knowledge` bucket (Wave 3b-(ii), S8/D17).

This is the ONE learning store allowed to carry ENTITIES — a durable, per-user
fact surfaced only in that user's context (05 §Write targets) — so its RBAC
boundary is load-bearing. The proofs only real Couchbase can give:

  - a `UserKnowledgeRecord` ROUND-TRIPS (commit → get) with its per-user scope,
    provenance, and structured payload intact;
  - PER-USER ISOLATION: two different `user_id`s NEVER cross-read — `list_for_user`
    is a `user_id`-parameterized N1QL scan that returns only the asked-for user's
    rows (no cross-user surface);
  - RBAC boundary (D17/D95): `user_knowledge_writer` can read/write its OWN bucket
    (positive control) but is DENIED a write to another store's bucket
    (`learning_corpus`) — a write probe so a plain not-found can never masquerade
    as access.

Provision first (idempotent):
    docker compose -f docker-compose.integration.yml up -d --wait couchbase
    ./scripts/couchbase-init.sh && ./scripts/learning-user-init.sh
Run (its OWN pytest process — the Couchbase C-ext segfaults when many Clusters
share one interpreter):
    RUN_COUCHBASE_TESTS=1 \
    USER_KNOWLEDGE_CONNECTION_STRING=couchbase://localhost \
    USER_KNOWLEDGE_USERNAME=user_knowledge_writer \
    USER_KNOWLEDGE_PASSWORD=user-writer-pass \
        uv run pytest tests/integration/test_learning_user_store_live.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from data_agent.learning.user.config import UserKnowledgeStoreConfig
from data_agent.learning.user.couchbase_user_store import COUCHBASE_AVAILABLE
from data_agent.learning.user.models import UserKnowledgeRecord, mint_record_id

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE or not os.environ.get("RUN_COUCHBASE_TESTS"),
    reason="Requires the 'couchbase' package AND a live Couchbase with the "
    "user_knowledge bucket + RBAC user (run scripts/learning-user-init.sh; "
    "set RUN_COUCHBASE_TESTS=1).",
)

_SECRET = "Jane-Doe-employee-E12345"


def _config(**overrides) -> UserKnowledgeStoreConfig:
    return UserKnowledgeStoreConfig(_env_file=None, **overrides)


def _record(user_id: str, candidate_id: str, *, statement: str = "prefers UTC timestamps",
            structured: dict | None = None) -> UserKnowledgeRecord:
    return UserKnowledgeRecord(
        record_id=mint_record_id(user_id, candidate_id),
        user_id=user_id,
        statement=statement,
        fact_type="preference",
        scope="user",
        structured=structured,
        source_session=f"sess-{candidate_id}",
        source_trace="trace-1",
        evidence_refs=(f"evidence::sess-{candidate_id}::abc",),
    )


@pytest.fixture
async def user_store():
    from data_agent.learning.user.couchbase_user_store import CouchbaseUserKnowledgeStore

    store = CouchbaseUserKnowledgeStore(_config())
    await store._cluster.on_connect()
    created: list[str] = []
    store._created = created  # type: ignore[attr-defined]
    yield store
    from couchbase.exceptions import DocumentNotFoundException

    for rid in created:
        try:
            await store._collection.remove(rid)
        except DocumentNotFoundException:
            pass


# --- round-trip, per-user scope ---------------------------------------------


async def test_commit_get_round_trips_scoped_live(user_store):
    uid = f"user-{uuid.uuid4().hex[:8]}"
    cid = f"candidate::{uuid.uuid4().hex[:8]}::0"
    rec = _record(uid, cid, statement="uses fiscal-year buckets",
                  structured={"tz": "UTC", "n": 3})
    await user_store.commit(rec)
    user_store._created.append(rec.record_id)

    got = await user_store.get(rec.record_id)
    assert got is not None
    assert got.record_id == rec.record_id
    assert got.user_id == uid
    assert got.statement == "uses fiscal-year buckets"
    assert got.structured == {"tz": "UTC", "n": 3}
    assert got.evidence_refs == rec.evidence_refs


async def test_get_missing_returns_none_live(user_store):
    assert await user_store.get(f"userknow::nobody::{uuid.uuid4().hex}") is None


# --- per-user isolation: two user_ids never cross-read ----------------------


async def test_two_users_never_cross_read_live(user_store):
    """`list_for_user` is `user_id`-parameterized: user A's rows are invisible to a
    list scoped to user B, and vice-versa — the D17 no-cross-user-surface invariant,
    on real N1QL."""
    ua = f"user-A-{uuid.uuid4().hex[:8]}"
    ub = f"user-B-{uuid.uuid4().hex[:8]}"
    rec_a1 = _record(ua, f"candidate::{uuid.uuid4().hex[:8]}::0", statement="A-fact-1")
    rec_a2 = _record(ua, f"candidate::{uuid.uuid4().hex[:8]}::0", statement="A-fact-2")
    rec_b1 = _record(ub, f"candidate::{uuid.uuid4().hex[:8]}::0", statement="B-fact-1")
    for rec in (rec_a1, rec_a2, rec_b1):
        await user_store.commit(rec)
        user_store._created.append(rec.record_id)

    # N1QL is eventually consistent w.r.t. the KV commits — poll until A's two rows
    # (and no more) appear.
    deadline = asyncio.get_event_loop().time() + 15.0
    a_rows: list[UserKnowledgeRecord] = []
    while asyncio.get_event_loop().time() < deadline:
        a_rows = await user_store.list_for_user(ua, limit=100)
        if len(a_rows) >= 2:
            break
        await asyncio.sleep(0.5)

    a_ids = {r.record_id for r in a_rows}
    assert a_ids == {rec_a1.record_id, rec_a2.record_id}
    assert all(r.user_id == ua for r in a_rows)
    # B's row is NEVER in A's surface.
    assert rec_b1.record_id not in a_ids

    b_rows = await user_store.list_for_user(ub, limit=100)
    b_ids = {r.record_id for r in b_rows}
    assert rec_b1.record_id in b_ids
    assert rec_a1.record_id not in b_ids
    assert rec_a2.record_id not in b_ids


# --- RBAC boundary (D17/D95) ------------------------------------------------


async def test_user_writer_denied_on_other_bucket_live(user_store):
    """`user_knowledge_writer` is scoped to `user_knowledge` ONLY: it can write its
    own bucket (positive control) but is DENIED a write to `learning_corpus` — a
    sibling learning store, off-limits (D17/D95). A write probe is used so a plain
    not-found can never masquerade as access."""
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import DocumentNotFoundException
    from couchbase.options import ClusterOptions

    config = _config()
    cluster = Cluster(
        config.user_knowledge_connection_string,
        ClusterOptions(
            PasswordAuthenticator(
                config.user_knowledge_username, config.user_knowledge_password
            )
        ),
    )
    await cluster.on_connect()
    try:
        # Positive control: writer CAN write+read its own bucket.
        own = cluster.bucket(config.user_knowledge_bucket).default_collection()
        probe = f"userknow::rbac-probe::{uuid.uuid4().hex[:8]}"
        await own.upsert(probe, {"ok": True})
        assert (await own.get(probe)).content_as[dict] == {"ok": True}
        await own.remove(probe)

        # Denial on learning_corpus (write probe) — a sibling store, still off-limits.
        corpus = cluster.bucket("learning_corpus").default_collection()
        with pytest.raises(Exception) as corpus_exc:  # noqa: PT011 - SDK maps authz to varied types
            await corpus.upsert(f"corpus::leak-{uuid.uuid4().hex[:8]}", {"leak": True})
        assert not isinstance(corpus_exc.value, DocumentNotFoundException)

        # Denial on learning_candidates (write probe) too — the entity-free sibling.
        candidates = cluster.bucket("learning_candidates").default_collection()
        with pytest.raises(Exception) as cand_exc:  # noqa: PT011
            await candidates.upsert(f"candidate::leak-{uuid.uuid4().hex[:8]}", {"leak": True})
        assert not isinstance(cand_exc.value, DocumentNotFoundException)
    finally:
        await cluster.close()
