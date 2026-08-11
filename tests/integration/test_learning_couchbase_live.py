"""Layer-2 integration — the learning-loop's Couchbase leg against a LIVE
Couchbase cluster (`l2-cb` in `docker-compose.integration.yml`). This is the leg
NOT yet live-validated, so it is the priority. Matrix rows 5/6/8/10 (design
§3/§6, D96).

The proofs, end-to-end against real Couchbase:
  1. `scan_idle_sessions` returns ONLY sessions whose `learning_status` is
     sweepable (`active`/`pending`) AND whose `last_activity` is older than the
     cutoff — a fresh session and a `done`/`processing` session are excluded — and
     each row carries a usable `META().cas`;
  2. `transition_learning_status` is a real CAS `replace` with a from-state
     assert: the happy path advances the flag and returns a NEW cas; a STALE-cas
     transition is rejected (`CASMismatchError`); a from-state mismatch is
     rejected;
  3. a transition writes ONLY `learning_status` (+ `learning_content_hash`) —
     `last_activity`, `messages` and the `tool_trail` are BYTE-unchanged (D72);
  4. (if Redis is also up) a full sweeper -> Redis -> consumer round-trip drives a
     real idle session all the way to `done` with zero PEL leak.

Skip-guarded on RUN_COUCHBASE_TESTS (+ the couchbase SDK); the round-trip test
additionally needs LEARNING_REDIS_TEST_URL. `uv run pytest` with no live stack
stays fully green. Run with the stack up:

    docker compose -f docker-compose.integration.yml up -d --wait couchbase l2-redis
    ./scripts/couchbase-init.sh
    RUN_COUCHBASE_TESTS=1 \
    COUCHBASE_CONNECTION_STRING=couchbase://localhost \
    COUCHBASE_USERNAME=admin COUCHBASE_PASSWORD=password \
    LEARNING_REDIS_TEST_URL=redis://localhost:6379/0 \
        uv run pytest tests/integration/test_learning_couchbase_live.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from data_agent.learning.config import LearningSettings
from data_agent.learning.models import LearningStatus, compute_content_hash
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.session.couchbase_store import COUCHBASE_AVAILABLE
from data_agent.runtime.session.models import SessionDoc, TrailEntry, TurnMessage
from data_agent.runtime.session.store import CASMismatchError

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE or not os.environ.get("RUN_COUCHBASE_TESTS"),
    reason="Requires the 'couchbase' package AND a live Couchbase cluster "
    "(set RUN_COUCHBASE_TESTS=1 with a reachable COUCHBASE_CONNECTION_STRING).",
)

_OLD = "2000-01-01T00:00:00+00:00"


def _settings() -> RuntimeSettings:
    return RuntimeSettings(_env_file=None)


def _session_doc(session_id: str, *, last_activity: str, status: str) -> SessionDoc:
    return SessionDoc(
        session_id=session_id,
        created_at=_OLD,
        last_activity=last_activity,
        learning_status=status,
        messages=[
            TurnMessage(turn_index=0, role="user", content="how much overtime?",
                        ts=_OLD, provenance=frozenset()),
            TurnMessage(turn_index=0, role="assistant", content="Let me check.",
                        ts=_OLD, provenance=frozenset()),
        ],
        tool_trail=[
            TrailEntry(
                turn_index=0, tool_call_id="call_1", tool_name="runQuery",
                args={"sql": "SELECT sum(ot) FROM hr.pay"}, status="ok",
                error_code=None, provenance=frozenset({("hr.pay", "ot")}),
                result_preview=None, result_full_ref="result::abc", ts=_OLD,
            )
        ],
    )


@pytest.fixture
async def store():
    from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

    settings = _settings()
    st = CouchbaseSessionStore(settings)
    # The store's own methods gate themselves (`CouchbaseConnectGate`), but the DDL
    # below bypasses them and drives the raw cluster (there is no store method for
    # CREATE INDEX), so this fixture still connects explicitly.
    await st.connect()
    # scan_idle_sessions runs N1QL over the sessions collection — a primary index
    # is required. IF NOT EXISTS makes this idempotent across runs.
    keyspace = (
        f"`{settings.couchbase_bucket}`.`{settings.couchbase_scope}`"
        f".`{settings.couchbase_sessions_collection}`"
    )
    index_result = st._cluster.query(f"CREATE PRIMARY INDEX IF NOT EXISTS ON {keyspace}")
    async for _ in index_result:  # drive the statement to completion
        pass
    created: list[str] = []
    st._created_keys = created  # type: ignore[attr-defined]
    yield st
    # Teardown: remove every doc this test created.
    from couchbase.exceptions import DocumentNotFoundException
    for sid in created:
        try:
            await st._sessions.remove(f"session::{sid}")
        except DocumentNotFoundException:
            pass


async def _seed(store, session_id: str, *, last_activity: str, status: str) -> None:
    doc = _session_doc(session_id, last_activity=last_activity, status=status)
    await store._upsert_doc(session_id, doc)
    store._created_keys.append(session_id)


async def _scan_until(store, *, statuses, cutoff, expect_id, timeout=15.0):
    """Poll `scan_idle_sessions` until *expect_id* appears (the GSI index is
    eventually consistent w.r.t. the KV upsert). Returns the full row list."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        rows = await store.scan_idle_sessions(
            statuses=statuses, last_activity_before=cutoff, limit=200
        )
        if any(d.session_id == expect_id for d, _ in rows) or expect_id is None:
            return rows
        if asyncio.get_event_loop().time() > deadline:
            return rows
        await asyncio.sleep(0.5)


