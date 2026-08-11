"""Layer-2 integration — the FULL factory-built learning consumer driving the
write-router pipeline (D102 §7.1) end-to-end against REAL infra (Wave 3b-(ii)).

The crown-jewel proof: `build_learning_consumer` assembles the six-stage pipeline
(generalize → leakage → dedup → schema_edit_pr → user_commit → writer) over a
SCRIPTED extractor model double + REAL Couchbase (session + candidate + audit +
corpus + user_knowledge stores) + REAL Redis (the jobs stream) + (when reachable)
the REAL l2-embedding service. A KEEP session enqueued on the real stream, driven
by one `run_once`, must land the right terminal state readable back from real
infra:

  * a clean blueprint flows extract → generalize → leakage(pass) → dedup(INSERT,
    corpus SEEDED at hit_count=1 in real Couchbase) → writer(auto-land `candidate`),
    readable back from the real `learning_candidates` store carrying its S4
    `generalization`, a SETTLED pass `entity_scan`, and its S6 `dedup`;
  * the corpus artifact is in real `learning_corpus` at hit_count=1; a SECOND
    identical session (a different session id ⇒ a different content hash, same
    canonical key) INCREMENTS it to 2 — cross-session D48 accrual, on real infra;
  * a `global_knowledge` candidate lands `in_review` (the human pre-gate);
  * a reroute lands a per-user fact in the real `user_knowledge` store, scoped to
    the session's AUTHENTICATED user (R6/D17), and the residual global flows on.

Provision first (idempotent):
    docker compose -f docker-compose.integration.yml up -d --wait couchbase l2-redis
    ./scripts/couchbase-init.sh && ./scripts/learning-audit-init.sh \
        && ./scripts/learning-candidates-init.sh && ./scripts/learning-corpus-init.sh \
        && ./scripts/learning-user-init.sh
Run (its OWN pytest process — the Couchbase C-ext segfaults when many Clusters
share one interpreter):
    RUN_COUCHBASE_TESTS=1 \
    COUCHBASE_CONNECTION_STRING=couchbase://localhost \
    COUCHBASE_USERNAME=admin COUCHBASE_PASSWORD=password \
    LEARNING_CANDIDATES_USERNAME=learning_candidates_writer \
    LEARNING_CANDIDATES_PASSWORD=candidates-writer-pass \
    LEARNING_AUDIT_USERNAME=learning_audit_writer LEARNING_AUDIT_PASSWORD=audit-writer-pass \
    LEARNING_CORPUS_USERNAME=learning_corpus_writer LEARNING_CORPUS_PASSWORD=corpus-writer-pass \
    USER_KNOWLEDGE_USERNAME=user_knowledge_writer USER_KNOWLEDGE_PASSWORD=user-writer-pass \
    LEARNING_REDIS_TEST_URL=redis://localhost:6379/0 \
        uv run pytest tests/integration/test_learning_pipeline_live.py -v

`EMBEDDING_TEST_URL` (e.g. http://localhost:18003/embed) is OPTIONAL: set it to wire
the real l2-embedding service into the S6 soft near-miss layer. It is deliberately
LEFT UNSET by default here — the durable, deterministically-keyed corpus makes the
insert/increment accrual assertions a HARD-KEY proof, and the insert-only default
(the factory's `_InsertOnlyEmbedder`) fail-softs the soft layer to `insert` (D52),
which keeps those assertions hermetic even against a leaked artifact from an
interrupted prior run. The real embedder path is proven live by
`test_embedding_api.py`.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import pytest

from data_agent.learning.candidate.couchbase_candidate_store import COUCHBASE_AVAILABLE
from data_agent.learning.candidate.models import mint_candidate_id
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.config import LearningSettings
from data_agent.learning.extractor.schema import EXTRACTOR_TOOL_NAME
from data_agent.learning.factory import build_learning_consumer
from data_agent.learning.leakage.scanner import SemanticScanRequest, SemanticScanResult
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.learning.triage import TriageVerdict
from data_agent.learning.user.models import mint_record_id
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.session.models import SessionDoc, TrailEntry, TurnMessage

_REDIS_URL = os.environ.get("LEARNING_REDIS_TEST_URL")
_EMBEDDING_URL = os.environ.get("EMBEDDING_TEST_URL")

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE
    or not os.environ.get("RUN_COUCHBASE_TESTS")
    or not _REDIS_URL,
    reason="Requires a live Couchbase (RUN_COUCHBASE_TESTS) with the audit / "
    "candidates / corpus / user_knowledge buckets provisioned AND a live Redis "
    "(LEARNING_REDIS_TEST_URL).",
)

_TS = "2000-01-01T00:00:00+00:00"

KEEP_VERDICT = TriageVerdict(decision="keep", reason="K1", target_hints=("blueprint",))

# The corpus is DURABLE (no TTL) and its `canonical_key` is DETERMINISTIC off the
# blueprint's resolved table/columns/AST. To keep re-runs HERMETIC — so a leftover
# artifact from an interrupted prior run can never turn this run's genuinely-new
# `insert` into a hard-key `increment` — each test parameterizes the payroll table
# with a per-run tag, minting a unique canonical key. (The two-session accrual test
# deliberately reuses ONE tag so the hard key collides and the count accrues.)


def _table(tag: str) -> str:
    return f"payroll.pf_{tag}"


def _payroll_sql(table: str) -> str:
    """The design §3.1 payroll worked example — the accepted SQL S4 reads off the
    trail and rewrites into the generalized blueprint template."""
    return (
        f"SELECT sum(gross_pay) AS total_earnings FROM {table} "
        "WHERE department = '0420' AND toYear(pay_period) = 2025 "
        "AND record_type = 'EARNING' AND region = 'NA'"
    )


def _catalog(table: str) -> dict[str, dict[str, str]]:
    """The D69 catalog the provenance extractor qualifies against (payroll slice)."""
    return {
        table: {
            "gross_pay": "Float64",
            "department": "String",
            "pay_period": "Date",
            "record_type": "String",
            "region": "String",
        }
    }


# --- scripted collaborators (deterministic, no real LLM) ---------------------


def _evidence_item(quote: str = "total earnings for Analytics in 2025") -> dict:
    return {"turn_ref": 0, "tool_call_ref": "tc1", "quote": quote}


def _blueprint_raw(table: str) -> dict:
    return {
        "type": "blueprint",
        "confidence": 0.9,
        "evidence": [_evidence_item()],
        "rationale": "reusable department-earnings report",
        "proposed_action": "new",
        "entity_self_check": {"contains_entities": False, "found": []},
        "depends_on": [],
        "payload": {
            "intent": "total earnings for a department in a given year",
            "kind": "single",
            "resolves": {"earnings": f"{table}.gross_pay"},
            "source_tool_call_refs": ["tc1"],
            "accepted_signal": "no_correction",
            "parameterization": [
                {"locator": {"table": table, "column": "department", "value": "0420"},
                 "role": "slot",
                 "slot": {"name": "department", "type": "entity",
                          "binds_to": f"{table}.department",
                          "required": True, "optional_pattern": None}},
                {"locator": {"table": table, "column": "pay_period", "value": "2025"},
                 "role": "slot",
                 "slot": {"name": "year", "type": "period",
                          "binds_to": f"{table}.pay_period",
                          "required": True, "optional_pattern": None}},
                {"locator": {"table": table, "column": "record_type", "value": "EARNING"},
                 "role": "inline", "why": "defines the metric 'earnings'"},
                {"locator": {"table": table, "column": "region", "value": "NA"},
                 "role": "slot",
                 "slot": {"name": "region", "type": "entity",
                          "binds_to": f"{table}.region",
                          "required": False, "optional_pattern": "TRUE"}},
            ],
            "result_signature": None,
            "notes": "",
        },
    }


def _global_knowledge_raw(*, statement: str) -> dict:
    return {
        "type": "global_knowledge",
        "confidence": 0.9,
        "evidence": [_evidence_item()],
        "rationale": "a reusable business rule worth learning globally",
        "proposed_action": "new",
        "entity_self_check": {"contains_entities": False, "found": []},
        "payload": {"statement": statement, "knowledge_type": "business_rule"},
    }


def _scripted_turn(candidates: list[dict]) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id="call_1", name=EXTRACTOR_TOOL_NAME,
                                    arguments={"candidates": candidates})]
    )


def _model(candidates: list[dict]) -> ScriptedModelClient:
    return ScriptedModelClient([_scripted_turn(candidates)])


@dataclass
class _ScriptedSemanticScanner:
    """A deterministic `SemanticEntityScanner` double — a fixed classification.
    NO real LLM (the Layer-3 double, here against live stores)."""

    classification: str = "clean"

    async def scan(self, request: SemanticScanRequest) -> SemanticScanResult:
        return SemanticScanResult(classification=self.classification, hits=())


# --- infra fixture: the real stores + queue, all cleaned up ------------------


def _settings() -> LearningSettings:
    return LearningSettings(_env_file=None)


def _seed_doc(sid: str, table: str) -> SessionDoc:
    """A KEEP payroll session: one accepted runQuery(tc1) carrying the §3.1 SQL that
    S4 rewrites into the generalized blueprint template."""
    return SessionDoc(
        session_id=sid, created_at=_TS, last_activity=_TS,
        learning_status=LearningStatus.QUEUED,
        messages=[
            TurnMessage(0, "user", "total earnings for dept 0420 in 2025?", _TS, frozenset()),
            TurnMessage(0, "assistant", "Department 0420 earned that total.", _TS, frozenset()),
        ],
        tool_trail=[TrailEntry(
            turn_index=0, tool_call_id="tc1", tool_name="runQuery",
            args={"sql": _payroll_sql(table)}, status="ok", error_code=None,
            provenance=frozenset(), result_preview=None, result_full_ref=None, ts=_TS,
        )],
    )


@pytest.fixture
async def infra():
    """Build every REAL learning store (session/candidate/audit/corpus/user) + the
    real Redis queue, tracking created keys for teardown. Each store owns its own
    Couchbase `Cluster`; all are CLOSED in teardown so this file never accumulates
    many open clusters in one interpreter (the C-ext segfault guard)."""
    import redis.asyncio as aioredis

    from data_agent.learning.audit.couchbase_audit_store import CouchbaseAuditStore
    from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
    from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus
    from data_agent.learning.redis_queue import RedisStreamsLearningQueue
    from data_agent.learning.user.config import UserKnowledgeStoreConfig
    from data_agent.learning.user.couchbase_user_store import CouchbaseUserKnowledgeStore
    from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

    settings = _settings()
    session_store = CouchbaseSessionStore(RuntimeSettings(_env_file=None))
    candidate_store = CouchbaseCandidateStore(settings)
    audit_store = CouchbaseAuditStore(settings)
    corpus_store = CouchbaseBlueprintCorpus(settings)
    user_store = CouchbaseUserKnowledgeStore(UserKnowledgeStoreConfig(_env_file=None))
    for st in (session_store, candidate_store, audit_store, corpus_store, user_store):
        await st.connect()

    # Optional: wire the real l2-embedding service into the S6 soft layer when
    # EMBEDDING_TEST_URL is set. Unset ⇒ the factory's insert-only default (hard-key
    # dedup only), which keeps the durable-corpus accrual assertions hermetic (D52).
    embedder = None
    if _EMBEDDING_URL:
        from data_agent.runtime.model.embedding_client import HttpEmbeddingClient

        embedder = HttpEmbeddingClient(
            url=_EMBEDDING_URL, api_key=os.environ.get("EMBEDDING_TEST_API_KEY", ""),
            model="all-mpnet-base-v2", timeout_seconds=30.0,
        )

    tag = uuid.uuid4().hex[:12]
    redis_client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    stream = f"test:pipeline:jobs:{tag}"
    dead = f"test:pipeline:jobs:dead:{tag}"
    queue = RedisStreamsLearningQueue(
        redis_client, stream=stream, group="learning-workers",
        consumer_name=f"worker-pl-{tag}", dead_letter_stream=dead,
    )
    await queue.ensure_group()

    created_sessions: list[str] = []
    created_candidates: list[str] = []
    created_corpus: list[str] = []
    created_user: list[str] = []
    created_audit: list[str] = []

    ns = _Infra(
        settings=settings, session_store=session_store, candidate_store=candidate_store,
        audit_store=audit_store, corpus_store=corpus_store, user_store=user_store,
        embedder=embedder, queue=queue,
        created_sessions=created_sessions, created_candidates=created_candidates,
        created_corpus=created_corpus, created_user=created_user, created_audit=created_audit,
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

        for sid in created_sessions:
            await _rm(session_store._sessions, f"session::{sid}")
        for cid in created_candidates:
            await _rm(candidate_store._collection, cid)
        for ckey in created_corpus:
            await _rm(corpus_store._collection, _corpus_doc_id(ckey))
        for rid in created_user:
            await _rm(user_store._collection, rid)
        for ref in created_audit:
            await _rm(audit_store._collection, ref)
        for st in (session_store, candidate_store, audit_store, corpus_store, user_store):
            await st.close()
        await redis_client.delete(stream, dead)
        async for key in redis_client.scan_iter(match=f"{stream}:enqueued:*"):
            await redis_client.delete(key)
        await redis_client.aclose()


@dataclass
class _Infra:
    settings: LearningSettings
    session_store: object
    candidate_store: object
    audit_store: object
    corpus_store: object
    user_store: object
    embedder: object | None
    queue: object
    created_sessions: list[str]
    created_candidates: list[str]
    created_corpus: list[str]
    created_user: list[str]
    created_audit: list[str]

    def consumer(self, model_client, *, catalog, semantic_scanner=None):
        return build_learning_consumer(
            self.settings,
            session_store=self.session_store,
            queue=self.queue,
            model_client=model_client,
            audit_store=self.audit_store,
            candidate_store=self.candidate_store,
            blueprint_corpus=self.corpus_store,
            user_store=self.user_store,
            catalog_schema=catalog,
            embedder=self.embedder,
            semantic_scanner=semantic_scanner,
            # Never sample so a clean blueprint auto-lands deterministically.
            sampler=lambda _env: False,
            # Force KEEP so triage is deterministic; the real loader still hydrates
            # the real session doc (user_id/scope/SQL) off live Couchbase.
            triage=lambda _s: KEEP_VERDICT,
        )

    async def enqueue(self, doc: SessionDoc, *, user_id: str | None = None) -> str:
        ch = compute_content_hash(doc)
        await self.session_store._upsert_doc(doc.session_id, doc)
        self.created_sessions.append(doc.session_id)
        await self.queue.enqueue(LearningJob.from_doc(doc, content_hash=ch, user_id=user_id))
        return ch


# --- clean blueprint end-to-end + cross-session corpus accrual ---------------


async def test_clean_blueprint_lands_enriched_and_corpus_accrues_live(infra):
    tag = uuid.uuid4().hex[:10]
    table = _table(tag)
    catalog = _catalog(table)
    sid = f"pl-clean-{tag}"
    doc = _seed_doc(sid, table)
    ch = await infra.enqueue(doc, user_id="user-1")
    cid = mint_candidate_id(ch, 0)
    infra.created_candidates.append(cid)

    result = await infra.consumer(
        _model([_blueprint_raw(table)]), catalog=catalog
    ).run_once()
    assert result.done == 1

    # Readable back from the REAL learning_candidates store, enriched by the pipeline.
    stored = await infra.candidate_store.get(cid)
    assert stored is not None
    assert stored.status == "candidate"                    # writer auto-landed it
    assert stored.type == "blueprint"
    # S4 generalization merged + statically validated ok (else it'd route to review).
    gen = stored.payload["generalization"]
    assert gen["static_validation"]["outcome"] == "ok"
    # S5 settled the preliminary pending self-check into an authoritative pass.
    assert LeakageVerdict.is_settled(stored.entity_scan)
    assert LeakageVerdict.from_doc(stored.entity_scan).result == "pass"
    # S6 adjudicated a genuinely-new insert.
    assert stored.dedup is not None
    assert stored.dedup.action == "insert"
    ckey = stored.dedup.canonical_key
    assert ckey
    infra.created_corpus.append(ckey)

    # The corpus artifact is in real learning_corpus, SEEDED at hit_count=1.
    art = await infra.corpus_store.get_by_canonical_key(ckey)
    assert art is not None
    assert art.hit_count == 1
    # A clean blueprint never reroutes a user fact.
    assert await infra.user_store.list_for_user("user-1", limit=10) == []

    # --- SECOND identical session (different id ⇒ different content hash, SAME
    # canonical key): the hard-key hit INCREMENTS the durable corpus count to 2 —
    # cross-session D48 accrual, on real infra. Its own candidate is dropped after
    # the extracted put; track it for cleanup.
    sid2 = f"pl-clean2-{tag}"
    doc2 = _seed_doc(sid2, table)                          # SAME table ⇒ SAME canonical key
    ch2 = await infra.enqueue(doc2, user_id="user-1")
    assert ch2 != ch                                       # session id ⇒ distinct hash
    infra.created_candidates.append(mint_candidate_id(ch2, 0))

    result2 = await infra.consumer(
        _model([_blueprint_raw(table)]), catalog=catalog
    ).run_once()
    assert result2.done == 1

    art2 = await infra.corpus_store.get_by_canonical_key(ckey)
    assert art2 is not None
    assert art2.hit_count == 2                              # 1 seeded + 1 cross-session hit


# --- global_knowledge → in_review (human pre-gate) ---------------------------


async def test_global_knowledge_lands_in_review_live(infra):
    tag = uuid.uuid4().hex[:10]
    table = _table(tag)
    sid = f"pl-gk-{tag}"
    doc = _seed_doc(sid, table)
    ch = await infra.enqueue(doc, user_id="user-1")
    cid = mint_candidate_id(ch, 0)
    infra.created_candidates.append(cid)

    result = await infra.consumer(
        _model([_global_knowledge_raw(statement="The fiscal year starts in April.")]),
        catalog=_catalog(table),
    ).run_once()
    assert result.done == 1

    stored = await infra.candidate_store.get(cid)
    assert stored is not None
    assert stored.type == "global_knowledge"
    assert stored.status == "in_review"                    # human pre-gate (D58a)
    # The gate settled a clean pass on the entity-free statement.
    assert LeakageVerdict.is_settled(stored.entity_scan)
    assert LeakageVerdict.from_doc(stored.entity_scan).result == "pass"


# --- reroute → per-user fact in the real user_knowledge store ----------------


async def test_reroute_commits_user_fact_into_real_user_store_live(infra):
    tag = uuid.uuid4().hex[:10]
    table = _table(tag)
    sid = f"pl-rr-{tag}"
    doc = _seed_doc(sid, table)
    ch = await infra.enqueue(doc, user_id="user-42")
    cid = mint_candidate_id(ch, 0)
    infra.created_candidates.append(cid)
    # The reroute path mints a deterministic per-user record id off the candidate id.
    rerouted_record_id = mint_record_id("user-42", f"{cid}::rerouted-userk")
    infra.created_user.append(rerouted_record_id)

    # The semantic scanner classifies the detected entity as a legitimate per-user fact.
    result = await infra.consumer(
        _model([_blueprint_raw(table)]), catalog=_catalog(table),
        semantic_scanner=_ScriptedSemanticScanner(classification="user_fact"),
    ).run_once()
    assert result.done == 1

    # The per-user fact landed in the REAL user_knowledge store, SCOPED to the
    # session's AUTHENTICATED user (never a payload-supplied id) — R6/D17. Read it
    # back by its deterministic id (KV, immediately consistent — no N1QL lag).
    record = await infra.user_store.get(rerouted_record_id)
    assert record is not None
    assert record.user_id == "user-42"
    assert record.record_id.startswith("userknow::user-42::")

    # The residual global candidate flowed on and the writer routed the near-miss.
    stored = await infra.candidate_store.get(cid)
    assert stored is not None
    assert stored.status == "in_review"
    assert LeakageVerdict.from_doc(stored.entity_scan).result == "reroute"
    # The residual blueprint passed through S6 and seeded a corpus artifact — clean it.
    if stored.dedup is not None and stored.dedup.canonical_key:
        infra.created_corpus.append(stored.dedup.canonical_key)
