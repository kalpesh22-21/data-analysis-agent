#!/usr/bin/env python
"""demo_flywheel_inbox_e2e — a REAL, human-in-the-loop end-to-end walk of the
Track-B learning FLYWHEEL against the LIVE stack (real OpenAI + l2-mcp +
Couchbase + Neo4j + ClickHouse, all already UP).

This is NOT a pytest test (the real LLM is nondeterministic) — it is a DEMO
driver, a sibling of `scripts/demo_learning_e2e_openai.py` (whose infra wiring it
reuses verbatim: `_load_openai_key`, the live-env block, `_build_infra`,
`_teardown`, `_CATALOG`, `_mint_bound`, `_park_foreign_idle_sessions`,
`_pick_openai_model`, `_print_model_emission`) and of
`scripts/run_ui_runtime_real.py` (whose `create_app` runtime wiring it drives
in-process). It walks THREE parts, each failing HONESTLY (print + return) if the
real model does not cooperate — a valid real-LLM outcome, not a hard error:

  PART A — a LIVE runtime turn: ask -> rectify intent -> a real session trail.
  PART B — learn from that live session, then HUMAN-ACCEPT via the review inbox
           (the `sampler=True` coin routes the blueprint to `in_review`, so a
           human `inbox.approve(...)` — not an auto-promotion — lands it).
  PART C — a VARIANT question (a DIFFERENT filter) AUTOPLAYS the learned
           blueprint through the SAME in-process runtime.

Run (from the repo root, the l2 stack UP):

    uv run python scripts/demo_flywheel_inbox_e2e.py
    # keep the landed blueprint + inbox state for inspection (skip teardown):
    KEEP=1 uv run python scripts/demo_flywheel_inbox_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- env
# Load OPENAI_API_KEY from .env (strip surrounding quotes) and set the FULL live
# env block the E2E test documents, BEFORE any settings object is constructed.
# (Copied EXACTLY from demo_learning_e2e_openai.py.)

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
    }
)

import httpx  # noqa: E402
import openai  # noqa: E402
from neo4j import AsyncGraphDatabase  # noqa: E402

from data_agent.learning.candidate.models import mint_candidate_id  # noqa: E402
from data_agent.learning.config import LearningSettings, learning_enabled  # noqa: E402
from data_agent.learning.extractor.grounding import known_rule_ids_from_catalog  # noqa: E402
from data_agent.learning.factory import (  # noqa: E402
    build_learning_consumer,
    build_promotion_write_plane,
)
from data_agent.learning.inbox.inbox import InboxTransitionError  # noqa: E402
from data_agent.learning.models import (  # noqa: E402
    SWEEPABLE_STATUSES,
    LearningStatus,
    compute_content_hash,
)
from data_agent.learning.promotion.landing import landing_id  # noqa: E402
from data_agent.learning.promotion.token_minter import (  # noqa: E402
    HttpTokenMinter,
    TenantClaims,
)
from data_agent.learning.sweeper import LearningSweeper  # noqa: E402
from data_agent.runtime.app import create_app  # noqa: E402
from data_agent.runtime.config import RuntimeSettings  # noqa: E402
from data_agent.runtime.mcp.real_client import RealMCPClient  # noqa: E402
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient  # noqa: E402
from data_agent.runtime.model.openai_client import build_openai_model_client  # noqa: E402
from data_agent.runtime.retrieval.corpus_loader import apply_schema  # noqa: E402
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex  # noqa: E402

# `_catalog` is a sibling module under `scripts/`. Put this script's own directory
# on `sys.path` so the import resolves BOTH when run as `python scripts/x.py` AND
# when the file is loaded by path (importlib `spec_from_file_location`, e.g. tests).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _catalog import catalog_dict, catalog_handle  # noqa: E402

# --------------------------------------------------------------------------- consts

TOKEN_SERVICE_URL = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")
TOKEN_ISSUER_API_KEY = os.environ.get("TOKEN_ISSUER_API_KEY", "issuer-key-abc123")

_TENANT_SETTINGS = RuntimeSettings(_env_file=None)

# The warehouse TENANT claims every minted token must carry. The MCP maps
# clientcode/proc_center/jti onto the paycom_* ClickHouse settings its row policies
# read; a token without them is rejected 403 MISSING_TENANT_CLAIM before any tool
# runs. Resolved through `RuntimeSettings` so this reads the SAME TENANT_* env vars
# ui/server.py and the learning scheduler read (defaults = the seeded local tenant).
_TENANT = TenantClaims(
    clientcode=_TENANT_SETTINGS.tenant_client_code,
    proc_center=_TENANT_SETTINGS.tenant_proc_center,
    jti=_TENANT_SETTINGS.tenant_jti,
)

_REDIS_URL = os.environ["LEARNING_REDIS_TEST_URL"]
_NEO4J_URI = os.environ["NEO4J_TEST_URI"]
_EMBEDDING_URL = os.environ["EMBEDDING_TEST_URL"]
_MCP_URL = os.environ["MCP_TEST_URL"]

# Runtime JWT verification against the live l2-token JWKS (mirrors
# run_ui_runtime_real.py). The MCP still enforces scope/session live (D57/D80).
_JWKS_URL = "http://localhost:19000/.well-known/jwks.json"
_TOKEN_ISSUER = "http://token:8000/"
_TOKEN_AUDIENCE = "clickhouse-api"
_PHOENIX_OTLP = os.environ.get("OTLP_ENDPOINT", "http://localhost:6006/v1/traces")

_MODEL = "all-mpnet-base-v2"  # the embedding model id (corpus stamp / recall parity key)
_OLD_TS = "2000-01-01T00:00:00+00:00"

_TABLE = "dbpcm_warehouse.employee"
_SALARY_COL = f"{_TABLE}.AnnualSalary"
_DEPT_COL = f"{_TABLE}.Department"

# PART A: ask, then rectify intent, in ONE session -> a real correction trail.
_QUESTION_A = "what is the total annual salary for the Sales department?"
# A schema-supported intent rectification that stays within the bound column scope
# ([AnnualSalary, Department]): swap the metric total->average on the same column
# and filter the model already used, so turn 2 converges cleanly (no new schema,
# no out-of-scope column) and the session stays learnable.
_RECTIFY_A = "actually, I want the AVERAGE annual salary per employee in Sales, not the total."
# PART C: the VARIANT — same shape, DIFFERENT filter — should autoplay the learned bp.
_QUESTION_C = "what is the average annual salary per employee in the Engineering department?"

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
    # Each store connects itself on first use (`CouchbaseConnectGate`); connecting
    # up-front here only makes a bad endpoint/credential fail during setup, with the
    # failing store named, instead of part-way through the demo.
    for st in (session_store, candidate_store, audit_store, corpus_store, user_store):
        await st.connect()

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
    await apply_schema(neo4j_driver, dimension=768)

    mcp_client = RealMCPClient(_MCP_URL)
    token_minter = HttpTokenMinter(
        TOKEN_SERVICE_URL, TOKEN_ISSUER_API_KEY, tenant=_TENANT
    )

    tag = uuid.uuid4().hex[:12]
    redis_client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    stream = f"demo:flywheel:jobs:{tag}"
    dead = f"demo:flywheel:jobs:dead:{tag}"
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
        await st.close()
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
            json={
                "user_name": "alice",
                "column_scope": scope,
                "session_id": session_id,
                # Without the tenant claims the MCP 403s every tool call
                # (MISSING_TENANT_CLAIM) — see `_TENANT`.
                "claims": _TENANT.as_claims(),
            },
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


# --------------------------------------------------------------------------- runtime


def _parse_sse(text: str) -> tuple[list[dict], dict | None, dict | None]:
    """Return (progress_events, result_dict, error_dict) parsed from the SSE body
    the runtime `/turn` + `/turn/resume` endpoints stream (mirrors
    demo_runtime_turn_traced.py::_parse_sse)."""
    progress: list[dict] = []
    result: dict | None = None
    error: dict | None = None
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            payload = json.loads(line[len("data:") :].strip())
            if event == "progress":
                progress.append(payload)
            elif event == "result":
                result = payload
            elif event == "error":
                error = payload
    return progress, result, error


def _build_runtime_app(infra: _Infra, model: str):
    """Build the REAL in-process runtime `AgentLoop` (via `create_app`) EXACTLY as
    `run_ui_runtime_real.py` wires it, with retrieval turned ON so a landed
    blueprint is recallable. The SAME live Couchbase session store + RealMCPClient
    that `_build_infra` opened are reused (one event loop, one bucket) so the
    session a runtime turn writes is the SAME doc the learning sweeper later reads.
    `create_app` builds the Neo4jVectorIndex + RetrievalPipeline + runBlueprint
    executor itself from these settings (retrieval_enabled + neo4j_url + embedder)."""
    settings = RuntimeSettings(
        _env_file=None,
        mcp_url=_MCP_URL,
        openai_api_key=_OPENAI_KEY,
        openai_model=model,
        openai_base_url="",
        jwks_url=_JWKS_URL,
        jwt_issuer=_TOKEN_ISSUER,
        jwt_audience=_TOKEN_AUDIENCE,
        couchbase_connection_string="couchbase://localhost",
        couchbase_username="admin",
        couchbase_password="password",
        otlp_endpoint=_PHOENIX_OTLP,
        otlp_project_name="data-agent-runtime",
        otlp_hide_llm_content=True,
        # Production loop tunables (NOT the scripted demo's low caps).
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        # Retrieval ON: recall the just-landed blueprint. embedding_model MUST equal
        # the corpus/landing stamp (_MODEL) or recall parity-filters to an empty set.
        retrieval_enabled=True,
        scratch_enabled=False,
        neo4j_url=_NEO4J_URI,
        neo4j_username=os.environ["NEO4J_TEST_USER"],
        neo4j_password=os.environ["NEO4J_TEST_PASSWORD"],
        embedding_api_url=_EMBEDDING_URL,
        embedding_model=_MODEL,
    )
    print(
        f"[RUNTIME] create_app(retrieval=ON, mcp={_MCP_URL}, model={model!r}, "
        f"embedding_model={_MODEL!r})"
    )
    return create_app(
        settings=settings,
        session_store=infra.session_store,
        mcp_client=infra.mcp_client,
        model_client=build_openai_model_client(api_key=_OPENAI_KEY, model=model, base_url=""),
        # Provenance catalog from the frozen catalog-export snapshot (D75 Wave 1b;
        # `databaseSchemaDocs/` is gone). Dev demo → reads the committed fixture.
        catalog=catalog_handle(),
    )


async def _drive_turn(
    client: httpx.AsyncClient, *, sid: str, jwt: str, message: str
) -> dict | None:
    """POST /turn (SSE) in-process; return the parsed `result` dict (or None)."""
    resp = await client.post(
        "/turn",
        headers={"Authorization": f"Bearer {jwt}", "X-Session-Id": sid},
        json={"message": message},
        timeout=180.0,
    )
    _progress, result, error = _parse_sse(resp.text)
    if error is not None:
        print(f"  [SSE error] {error}")
    return result


async def _drive_resume(
    client: httpx.AsyncClient, *, sid: str, jwt: str, answer: str
) -> dict | None:
    """POST /turn/resume (SSE) in-process; return the parsed `result` dict (or None)."""
    resp = await client.post(
        "/turn/resume",
        headers={"Authorization": f"Bearer {jwt}", "X-Session-Id": sid},
        json={"answer": answer},
        timeout=180.0,
    )
    _progress, result, error = _parse_sse(resp.text)
    if error is not None:
        print(f"  [SSE error] {error}")
    return result


async def _drive_to_answer(
    client: httpx.AsyncClient, *, sid: str, jwt: str, message: str, max_continues: int = 3
) -> dict | None:
    """Drive /turn and auto-'continue' through `paused_budget_cap` checkpoints (as a
    user clicking 'continue' would), granting up to `max_continues` more budget windows
    so a COLD multi-step turn (schema discovery, no blueprint yet) can converge instead
    of stopping at the first soft cap."""
    result = await _drive_turn(client, sid=sid, jwt=jwt, message=message)
    continues = 0
    while (
        result is not None
        and result.get("status") == "paused_budget_cap"
        and continues < max_continues
    ):
        continues += 1
        print(
            f"  [budget cap after {result.get('tool_calls_made')} tool calls — "
            f"resuming 'continue' ({continues}/{max_continues})]"
        )
        result = await _drive_resume(client, sid=sid, jwt=jwt, answer="continue")
    return result


def _print_runtime_result(label: str, result: dict | None) -> None:
    print(f"\n  --- {label} ---")
    if result is None:
        print("  (no result event — turn errored or streamed nothing)")
        return
    print(f"  status         : {result.get('status')}")
    print(f"  tool_calls_made: {result.get('tool_calls_made')}")
    print(f"  assistant_text : {result.get('assistant_text')!r}")
    print(f"  sql            : {result.get('sql')}")
    rt = result.get("result_table")
    if isinstance(rt, dict):
        print(
            f"  result_table   : row_count={rt.get('row_count')} "
            f"columns={rt.get('columns')} preview_rows={rt.get('preview_rows')}"
        )
    bpu = result.get("blueprint_use")
    if bpu is not None:
        print(f"  blueprint_use  : {bpu}")


def _looks_like_ran_query(result: dict | None) -> bool:
    """A turn 'actually ran a query and returned a number' iff a runQuery/runBlueprint
    SQL is present AND some numeric-looking result rows came back."""
    if result is None:
        return False
    if not result.get("sql"):
        return False
    rt = result.get("result_table")
    if not isinstance(rt, dict):
        return False
    return bool(rt.get("preview_rows")) or int(rt.get("row_count") or 0) > 0


# --------------------------------------------------------------------------- run


async def _run() -> int:
    if not learning_enabled():
        raise SystemExit("LEARNING_ENABLED must be truthy")

    model = await _pick_openai_model()
    infra = await _build_infra()

    tag = uuid.uuid4().hex[:10]
    sid_a = f"flywheel-a-{tag}"  # underscore-free (D5/couchbase key + JWT-bound)
    sid_c = f"flywheel-c-{tag}"

    part_a = part_b = part_c = False
    keys: dict[str, object] = {"session_a": sid_a, "session_c": sid_c}

    app = _build_runtime_app(infra, model)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://runtime")

    try:
        # ==================================================================== PART A
        print("\n" + "#" * 72)
        print("# [PART A] a LIVE runtime turn: ask -> rectify intent -> verify")
        print("#" * 72)

        jwt_a = await _mint_bound([_SALARY_COL, _DEPT_COL], sid_a)
        infra.created_sessions.append(sid_a)

        print(f"\n[STAGE A1] POST /turn  session={sid_a!r}  q={_QUESTION_A!r}")
        turn1 = await _drive_to_answer(client, sid=sid_a, jwt=jwt_a, message=_QUESTION_A)
        _print_runtime_result("TURN 1 (ask)", turn1)

        if not _looks_like_ran_query(turn1):
            print(
                "\n[PART A] the real model did NOT run a query returning a number on turn 1 "
                "(a valid real-LLM outcome) — reporting as-is and stopping honestly."
            )
            await _finish(infra, keys, part_a, part_b, part_c)
            return 0
        part_a = True  # the ask ran a real query

        # TURN 2 — RECTIFY INTENT in the SAME session. If turn 1 paused on an
        # askUser, answer the clarify with the correction; else send it as a
        # fresh follow-up user message. Either way the session trail records a
        # real user correction (accepted_signal -> correction).
        print(f"\n[STAGE A2] rectify intent (same session {sid_a!r}): {_RECTIFY_A!r}")
        if turn1.get("status") == "paused_ask_user":
            turn2 = await _drive_resume(client, sid=sid_a, jwt=jwt_a, answer=_RECTIFY_A)
            _print_runtime_result("TURN 2 (clarify resume / correction)", turn2)
        else:
            turn2 = await _drive_to_answer(client, sid=sid_a, jwt=jwt_a, message=_RECTIFY_A)
            _print_runtime_result("TURN 2 (follow-up correction)", turn2)
        if turn2 is None:
            print(
                "  [PART A] the correction turn produced no result — proceeding with the "
                "turn-1 trail regardless (the session is still a real, learnable session)."
            )

        # The session is now a REAL Couchbase session the sweeper can pick up.
        doc_a, _cas = await infra.session_store._get_doc(sid_a)
        if doc_a is None:
            print("[PART A] the runtime session did not persist to Couchbase — stopping honestly.")
            await _finish(infra, keys, part_a, part_b, part_c)
            return 0
        content_hash = compute_content_hash(doc_a)
        keys["content_hash"] = content_hash
        print(
            f"\n[STAGE A3] captured live session {sid_a!r} "
            f"(messages={len(doc_a.messages)} trail={len(doc_a.tool_trail)} "
            f"content_hash={content_hash[:12]}...)"
        )

        # ==================================================================== PART B
        print("\n" + "#" * 72)
        print("# [PART B] learn from the live session, then HUMAN-ACCEPT via the inbox")
        print("#" * 72)

        # Idle the session so it is sweepable (last_activity excludes the content
        # hash — mutating it is safe). learning_status is 'active' on a fresh
        # runtime session, so it is claimable.
        doc_a.last_activity = _OLD_TS
        doc_a.learning_status = LearningStatus.ACTIVE
        await infra.session_store._upsert_doc(sid_a, doc_a)
        parked = await _park_foreign_idle_sessions(infra, sid_a)
        print(f"[STAGE B1] idled session for sweep; parked {parked} foreign idle session(s)")

        sweeper = LearningSweeper(infra.session_store, infra.queue, infra.settings)
        swept_doc = None
        last_sweep = None
        for _ in range(30):
            last_sweep = await sweeper.run_once()
            if last_sweep.disabled:
                raise SystemExit("sweeper reported disabled — LEARNING_ENABLED not set?")
            swept_doc, _ = await infra.session_store._get_doc(sid_a)
            if swept_doc is not None and swept_doc.learning_status == LearningStatus.QUEUED:
                break
            await asyncio.sleep(0.5)
        if swept_doc is None or swept_doc.learning_status != LearningStatus.QUEUED:
            print(f"[PART B] session not swept->queued (last={last_sweep}) — stopping honestly.")
            await _finish(infra, keys, part_a, part_b, part_c)
            return 0
        print(
            f"[STAGE B2] SWEPT (scanned={last_sweep.scanned} claimed={last_sweep.claimed} "
            f"enqueued={last_sweep.enqueued}); session->QUEUED"
        )

        # CONSUME with the REAL extractor. sampler=True is LOAD-BEARING: it routes a
        # clean blueprint candidate to `in_review` (reason blueprint_sampled) so it
        # lands in the HUMAN inbox instead of auto-promoting.
        consumer = build_learning_consumer(
            infra.settings,
            session_store=infra.session_store,
            queue=infra.queue,
            model_client=build_openai_model_client(api_key=_OPENAI_KEY, model=model, base_url=""),
            audit_store=infra.audit_store,
            candidate_store=infra.candidate_store,
            blueprint_corpus=infra.corpus_store,
            user_store=infra.user_store,
            catalog_schema=_CATALOG,
            embedder=infra.embedder,
            known_rules=known_rule_ids_from_catalog(catalog_dict()),
            sampler=lambda _env: True,  # route the blueprint to the HUMAN inbox
        )
        capturing = _CapturingExtractor(consumer._extractor)
        consumer._extractor = capturing
        consumed = await consumer.run_once()
        print(
            f"[STAGE B3] CONSUMED (done={consumed.done}) with the REAL {model!r} extractor "
            "(sampler=True -> in_review)"
        )
        _print_model_emission(capturing.last_result)

        # Register EVERY produced candidate ordinal for teardown (declines make none).
        result = capturing.last_result
        n = 0 if result is None else len(result.candidates)
        for ordinal in range(max(n, 1)):
            cid = mint_candidate_id(content_hash, ordinal)
            env = await infra.candidate_store.get(cid)
            if env is not None:
                infra.created_candidates.append(cid)

        # Build the inbox + FULLY-ACTIVATED write plane (probe/resolver/landing).
        scheduler, inbox = build_promotion_write_plane(
            infra.settings,
            candidate_store=infra.candidate_store,
            hit_counts=infra.corpus_store,
            mcp_client=infra.mcp_client,
            token_minter=infra.token_minter,
            neo4j_driver=infra.neo4j_driver,
            embedding_client=infra.embedder,
            model_id=_MODEL,
            # PriorArt Slice 2: the SAME corpus object as `hit_counts`, so a
            # reject/retract in this demo stamps the artifact terminal and the
            # flywheel actually demonstrates that a declined idea stops
            # surfacing as live prior art.
            corpus_status=infra.corpus_store,
        )

        # Locate our sampled blueprint in the inbox. NOTE: Couchbase's N1QL query
        # service (the GSI behind `inbox.list` -> `list_by_status("in_review")`) is
        # flaky on the currently-unhealthy l2-cb and intermittently returns empty,
        # so prefer a reliable KV get-by-known-id (the candidate_id embeds this
        # session's content_hash), projecting the SAME `InboxItem` the reviewer UI
        # renders. Fall back to `inbox.list()` only if the id path finds nothing.
        from data_agent.learning.inbox.models import InboxItem

        item = None
        for ordinal in range(max(n, 1)):
            env0 = await infra.candidate_store.get(mint_candidate_id(content_hash, ordinal))
            if (
                env0 is not None
                and env0.type == "blueprint"
                and str(getattr(env0.status, "value", env0.status)) == "in_review"
            ):
                item = InboxItem.from_envelope(env0)
                break
        if item is None:
            items = await inbox.list(limit=200)
            item = next(
                (
                    it
                    for it in items
                    if content_hash in it.candidate_id
                    and it.type == "blueprint"
                    and it.reason == "blueprint_sampled"
                ),
                None,
            )
        if item is None:
            print(
                "[PART B] no sampled blueprint landed in the review inbox — the real model "
                "produced no clean blueprint candidate from this session (a valid real-LLM "
                "outcome). Reporting as-is and stopping honestly."
            )
            await _finish(infra, keys, part_a, part_b, part_c)
            return 0

        keys["candidate_id"] = item.candidate_id
        print("[STAGE B4] the reviewer's inbox view (InboxItem):")
        print(f"  candidate_id : {item.candidate_id}")
        print(f"  type         : {item.type}")
        print(f"  reason       : {item.reason}")
        print(f"  summary      : {item.summary!r}")
        print(f"  payload_view : {item.payload_view}")
        print(f"  evidence_refs: {item.evidence_refs}")
        print(f"  entity_scan  : {item.entity_scan}")
        print(f"  dedup        : {item.dedup}")

        # Register teardown artifacts from the candidate envelope BEFORE approve, so a
        # partial run never strands a corpus/neo4j artifact.
        env = await infra.candidate_store.get(item.candidate_id)
        if env is not None:
            if item.candidate_id not in infra.created_candidates:
                infra.created_candidates.append(item.candidate_id)
            if env.dedup is not None and env.dedup.canonical_key:
                infra.created_corpus.append(env.dedup.canonical_key)
            node_id = landing_id(env)
            infra.created_neo4j_ids.append(node_id)
            keys["blueprint_id"] = node_id

        # HUMAN ACCEPT — the single guarded approve path (strip + deps + static +
        # golden-replay vs live ClickHouse + landing).
        print(f"\n[STAGE B5] HUMAN ACCEPT -> inbox.approve({item.candidate_id})")
        try:
            approved = await inbox.approve(item.candidate_id)
        except InboxTransitionError as exc:
            print(
                f"[PART B] approve HELD: {exc} — a valid guarded outcome (replay/deps/static). "
                "Reporting as-is and stopping honestly."
            )
            await _finish(infra, keys, part_a, part_b, part_c)
            return 0

        node_id = landing_id(approved)
        async with infra.neo4j_driver.session() as s:
            row = await (
                await s.run(
                    "MATCH (b:Blueprint {id: $id}) RETURN b.created_by AS created_by, "
                    "b.status AS status, b.source_candidate_id AS src",
                    {"id": node_id},
                )
            ).single()
        if approved.status != "validated" or row is None:
            print(
                f"[PART B] approve did not reach validated+landed "
                f"(status={approved.status}, neo4j_row={row}). Reporting as-is."
            )
            await _finish(infra, keys, part_a, part_b, part_c)
            return 0
        keys["blueprint_id"] = node_id
        part_b = True
        print(
            f"[STAGE B5] VALIDATED + LANDED -> :Blueprint {node_id} "
            f"(created_by={row['created_by']}, status={row['status']}, src={row['src']})"
        )

        # ==================================================================== PART C
        print("\n" + "#" * 72)
        print("# [PART C] a VARIANT question AUTOPLAYS the blueprint through the RUNTIME")
        print("#" * 72)

        jwt_c = await _mint_bound([_SALARY_COL, _DEPT_COL], sid_c)
        infra.created_sessions.append(sid_c)

        print(f"\n[STAGE C1] POST /turn  session={sid_c!r}  q={_QUESTION_C!r}")
        turnc = await _drive_to_answer(client, sid=sid_c, jwt=jwt_c, message=_QUESTION_C)
        _print_runtime_result("VARIANT TURN (Engineering)", turnc)

        bpu = (turnc or {}).get("blueprint_use")
        # Backstop: did a runBlueprint entry actually get persisted to the trail?
        doc_c, _ = await infra.session_store._get_doc(sid_c)
        ran_blueprint = bool(doc_c and any(e.tool_name == "runBlueprint" for e in doc_c.tool_trail))
        if bpu is not None and bpu.get("blueprint_id"):
            slots = bpu.get("slots") or {}
            print(
                f"\n[STAGE C2] AUTOPLAY FIRED: blueprint_id={bpu.get('blueprint_id')} slots={slots}"
            )
            if any(str(v).lower() == "engineering" for v in slots.values()):
                print(
                    "           slots bound to department=Engineering — the blueprint "
                    "learned from 'Sales' autoplayed for 'Engineering'. PAYOFF."
                )
            part_c = True
        elif ran_blueprint:
            print(
                "\n[STAGE C2] the runtime ran runBlueprint (trail shows it) but the enriched "
                "blueprint_use was not surfaced on the result — inspecting the trail confirms "
                "the fast path fired."
            )
            part_c = True
        else:
            print(
                "\n[STAGE C2] the runtime took the RAW path (no runBlueprint) — a valid real-LLM "
                "outcome (recall may not have surfaced it, or the model chose raw tools). "
                "Reporting the runtime outcome honestly."
            )

        # Deterministic recall BACKSTOP (like the learning demo's STAGE 5): does the
        # variant question vector recall the just-landed blueprint node at all?
        print("\n[STAGE C3] deterministic recall backstop (Neo4jVectorIndex.recall):")
        query_vector = (await infra.embedder.embed([_QUESTION_C]))[0]
        index = Neo4jVectorIndex(
            url=_NEO4J_URI,
            auth=(os.environ["NEO4J_TEST_USER"], os.environ["NEO4J_TEST_PASSWORD"]),
            expected_model=_MODEL,
            timeout_seconds=15.0,
        )
        try:
            recalled = await index.recall(query_vector=query_vector, kind="blueprint", k=30)
            landed = next((c for c in recalled if c.id == node_id), None)
            if landed is None:
                print(
                    f"           the learned blueprint {node_id} was NOT recalled for "
                    f"{_QUESTION_C!r} (semantic distance) — recall returned {len(recalled)} node(s)."
                )
            else:
                print(
                    f"           RECALLED: {_QUESTION_C!r} surfaced {node_id} "
                    f"(uses={sorted(landed.uses)}) — it IS recallable."
                )
        finally:
            await index.close()

        await _finish(infra, keys, part_a, part_b, part_c)
        return 0
    finally:
        await client.aclose()
        if os.environ.get("KEEP") == "1":
            print(
                "\n[TEARDOWN] KEEP=1 — SKIPPING teardown so you can inspect the landed "
                "blueprint + inbox state (session/candidate/corpus/neo4j left in place)."
            )
        else:
            await _teardown(infra)
            print("\n[TEARDOWN] cleaned created session/candidate/corpus/neo4j artifacts.")


async def _finish(infra, keys, part_a, part_b, part_c) -> None:  # noqa: ANN001
    print("\n" + "=" * 70)
    print(">>> [SUMMARY]")
    print("=" * 70)
    print(f"  PART A (live runtime turn + rectify): {'DONE' if part_a else 'not reached'}")
    print(f"  PART B (learn + human inbox approve): {'DONE' if part_b else 'not reached'}")
    print(f"  PART C (variant autoplay):            {'DONE' if part_c else 'not reached'}")
    print("  key ids:")
    for k, v in keys.items():
        print(f"    {k:14s}: {v}")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