def _cutoff() -> str:
    return (datetime.now(UTC) - timedelta(seconds=1800)).isoformat()


# --- Row 6: scan_idle_sessions selectivity ----------------------------------


async def test_scan_returns_only_idle_sweepable_sessions(store):
    tag = uuid.uuid4().hex[:8]
    idle_active = f"idle-active-{tag}"
    idle_pending = f"idle-pending-{tag}"
    fresh = f"fresh-{tag}"
    done = f"done-{tag}"

    await _seed(store, idle_active, last_activity=_OLD, status=LearningStatus.ACTIVE)
    await _seed(store, idle_pending, last_activity=_OLD, status=LearningStatus.PENDING)
    await _seed(store, fresh, last_activity=datetime.now(UTC).isoformat(),
                status=LearningStatus.ACTIVE)
    await _seed(store, done, last_activity=_OLD, status=LearningStatus.DONE)

    rows = await _scan_until(
        store,
        statuses=[LearningStatus.ACTIVE, LearningStatus.PENDING],
        cutoff=_cutoff(),
        expect_id=idle_active,
    )
    ids = {d.session_id for d, _ in rows}

    assert idle_active in ids
    assert idle_pending in ids
    assert fresh not in ids   # too recent
    assert done not in ids    # not sweepable
    # Each returned row carries a usable (truthy) CAS token.
    for d, cas in rows:
        if d.session_id in (idle_active, idle_pending):
            assert cas


# --- Row 5/8: transition_learning_status CAS semantics ----------------------


async def test_transition_happy_path_returns_new_cas(store):
    sid = f"trans-ok-{uuid.uuid4().hex[:8]}"
    await _seed(store, sid, last_activity=_OLD, status=LearningStatus.ACTIVE)
    _, cas = await store.get_session_with_cas(sid)

    new_cas = await store.transition_learning_status(
        sid, LearningStatus.ACTIVE, LearningStatus.PENDING, cas
    )
    assert new_cas
    assert new_cas != cas
    doc, _ = await store.get_session_with_cas(sid)
    assert doc.learning_status == LearningStatus.PENDING


async def test_stale_cas_transition_is_rejected(store):
    sid = f"trans-stale-{uuid.uuid4().hex[:8]}"
    await _seed(store, sid, last_activity=_OLD, status=LearningStatus.ACTIVE)
    _, cas = await store.get_session_with_cas(sid)

    # A concurrent write advances the doc (and its cas).
    await store.transition_learning_status(sid, LearningStatus.ACTIVE, LearningStatus.PENDING, cas)

    # The now-stale snapshot cas must be rejected — single-writer-per-session.
    with pytest.raises(CASMismatchError):
        await store.transition_learning_status(
            sid, LearningStatus.PENDING, LearningStatus.QUEUED, cas
        )


async def test_from_state_mismatch_is_rejected(store):
    sid = f"trans-from-{uuid.uuid4().hex[:8]}"
    await _seed(store, sid, last_activity=_OLD, status=LearningStatus.QUEUED)
    _, cas = await store.get_session_with_cas(sid)
    # The doc is `queued`, but we assert `active` -> rejected even with a valid cas.
    with pytest.raises(CASMismatchError):
        await store.transition_learning_status(
            sid, LearningStatus.ACTIVE, LearningStatus.PENDING, cas
        )


