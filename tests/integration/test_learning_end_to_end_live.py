"""Layer-2 integration — the FULL OPERATIONAL Track-B learning loop, ENABLED, driven
end-to-end against REAL infra: session-close → SWEEP → consume → PROMOTE → LAND →
RECALL. This is the one chain the component-scoped live tests deliberately DON'T
chain — `test_learning_pipeline_live.py` bypasses the sweeper (it enqueues directly)
and stubs promotion; `test_learning_corpus_landing_live.py` lands a hand-built
fixture, never a candidate the real consumer produced. Here every seam is REAL and
only the "brain" (the extractor model) is scripted:

  1. QUESTION ASKED & ANSWERED (genuine): a CLOSED session in real Couchbase whose
     tool trail carries an ACCEPTED `runQuery` whose SQL we ALSO run live through the
     MCP → ClickHouse (a scoped, session-bound JWT) so the accepted SQL is real.
  2. SWEEP (real `LearningSweeper` over real Couchbase + real Redis): claims the idle
     session and XADDs the job to the real jobs stream (the operational close→enqueue
     the pipeline-live test bypasses).
  3. CONSUME (real `build_learning_consumer` write plane + a SCRIPTED extractor that
     lifts the accepted SQL into a blueprint candidate): the candidate lands enriched
     (S4 generalization + a settled S5 pass + an S6 insert) and the durable corpus
     artifact is seeded at hit_count=1.
  4. PROMOTE + LAND (real `PromotionScheduler` with the REAL `MCPWarehouseProbe`
     replay-gating against live ClickHouse, the REAL `CandidateStoreDependencyResolver`,
     and the REAL `CorpusLandingWriter` over real neo4j + real l2-embedding): once
     hit_count reaches the threshold T, the candidate reaches `validated` AND a
     `:Blueprint` node is landed in real neo4j (created_by='learning', the
     deterministic `bp::` id).
  5. RECALL (real `Neo4jVectorIndex.recall` with the embedding of a RELATED question):
     the just-learned blueprint is surfaced (byte-exact `uses`) — it became recallable.
     BONUS: a DEMOTE via the scheduler then EXCLUDES it from recall (the forget path).

The blueprint is built on the REAL `dbpcm_warehouse.employee` table (5 rows, grain
EmployeeCode) that the existing MCP live tests use, so the golden-replay SQL actually
executes against live ClickHouse under a token scoped to EXACTLY the blueprint's `uses`.

Provision first (idempotent — buckets already exist; the neo4j corpus schema is applied
in the fixture): the l2 stack must be UP. Run in its OWN pytest process with the FULL
env incl. `LEARNING_ENABLED=1` (the sweeper + scheduler read the kill-switch fresh per
cycle):

    RUN_COUCHBASE_TESTS=1 LEARNING_ENABLED=1 \
    COUCHBASE_CONNECTION_STRING=couchbase://localhost \
    COUCHBASE_USERNAME=admin COUCHBASE_PASSWORD=password \
    LEARNING_CANDIDATES_USERNAME=learning_candidates_writer \
    LEARNING_CANDIDATES_PASSWORD=candidates-writer-pass \
    LEARNING_AUDIT_USERNAME=learning_audit_writer LEARNING_AUDIT_PASSWORD=audit-writer-pass \
    LEARNING_CORPUS_USERNAME=learning_corpus_writer LEARNING_CORPUS_PASSWORD=corpus-writer-pass \
    USER_KNOWLEDGE_USERNAME=user_knowledge_writer USER_KNOWLEDGE_PASSWORD=user-writer-pass \
    LEARNING_REDIS_TEST_URL=redis://localhost:6379/0 \
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
    MCP_TEST_URL=http://localhost:18090/mcp \
        uv run pytest tests/integration/test_learning_end_to_end_live.py -v -s

Skip-guarded on the full infra env so `uv run pytest` with no live stack stays green.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, replace

import httpx
import pytest
from neo4j import AsyncGraphDatabase

from data_agent.learning.candidate.couchbase_candidate_store import COUCHBASE_AVAILABLE
from data_agent.learning.candidate.models import mint_candidate_id
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.config import LearningSettings, learning_enabled
from data_agent.learning.extractor.schema import EXTRACTOR_TOOL_NAME
from data_agent.learning.factory import build_learning_consumer, build_promotion_write_plane
from data_agent.learning.models import (
    SWEEPABLE_STATUSES,
    LearningStatus,
    compute_content_hash,
)
from data_agent.learning.promotion.landing import landing_id
from data_agent.learning.promotion.models import policy_from_settings
from data_agent.learning.promotion.token_minter import HttpTokenMinter
from data_agent.learning.sweeper import LearningSweeper
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.retrieval.corpus_loader import apply_schema
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex
from data_agent.runtime.session.models import SessionDoc, TrailEntry, TurnMessage

from .conftest import TOKEN_ISSUER_API_KEY, TOKEN_SERVICE_URL

_REDIS_URL = os.environ.get("LEARNING_REDIS_TEST_URL")
_NEO4J_URI = os.environ.get("NEO4J_TEST_URI")
_EMBEDDING_URL = os.environ.get("EMBEDDING_TEST_URL")
_MCP_URL = os.environ.get("MCP_TEST_URL")

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE
    or not os.environ.get("RUN_COUCHBASE_TESTS")
    or not _REDIS_URL
    or not _NEO4J_URI
    or not _EMBEDDING_URL
    or not _MCP_URL,
    reason="Requires the FULL live l2 stack: Couchbase (RUN_COUCHBASE_TESTS) with the "
    "audit/candidates/corpus/user buckets, Redis (LEARNING_REDIS_TEST_URL), neo4j "
    "(NEO4J_TEST_URI), the embedding API (EMBEDDING_TEST_URL), and the MCP + token IdP "
    "(MCP_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_OLD_TS = "2000-01-01T00:00:00+00:00"

# The REAL warehouse table the existing MCP live tests use (5 rows, grain EmployeeCode).
_TABLE = "dbpcm_warehouse.employee"
_SALARY_COL = f"{_TABLE}.AnnualSalary"
_DEPT_COL = f"{_TABLE}.Department"
# The blueprint's declared footprint (D87) — the token the replay mints is scoped to
# EXACTLY these; the generalized template references only these two columns.
_EXPECTED_USES = frozenset({_SALARY_COL, _DEPT_COL})

# The genuine analytical question + its accepted SQL (a real answer: Sales earns
# 75000 + 72000 = 147000 over the seeded rows).
_QUESTION = "what is the total annual salary for the Sales department?"
_ACCEPTED_SQL = (
    f"SELECT sum(AnnualSalary) AS total_salary FROM {_TABLE} WHERE Department = 'Sales'"
)
_INTENT = "total annual salary for a department"
# A RELATED (not identical) question for the recall proof — genuine semantic recall.
_RELATED_QUESTION = "how much total salary does a department pay its employees"

# The D69 catalog the S4 provenance extractor qualifies the generalized template
# against (the real employee columns; types are immaterial to provenance).
_CATALOG = {
    _TABLE: {
        "ClientCode": "String",
        "EmployeeCode": "String",
        "Department": "String",
        "EmployeeName": "String",
        "EmployeeStatus": "String",
        "AnnualSalary": "Decimal(18, 6)",
    }
}


# --- the scripted extractor: lifts the accepted SQL into a blueprint candidate -----


def _blueprint_raw() -> dict:
    """A deterministic single-blueprint plan: one entity slot on Department. S4 rewrites
    the accepted SQL into `... WHERE Department = {department}` and derives `uses` off
    the two referenced columns. No result_signature ⇒ the replay gate verifies the
    template still EXECUTES against live ClickHouse (structure), no value oracle (D98)."""
    return {
        "type": "blueprint",
        "confidence": 0.9,
        "evidence": [{"turn_ref": 0, "tool_call_ref": "tc1", "quote": "total salary for Sales"}],
        "rationale": "reusable department salary-total report",
        "proposed_action": "new",
        "entity_self_check": {"contains_entities": False, "found": []},
        "depends_on": [],
        "payload": {
            "intent": _INTENT,
            "kind": "single",
            "resolves": {"total_salary": _SALARY_COL},
            "source_tool_call_refs": ["tc1"],
            "accepted_signal": "no_correction",
            "parameterization": [
                {
                    "locator": {"table": _TABLE, "column": "Department", "value": "Sales"},
                    "role": "slot",
                    "slot": {
                        "name": "department",
                        "type": "entity",
                        "binds_to": _DEPT_COL,
                        "required": True,
                        "optional_pattern": None,
                    },
                },
            ],
            "result_signature": None,
            "notes": "",
        },
    }


def _scripted_model() -> ScriptedModelClient:
    return ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name=EXTRACTOR_TOOL_NAME,
                        arguments={"candidates": [_blueprint_raw()]},
                    )
                ]
            )
        ]
    )


def _closed_session(sid: str) -> SessionDoc:
    """A CLOSED (idle, learning_status=active) session whose ONLY tool call is the
    genuinely-accepted `runQuery` carrying the real SQL. `last_activity` is far in the
    past so the sweeper's idle cutoff claims it."""
    return SessionDoc(
        session_id=sid,
        created_at=_OLD_TS,
        last_activity=_OLD_TS,
        learning_status=LearningStatus.ACTIVE,
        messages=[
            TurnMessage(0, "user", _QUESTION, _OLD_TS, frozenset()),
            TurnMessage(0, "assistant", "Sales earned 147000 in total.", _OLD_TS, frozenset()),
        ],
        tool_trail=[
            TrailEntry(
                turn_index=0,
                tool_call_id="tc1",
                tool_name="runQuery",
                args={"sql": _ACCEPTED_SQL},
                status="ok",
                error_code=None,
                provenance=frozenset(),
                result_preview=None,
                result_full_ref=None,
                ts=_OLD_TS,
            )
        ],
    )


