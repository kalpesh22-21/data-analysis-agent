#!/usr/bin/env python
"""demo_learning_e2e_openai — a REAL-OpenAI + Phoenix-traced end-to-end run of the
Track-B learning loop.

This is NOT a pytest test (the real LLM is nondeterministic). It reuses the STAGE
seeding/sweep/consume/promote/land/recall pattern of
`tests/integration/test_learning_end_to_end_live.py` but swaps in:

  1. a REAL OpenAI extractor (`build_openai_model_client` wrapped by the real
     `LearningExtractor` the factory builds) making the learning decision, and
  2. Phoenix OTel tracing CHAINED into ONE trace per session — the sweeper's
     `learning.enqueue` is the per-session ROOT; its W3C `traceparent` rides on the
     job so `learning.consume`/`triage`/`extract` nest under it, and the same
     traceparent carried on the candidate makes the scheduler's own `promote`/`land`
     spans (its REAL tracer seam, not manual wrappers) continue the SAME trace.
     Exported to the `learning-loop` Phoenix project. `LEARNING_TRACE_VERBOSE=1` is
     set here so the spans additionally carry human-readable content (question /
     accepted SQL / learned intent) — the entity-bearing diagnostic posture.

Run (from the repo root, the l2 stack + Phoenix UP):

    LEARNING_TRACE_VERBOSE=1 DEMO_MODEL=gpt-5.5 uv run python scripts/demo_learning_e2e_openai.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- env
# Load OPENAI_API_KEY from .env (strip surrounding quotes) and set the FULL live
# env block the E2E test documents, BEFORE any settings object is constructed.

_REPO = Path(__file__).resolve().parent.parent


def _load_openai_key() -> str:
    env_path = _REPO / ".env"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("OPENAI_API_KEY="):
            val = line.split("=", 1)[1].strip()
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            return val
    raise SystemExit("OPENAI_API_KEY not found in .env")


_OPENAI_KEY = _load_openai_key()

os.environ.update(
    {
        "LEARNING_ENABLED": "1",
        "RUN_COUCHBASE_TESTS": "1",
        "COUCHBASE_CONNECTION_STRING": "couchbase://localhost",
        "COUCHBASE_USERNAME": "admin",
        "COUCHBASE_PASSWORD": "password",
        "LEARNING_CANDIDATES_USERNAME": "learning_candidates_writer",
        "LEARNING_CANDIDATES_PASSWORD": "candidates-writer-pass",
        "LEARNING_AUDIT_USERNAME": "learning_audit_writer",
        "LEARNING_AUDIT_PASSWORD": "audit-writer-pass",
        "LEARNING_CORPUS_USERNAME": "learning_corpus_writer",
        "LEARNING_CORPUS_PASSWORD": "corpus-writer-pass",
        "USER_KNOWLEDGE_USERNAME": "user_knowledge_writer",
        "USER_KNOWLEDGE_PASSWORD": "user-writer-pass",
        "LEARNING_REDIS_TEST_URL": "redis://localhost:6379/0",
        "NEO4J_TEST_URI": "bolt://localhost:7687",
        "NEO4J_TEST_USER": "neo4j",
        "NEO4J_TEST_PASSWORD": "testpassword",
        "EMBEDDING_TEST_URL": "http://localhost:18003/embed",
        "MCP_TEST_URL": "http://localhost:18090/mcp",
        # (The Phoenix project name is now set IN CODE by `configure_learning_tracing`
        # via `configure_tracing(project_name="learning-loop")` — no
        # OTEL_RESOURCE_ATTRIBUTES env hack is needed to land in the `learning-loop`
        # project. Phoenix groups traces by the `openinference.project.name` resource
        # attribute, which the code now sets directly.)
        # Turn the D25 verbose gate ON for this DIAGNOSTIC run so the spans carry the
        # human-readable content (question / accepted SQL / learned intent). This makes
        # the learning-loop Phoenix project entity-bearing — a controlled demo posture.
        "LEARNING_TRACE_VERBOSE": "1",
    }
)

import httpx  # noqa: E402
import openai  # noqa: E402
from neo4j import AsyncGraphDatabase  # noqa: E402
from openinference.semconv.trace import OpenInferenceSpanKindValues  # noqa: E402

from data_agent.learning.candidate.models import mint_candidate_id  # noqa: E402
from data_agent.learning.config import LearningSettings, learning_enabled  # noqa: E402
from data_agent.learning.extractor.grounding import known_rule_ids_from_catalog  # noqa: E402
from data_agent.learning.factory import (  # noqa: E402
    build_learning_consumer,
    build_promotion_write_plane,
)
from data_agent.learning.models import (  # noqa: E402
    SWEEPABLE_STATUSES,
    LearningStatus,
    compute_content_hash,
)
from data_agent.learning.observability import (  # noqa: E402
    configure_learning_tracing,
    context_from_traceparent,
    get_learning_tracer,
    learning_recall_span,
)
from data_agent.learning.promotion.landing import landing_id  # noqa: E402
from data_agent.learning.promotion.models import PromotionPolicy  # noqa: E402
from data_agent.learning.promotion.token_minter import HttpTokenMinter  # noqa: E402
from data_agent.learning.sweeper import LearningSweeper  # noqa: E402
from data_agent.runtime.config import RuntimeSettings  # noqa: E402
from data_agent.runtime.mcp.real_client import RealMCPClient  # noqa: E402
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient  # noqa: E402
from data_agent.runtime.model.openai_client import build_openai_model_client  # noqa: E402
from data_agent.runtime.observability.tracing import span  # noqa: E402
from data_agent.runtime.retrieval.corpus_loader import apply_schema  # noqa: E402
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex  # noqa: E402
from data_agent.runtime.session.models import SessionDoc, TrailEntry, TurnMessage  # noqa: E402

# `_catalog` is a sibling module under `scripts/`. Put this script's own directory
# on `sys.path` so the import resolves BOTH when run as `python scripts/x.py` AND
# when the file is loaded by path (importlib `spec_from_file_location`, e.g. tests).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _catalog import catalog_dict  # noqa: E402

# --------------------------------------------------------------------------- consts

TOKEN_SERVICE_URL = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")
TOKEN_ISSUER_API_KEY = os.environ.get("TOKEN_ISSUER_API_KEY", "issuer-key-abc123")

_REDIS_URL = os.environ["LEARNING_REDIS_TEST_URL"]
_NEO4J_URI = os.environ["NEO4J_TEST_URI"]
_EMBEDDING_URL = os.environ["EMBEDDING_TEST_URL"]
_MCP_URL = os.environ["MCP_TEST_URL"]

_PHOENIX_OTLP = "http://localhost:6006/v1/traces"
_PHOENIX_UI = "http://localhost:6006"
_PHOENIX_GRAPHQL = "http://localhost:6006/graphql"

_MODEL = "all-mpnet-base-v2"  # the embedding model id (corpus/recall)
_OLD_TS = "2000-01-01T00:00:00+00:00"

_TABLE = "dbpcm_warehouse.employee"
_SALARY_COL = f"{_TABLE}.AnnualSalary"
_DEPT_COL = f"{_TABLE}.Department"

_QUESTION = "what is the total annual salary for the Sales department?"
_ACCEPTED_SQL = f"SELECT sum(AnnualSalary) AS total_salary FROM {_TABLE} WHERE Department = 'Sales'"
_RELATED_QUESTION = "how much total salary does a department pay its employees"

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

# OpenAI model candidates, tried in order until one answers (account may 404 some).
_MODEL_CANDIDATES = ("gpt-5.5", "gpt-4o", "gpt-4.1", "gpt-4o-mini")


def _closed_session(sid: str) -> SessionDoc:
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


class _CapturingExtractor:
    """Transparent wrapper around the real `LearningExtractor` that records the
    `ExtractionResult` the model produced so the demo can PRINT what the LLM
    actually emitted (candidates + declines)."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.last_result = None

    async def extract(self, summary, verdict):  # noqa: ANN001
        result = await self._inner.extract(summary, verdict)
        self.last_result = result
        return result


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
    redis_client: object
    stream: str
    dead: str
    created_sessions: list
    created_candidates: list
    created_corpus: list
    created_neo4j_ids: list


