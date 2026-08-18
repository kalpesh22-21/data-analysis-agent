"""Shared rig for the standalone live demos under `scripts/`.

`demo_learning_e2e_openai.py` and `demo_flywheel_inbox_e2e.py` are executable
documentation: each walks a real path through the REAL stack (live OpenAI, l2-mcp,
Couchbase, Neo4j, Redis, ClickHouse) and narrates it. They are not tests, and their
value is that a reader can follow the STORY. What they should not also carry is ~400
identical lines of rig — the live-env block, the five Couchbase stores, the Redis
stream, the teardown, the token mint, the model preflight — copied between them, where
a fix to one copy silently leaves the other running the old wiring.

So the RIG lives here and the demos keep their narrative. Everything in this module is
infrastructure a reader can take on trust; everything left in a demo is the thing that
demo is about.

`demo_runtime_turn_traced.py` and `run_ui_runtime_real.py` share the smaller half:
reading the OpenAI key out of `.env`, the model preflight, the Phoenix span-attribute
helpers, the token mint.

Import pattern (mirrors `scripts/_catalog.py` and `seed_neo4j_corpus.py`): this is a
SIBLING module under `scripts/`, so an importing script puts its own directory on
`sys.path` first —

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _e2e_harness import ...

which resolves both when run as `python scripts/x.py` and when the file is loaded by
path (importlib `spec_from_file_location`, e.g. from a test).

NO IMPORT-TIME SIDE EFFECTS, deliberately: the demos set their env block BEFORE
constructing any settings object, so nothing here may build a settings object, open a
socket, or read the ENVIRONMENT at import. `apply_live_env()` is an explicit call, and
EVERY env read is behind a function (`redis_url()`, `mcp_url()`, `token_service_url()`,
…) so none of them can run before it. A module constant would not just be stale — it
would make `apply_live_env(TOKEN_SERVICE_URL=...)` look accepted and be ignored.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai

from data_agent.learning.config import LearningSettings
from data_agent.learning.models import SWEEPABLE_STATUSES, LearningStatus
from data_agent.learning.promotion.token_minter import HttpTokenMinter, TenantClaims
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient

_REPO = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- env

# The FULL live env block the E2E integration test documents — the l2 stack's
# endpoints and the per-bucket RBAC writers. Applied by `apply_live_env()` BEFORE any
# settings object is constructed (every settings field below is read at construction,
# not at import).
_LIVE_ENV = {
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


def apply_live_env(**extra: str) -> None:
    """Set the live-stack env block (plus any demo-specific *extra*) in place.

    Call this at module level, before the demo constructs any settings object.
    (The Phoenix project name is set IN CODE by `configure_learning_tracing` /
    `configure_tracing(project_name=...)`; no OTEL_RESOURCE_ATTRIBUTES hack is
    involved.)
    """
    os.environ.update(_LIVE_ENV)
    os.environ.update(extra)


def redis_url() -> str:
    return os.environ["LEARNING_REDIS_TEST_URL"]


def neo4j_uri() -> str:
    return os.environ["NEO4J_TEST_URI"]


def neo4j_auth() -> tuple[str, str]:
    return (os.environ["NEO4J_TEST_USER"], os.environ["NEO4J_TEST_PASSWORD"])


def embedding_url() -> str:
    return os.environ["EMBEDDING_TEST_URL"]


def mcp_url() -> str:
    return os.environ["MCP_TEST_URL"]


# --------------------------------------------------------------------------- consts

PHOENIX_OTLP = "http://localhost:6006/v1/traces"
PHOENIX_UI = "http://localhost:6006"
PHOENIX_GRAPHQL = "http://localhost:6006/graphql"

# The embedding model id stamped on corpus artifacts and landed blueprints. Recall
# parity-filters on it, so a demo that embeds with a different model recalls nothing.
EMBEDDING_MODEL = "all-mpnet-base-v2"

# Old enough that any sweep window considers a seeded session idle.
OLD_TS = "2000-01-01T00:00:00+00:00"

# The warehouse is snake_case (docker/clickhouse-init/hr-4tables-snake-migration.sql,
# aligned column-for-column with the Semantic Catalog YAMLs). These names are
# CASE-SENSITIVE all the way down: the MCP's `getTableSchema` overlay binds by exact
# column name, and the JWT column scope is matched as an exact string. A stale
# CamelCase name here does not error — it silently matches NOTHING, so the overlay
# scope-filters the schema down to an empty column list and the model, shown a table
# with no columns, invents them (-> PARSE_FAILED_CLOSED).
TABLE = "dbpcm_warehouse.employee"
SALARY_COL = f"{TABLE}.annual_salary"
# Departments are filtered by NAME in the demos ("the Sales department"); the catalog's
# `department` ambiguity resolves to department_code/department_name, and the seeded
# rows carry both (D02/'Sales').
DEPT_COL = f"{TABLE}.department_name"
DEPT_CODE_COL = f"{TABLE}.department_code"
STATUS_COL = f"{TABLE}.employee_status"
EMPLOYEE_COL = f"{TABLE}.employee_code"

# The column scope the demos mint their JWTs with. Deliberately MINIMAL, but it must
# cover every column the demo's expected SQL can legitimately touch, because the
# overlay drops out-of-scope columns from `getTableSchema` AND drops any rule /
# ambiguity whose predicate references one:
#   * annual_salary   — the measure (sum/avg).
#   * department_name — the filter the demos ask for ("Sales", "Engineering").
#   * department_code — the OTHER half of the catalog's `department` ambiguity; in
#     scope so the ambiguity entry survives the filter intact and a model that
#     resolves "Sales" to D02 is not a scope violation.
#   * employee_status — the `exclude_not_hired_default` rule predicates on it. Out of
#     scope, that default rule is silently dropped from the schema the model sees.
#   * employee_code   — the "AVERAGE annual salary PER EMPLOYEE" rectification invites
#     sum(annual_salary) / count(DISTINCT employee_code) as readily as avg().
DEMO_COLUMN_SCOPE = [SALARY_COL, DEPT_COL, DEPT_CODE_COL, STATUS_COL, EMPLOYEE_COL]

# The sqlglot schema the extractor's static validation reads. A deliberate MINI
# catalog — only the columns the demos touch — but the names and ClickHouse types are
# the REAL ones (tests/fixtures/catalog_export.json / the snake migration), because
# static validation resolves the model's proposed SQL against exactly this mapping.
CATALOG = {
    TABLE: {
        "client_code": "String",
        "employee_code": "String",
        "department_code": "Nullable(String)",
        "department_name": "Nullable(String)",
        "employee_name": "Nullable(String)",
        "employee_status": "Nullable(String)",
        "annual_salary": "Nullable(Decimal(18, 6))",
    }
}

# --------------------------------------------------------------------------- OpenAI

# OpenAI model candidates, tried in order until one answers (an account may 404 some).
MODEL_CANDIDATES = ("gpt-5.5", "gpt-4o", "gpt-4.1", "gpt-4o-mini")


def load_openai_key() -> str:
    """Read + strip the OPENAI_API_KEY from the repo-root `.env` (quotes tolerated)."""
    env_path = _REPO / ".env"
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("OPENAI_API_KEY="):
            val = line.split("=", 1)[1].strip()
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            return val
    raise SystemExit("OPENAI_API_KEY not found in .env")


def pick_openai_model(api_key: str, *, label: str = "[MODEL]") -> str:
    """The first candidate the account can actually call on the Responses API.

    A 404 / no-access model moves to the next; a total miss is a `SystemExit`.
    `DEMO_MODEL` short-circuits the candidate list. SYNCHRONOUS with an explicit
    *api_key*: this is a startup selection step, run once, and the persistent
    launcher must be able to do it BEFORE uvicorn's event loop exists.
    """
    client = openai.OpenAI(api_key=api_key)
    candidates = (os.environ["DEMO_MODEL"],) if os.environ.get("DEMO_MODEL") else MODEL_CANDIDATES
    last_err: Exception | None = None
    for model in candidates:
        try:
            client.responses.create(model=model, input=[{"role": "user", "content": "ping"}])
            print(f"{label} preflight OK on {model!r} (OpenAI Responses API)")
            return model
        except openai.NotFoundError as exc:
            print(f"{label} {model!r} unavailable (404) — trying next: {exc}")
            last_err = exc
        except Exception as exc:  # noqa: BLE001 - preflight is best-effort selection
            print(f"{label} {model!r} errored ({type(exc).__name__}): {exc} — trying next")
            last_err = exc
    raise SystemExit(f"No OpenAI model candidate worked: {last_err}")


async def pick_openai_model_async(api_key: str, *, label: str = "[MODEL]") -> str:
    """`pick_openai_model` for a demo already inside `asyncio.run`.

    Off-thread rather than sync-in-the-loop: the preflight is the first thing a demo
    does and nothing else is in flight, but a blocking network call inside `async def`
    is a habit worth not writing down.
    """
    return await asyncio.to_thread(pick_openai_model, api_key, label=label)


# --------------------------------------------------------------------------- tokens

# The live l2-token IdP (docker-compose.integration.yml), mirroring
# tests/integration/conftest.py + scripts/run_ui_runtime.py.
#
# FUNCTIONS, not module constants: a constant is read at IMPORT, which is before the
# demo's `apply_live_env()` call, so `apply_live_env(TOKEN_SERVICE_URL=...)` would have
# been accepted and silently ignored. Every env read in this module is deferred for the
# same reason — the whole point of the explicit `apply_live_env()` is that nothing may
# have read the environment before it runs.


def token_service_url() -> str:
    return os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")


def token_issuer_api_key() -> str:
    return os.environ.get("TOKEN_ISSUER_API_KEY", "issuer-key-abc123")


# The httpx default timeout the hand-rolled `httpx.AsyncClient()` mints ran under.
_MINT_TIMEOUT_SECONDS = 5.0


def tenant_claims() -> TenantClaims:
    """The warehouse TENANT claims every minted token must carry.

    The MCP maps clientcode/proc_center/jti onto the paycom_* ClickHouse settings its
    row policies read; a token without them is rejected 403 MISSING_TENANT_CLAIM before
    any tool runs. Resolved through `RuntimeSettings` so this reads the SAME TENANT_*
    env vars `ui/server.py` and the learning scheduler read (defaults = the seeded
    local tenant).
    """
    settings = RuntimeSettings(_env_file=None)
    return TenantClaims(
        clientcode=settings.tenant_client_code,
        proc_center=settings.tenant_proc_center,
        jti=settings.tenant_jti,
    )


async def mint_bound_token(
    column_scope: list[str],
    session_id: str,
    *,
    user_name: str = "alice",
    allow_unscoped: bool = False,
) -> str:
    """Mint a session-BOUND JWT for a demo, through the shared `HttpTokenMinter`.

    `column_scope` is what the MCP enforces (D57/D80); `session_id` is stamped as
    `sid_hash` and must be the SAME id the caller sends as `X-Session-Id`.
    `ttl_seconds=None` defers to the IdP's configured lifetime — a demo run outlives
    the promotion probe's deliberately short 300s. `allow_unscoped=True` is required
    to mint the allow-all (`[]`) token a whole-warehouse demo turn wants; the default
    keeps the offline plane's backstop.
    """
    minter = HttpTokenMinter(
        token_service_url(),
        token_issuer_api_key(),
        tenant=tenant_claims(),
        user_name=user_name,
        ttl_seconds=None,
        timeout=_MINT_TIMEOUT_SECONDS,
        allow_unscoped=allow_unscoped,
    )
    return await minter.mint(list(column_scope), session_id=session_id)


# --------------------------------------------------------------------------- infra


class CapturingExtractor:
    """Transparent wrapper around the real `LearningExtractor` that records the
    `ExtractionResult` the model produced so a demo can PRINT what the LLM actually
    emitted (candidates + declines)."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.last_result = None

    async def extract(self, summary, verdict):  # noqa: ANN001
        result = await self._inner.extract(summary, verdict)
        self.last_result = result
        return result