# --- Row: D72 read-only — only the lifecycle flag changes -------------------


async def test_transition_writes_only_lifecycle_flag(store):
    sid = f"readonly-{uuid.uuid4().hex[:8]}"
    await _seed(store, sid, last_activity=_OLD, status=LearningStatus.ACTIVE)
    before, cas = await store.get_session_with_cas(sid)
    before_wire = before.to_doc()
    content_hash = compute_content_hash(before)

    await store.transition_learning_status(
        sid, LearningStatus.ACTIVE, LearningStatus.PENDING, cas, content_hash=content_hash
    )

    after, _ = await store.get_session_with_cas(sid)
    after_wire = after.to_doc()

    assert after.learning_status == LearningStatus.PENDING
    assert after.learning_content_hash == content_hash
    # Everything else is byte-identical — last_activity NOT bumped (no resurrection).
    assert after_wire["last_activity"] == before_wire["last_activity"]
    assert after_wire["created_at"] == before_wire["created_at"]
    assert after_wire["messages"] == before_wire["messages"]
    assert after_wire["tool_trail"] == before_wire["tool_trail"]


# --- Row 10: full round-trip against real Couchbase + real Redis -------------


@pytest.mark.skipif(
    not os.environ.get("LEARNING_REDIS_TEST_URL"),
    reason="The round-trip additionally needs a live Redis (LEARNING_REDIS_TEST_URL).",
)
async def test_full_sweeper_redis_consumer_round_trip(store):
    from data_agent.learning.consumer import LearningConsumer
    from data_agent.learning.redis_queue import RedisStreamsLearningQueue
    from data_agent.learning.sweeper import LearningSweeper

    tag = uuid.uuid4().hex[:12]
    sid = f"roundtrip-{tag}"
    await _seed(store, sid, last_activity=_OLD, status=LearningStatus.ACTIVE)

    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(os.environ["LEARNING_REDIS_TEST_URL"], decode_responses=True)
    stream = f"test:rt:jobs:{tag}"
    dead = f"test:rt:jobs:dead:{tag}"
    queue = RedisStreamsLearningQueue(
        redis_client, stream=stream, group="learning-workers",
        consumer_name=f"worker-rt-{tag}", dead_letter_stream=dead,
    )
    settings = LearningSettings(_env_file=None, learning_idle_threshold_seconds=1800)

    try:
        await queue.ensure_group()
        # Give the GSI a chance to see the seeded doc before the sweep scans.
        await _scan_until(
            store, statuses=[LearningStatus.ACTIVE, LearningStatus.PENDING],
            cutoff=_cutoff(), expect_id=sid,
        )

        # NOTE: the sweep is bucket-wide — the shared cluster may hold OTHER idle
        # sessions from prior runs, so we assert on OUR session end-to-end, not on
        # global stream/PEL counts (isolation over a shared, TTL'd bucket).
        sweep = await LearningSweeper(store, queue, settings).run_once()
        assert sweep.enqueued >= 1
        doc, _ = await store.get_session_with_cas(sid)
        assert doc.learning_status == LearningStatus.QUEUED

        # Find OUR message on the real stream.
        my_msg_id = None
        for entry_id, fields in await redis_client.xrange(stream):
            if fields.get("session_id") == sid:
                my_msg_id = entry_id
                break
        assert my_msg_id is not None

        # Drive the consumer until OUR session reaches `done` (the batch may hold
        # other sessions' jobs too; a bounded loop drains progressively).
        consumer = LearningConsumer(store, queue, settings)
        for _ in range(20):
            await consumer.run_once()
            doc2, _ = await store.get_session_with_cas(sid)
            if doc2.learning_status == LearningStatus.DONE:
                break

        doc2, _ = await store.get_session_with_cas(sid)
        assert doc2.learning_status == LearningStatus.DONE
        assert doc2.learning_content_hash == compute_content_hash(doc2)
        # OUR message is ACKed off the PEL (zero leak for this job).
        my_pending = await redis_client.xpending_range(
            stream, "learning-workers", min=my_msg_id, max=my_msg_id, count=1
        )
        assert my_pending == []
    finally:
        await redis_client.delete(stream, dead)
        async for key in redis_client.scan_iter(match=f"{stream}:enqueued:*"):
            await redis_client.delete(key)
        await redis_client.aclose()