# --- the real-infra fixture (every store/client, cleaned up) -----------------------


@dataclass
class _Infra:
    settings: LearningSettings
    session_store: object
    candidate_store: object
    audit_store: object
    corpus_store: object
    user_store: object
    queue: object
    embedder: object
    neo4j_driver: object
    mcp_client: object
    token_minter: object
    created_sessions: list[str]
    created_candidates: list[str]
    created_corpus: list[str]
    created_neo4j_ids: list[str]


@pytest.fixture
async def infra():
    import redis.asyncio as aioredis

    from data_agent.learning.audit.couchbase_audit_store import CouchbaseAuditStore
    from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
    from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus
    from data_agent.learning.redis_queue import RedisStreamsLearningQueue
    from data_agent.learning.user.config import UserKnowledgeStoreConfig
    from data_agent.learning.user.couchbase_user_store import CouchbaseUserKnowledgeStore
    from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

    settings = LearningSettings(_env_file=None)
    session_store = CouchbaseSessionStore(RuntimeSettings(_env_file=None))
    candidate_store = CouchbaseCandidateStore(settings)
    audit_store = CouchbaseAuditStore(settings)
    corpus_store = CouchbaseBlueprintCorpus(settings)
    user_store = CouchbaseUserKnowledgeStore(UserKnowledgeStoreConfig(_env_file=None))
    for st in (session_store, candidate_store, audit_store, corpus_store, user_store):
        await st._cluster.on_connect()

    embedder = HttpEmbeddingClient(
        url=_EMBEDDING_URL,
        api_key=os.environ.get("EMBEDDING_TEST_API_KEY", ""),
        model=_MODEL,
        timeout_seconds=30.0,
    )

    # Real neo4j driver + apply the corpus schema (idempotent — constraints + native
    # vector indexes). NOT wiped: we use a per-run canonical key so nothing collides,
    # and teardown removes exactly the node we land.
    neo4j_driver = AsyncGraphDatabase.driver(
        _NEO4J_URI,
        auth=(
            os.environ.get("NEO4J_TEST_USER", "neo4j"),
            os.environ.get("NEO4J_TEST_PASSWORD", "testpassword"),
        ),
    )
    await apply_schema(neo4j_driver, dimension=768)

    mcp_client = RealMCPClient(_MCP_URL)
    token_minter = HttpTokenMinter(TOKEN_SERVICE_URL, TOKEN_ISSUER_API_KEY)

    tag = uuid.uuid4().hex[:12]
    redis_client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    stream = f"test:e2e:jobs:{tag}"
    dead = f"test:e2e:jobs:dead:{tag}"
    queue = RedisStreamsLearningQueue(
        redis_client,
        stream=stream,
        group="learning-workers",
        consumer_name=f"worker-e2e-{tag}",
        dead_letter_stream=dead,
    )
    await queue.ensure_group()

    ns = _Infra(
        settings=settings,
        session_store=session_store,
        candidate_store=candidate_store,
        audit_store=audit_store,
        corpus_store=corpus_store,
        user_store=user_store,
        queue=queue,
        embedder=embedder,
        neo4j_driver=neo4j_driver,
        mcp_client=mcp_client,
        token_minter=token_minter,
        created_sessions=[],
        created_candidates=[],
        created_corpus=[],
        created_neo4j_ids=[],
    )
    try:
        yield ns
    finally:
        from couchbase.exceptions import DocumentNotFoundException

        from data_agent.learning.dedup.couchbase_corpus import _doc_id as _corpus_doc_id

        async def _rm(coll, key):
            try:
                await coll.remove(key)
            except DocumentNotFoundException:
                pass

        for sid in ns.created_sessions:
            await _rm(session_store._sessions, f"session::{sid}")
        for cid in ns.created_candidates:
            await _rm(candidate_store._collection, cid)
        for ckey in ns.created_corpus:
            await _rm(corpus_store._collection, _corpus_doc_id(ckey))
        for node_id in ns.created_neo4j_ids:
            async with neo4j_driver.session() as s:
                await s.run("MATCH (b:Blueprint {id: $id}) DETACH DELETE b", {"id": node_id})
        for st in (session_store, candidate_store, audit_store, corpus_store, user_store):
            await st._cluster.close()
        await neo4j_driver.close()
        await redis_client.delete(stream, dead)
        async for key in redis_client.scan_iter(match=f"{stream}:enqueued:*"):
            await redis_client.delete(key)
        await redis_client.aclose()


