"""Layer-2 integration — the S2 consumer (load → triage → skip/keep → done)
against REAL Couchbase (`session` + `session_results`) + REAL Redis
(matrix rows C1/C2/C5/C6, and the zero-evidence invariant, live).

The proofs, end-to-end against real infra:
  C6. the loader hydrates the D46 full result from the real `session_results`
      collection during a live consume (`full_result_loaded=True`, real columns);
  C1/C2. a skip session and a keep session both end `done` in real Couchbase;
  C5. D72 — the only session change is `learning_status`(+hash);
  A5/row 6. the injected `InMemoryAuditStore` receives ZERO snapshot calls (S2
      writes no evidence, live).

Requires BOTH Couchbase (RUN_COUCHBASE_TESTS) and Redis (LEARNING_REDIS_TEST_URL).
Run with the stack up:

    docker compose -f docker-compose.integration.yml up -d --wait couchbase l2-redis
    ./scripts/couchbase-init.sh
    RUN_COUCHBASE_TESTS=1 COUCHBASE_CONNECTION_STRING=couchbase://localhost \
    COUCHBASE_USERNAME=admin COUCHBASE_PASSWORD=password \
    LEARNING_REDIS_TEST_URL=redis://localhost:6379/0 \
        uv run pytest tests/integration/test_learning_consumer_slice2_live.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.learning.summary import load_session_summary
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.session.couchbase_store import COUCHBASE_AVAILABLE
from data_agent.runtime.session.models import SessionDoc, TrailEntry, TurnMessage

_REDIS_URL = os.environ.get("LEARNING_REDIS_TEST_URL")

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE
    or not os.environ.get("RUN_COUCHBASE_TESTS")
    or not _REDIS_URL,
    reason="Requires a live Couchbase (RUN_COUCHBASE_TESTS) AND a live Redis "
    "(LEARNING_REDIS_TEST_URL).",
)

_TS = "2000-01-01T00:00:00+00:00"


@pytest.fixture
async def store():
    from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

    st = CouchbaseSessionStore(RuntimeSettings(_env_file=None))
    await st._cluster.on_connect()
    created: list[str] = []
    st._created = created  # type: ignore[attr-defined]
    yield st
    from couchbase.exceptions import DocumentNotFoundException
    for sid in created:
        try:
            await st._sessions.remove(f"session::{sid}")
        except DocumentNotFoundException:
            pass


@pytest.fixture
async def redis_queue():
    import redis.asyncio as aioredis

    from data_agent.learning.redis_queue import RedisStreamsLearningQueue

    tag = uuid.uuid4().hex[:12]
    client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    stream = f"test:s2:jobs:{tag}"
    dead = f"test:s2:jobs:dead:{tag}"
    queue = RedisStreamsLearningQueue(
        client, stream=stream, group="learning-workers",
        consumer_name=f"worker-s2-{tag}", dead_letter_stream=dead,
    )
    await queue.ensure_group()
    yield queue
    await client.delete(stream, dead)
    async for key in client.scan_iter(match=f"{stream}:enqueued:*"):
        await client.delete(key)
    await client.aclose()


def _seed_doc(sid: str, *, messages, tool_trail, status=LearningStatus.QUEUED) -> SessionDoc:
    return SessionDoc(
        session_id=sid, created_at=_TS, last_activity=_TS, learning_status=status,
        messages=list(messages), tool_trail=list(tool_trail),
    )


async def test_keep_session_hydrates_full_result_live(store, redis_queue):
    sid = f"s2keep-{uuid.uuid4().hex[:10]}"
    # A full result in the real session_results collection (D46).
    ref = await store.write_full_result(
        sid, uuid.uuid4().hex, {"columns": ["dept", "ot"], "row_count": 2,
                                "rows": [["sales", 12000], ["mktg", 3000]]},
    )
    doc = _seed_doc(
        sid,
        messages=[TurnMessage(0, "user", "overtime by dept?", _TS, frozenset()),
                  TurnMessage(0, "assistant", "Sales paid $12k.", _TS, frozenset())],
        tool_trail=[TrailEntry(
            turn_index=0, tool_call_id="call_ok", tool_name="runQuery",
            args={"sql": "SELECT dept, sum(ot) FROM hr.pay GROUP BY dept"}, status="ok",
            error_code=None, provenance=frozenset({("hr.pay", "ot")}),
            result_preview=None, result_full_ref=ref, ts=_TS,
        )],
    )
    await store._upsert_doc(sid, doc)
    store._created.append(sid)
    await redis_queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))

    captured: list = []

    async def capturing_loader(d, s, *, job):
        summary = await load_session_summary(d, s, job=job)
        captured.append(summary)
        return summary

    audit = InMemoryAuditStore()
    consumer = LearningConsumer(store, redis_queue, LearningSettings(_env_file=None),
                               summary_loader=capturing_loader, audit=audit)

    result = await consumer.run_once()

    assert result.done == 1
    # C6: the D46 full result was hydrated from the real session_results collection.
    assert len(captured) == 1
    tc = captured[0].tool_calls[0]
    assert tc.full_result_loaded is True
    assert tc.result_columns == ("dept", "ot")
    assert tc.result_row_count == 2
    assert captured[0].accepted_signal == "no_correction"  # accepted successful query → K1
    # C1/C2: the session reached `done` in real Couchbase.
    doc2, _ = await store.get_session_with_cas(sid)
    assert doc2.learning_status == LearningStatus.DONE
    assert doc2.learning_content_hash == compute_content_hash(doc2)
    # row 6 / A5: S2 writes NO evidence, even live.
    assert audit.snapshot_calls == 0


async def test_read_full_result_live_hit_and_miss(store):
    """`read_full_result` (D46, read-only) against real Couchbase: returns the
    stored full result for a live ref, and None for a missing/purged ref (never
    a crash)."""
    sid = f"s2rfr-{uuid.uuid4().hex[:10]}"
    payload = {"columns": ["a", "b"], "row_count": 1, "rows": [[1, 2]]}
    ref = await store.write_full_result(sid, uuid.uuid4().hex, payload)

    got = await store.read_full_result(sid, ref)
    assert got == payload

    missing = await store.read_full_result(sid, f"result::{uuid.uuid4()}")
    assert missing is None


async def test_skip_session_ends_done_live_and_d72(store, redis_queue):
    sid = f"s2skip-{uuid.uuid4().hex[:10]}"
    doc = _seed_doc(
        sid,
        messages=[TurnMessage(0, "user", "hello", _TS, frozenset()),
                  TurnMessage(0, "assistant", "hi there!", _TS, frozenset())],
        tool_trail=[],  # chat-only → triage skip_no_tool_calls
    )
    await store._upsert_doc(sid, doc)
    store._created.append(sid)
    before, _ = await store.get_session_with_cas(sid)
    before_wire = before.to_doc()
    await redis_queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))

    audit = InMemoryAuditStore()
    consumer = LearningConsumer(store, redis_queue, LearningSettings(_env_file=None), audit=audit)
    result = await consumer.run_once()

    assert result.done == 1
    after, _ = await store.get_session_with_cas(sid)
    after_wire = after.to_doc()
    assert after.learning_status == LearningStatus.DONE
    # C5 / D72: ONLY the lifecycle flag (+hash) changed — messages/trail/last_activity untouched.
    assert after_wire["messages"] == before_wire["messages"]
    assert after_wire["tool_trail"] == before_wire["tool_trail"]
    assert after_wire["last_activity"] == before_wire["last_activity"]
    assert audit.snapshot_calls == 0