async def _build_infra() -> _Infra:
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

    neo4j_driver = AsyncGraphDatabase.driver(
        _NEO4J_URI,
        auth=(os.environ["NEO4J_TEST_USER"], os.environ["NEO4J_TEST_PASSWORD"]),
    )
    await apply_schema(neo4j_driver)

    mcp_client = RealMCPClient(_MCP_URL)
    token_minter = HttpTokenMinter(TOKEN_SERVICE_URL, TOKEN_ISSUER_API_KEY)

    tag = uuid.uuid4().hex[:12]
    redis_client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    stream = f"demo:e2e:jobs:{tag}"
    dead = f"demo:e2e:jobs:dead:{tag}"
    queue = RedisStreamsLearningQueue(
        redis_client,
        stream=stream,
        group="learning-workers",
        consumer_name=f"worker-demo-{tag}",
        dead_letter_stream=dead,
    )
    await queue.ensure_group()

    return _Infra(
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
        redis_client=redis_client,
        stream=stream,
        dead=dead,
        created_sessions=[],
        created_candidates=[],
        created_corpus=[],
        created_neo4j_ids=[],
    )


async def _teardown(infra: _Infra) -> None:
    from couchbase.exceptions import DocumentNotFoundException

    from data_agent.learning.dedup.couchbase_corpus import _doc_id as _corpus_doc_id

    async def _rm(coll, key):
        try:
            await coll.remove(key)
        except DocumentNotFoundException:
            pass

    for sid in infra.created_sessions:
        await _rm(infra.session_store._sessions, f"session::{sid}")
    for cid in infra.created_candidates:
        await _rm(infra.candidate_store._collection, cid)
    for ckey in infra.created_corpus:
        await _rm(infra.corpus_store._collection, _corpus_doc_id(ckey))
    for node_id in infra.created_neo4j_ids:
        async with infra.neo4j_driver.session() as s:
            await s.run("MATCH (b:Blueprint {id: $id}) DETACH DELETE b", {"id": node_id})
    for st in (
        infra.session_store,
        infra.candidate_store,
        infra.audit_store,
        infra.corpus_store,
        infra.user_store,
    ):
        await st._cluster.close()
    await infra.neo4j_driver.close()
    await infra.redis_client.delete(infra.stream, infra.dead)
    async for key in infra.redis_client.scan_iter(match=f"{infra.stream}:enqueued:*"):
        await infra.redis_client.delete(key)
    await infra.redis_client.aclose()