async def _park_foreign_idle_sessions(infra: _Infra, keep_sid: str) -> int:
    """Deterministic sweep isolation: the REAL sweeper scans ALL idle sweepable sessions,
    so any stale idle session (e.g. a prior aborted run) would also be claimed onto our
    fresh stream. Park every foreign idle sweepable session to `done` (out of the
    sweepable set) so the sweep this run claims EXACTLY our seeded session. Ephemeral l2
    test infra only — returns how many were parked (reported as integration friction)."""
    idle = await infra.session_store.scan_idle_sessions(
        statuses=SWEEPABLE_STATUSES,
        last_activity_before="2099-01-01T00:00:00+00:00",
        limit=500,
    )
    parked = 0
    for doc, _cas in idle:
        if doc.session_id == keep_sid:
            continue
        doc.learning_status = LearningStatus.DONE
        await infra.session_store._upsert_doc(doc.session_id, doc)
        parked += 1
    return parked


async def _mint_bound(scope: list[str], session_id: str) -> str:
    """Mint a session-bound JWT via the live token IdP (same shape as conftest.mint)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_SERVICE_URL,
            headers={"Authorization": f"Bearer {TOKEN_ISSUER_API_KEY}"},
            json={"user_name": "alice", "column_scope": scope, "session_id": session_id},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def test_learning_loop_learns_a_question_end_to_end_live(infra: _Infra) -> None:
    assert learning_enabled(), (
        "LEARNING_ENABLED must be truthy — the sweeper + scheduler read the kill-switch "
        "fresh per cycle and would no-op otherwise. Export LEARNING_ENABLED=1."
    )

    tag = uuid.uuid4().hex[:10]
    sid = f"e2e-{tag}"

    # ================================================================= STAGE 1
    # The question is genuinely asked & answered: run the ACCEPTED SQL live through the
    # real MCP → ClickHouse (a scoped, session-bound token) so the trail SQL is real.
    probe_session = f"e2e-probe-{tag}"
    probe_jwt = await _mint_bound([_SALARY_COL, _DEPT_COL], probe_session)
    live_result = await infra.mcp_client.call_tool(
        "runQuery", {"sql": _ACCEPTED_SQL}, jwt=probe_jwt, session_id=probe_session
    )
    assert live_result["row_count"] == 1, live_result
    assert live_result["columns"] == ["total_salary"], live_result
    print(f"[STAGE 1] accepted SQL ran live → {live_result['rows']} (columns={live_result['columns']})")

    # Seed the CLOSED session in real Couchbase (idle, active, old last_activity).
    doc = _closed_session(sid)
    content_hash = compute_content_hash(doc)
    await infra.session_store._upsert_doc(sid, doc)
    infra.created_sessions.append(sid)
    cid = mint_candidate_id(content_hash, 0)
    infra.created_candidates.append(cid)

    parked = await _park_foreign_idle_sessions(infra, sid)
    print(f"[STAGE 1] seeded CLOSED session {sid!r} (content_hash={content_hash[:12]}…); "
          f"parked {parked} foreign idle session(s) for sweep isolation")

    # ================================================================= STAGE 2 — SWEEP
    # The REAL sweeper claims via an N1QL idle-scan whose GSI is eventually consistent
    # (default NOT_BOUNDED), so a just-upserted session can be momentarily invisible to
    # the scan. Retry `run_once` (idempotent — a claimed session leaves the sweepable
    # set, XADD is content-hash idempotent) until OUR session is claimed → queued.
    sweeper = LearningSweeper(infra.session_store, infra.queue, infra.settings)
    swept_doc = None
    last_sweep = None
    for _ in range(30):
        last_sweep = await sweeper.run_once()
        assert not last_sweep.disabled, "kill-switch reported disabled — LEARNING_ENABLED not set?"
        swept_doc, _ = await infra.session_store._get_doc(sid)
        if swept_doc is not None and swept_doc.learning_status == LearningStatus.QUEUED:
            break
        await asyncio.sleep(0.5)
    assert swept_doc is not None
    assert swept_doc.learning_status == LearningStatus.QUEUED, (
        f"session was not swept→queued within the retry budget (last={last_sweep}, "
        f"status={swept_doc.learning_status}) — GSI never caught up?"
    )
    assert swept_doc.learning_content_hash == content_hash
    print(f"[STAGE 2] SWEPT (last cycle: scanned={last_sweep.scanned} "
          f"claimed={last_sweep.claimed} enqueued={last_sweep.enqueued}); "
          f"session→QUEUED, job XADDed to the real stream")

    # ================================================================= STAGE 3 — CONSUME
    consumer = build_learning_consumer(
        infra.settings,
        session_store=infra.session_store,
        queue=infra.queue,
        model_client=_scripted_model(),
        audit_store=infra.audit_store,
        candidate_store=infra.candidate_store,
        blueprint_corpus=infra.corpus_store,
        user_store=infra.user_store,
        catalog_schema=_CATALOG,
        embedder=infra.embedder,
        # Never sample so a clean blueprint auto-lands as `candidate` deterministically.
        sampler=lambda _env: False,
    )
    consumed = await consumer.run_once()
    assert consumed.done >= 1, consumed

    stored = await infra.candidate_store.get(cid)
    assert stored is not None, "consumer did not persist our candidate"
    assert stored.type == "blueprint"
    assert stored.status == "candidate"  # writer auto-landed it
    gen = stored.payload["generalization"]
    assert gen["static_validation"]["outcome"] == "ok", gen["static_validation"]
    assert set(gen["uses"]) == _EXPECTED_USES, gen["uses"]
    assert LeakageVerdict.is_settled(stored.entity_scan)
    assert LeakageVerdict.from_doc(stored.entity_scan).result == "pass"
    assert stored.dedup is not None and stored.dedup.action == "insert"
    ckey = stored.dedup.canonical_key
    assert ckey
    infra.created_corpus.append(ckey)
    node_id = landing_id(stored)
    infra.created_neo4j_ids.append(node_id)

    artifact = await infra.corpus_store.get_by_canonical_key(ckey)
    assert artifact is not None and artifact.hit_count == 1
    print(f"[STAGE 3] CONSUMED: candidate {cid} enriched (gen ok, uses={sorted(gen['uses'])}, "
          f"S5 pass, S6 insert); corpus artifact seeded hit_count=1 (canonical_key={ckey[:20]}…)")

    # ================================================================= STAGE 4 — PROMOTE
    # Drive hit_count to the threshold T via the DURABLE corpus counter (the exact
    # atomic server-side +1 the S6 cross-session accrual uses; cross-session accrual
    # itself is proven by test_learning_pipeline_live). 1 seeded + 2 = 3 = T.
    # An EXPLICIT threshold above the shipped 1: this live test drives the
    # corroboration gate itself (it seeds three sightings and asserts the count), which
    # the shipped configuration cannot exercise because every candidate clears T=1 on its
    # first sighting.
    policy = replace(policy_from_settings(infra.settings), blueprint_hit_threshold=3)
    await infra.corpus_store.increment_hit_count(ckey)
    await infra.corpus_store.increment_hit_count(ckey)
    assert await infra.corpus_store.hit_count(ckey) == policy.blueprint_hit_threshold

    scheduler, inbox = build_promotion_write_plane(
        infra.settings,
        candidate_store=infra.candidate_store,
        hit_counts=infra.corpus_store,
        mcp_client=infra.mcp_client,
        token_minter=infra.token_minter,
        neo4j_driver=infra.neo4j_driver,
        embedding_client=infra.embedder,
        model_id=_MODEL,
        policy=policy,
    )
    # PLAN §4: the cron ROUTES to `in_review`; only a human approve lands. So this stage
    # is now two steps, and the second one is the point — a live end-to-end that stopped
    # at `in_review` would prove the queue fills but never that anything reaches the
    # graph, which is what stages 4 and 5 exist for.
    #
    # The scheduler scans candidates via an N1QL `list_by_status` (same eventually-
    # consistent GSI as the sweeper), so the just-consumed candidate can be momentarily
    # invisible. Retry `run_once` until OUR candidate is routed (authoritative via the KV
    # `get` → `in_review`); capture the route DECISION on the cycle it fires.
    route_decision = None
    routed = None
    for _ in range(30):
        promo = await scheduler.run_once()
        assert not promo.disabled
        d = next((x for x in promo.decisions if x.candidate_id == cid), None)
        if d is not None and d.action == "route":
            route_decision = d
        routed = await infra.candidate_store.get(cid)
        if routed is not None and routed.status == "in_review":
            break
        await asyncio.sleep(0.5)
    assert routed is not None and routed.status == "in_review", (
        "candidate never reached the review queue within the retry budget "
        f"(status={None if routed is None else routed.status})"
    )
    if route_decision is not None:  # the cycle that routed it was observed directly
        assert route_decision.action == "route", route_decision
        assert route_decision.to_status == "in_review"

    # A human approves it — the ONE edge that lands into neo4j. This re-runs the entity
    # strip, the depends_on guard, static validation and a REAL golden replay against
    # live ClickHouse before anything is written.
    validated = await inbox.approve(cid)
    assert validated is not None and validated.status == "validated", (
        f"approve did not validate (status={None if validated is None else validated.status})"
    )

    # The :Blueprint node is really in neo4j, distinguishable as loop-landed.
    async with infra.neo4j_driver.session() as s:
        row = await (
            await s.run(
                "MATCH (b:Blueprint {id: $id}) RETURN b.created_by AS created_by, "
                "b.source_candidate_id AS src, b.status AS status",
                {"id": node_id},
            )
        ).single()
    assert row is not None, f"no :Blueprint landed at {node_id}"
    assert row["created_by"] == "learning"
    assert row["src"] == cid
    assert row["status"] == "validated"
    print(f"[STAGE 4] ROUTED → REVIEWED → LANDED: cron routed to in_review, human approve "
          f"replay-gated against live ClickHouse → validated; :Blueprint {node_id} in real "
          f"neo4j (created_by=learning, source={cid})")

    # ================================================================= STAGE 5 — RECALL
    query_vector = (await infra.embedder.embed([_RELATED_QUESTION]))[0]
    index = Neo4jVectorIndex(
        url=_NEO4J_URI,
        auth=(
            os.environ.get("NEO4J_TEST_USER", "neo4j"),
            os.environ.get("NEO4J_TEST_PASSWORD", "testpassword"),
        ),
        expected_model=_MODEL,
        timeout_seconds=15.0,
    )
    try:
        recalled = await index.recall(query_vector=query_vector, kind="blueprint", k=30)
        landed = next((c for c in recalled if c.id == node_id), None)
        assert landed is not None, (
            f"the just-learned blueprint {node_id} was not recalled for a related question"
        )
        assert landed.uses == _EXPECTED_USES, landed.uses
        print(f"[STAGE 5] RECALLED: '{_RELATED_QUESTION}' surfaced {node_id} "
              f"(byte-exact uses={sorted(landed.uses)}) — it became recallable")

        # ------------------------------------------------- BONUS — DEMOTE → forget
        demote = await scheduler.apply_user_correction(validated)
        assert demote.action == "demote", (demote.action, demote.reason)
        demoted = await infra.candidate_store.get(cid)
        assert demoted is not None and demoted.status == "candidate"

        after = await index.recall(query_vector=query_vector, kind="blueprint", k=30)
        assert not any(c.id == node_id for c in after), (
            "a demoted blueprint must no longer be recallable (the forget path)"
        )

        # PLAN §4 — and the correction STAYS applied. The next cron cycle re-runs every
        # guard, the live replay passes again (a correction is about a VALUE, D98), and
        # the hit count is still 3 — yet the blueprint does not come back, because the
        # cron's only destination is the review queue. This is the live counterpart of
        # `test_a_user_correction_is_no_longer_erased_by_the_next_cron_cycle`.
        for _ in range(30):
            await scheduler.run_once()
            after_cron = await infra.candidate_store.get(cid)
            if after_cron is not None and after_cron.status == "in_review":
                break
            await asyncio.sleep(0.5)
        assert after_cron is not None and after_cron.status in ("candidate", "in_review"), (
            f"a corrected blueprint must never auto-return to validated (status="
            f"{None if after_cron is None else after_cron.status})"
        )
        still_gone = await index.recall(query_vector=query_vector, kind="blueprint", k=30)
        assert not any(c.id == node_id for c in still_gone), (
            "a corrected blueprint must stay un-recallable across cron cycles"
        )
        print(f"[STAGE 5 BONUS] DEMOTED via scheduler → candidate → re-routed to in_review; "
              f"recall still EXCLUDES {node_id} across cron cycles (the correction sticks)")
    finally:
        await index.close()