@dataclass
class Infra:
    """Every live dependency a demo opened, plus the artifacts it must clean up.

    The four `created_*` lists are the teardown ledger: a demo appends to them AS IT
    CREATES things, so a run that stops half-way (an honest real-LLM outcome) still
    cleans up exactly what it made.
    """

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


async def build_infra(*, stream_prefix: str) -> Infra:
    """Open every live dependency: the five Couchbase stores, the embedder, Neo4j, the
    MCP client, the token minter, and a demo-private Redis stream.

    *stream_prefix* namespaces this demo's Redis keys (`<prefix>:jobs:<tag>`), so two
    demos — or two runs — never consume each other's jobs.
    """
    import redis.asyncio as aioredis
    from neo4j import AsyncGraphDatabase

    from data_agent.learning.audit.couchbase_audit_store import CouchbaseAuditStore
    from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
    from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus
    from data_agent.learning.redis_queue import RedisStreamsLearningQueue
    from data_agent.learning.user.config import UserKnowledgeStoreConfig
    from data_agent.learning.user.couchbase_user_store import CouchbaseUserKnowledgeStore
    from data_agent.runtime.retrieval.corpus_loader import apply_schema
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
        url=embedding_url(),
        api_key=os.environ.get("EMBEDDING_TEST_API_KEY", ""),
        model=EMBEDDING_MODEL,
        timeout_seconds=30.0,
    )

    neo4j_driver = AsyncGraphDatabase.driver(neo4j_uri(), auth=neo4j_auth())
    await apply_schema(neo4j_driver, dimension=768)

    mcp_client = RealMCPClient(mcp_url())
    token_minter = HttpTokenMinter(
        token_service_url(), token_issuer_api_key(), tenant=tenant_claims()
    )

    tag = uuid.uuid4().hex[:12]
    redis_client = aioredis.from_url(redis_url(), decode_responses=True)
    stream = f"{stream_prefix}:jobs:{tag}"
    dead = f"{stream_prefix}:jobs:dead:{tag}"
    queue = RedisStreamsLearningQueue(
        redis_client,
        stream=stream,
        group="learning-workers",
        consumer_name=f"worker-demo-{tag}",
        dead_letter_stream=dead,
    )
    await queue.ensure_group()

    return Infra(
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


async def teardown(infra: Infra) -> None:
    """Remove every artifact the run created, then close every connection."""
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


async def park_foreign_idle_sessions(infra: Infra, keep_sid: str) -> int:
    """Mark every OTHER sweepable session DONE so this run's sweep claims exactly one.

    A shared dev bucket accumulates idle sessions from earlier runs; without this the
    sweeper enqueues them too and the demo narrates someone else's session.
    """
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


# --------------------------------------------------------------------------- output


def print_model_emission(result) -> None:  # noqa: ANN001
    """Print the RAW `ExtractionResult` the live model emitted — candidates with their
    full slot plan, and declines with their reason. This is the whole point of running
    a demo against a real LLM instead of a fixture."""
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


def parse_sse(text: str) -> tuple[list[dict], dict | None, dict | None]:
    """Return (progress_events, result_dict, error_dict) parsed from the SSE body the
    runtime's `/turn` + `/turn/resume` endpoints stream."""
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


def flatten(d: dict, prefix: str = "") -> dict:
    """Flatten Phoenix's NESTED attribute JSON back to dotted OTel keys, so
    `{"session": {"id": x}}` reads as `{"session.id": x}` (the form the code set)."""
    out: dict = {}
    for key, value in d.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, dotted + "."))
        else:
            out[dotted] = value
    return out


