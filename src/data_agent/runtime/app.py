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
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.auth.jwt_verify import JWTVerificationError, verify_jwt
from data_agent.runtime.blueprint.executor import BlueprintExecutor
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
from data_agent.runtime.config import (
    RuntimeSettings,
    effective_llm_hide,
    get_runtime_settings,
)
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.llm_summarizer import build_llm_summarizer
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolObserver
from data_agent.runtime.loop.agent_loop import AgentLoop, RuntimeTool, TurnOutcome
from data_agent.runtime.mcp.client import MCPClient
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.mcp.scratch_client import ScratchClient
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
from data_agent.runtime.session_history import project_history

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
        # UI Slice 1 (docs/decisions/ui-slice1-enriched-result-contract.md §1):
        # 5 additive, nullable fields. `sql`/`blueprint_use`/`verification` pass
        # through as-is; `result_table` serializes via `ResultPreview.to_doc()`;
        # `provenance` projects the fail-closed `frozenset[(db.table, column)]`
        # union to a sorted, deduped list of `"database.table.column"` strings.
        "sql": outcome.sql,
        "result_table": outcome.result_table.to_doc() if outcome.result_table else None,
        "blueprint_use": outcome.blueprint_use,
        "verification": outcome.verification,
        "provenance": (
            sorted(f"{db}.{col}" for db, col in outcome.provenance)
            if outcome.provenance is not None
            else None
        ),
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
    # The D93 scratch-write side-channel client (table-intermediate Slice 2). When
    # None AND `scratch_enabled`, a real `ScratchClient` is built from settings
    # (same MCP host). Pass a `FakeScratchClient` in a smoke test.
    scratch_client: Any = None,
    # Test-only seam (D-L3-5). Kept `Any` rather than `SpanExporter | None`
    # because the `/_test/spans` route below duck-types `get_finished_spans()`,
    # which lives on `InMemorySpanExporter`, not the `SpanExporter` base — typing
    # it to the base would force a cast/import at the route for no added safety.
    span_exporter: Any = None,
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
    # The scratch side-channel client is a singleton shared by every per-request
    # BlueprintExecutor (it holds no per-request state — creds ride each call).
    if scratch_client is None and settings.scratch_enabled:
        scratch_client = ScratchClient(settings.scratch_api_base())
    session_store = session_store or CouchbaseSessionStore(settings)
    model_client = model_client or build_openai_model_client(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        base_url=settings.openai_base_url,
    )

    # The effective LLM-content hide (config.effective_llm_hide): normally
    # `otlp_hide_llm_content` (D25 default True), but the master TELEMETRY DEBUG
    # switch `otlp_disable_redaction` forces the reveal — disabling redaction across
    # the board also shows the LLM Q/A + exception events, so a debugging operator
    # sees the whole turn. Access-controlled (makes the Phoenix project entity-
    # bearing); default keeps content hidden. Passed identically to
    # configure_tracing (exception scrubber) AND instrument_openai (attribute
    # TraceConfig) so both channels agree.
    hide_llm_content = effective_llm_hide(settings)

    tracer_provider = tracing.configure_tracing(
        otlp_endpoint=settings.otlp_endpoint,
        service_name=settings.otlp_service_name,
        # Phoenix groups traces by `openinference.project.name`, NOT service.name —
        # set the runtime's named project in code so a normal turn is findable in
        # the Phoenix UI (no OTEL_RESOURCE_ATTRIBUTES env hack).
        project_name=settings.otlp_project_name,
        # D25: when hiding LLM content, also install the LLMExceptionEventScrubber
        # (TraceConfig masks attributes, not the `exception` EVENT the OpenAI
        # instrumentor records — which embeds the response error body). Same gate
        # as instrument_openai's hide_content below.
        hide_llm_content=hide_llm_content,
        # Test-only seam (D-L3-5): an injected in-memory exporter captures the
        # manual AGENT/TOOL/CHAIN/GUARDRAIL spans this provider's tracer emits,
        # so the Layer-3 D25 scenario can dump + assert them PII-clean without a
        # Phoenix container. `None` in production → byte-identical provider.
        span_exporter=span_exporter,
    )
    # D25: hide the auto-instrumented OpenAI LLM span's raw prompt/completion by
    # default (the online per-turn Phoenix project is a shape/count/latency-only
    # surface — the completion embeds cell values / the query-derived answer). The
    # reveal is an explicit, access-controlled opt-in (`otlp_hide_llm_content`),
    # mirroring the learning loop's verbose gate.
    tracing.instrument_openai(tracer_provider, hide_content=hide_llm_content)
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
            # Access-controlled TELEMETRY DEBUG switch: when set, the TOOL span
            # carries the REAL args + result preview (not the D25 masked shape) so
            # a debugging operator sees the real tool call in Phoenix. Telemetry-
            # only — the dispatched call + enforced scope are unchanged.
            disable_redaction=settings.otlp_disable_redaction,
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
            # Access-controlled TELEMETRY DEBUG switch: reveals the real
            # concept/period values on the resolveValues span. Telemetry-only.
            disable_redaction=settings.otlp_disable_redaction,
        )
        # The runtime-tool registry (read-tools §2): `resolveValues` is always
        # wired; the three read tools are wired ONLY when the retrieval pipeline
        # is active — they share the one pipeline + store singleton, and carry
        # this request's observer/tracer for progress + the nested TOOL span.
        runtime_tools: dict[str, RuntimeTool] = {"resolveValues": composite}
        blueprint_executor: BlueprintExecutor | None = None
        if active_retrieval is not None:
            # `disable_redaction` (telemetry-only debug switch) reveals the real
            # `query` free text on these read-tool spans when set; default off
            # keeps the D25-redacted span.
            runtime_tools["searchBlueprints"] = SearchBlueprintsTool(
                pipeline=active_retrieval,
                default_k=settings.retrieval_search_default_k,
                max_k=settings.retrieval_search_max_k,
                preview_row_count=settings.preview_row_count,
                observer=observer,
                tracer=tracer,
                disable_redaction=settings.otlp_disable_redaction,
            )
            runtime_tools["searchKnowledge"] = SearchKnowledgeTool(
                pipeline=active_retrieval,
                knowledge_k=settings.retrieval_search_knowledge_k,
                preview_row_count=settings.preview_row_count,
                observer=observer,
                tracer=tracer,
                disable_redaction=settings.otlp_disable_redaction,
            )
            runtime_tools["getBlueprint"] = GetBlueprintTool(
                vector_index=active_retrieval.vector_index,
                preview_row_count=settings.preview_row_count,
                observer=observer,
                tracer=tracer,
                disable_redaction=settings.otlp_disable_redaction,
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
                resolve_via_gap_threshold=settings.resolve_via_gap_threshold,
                resolve_via_min_confidence=settings.resolve_via_min_confidence,
                preview_row_count=settings.preview_row_count,
                # Table-intermediate Slice 2: the materialize-and-join fast path.
                # `None` (scratch disabled/unwired) → table intermediates stay
                # UNSUPPORTED → raw loop (clean degrade).
                scratch_client=scratch_client,
                scratch_max_rows=settings.scratch_max_rows,
                scratch_max_columns=settings.scratch_max_columns,
                observer=observer,
            )
            runtime_tools["runBlueprint"] = RunBlueprintTool(
                executor=blueprint_executor,
                observer=observer,
                tracer=tracer,
                # Telemetry-only debug switch: reveals the real slot_bindings
                # values on the runBlueprint span when set; default off keeps the
                # D25 span (slot names only). The executor still gets raw args.
                disable_redaction=settings.otlp_disable_redaction,
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

    # Test-only span-dump endpoint (D-L3-5), registered ONLY when a
    # `span_exporter` is injected (the Layer-3 demo launcher's in-memory
    # exporter). Production passes `span_exporter=None`, so this route never
    # exists — the HTTP surface is byte-identical. It dumps each captured span's
    # name, kind, and attributes (keys AND stringified values) so the D25
    # scenario can assert PII-cleanliness OVER THE REAL VALUES — proving the
    # invariant, not merely that keys look benign. Safe to expose values here:
    # the endpoint is a test seam gated on the injected exporter, and the whole
    # point is to inspect what actually reaches a span attribute.
    if span_exporter is not None:

        @app.get("/_test/spans")
        async def _test_spans() -> dict[str, Any]:
            spans = span_exporter.get_finished_spans()
            dumped: list[dict[str, Any]] = []
            for s in spans:
                dumped.append(
                    {
                        "name": s.name,
                        "kind": s.attributes.get(
                            "openinference.span.kind"
                        ),
                        "attributes": {k: str(v) for k, v in s.attributes.items()},
                    }
                )
            return {"spans": dumped, "count": len(dumped)}

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

    @app.get("/session/history")
    async def session_history(
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ) -> JSONResponse:
        """UI Slice 3 (docs/decisions/ui-slice3-history-lineage-contract.md): a
        scope-filtered, read-only projection of the persisted `SessionDoc` into a
        `turns[]` transcript. Same `_extract_credentials` auth (401/400) as
        `/turn`; one store read; no CAS/loop/KV de-ref and no writes beyond the
        store's doc auto-create for an unknown (authenticated) session. The
        two D44 filters run over THIS request's `column_scope` inside
        `project_history` before any serialization, so a past turn's answer /
        tool-call is fail-closed dropped if it is no longer in scope — the read
        sibling of the live replay gate. An unknown session yields an empty doc →
        `turns: []` (never 404)."""
        credentials = _extract_credentials(
            authorization=authorization, session_id=x_session_id, settings=settings
        )
        assert x_session_id is not None  # narrowed by _extract_credentials
        doc = await session_store.get_or_create_session(x_session_id)
        body = project_history(
            doc.messages, doc.tool_trail, credentials.column_scope, doc.pause_checkpoint
        )
        return JSONResponse(content={"session_id": x_session_id, **body})

    return app


__all__ = ["ResumeRequest", "TurnRequest", "create_app"]