async def _park_foreign_idle_sessions(infra: _Infra, keep_sid: str) -> int:
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


async def _mint_bound(scope: list, session_id: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_SERVICE_URL,
            headers={"Authorization": f"Bearer {TOKEN_ISSUER_API_KEY}"},
            json={"user_name": "alice", "column_scope": scope, "session_id": session_id},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _pick_openai_model() -> str:
    """Return the first candidate model the account can actually call (Responses
    API). A 404 model / no-access error moves to the next; anything else re-raised."""
    client = openai.AsyncOpenAI(api_key=_OPENAI_KEY)
    last_err = None
    candidates = (os.environ["DEMO_MODEL"],) if os.environ.get("DEMO_MODEL") else _MODEL_CANDIDATES
    for model in candidates:
        try:
            await client.responses.create(model=model, input=[{"role": "user", "content": "ping"}])
            print(f"[MODEL] preflight OK on {model!r} (OpenAI Responses API)")
            return model
        except openai.NotFoundError as exc:
            print(f"[MODEL] {model!r} unavailable (404) — trying next: {exc}")
            last_err = exc
        except Exception as exc:  # noqa: BLE001
            print(f"[MODEL] {model!r} errored ({type(exc).__name__}): {exc} — trying next")
            last_err = exc
    raise SystemExit(f"No OpenAI model candidate worked: {last_err}")


def _print_model_emission(result) -> None:  # noqa: ANN001
    print("\n" + "=" * 70)
    print(">>> WHAT THE REAL OpenAI MODEL EMITTED (raw ExtractionResult)")
    print("=" * 70)
    if result is None:
        print("  (extractor was never invoked — triage did not reach KEEP?)")
        return
    print(f"  candidates: {len(result.candidates)}   declines: {len(result.declines)}")
    for i, cand in enumerate(result.candidates):
        h = cand.header
        print(
            f"\n  --- candidate[{i}] type={h.type} confidence={h.confidence} "
            f"proposed_action={h.proposed_action}"
        )
        print(f"      rationale: {h.rationale}")
        print(
            f"      entity_self_check: contains_entities={h.entity_self_check.contains_entities} "
            f"found={list(h.entity_self_check.found)}"
        )
        payload = cand.payload
        if hasattr(payload, "intent"):
            print(f"      intent (entity-free, embedded): {payload.intent!r}")
            print(f"      kind: {payload.kind}   resolves: {payload.resolves}")
            print(f"      accepted_signal: {payload.accepted_signal}")
            print("      parameterization (the slot plan the model chose):")
            for p in payload.parameterization:
                loc = p.locator
                slot = p.slot
                slot_desc = (
                    f"slot(name={slot.name!r}, type={slot.type}, binds_to={slot.binds_to}, "
                    f"required={slot.required})"
                    if slot is not None
                    else f"role={p.role} rule_id={p.rule_id} why={p.why}"
                )
                print(
                    f"        - locator(table={loc.table}, column={loc.column}, "
                    f"value={loc.value!r}) role={p.role} -> {slot_desc}"
                )
            if payload.result_signature is not None:
                print(f"      result_signature: {payload.result_signature}")
            if payload.notes:
                print(f"      notes: {payload.notes}")
        else:
            print(f"      payload: {payload}")
    for d in result.declines:
        print(f"\n  --- DECLINE type={d.type} reason={d.reason} detail={d.detail!r}")
    print("=" * 70 + "\n")


def _flatten(d: dict, prefix: str = "") -> dict:
    """Flatten Phoenix's NESTED attribute JSON back to dotted OTel keys, so
    `{"session": {"id": x}}` reads as `{"session.id": x}` (the form the code set)."""
    out: dict = {}
    for key, value in d.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, dotted + "."))
        else:
            out[dotted] = value
    return out