def span_attrs(node: Any) -> dict:
    """Phoenix returns span attributes as a JSON string of a NESTED object (or a dict
    on some builds). Normalize to a FLAT dotted-key dict; tolerate absence."""
    raw = node.get("attributes")
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return flatten(raw) if isinstance(raw, dict) else {}


__all__ = [
    "CATALOG",
    "DEMO_COLUMN_SCOPE",
    "DEPT_CODE_COL",
    "DEPT_COL",
    "EMBEDDING_MODEL",
    "EMPLOYEE_COL",
    "MODEL_CANDIDATES",
    "OLD_TS",
    "PHOENIX_GRAPHQL",
    "PHOENIX_OTLP",
    "PHOENIX_UI",
    "SALARY_COL",
    "STATUS_COL",
    "TABLE",
    "CapturingExtractor",
    "Infra",
    "apply_live_env",
    "build_infra",
    "embedding_url",
    "flatten",
    "load_openai_key",
    "mcp_url",
    "mint_bound_token",
    "neo4j_auth",
    "neo4j_uri",
    "park_foreign_idle_sessions",
    "parse_sse",
    "pick_openai_model",
    "pick_openai_model_async",
    "print_model_emission",
    "redis_url",
    "span_attrs",
    "teardown",
    "tenant_claims",
    "token_issuer_api_key",
    "token_service_url",
]
