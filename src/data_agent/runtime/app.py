"""app.py — the composition root (design §1/§9, item 11 of the Pass-B brief).

Wires `RealMCPClient` + `CouchbaseSessionStore` + `OpenAIModelClient` +
`ToolDispatcher` + `ContextAssembler` + `AgentLoop` + observability into one
FastAPI app, per `RuntimeSettings`.

HTTP surface:
    `POST /turn`         — reads `Authorization: Bearer <jwt>` + `X-Session-Id`,
                            builds `RuntimeCredentials` via `auth/jwt_verify.py`,
                            runs a fresh turn (`AgentLoop.run`), and streams
                            progress + the final result over SSE (OQ-F).
    `POST /turn/resume`  — same headers, CAS-consumes the pending checkpoint
                            and continues (`AgentLoop.resume`), same SSE shape.

D5 model-invisibility (load-bearing): `RuntimeCredentials` is built exactly
once per request, here, from the inbound headers, and is threaded as an
explicit argument into `AgentLoop.run`/`resume` — it is never placed into any
JSON body, any `messages` payload, or any span/progress attribute.

Import-time safety (acceptance criterion): `create_app(...)` is a factory,
not a module-level singleton — merely `import data_agent.runtime.app` never
constructs a real MCP/Couchbase/OpenAI client or makes a network call.
`create_app()` (no arguments) is the real-infra entrypoint for a deployment;
tests call `create_app(session_store=..., mcp_client=..., model_client=...)`
with Layer-1 fakes for a full no-infra smoke test of the HTTP wiring itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.auth.jwt_verify import JWTVerificationError, verify_jwt
from data_agent.runtime.blueprint.executor import BlueprintExecutor
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
from data_agent.runtime.config import RuntimeSettings, get_runtime_settings
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.llm_summarizer import build_llm_summarizer
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolObserver
from data_agent.runtime.loop.agent_loop import AgentLoop, RuntimeTool, TurnOutcome
from data_agent.runtime.mcp.client import MCPClient
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.mcp.tool_schema import ToolSchemaCache
from data_agent.runtime.model.client import ModelClient
from data_agent.runtime.model.embedding_client import EmbeddingClient, HttpEmbeddingClient
from data_agent.runtime.model.openai_client import build_openai_model_client
from data_agent.runtime.model.reranker_client import HttpRerankerClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.progress import ProgressEmitter, combine_observers
from data_agent.runtime.observability.redaction import hash_scope
from data_agent.runtime.provenance.catalog_handle import CatalogHandle, load_catalog_handle
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import (
    GetBlueprintTool,
    SearchBlueprintsTool,
    SearchKnowledgeTool,
)
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex
from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore
from data_agent.runtime.session.store import AlreadyConsumedError, CASMismatchError, SessionStore

# S5: bounded-length, restricted-charset validation for the (unsigned,
# UI-supplied) X-Session-Id header — it is used verbatim to build Couchbase
# document keys (session::<session_id>), so an unbounded/arbitrary-charset
# value is a minor injection/DoS surface even though it carries no auth
# weight of its own (the JWT is the trust boundary, D5/D79b). UUID4-shaped by
# convention, but kept a little more permissive (any URL-safe token up to 128
# chars) so we don't hard-couple to UUID specifically.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_logger = logging.getLogger(__name__)

# B4-class fix (2026-07-01, second pass): an unexpected exception surfacing
# from anywhere inside `AgentLoop.run`/`resume` (a raw `OpenAIModelClient`
# transport error, `ToolSchemaCache.get_schemas` -> `list_tools()` failure, a
# Couchbase SDK error outside the CAS wrapper, ...) must never stream its raw
# `str(exc)` to the client over SSE — same info-disclosure class the B4 fix in
# `dispatch/tool_dispatcher.py` already closed for the tool-dispatch path.
# The real exception is logged server-side only; the client/model only ever
# sees this generic, canned message.
_INTERNAL_ERROR_MESSAGE = "Something went wrong processing this turn. Please try again."


class TurnRequest(BaseModel):
    message: str


class ResumeRequest(BaseModel):
    answer: str


def _extract_credentials(
    *, authorization: str | None, session_id: str | None, settings: RuntimeSettings
) -> RuntimeCredentials:
    """Build `RuntimeCredentials` from the inbound request (design §2) — the
    ONLY place a `RuntimeCredentials` is constructed."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing X-Session-Id header.")
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Malformed X-Session-Id header.")

    token = authorization.split(" ", 1)[1].strip()
    try:
        column_scope = verify_jwt(
            token,
            jwks_url=settings.jwks_url,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
    except JWTVerificationError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc

    return RuntimeCredentials(session_id=session_id, jwt=token, column_scope=column_scope)


def _format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _outcome_to_dict(outcome: TurnOutcome) -> dict[str, Any]:
    return {
        "status": outcome.status,
        "assistant_text": outcome.assistant_text,
        "pending_question": outcome.pending_question,
        "tool_calls_made": outcome.tool_calls_made,
    }


async def _stream_turn(
    agent_loop_call: Callable[[], Awaitable[TurnOutcome]], emitter: ProgressEmitter
) -> AsyncIterator[str]:
    """Drive one `AgentLoop.run`/`resume` call as a background task while
    streaming its `ProgressEmitter` output as SSE `progress` events, then a
    final `result` event (or an `error` event on failure)."""

    async def _runner() -> TurnOutcome:
        try:
            return await agent_loop_call()
        finally:
            emitter.close()

    task = asyncio.create_task(_runner())
    async for event in emitter.stream():
        yield _format_sse("progress", {"step": event.step, "shape": event.shape})

    try:
        outcome = await task
    except (AlreadyConsumedError, CASMismatchError) as exc:
        yield _format_sse("error", {"code": type(exc).__name__, "message": str(exc)})
        return
    except Exception:  # noqa: BLE001 - last-resort SSE error framing, never a bare 500
        # Never forward the raw exception text to the client/model (D5/D25,
        # same class as B4) — log it server-side only, yield a generic canned
        # message to the SSE `error` event.
        _logger.exception("Unhandled exception during AgentLoop.run/resume")
        yield _format_sse("error", {"code": "INTERNAL_ERROR", "message": _INTERNAL_ERROR_MESSAGE})
        return

    yield _format_sse("result", _outcome_to_dict(outcome))


def create_app(
    *,
    settings: RuntimeSettings | None = None,
    session_store: SessionStore | None = None,
    mcp_client: MCPClient | None = None,
    model_client: ModelClient | None = None,
    catalog: CatalogHandle | None = None,
    embedding_client: EmbeddingClient | None = None,
    resolve_values: ResolveValuesComposite | None = None,
    retrieval: RetrievalPipeline | None = None,
) -> FastAPI:
    """Build the FastAPI app. All dependencies default to the real
    implementations, sourced from *settings* — pass Layer-1 fakes for any of
    them (e.g. in a smoke test) to avoid touching real infra entirely.

    *retrieval* (design §12 / neo4j-corpus-design §2.4): the D7/D8 pipeline
    pre-injected into `ContextAssembler`. When left `None` AND `neo4j_url` + an
    embedder are configured, a `Neo4jVectorIndex`-backed pipeline is constructed
    here (Slice 2) and its driver is closed on app shutdown; absent either the
    store or the embedder it stays `None` (byte-identical Phase-0 parity, D86).
    Tests inject a pipeline (real D71 clients + a seeded `FakeVectorIndex` or a
    live `Neo4jVectorIndex`) to exercise the whole embed→rerank→inject path."""
    settings = settings or get_runtime_settings()
    catalog = catalog or load_catalog_handle()
    mcp_client = mcp_client or RealMCPClient(settings.mcp_url)
    session_store = session_store or CouchbaseSessionStore(settings)
    model_client = model_client or build_openai_model_client(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        base_url=settings.openai_base_url,
    )

    tracer_provider = tracing.configure_tracing(
        otlp_endpoint=settings.otlp_endpoint, service_name=settings.otlp_service_name
    )
    tracing.instrument_openai(tracer_provider)
    tracer = tracing.get_tracer(tracer_provider)

    # D77/OQ-1: the real `HttpEmbeddingClient` is wired only when the custom
    # embedding API is configured; otherwise the composite runs with NO
    # embedding client and degrades to frequency-only ranking (design §3.2) —
    # an unconfigured/unreachable embedder never breaks the tool. (Deviation
    # from OQ-1's literal "FakeEmbeddingClient when unconfigured": a no-op
    # absent client is more honest in production than fake hash-vector ranking;
    # `FakeEmbeddingClient` stays a test-only double.)
    if embedding_client is None and settings.embedding_api_url:
        embedding_client = HttpEmbeddingClient(
            url=settings.embedding_api_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            timeout_seconds=settings.embedding_timeout_seconds,
            tracer=tracer,
        )

    # Slice 2 (neo4j-corpus-design §2.4): wire the neo4j-backed retrieval
    # pipeline ONLY when BOTH a store (`neo4j_url`) and an embedder are
    # configured AND retrieval is enabled — retrieval needs an embedder to embed
    # the question AND a store to recall from; absent any of those it stays
    # `None` (Phase-0 parity, D86). Gating on `retrieval_enabled` here (not only
    # at the ContextAssembler below) means a disabled deployment opens NO driver
    # pool (S4). An injected `retrieval` (Layer-1/2 tests) is honored as-is and
    # never rebuilt here. The reranker is optional: absent `reranker_api_url` the
    # pipeline degrades to recall order.
    vector_index: Neo4jVectorIndex | None = None
    if (
        retrieval is None
        and settings.retrieval_enabled
        and settings.neo4j_url
        and embedding_client is not None
    ):
        # B2: `embedding_model` is the read-path parity key — an empty value
        # parity-filters recall on '' and yields a permanently EMPTY corpus. Warn
        # loudly server-side rather than silently retrieve nothing.
        if not settings.embedding_model:
            _logger.warning(
                "neo4j retrieval is configured but embedding_model is empty — recall "
                "parity-filters on embedding_model='' and will return an EMPTY corpus. "
                "Set embedding_model to match the corpus stamp "
                "(see scripts/seed_neo4j_corpus.py)."
            )
        vector_index = Neo4jVectorIndex(
            url=settings.neo4j_url,
            auth=(settings.neo4j_username, settings.neo4j_password),
            expected_model=settings.embedding_model,
            timeout_seconds=settings.neo4j_timeout_seconds,
            tracer=tracer,
        )
        reranker = (
            HttpRerankerClient(
                url=settings.reranker_api_url,
                api_key=settings.reranker_api_key,
                model=settings.reranker_model,
                timeout_seconds=settings.reranker_timeout_seconds,
                tracer=tracer,
            )
            if settings.reranker_api_url
            else None
        )
        retrieval = RetrievalPipeline(
            embedding_client=embedding_client,
            reranker=reranker,
            vector_index=vector_index,
            user_memory=NullUserMemoryProvider(),
            recall_k=settings.retrieval_recall_k,
            top_k_blueprints=settings.retrieval_top_k_blueprints,
            top_k_knowledge=settings.retrieval_top_k_knowledge,
            knowledge_min_score=settings.retrieval_knowledge_min_score,
            reranker_model=settings.reranker_model,
            tracer=tracer,
        )

    # `retrieval_enabled=False` is a hard master switch: no pre-injection AND no
    # read tools (they share the one pipeline/store singleton). When active, the
    # three read tools are wired over the SAME pipeline + store; when None, they
    # are simply absent from the registry → the loop advertises them but returns
    # RETRIEVAL_TOOL_UNAVAILABLE (Phase-0 parity, read-tools §6).
    active_retrieval = retrieval if settings.retrieval_enabled else None

    tool_schema_cache = ToolSchemaCache(mcp_client)
    summarizer = build_llm_summarizer(model_client)
    context_assembler = ContextAssembler(
        session_store,
        history_token_budget=settings.history_token_budget(),
        preview_row_count=settings.preview_row_count,
        summarizer=summarizer,
        retrieval=active_retrieval,
        tracer=tracer,
    )

    # B5: TOOL spans are emitted by ToolDispatcher itself (wired via the
    # `tracer=` constructor argument below); this only covers AgentLoop's own
    # (non-tool) `loop_*` stage boundaries as lightweight GUARDRAIL events,
    # through a strict attribute allowlist (never a bare type-filter — see
    # `tracing.guardrail_observer`'s docstring for why that matters for
    # `loop_paused_ask_user`'s `question` payload specifically).
    _tracing_observer = tracing.guardrail_observer(tracer)

    async def _tools_provider(credentials: RuntimeCredentials) -> list[dict[str, Any]]:
        # The live MCP authenticates tools/list too (no anonymous
        # introspection) — thread this turn's credentials through, but the
        # catalogue itself is scope-independent and cached by
        # ToolSchemaCache after the first successful fetch (see its
        # docstring).
        return await tool_schema_cache.get_schemas(
            jwt=credentials.jwt, session_id=credentials.session_id
        )

    def _build_agent_loop(observer: ToolObserver) -> AgentLoop:
        dispatcher = ToolDispatcher(
            mcp_client,
            catalog,
            preview_row_count=settings.preview_row_count,
            observer=observer,
            tracer=tracer,
        )
        # D77: the composite wraps the SAME dispatcher (so its inner runQuery
        # shares the per-request observer/tracer and the free D5/D57/provenance
        # path); an injected `resolve_values` (Layer-1 smoke test) overrides it.
        composite = resolve_values or ResolveValuesComposite(
            tool_dispatcher=dispatcher,
            catalog=catalog,
            embedding_client=embedding_client,
            query_limit=settings.resolve_values_query_limit,
            top_k=settings.resolve_values_top_k,
            similarity_weight=settings.resolve_values_similarity_weight,
            preview_row_count=settings.preview_row_count,
            observer=observer,
            tracer=tracer,
        )
        # The runtime-tool registry (read-tools §2): `resolveValues` is always
        # wired; the three read tools are wired ONLY when the retrieval pipeline
        # is active — they share the one pipeline + store singleton, and carry
        # this request's observer/tracer for progress + the nested TOOL span.
        runtime_tools: dict[str, RuntimeTool] = {"resolveValues": composite}
        blueprint_executor: BlueprintExecutor | None = None
        if active_retrieval is not None:
            runtime_tools["searchBlueprints"] = SearchBlueprintsTool(
                pipeline=active_retrieval,
                default_k=settings.retrieval_search_default_k,
                max_k=settings.retrieval_search_max_k,
                preview_row_count=settings.preview_row_count,
                observer=observer,
                tracer=tracer,
            )
            runtime_tools["searchKnowledge"] = SearchKnowledgeTool(
                pipeline=active_retrieval,
                knowledge_k=settings.retrieval_search_knowledge_k,
                preview_row_count=settings.preview_row_count,
                observer=observer,
                tracer=tracer,
            )
            runtime_tools["getBlueprint"] = GetBlueprintTool(
                vector_index=active_retrieval.vector_index,
                preview_row_count=settings.preview_row_count,
                observer=observer,
                tracer=tracer,
            )
            # runBlueprint (runblueprint-design §5, Slice B): the deterministic
            # fast path. Wired ONLY when retrieval is active — it reads the SAME
            # `vector_index` (getBlueprint by id) and issues its per-node runQuery
            # through the SAME per-request `dispatcher` (D57/D64/D5 + provenance
            # free). Without this, the advertised schema (12 tools) would always
            # return RUN_BLUEPRINT_UNAVAILABLE (the loop's unwired-tool path).
            # Slice C: the executor also gets the `resolveValues` composite as its
            # D67 `resolve_via` hook (`.resolve()`, no model round-trip) so a
            # blueprint rule that expands a concept→code-set filters through the
            # same scope-enforced path (§3.4). The SAME executor instance is handed
            # to the loop so `AgentLoop.resume` can re-enter a paused mid-DAG
            # blueprint at `awaiting_node` (D45, §2.5).
            blueprint_executor = BlueprintExecutor(
                tool_dispatcher=dispatcher,
                vector_index=active_retrieval.vector_index,
                resolve_values=composite,
                preview_row_count=settings.preview_row_count,
                observer=observer,
            )
            runtime_tools["runBlueprint"] = RunBlueprintTool(
                executor=blueprint_executor,
                observer=observer,
                tracer=tracer,
            )
        return AgentLoop(
            model_client=model_client,
            tool_dispatcher=dispatcher,
            context_assembler=context_assembler,
            session_store=session_store,
            tools_provider=_tools_provider,
            max_loop_iterations=settings.max_loop_iterations,
            max_wall_clock_seconds=settings.max_wall_clock_seconds,
            max_budget_windows=settings.max_budget_windows,
            token_budget=settings.model_context_window,
            max_tool_calls_per_iteration=settings.max_tool_calls_per_iteration,
            observer=observer,
            runtime_tools=runtime_tools,
            blueprint_executor=blueprint_executor,
        )

    # Close the neo4j driver pool on shutdown (design §2.4, N1: lifespan not the
    # deprecated on_event). Only closes when this app OWNS a `Neo4jVectorIndex` —
    # an injected `retrieval` (tests) owns its own store lifecycle.
    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if vector_index is not None:
                await vector_index.close()

    app = FastAPI(title="data-agent-runtime", lifespan=_lifespan)

    @app.post("/turn")
    async def turn(
        body: TurnRequest,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ) -> StreamingResponse:
        credentials = _extract_credentials(
            authorization=authorization, session_id=x_session_id, settings=settings
        )
        assert x_session_id is not None  # narrowed by _extract_credentials
        emitter = ProgressEmitter()
        agent_loop = _build_agent_loop(combine_observers(emitter.observe, _tracing_observer))

        # Best-effort AGENT-span turn index (design §7): a cheap, read-only
        # peek at the next turn_index AgentLoop.run() will itself assign —
        # never load-bearing for correctness, purely a telemetry label.
        peek_doc = await session_store.get_or_create_session(x_session_id)
        turn_index_hint = (peek_doc.messages[-1].turn_index + 1) if peek_doc.messages else 0

        async def _call() -> TurnOutcome:
            with tracing.agent_span(
                tracer, scope_hash=hash_scope(credentials.column_scope), turn_index=turn_index_hint
            ):
                return await agent_loop.run(
                    session_id=x_session_id, credentials=credentials, user_message=body.message
                )

        return StreamingResponse(_stream_turn(_call, emitter), media_type="text/event-stream")

    @app.post("/turn/resume")
    async def resume(
        body: ResumeRequest,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ) -> StreamingResponse:
        credentials = _extract_credentials(
            authorization=authorization, session_id=x_session_id, settings=settings
        )
        assert x_session_id is not None  # narrowed by _extract_credentials

        # Cheap, non-consuming pre-check for a clean 409 (D45): a genuinely
        # concurrent double-resume still races safely inside AgentLoop.resume
        # itself and is reported as an `error` SSE event by `_stream_turn`.
        doc, _ = await session_store.get_session_with_cas(x_session_id)
        if doc.pause_checkpoint is None or doc.pause_checkpoint.consumed:
            raise HTTPException(status_code=409, detail="No pending checkpoint for this session.")
        turn_index_hint = doc.messages[-1].turn_index if doc.messages else 0

        emitter = ProgressEmitter()
        agent_loop = _build_agent_loop(combine_observers(emitter.observe, _tracing_observer))

        async def _call() -> TurnOutcome:
            with tracing.agent_span(
                tracer, scope_hash=hash_scope(credentials.column_scope), turn_index=turn_index_hint
            ):
                return await agent_loop.resume(
                    session_id=x_session_id, credentials=credentials, answer=body.answer
                )

        return StreamingResponse(_stream_turn(_call, emitter), media_type="text/event-stream")

    return app


__all__ = ["ResumeRequest", "TurnRequest", "create_app"]