def _span_attrs(node) -> dict:  # noqa: ANN001
    """Phoenix returns span attributes as a JSON string of a NESTED object (or a
    dict on some builds). Normalize to a FLAT dotted-key dict; tolerate absence."""
    import json as _json

    raw = node.get("attributes")
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return _flatten(raw) if isinstance(raw, dict) else {}


async def _confirm_phoenix(sid: str) -> None:
    query = (
        "{ projects { edges { node { name traceCount recordCount "
        "spans(first: 1000, sort: {col: startTime, dir: desc}) { edges { node { "
        "name spanKind spanId parentId attributes context { traceId spanId } } } } "
        "} } } }"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(_PHOENIX_GRAPHQL, json={"query": query})
        resp.raise_for_status()
        data = resp.json()
    projects = data.get("data", {}).get("projects", {}).get("edges", [])
    target = next((e["node"] for e in projects if e["node"]["name"] == "learning-loop"), None)
    print("=" * 70)
    print(">>> PHOENIX TRACE CONFIRMATION")
    print("=" * 70)
    if target is None:
        print("  project 'learning-loop' NOT found. Projects present:")
        for edge in projects:
            print(f"    - {edge['node']['name']} (traces={edge['node']['traceCount']})")
        print(f"  Open the Phoenix UI to inspect: {_PHOENIX_UI}")
        return
    spans = [e["node"] for e in target["spans"]["edges"]]
    print(
        f"  project: 'learning-loop'  traceCount={target['traceCount']} "
        f"recordCount={target['recordCount']}  spans retrieved={len(spans)}"
    )

    # Group by traceId; find THE trace carrying this session's spans (session.id==sid).
    def _trace_id(s):  # noqa: ANN001
        return (s.get("context") or {}).get("traceId")

    by_trace: dict[str, list] = {}
    for s in spans:
        by_trace.setdefault(_trace_id(s), []).append(s)

    session_traces = {_trace_id(s) for s in spans if _span_attrs(s).get("session.id") == sid}
    if not session_traces:
        print(
            f"  no spans found for session.id={sid!r} yet (ingestion lag?). "
            f"Open {_PHOENIX_UI} (project: learning-loop)."
        )
        return
    print(
        f"\n  session {sid!r} spans span {len(session_traces)} traceId(s): "
        f"{'ONE trace (chained ✓)' if len(session_traces) == 1 else 'MULTIPLE (NOT chained!)'}"
    )

    for tid in session_traces:
        members = by_trace.get(tid, [])
        print(f"\n  ── traceId {tid}  ({len(members)} spans) ──")
        # Build the parent→children tree and print it depth-first from the roots.
        by_span = {s["spanId"]: s for s in members}
        children: dict[str | None, list] = {}
        for s in members:
            parent = s.get("parentId")
            parent = parent if parent in by_span else None  # cross-trace/None → root
            children.setdefault(parent, []).append(s)

        def _print(node_id, depth, kids):  # noqa: ANN001
            for s in kids.get(node_id, []):
                indent = "    " + "  " * depth
                print(f"{indent}└─ {s['name']} [{s.get('spanKind')}]")
                _print(s["spanId"], depth + 1, kids)

        _print(None, 0, children)

        # A couple of the human-readable (verbose) attribute values, if present.
        wanted = (
            "learning.question",
            "learning.accepted_sql",
            "learning.extract.intent",
            "learning.blueprint.intent",
        )
        printed_header = False
        for s in members:
            attrs = _span_attrs(s)
            hits = {k: attrs[k] for k in wanted if k in attrs}
            if not hits:
                continue
            if not printed_header:
                print("    human-readable (verbose) attrs:")
                printed_header = True
            for k, v in hits.items():
                print(f"      {s['name']}.{k} = {v!r}")

    print(f"\n  Open the Phoenix UI: {_PHOENIX_UI}  (project: learning-loop)")
    print("=" * 70)


async def _run() -> int:
    if not learning_enabled():
        raise SystemExit("LEARNING_ENABLED must be truthy")

    model = await _pick_openai_model()

    # ---------------------------------------------------------------- tracing
    provider = configure_learning_tracing(otlp_endpoint=_PHOENIX_OTLP)
    tracer = get_learning_tracer(provider)
    print(f"[TRACE] Phoenix OTLP exporter -> {_PHOENIX_OTLP} (service.name=learning-loop)")

    infra = await _build_infra()
    tag = uuid.uuid4().hex[:10]
    sid = f"demo-{tag}"

    try:
        # ============================================================ STAGE 1
        probe_session = f"demo-probe-{tag}"
        probe_jwt = await _mint_bound([_SALARY_COL, _DEPT_COL], probe_session)
        live_result = await infra.mcp_client.call_tool(
            "runQuery", {"sql": _ACCEPTED_SQL}, jwt=probe_jwt, session_id=probe_session
        )
        print(
            f"[STAGE 1] accepted SQL ran live -> {live_result['rows']} "
            f"(columns={live_result['columns']})"
        )

        doc = _closed_session(sid)
        content_hash = compute_content_hash(doc)
        await infra.session_store._upsert_doc(sid, doc)
        infra.created_sessions.append(sid)
        parked = await _park_foreign_idle_sessions(infra, sid)
        print(
            f"[STAGE 1] seeded CLOSED session {sid!r} (content_hash={content_hash[:12]}...); "
            f"parked {parked} foreign idle session(s)"
        )

        # ============================================================ STAGE 2 — SWEEP
        sweeper = LearningSweeper(infra.session_store, infra.queue, infra.settings, tracer=tracer)
        swept_doc = None
        last_sweep = None
        for _ in range(30):
            last_sweep = await sweeper.run_once()
            if last_sweep.disabled:
                raise SystemExit("sweeper reported disabled — LEARNING_ENABLED not set?")
            swept_doc, _ = await infra.session_store._get_doc(sid)
            if swept_doc is not None and swept_doc.learning_status == LearningStatus.QUEUED:
                break
            await asyncio.sleep(0.5)
        if swept_doc is None or swept_doc.learning_status != LearningStatus.QUEUED:
            raise SystemExit(f"session not swept->queued (last={last_sweep})")
        print(
            f"[STAGE 2] SWEPT (scanned={last_sweep.scanned} claimed={last_sweep.claimed} "
            f"enqueued={last_sweep.enqueued}); session->QUEUED, job XADDed [traced]"
        )

        # ============================================================ STAGE 3 — CONSUME (REAL LLM)
        consumer = build_learning_consumer(
            infra.settings,
            session_store=infra.session_store,
            queue=infra.queue,
            tracer=tracer,
            model_client=build_openai_model_client(api_key=_OPENAI_KEY, model=model, base_url=""),
            audit_store=infra.audit_store,
            candidate_store=infra.candidate_store,
            blueprint_corpus=infra.corpus_store,
            user_store=infra.user_store,
            catalog_schema=_CATALOG,
            embedder=infra.embedder,
            # Rule-role grounding from the frozen catalog-export snapshot (D75 Wave 1b;
            # `databaseSchemaDocs/` is gone). Dev demo → reads the committed fixture.
            known_rules=known_rule_ids_from_catalog(catalog_dict()),
            sampler=lambda _env: False,
        )
        # Wrap the factory-built real extractor to capture what the model emits.
        capturing = _CapturingExtractor(consumer._extractor)
        consumer._extractor = capturing

        consumed = await consumer.run_once()
        print(
            f"[STAGE 3] CONSUMED (done={consumed.done}) with the REAL {model!r} extractor [traced]"
        )

        _print_model_emission(capturing.last_result)

        # Locate the auto-landed blueprint candidate the model produced.
        result = capturing.last_result
        n = 0 if result is None else len(result.candidates)
        stored = None
        cid = None
        for ordinal in range(max(n, 1)):
            candidate_id = mint_candidate_id(content_hash, ordinal)
            infra.created_candidates.append(candidate_id)
            env = await infra.candidate_store.get(candidate_id)
            if env is None:
                continue
            if stored is None and env.type == "blueprint":
                stored = env
                cid = candidate_id

        if stored is None:
            print(
                "[STAGE 3] the real model produced NO auto-landable blueprint candidate — "
                "reporting the traced run as-is (a valid real-LLM outcome)."
            )
            await _flush_and_confirm(provider, model, sid, stages_done=3, extractor_real=True)
            return 0

        gen = stored.payload.get("generalization")
        print(f"[STAGE 3] stored blueprint {cid} status={stored.status}")
        if gen is not None:
            sv = gen.get("static_validation", {})
            print(
                f"          generalization.static_validation={sv.get('outcome')} "
                f"uses={sorted(gen.get('uses', []))}"
            )
            print(f"          static_validation detail: {sv}")
            if gen.get("template"):
                print(f"          generalized template: {gen.get('template')}")
        if stored.dedup is not None:
            print(
                f"          dedup.action={stored.dedup.action} "
                f"canonical_key={stored.dedup.canonical_key}"
            )
            # Register for teardown NOW (any dedup write must be cleaned, even if we
            # skip promotion below) so a partial run never strands a corpus artifact.
            infra.created_corpus.append(stored.dedup.canonical_key)
        infra.created_neo4j_ids.append(landing_id(stored))

        if stored.status != "candidate" or stored.dedup is None:
            print(
                f"[STAGE 3] candidate did NOT auto-land as 'candidate' (status={stored.status}) — "
                "the model's plan routed to review or failed static validation. "
                "Reporting the traced run; skipping promotion."
            )
            await _flush_and_confirm(provider, model, sid, stages_done=3, extractor_real=True)
            return 0

        ckey = stored.dedup.canonical_key
        node_id = landing_id(stored)
        artifact = await infra.corpus_store.get_by_canonical_key(ckey)
        print(
            f"[STAGE 3] corpus artifact seeded hit_count={artifact.hit_count} "
            f"(canonical_key={ckey[:20]}...)"
        )

        # ============================================================ STAGE 4 — PROMOTE + LAND
        policy = PromotionPolicy(blueprint_hit_threshold=3)
        await infra.corpus_store.increment_hit_count(ckey)
        await infra.corpus_store.increment_hit_count(ckey)

        scheduler, _inbox = build_promotion_write_plane(
            infra.settings,
            candidate_store=infra.candidate_store,
            hit_counts=infra.corpus_store,
            mcp_client=infra.mcp_client,
            token_minter=infra.token_minter,
            neo4j_driver=infra.neo4j_driver,
            embedding_client=infra.embedder,
            model_id=_MODEL,
            policy=policy,
            tracer=tracer,  # REAL scheduler tracer seam: promote/land emit their own
            # spans STARTED under the candidate's traceparent → the SAME session trace.
        )
        validated = None
        last_decision = None
        for _ in range(30):
            promo = await scheduler.run_once()
            if promo.disabled:
                raise SystemExit("scheduler disabled")
            d = next((x for x in promo.decisions if x.candidate_id == cid), None)
            if d is not None:
                last_decision = d
            validated = await infra.candidate_store.get(cid)
            if validated is not None and validated.status == "validated":
                break
            await asyncio.sleep(0.5)
        if last_decision is not None:
            print(
                f"[STAGE 4] scheduler decision for {cid}: action={last_decision.action} "
                f"to_status={getattr(last_decision, 'to_status', None)} "
                f"reason={getattr(last_decision, 'reason', None)}"
            )
        if validated is None or validated.status != "validated":
            reason = getattr(last_decision, "reason", None) if last_decision else None
            print(
                f"[STAGE 4] candidate did NOT reach 'validated' "
                f"(status={None if validated is None else validated.status}, "
                f"decision_reason={reason}). Reporting the traced run; skipping recall."
            )
            await _flush_and_confirm(provider, model, sid, stages_done=4, extractor_real=True)
            return 0

        async with infra.neo4j_driver.session() as s:
            row = await (
                await s.run(
                    "MATCH (b:Blueprint {id: $id}) RETURN b.created_by AS created_by, "
                    "b.source_candidate_id AS src, b.status AS status",
                    {"id": node_id},
                )
            ).single()
        print(
            f"[STAGE 4] PROMOTED + LANDED (replay-gated vs live ClickHouse) -> validated; "
            f":Blueprint {node_id} in neo4j (created_by={row['created_by']}, src={row['src']}) [traced]"
        )

        # ============================================================ STAGE 5 — RECALL
        query_vector = (await infra.embedder.embed([_RELATED_QUESTION]))[0]
        index = Neo4jVectorIndex(
            url=_NEO4J_URI,
            auth=(os.environ["NEO4J_TEST_USER"], os.environ["NEO4J_TEST_PASSWORD"]),
            expected_model=_MODEL,
            timeout_seconds=15.0,
        )
        # Continue the SAME session trace: the recall/demote spans start under the
        # candidate's propagated traceparent (fail-open None ⇒ a normal root span).
        session_ctx = context_from_traceparent(validated.traceparent)
        try:
            with learning_recall_span(tracer, session_id=sid, context=session_ctx):
                recalled = await index.recall(query_vector=query_vector, kind="blueprint", k=30)
            landed = next((c for c in recalled if c.id == node_id), None)
            if landed is None:
                print(
                    f"[STAGE 5] the learned blueprint {node_id} was NOT recalled for the "
                    "related question (semantic distance) — reporting as-is."
                )
            else:
                print(
                    f"[STAGE 5] RECALLED: '{_RELATED_QUESTION}' surfaced {node_id} "
                    f"(uses={sorted(landed.uses)}) — it became recallable [traced]"
                )

            # BONUS — DEMOTE -> forget
            with span(
                tracer,
                "learning.demote",
                OpenInferenceSpanKindValues.CHAIN,
                {"session.id": sid, "learning.candidate_id": cid},
                context=session_ctx,
            ):
                demote = await scheduler.apply_user_correction(validated)
            demoted = await infra.candidate_store.get(cid)
            after = await index.recall(query_vector=query_vector, kind="blueprint", k=30)
            still = any(c.id == node_id for c in after)
            print(
                f"[STAGE 5 BONUS] DEMOTED (action={demote.action}) -> status="
                f"{None if demoted is None else demoted.status}; recall now "
                f"{'STILL contains' if still else 'EXCLUDES'} {node_id} (forget path)"
            )
        finally:
            await index.close()

        await _flush_and_confirm(provider, model, sid, stages_done=5, extractor_real=True)
        return 0
    finally:
        await _teardown(infra)


async def _flush_and_confirm(provider, model, sid, *, stages_done, extractor_real) -> None:  # noqa: ANN001
    # Flush the BatchSpanProcessor so spans export before we query Phoenix / exit.
    provider.force_flush()
    print(
        f"\n[SUMMARY] OpenAI model used: {model!r} | stages completed: {stages_done}/5 | "
        f"real extractor: {extractor_real}"
    )
    # Give Phoenix a moment to ingest the flushed batch (indexing lag).
    await asyncio.sleep(5.0)
    await _confirm_phoenix(sid)


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
