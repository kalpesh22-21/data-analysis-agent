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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.auth.jwt_verify import JWTVerificationError, verify_jwt
from data_agent.runtime.blueprint.executor import BlueprintExecutor
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.catalog.export_client import build_catalog_cache
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.composite.record_assumptions import RecordAssumptionsTool
from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
from data_agent.runtime.config import (
    RuntimeSettings,
    effective_llm_hide,
    get_runtime_settings,
)
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.discovery_emulation import (
    EmulatedDiscovery,
    EmulatedDiscoveryCache,
    build_emulated_discovery,
)
from data_agent.runtime.context.llm_summarizer import build_llm_summarizer
from data_agent.runtime.dispatch.tool_dispatcher import (
    CatalogProvider,
    ToolDispatcher,
    ToolObserver,
)
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
from data_agent.runtime.observability.progress_summarizer import ProgressSummarizer
from data_agent.runtime.observability.redaction import hash_scope
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.query_page import (
    QueryPageError,
    build_page_sql,
    clamp_page_params,
)
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


class QueryPageRequest(BaseModel):
    """`POST /query/page` — one page of the model-designated answer table.

    `limit`/`offset` are permissive (`Any`, clamped by `clamp_page_params`) rather
    than validated ints: a bad paging param is UI plumbing, not a reason to 422 a
    user's scroll. `sql` is the only field that can fail the request.
    """

    sql: str
    limit: Any = None
    offset: Any = None


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
        # additive, nullable fields. `sql_executed`/`answer_sql`/`blueprint_use`/
        # `verification` pass through as-is; `provenance` projects the fail-closed
        # `frozenset[(db.table, column)]` union to a sorted, deduped list of
        # `"database.table.column"` strings.
        #
        # `sql_executed` (was `sql`) is EVERY query the turn ran — the audit list,
        # including probes and intermediate steps. `answer_sql` is the ONE query the
        # model designated as the answer via `presentTable`; the UI runs that itself
        # against `POST /query/page` and renders it with real paging. The two are
        # deliberately distinct: what ran, versus what the answer IS.
        #
        # `result_table` is GONE. It carried a fixed ~20-row `ResultPreview` of
        # whichever query happened to run last — unpageable, and chosen by the
        # runtime rather than the model.
        "sql_executed": outcome.sql_executed,
        "answer_sql": outcome.answer_sql,
        "blueprint_use": outcome.blueprint_use,
        "verification": outcome.verification,
        "provenance": (
            sorted(f"{db}.{col}" for db, col in outcome.provenance)
            if outcome.provenance is not None
            else None
        ),
        # recordAssumptions (docs/decisions/ui-assumptions-contract.md): the
        # model-declared, plain-English assumptions behind this answer. Pass-through
        # list-or-`None` (the `[] -> None` fork mirrors `sql`), so an old client
        # ignores the unknown key.
        "assumptions": outcome.assumptions,
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
    # Additional `ToolObserver`s folded into the per-request observer chain
    # (Release 1, 07 §B.1). The Layer-4 eval harness reads §06's routing
    # telemetry, and there was no way in: the chain is built INSIDE the request
    # handlers below, and SSE is not a fallback (`observability/progress.py`'s
    # `to_progress_event` drops every event it does not recognise, which is most
    # of §06's set).
    #
    # Without this seam the harness has to hand-assemble an `AgentLoop`,
    # duplicating `_build_agent_loop` — so Layer 4 would test a runtime that is
    # not the shipped one and would drift silently. Worse, `ContextAssembler`
    # takes `base_system_prompt: str | None = None` and the Layer-1 tests run
    # promptless by default, so a hand-assembled harness can run with NO SYSTEM
    # PROMPT AT ALL and nothing signals it — disqualifying for a suite whose
    # purpose is testing the prompt.
    #
    # Observers are called for their side effects only and must not raise; they
    # are appended AFTER the progress emitter and the tracing observer, so a slow
    # or noisy recorder can never displace either.
    extra_observers: Sequence[ToolObserver] = (),
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

    # LLM-generated progress summaries (opt-in, `progress_summary_enabled`). Built
    # ONCE here from a SECOND, cheap `OpenAIModelClient` on `openai_summary_model`
    # (its own lightweight instance sharing the OpenAI api_key/base_url). Wired ONLY
    # when the flag is on AND an OpenAI key is present — absent either it stays
    # `None`, the AgentLoop skips summarization, and behavior is byte-identical to
    # today (no extra LLM call, no D25 relaxation). The extra call is auto-covered by
    # `instrument_openai` below, like every other OpenAI round-trip.
    progress_summarizer: ProgressSummarizer | None = None
    if settings.progress_summary_enabled and settings.openai_api_key:
        summary_model_client = build_openai_model_client(
            api_key=settings.openai_api_key,
            model=settings.openai_summary_model,
            base_url=settings.openai_base_url,
        )
        progress_summarizer = ProgressSummarizer(
            summary_model_client,
            timeout_seconds=settings.progress_summary_timeout_seconds,
        )

    # The effective LLM-content hide (config.effective_llm_hide): normally
    # `otlp_hide_llm_content` (D25 amended 2026-07-15 — now defaults FALSE, i.e.
    # REVEAL), and the master TELEMETRY DEBUG switch `otlp_disable_redaction` also
    # forces the reveal. So by default the whole turn's LLM Q/A + exception events are
    # visible — the Phoenix project is entity-bearing BY DEFAULT and MUST be
    # access-controlled; hiding is the explicit opt-OUT (`otlp_hide_llm_content=True`).
    # Passed identically to configure_tracing (exception scrubber) AND instrument_openai
    # (attribute TraceConfig) so both channels agree.
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
        # Design §7 noise reduction: drop the per-turn plumbing spans
        # (context.assembly / loop_model_call_start / loop_turn_done by default) at
        # the exporter so Phoenix shows only the meaningful spans. Name-based +
        # tunable via RuntimeSettings.otlp_drop_span_names ([] disables it). This
        # only changes WHICH spans export — not what CONTENT a kept span carries.
        drop_span_names=settings.otlp_drop_span_names,
    )
    # D25 amended 2026-07-15: the auto-instrumented OpenAI LLM span's raw prompt/
    # completion is REVEALED by default (the online per-turn Phoenix project is
    # entity-bearing BY DEFAULT — the completion embeds cell values / the query-derived
    # answer — and MUST be access-controlled). Hiding is the explicit opt-OUT
    # (`otlp_hide_llm_content=True`), mirroring the learning loop's verbose gate.
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

    # D75 Wave 1b: the runtime rebuilds its catalog handle from the MCP
    # `/catalog/export` (via the process-wide `CatalogCache`) instead of a local
    # `databaseSchemaDocs/` copy. An INJECTED `catalog` handle (tests / a fixed
    # catalog) is honored verbatim and used as a fixed handle — no cache, no fetch.
    # Otherwise a `CatalogCache` is built from settings (`catalog_source`: live 'mcp'
    # export or the offline 'fixture' JSON) and exposed to the dispatcher/composite as
    # an async provider that resolves the immutable handle from THIS turn's
    # credentials. The cache is lazy (no fetch at construction — import/build-time
    # safety preserved) and fetches exactly once (scope-independent, D5-safe:
    # credentials authenticate the fetch only, never entering a handle).
    #
    # Reader-only runtime (singleton-hydrator redesign): the runtime NO LONGER seeds the
    # neo4j graph on the request path — the independent `replicas:1` hydrator daemon
    # (scripts/run_hydrator.py) owns ALL seeding + the nuke/rebuild. The catalog cache is
    # KEPT (it builds the per-turn provenance handle via `ToolDispatcher._resolve_catalog`
    # → `capture_provenance`), but with NO `on_catalog_loaded` seed callback. The cache's
    # HTTP client is built with `service_key=settings.mcp_service_key` (via
    # `build_catalog_cache`), so the runtime's catalog-handle fetch uses the STATIC
    # service key — a full decouple, no user JWT ever reaches the MCP export. The
    # per-turn `get_catalog_handle(jwt=, session_id=)` still passes the request creds,
    # but the service-key client ignores them (the export is scope-independent + cached).
    catalog_provider: CatalogHandle | CatalogProvider
    if catalog is not None:
        catalog_provider = catalog
    else:
        catalog_cache = build_catalog_cache(settings)

        async def _catalog_provider(credentials: RuntimeCredentials) -> CatalogHandle:
            return await catalog_cache.get_catalog_handle(
                jwt=credentials.jwt, session_id=credentials.session_id
            )

        catalog_provider = _catalog_provider

    tool_schema_cache = ToolSchemaCache(mcp_client)
    summarizer = build_llm_summarizer(model_client)
    context_assembler = ContextAssembler(
        session_store,
        history_token_budget=settings.history_token_budget(),
        preview_row_count=settings.preview_row_count,
        summarizer=summarizer,
        retrieval=active_retrieval,
        base_system_prompt=settings.effective_agent_system_prompt(),
        tracer=tracer,
    )

    # B5: TOOL spans are emitted by ToolDispatcher itself (wired via the
    # `tracer=` constructor argument below); this only covers AgentLoop's own
    # (non-tool) `loop_*` stage boundaries as lightweight GUARDRAIL events,
    # through a strict attribute allowlist (never a bare type-filter — see
    # `tracing.guardrail_observer`'s docstring for why that matters for
    # `loop_paused_ask_user`'s `question` payload specifically).
    _tracing_observer = tracing.guardrail_observer(tracer)

    # Emulated-discovery sweep cache — built ONCE per app, NOT per request, so it
    # actually spans a session. `_build_agent_loop` runs per request, so a cache
    # created in there would memoize nothing across turns and the sweep would still
    # re-dispatch on every budget window.
    discovery_emulation_cache = EmulatedDiscoveryCache()

    async def _tools_provider(credentials: RuntimeCredentials) -> list[dict[str, Any]]:
        # The live MCP authenticates tools/list too (no anonymous
        # introspection) — thread this turn's credentials through, but the
        # catalogue itself is scope-independent and cached by
        # ToolSchemaCache after the first successful fetch (see its
        # docstring).
        return await tool_schema_cache.get_schemas(
            jwt=credentials.jwt, session_id=credentials.session_id
        )

    def _build_dispatcher(observer: ToolObserver) -> ToolDispatcher:
        """The ONE `ToolDispatcher` construction, shared by the agent loop and the
        `/query/page` endpoint. Extracted so the paging endpoint provably runs the
        SAME enforced path the model does — same catalog provider, same scope
        enforcement, same denial mapping, same telemetry — rather than a
        near-identical copy that could drift apart from it."""
        return ToolDispatcher(
            mcp_client,
            catalog_provider,
            preview_row_count=settings.preview_row_count,
            # Per-result preview size cap (2026-08 fix): bounds a single stored tool
            # result (esp. a wide getTableSchema) so the trail cannot balloon.
            max_tool_result_tokens=settings.max_tool_result_tokens,
            observer=observer,
            tracer=tracer,
            # Access-controlled TELEMETRY DEBUG switch: when set, the TOOL span
            # carries the REAL args + result preview (not the D25 masked shape) so
            # a debugging operator sees the real tool call in Phoenix. Telemetry-
            # only — the dispatched call + enforced scope are unchanged.
            disable_redaction=settings.otlp_disable_redaction,
        )

    def _build_agent_loop(observer: ToolObserver) -> AgentLoop:
        dispatcher = _build_dispatcher(observer)
        # Emulated-discovery injection (context/discovery_emulation.py): a per-
        # window closure that sweeps listDatabases+listTables through the SAME
        # per-request `dispatcher` (so D5/D57/denial-mapping/telemetry are the
        # free path) and returns the synthetic rendered entries + guard signatures.
        # Gated on the setting; `None` (disabled) → AgentLoop runs byte-identically
        # to before this feature.
        discovery_emulation_provider = None
        if settings.discovery_emulation_enabled:

            async def _discovery_emulation_provider(
                creds: RuntimeCredentials,
            ) -> EmulatedDiscovery:
                # ONCE PER SESSION: `_run_loop` is re-entered by run()/resume()/the
                # blueprint approval-resume, so without this the sweep re-dispatched
                # to the MCP on every budget window. The cache serves the first
                # non-empty sweep for the rest of the session; a degraded one is not
                # memoized, so a transient MCP blip retries next window.
                return await discovery_emulation_cache.get_or_build(
                    creds.session_id,
                    lambda: build_emulated_discovery(
                        dispatcher,
                        creds,
                        base_database=settings.base_database,
                        preview_row_count=settings.preview_row_count,
                        observer=observer,
                    ),
                )

            discovery_emulation_provider = _discovery_emulation_provider
        # D77: the composite wraps the SAME dispatcher (so its inner runQuery
        # shares the per-request observer/tracer and the free D5/D57/provenance
        # path); an injected `resolve_values` (Layer-1 smoke test) overrides it.
        composite = resolve_values or ResolveValuesComposite(
            tool_dispatcher=dispatcher,
            catalog=catalog_provider,
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
        # `recordAssumptions` (docs/decisions/ui-assumptions-contract.md): ALWAYS
        # wired — it has no backing stack (it only echoes the model's plain-English
        # assumptions into the turn result), so it is never subject to the
        # advertised-but-unwired `RUNTIME_TOOL_UNAVAILABLE` path.
        runtime_tools["recordAssumptions"] = RecordAssumptionsTool()
        # `answerWithTable` (composite/answer_with_table.py): ALWAYS wired, same
        # reasoning — it only echoes the model's final prose + designated query into
        # the turn result and has no backing stack. It is TERMINAL: a successful call
        # ends the turn, so the model does not spend a further round-trip restating
        # an answer it already wrote. The UI pages the designated query itself via
        # `POST /query/page`.
        runtime_tools["answerWithTable"] = AnswerWithTableTool()
        # `updateAnalysisState` (Release 1, composite/analysis_state.py): ALWAYS
        # wired — its only dependency is the session store, so it is never subject
        # to the advertised-but-unwired `RUNTIME_TOOL_UNAVAILABLE` path. Unlike the
        # two composite tools above it takes `observer` and `tracer` and self-emits
        # its dispatch events, exactly like the three retrieval read tools below:
        # `_run_runtime_tool` emits nothing on a composite tool's behalf, so
        # copying the `RecordAssumptionsTool()` shape would leave the one feature
        # whose telemetry IS the deliverable completely mute.
        runtime_tools["updateAnalysisState"] = UpdateAnalysisStateTool(
            session_store=session_store, observer=observer, tracer=tracer
        )
        blueprint_executor: BlueprintExecutor | None = None
        if active_retrieval is not None:
            # `disable_redaction` (telemetry-only debug switch) reveals the real
            # `query` free text on these read-tool spans when set; default off
            # keeps the D25-redacted span.
            #
            # `max_result_tokens` is the SAME per-result preview cap the dispatcher
            # takes. It is passed explicitly because `_build_preview` defaults it:
            # without it these tools were pinned to the 4,000-token default however
            # the operator configured `max_tool_result_tokens`, and the one shape
            # that hits the cap is an enriched `searchBlueprints` card list at a
            # large `k`.
            runtime_tools["searchBlueprints"] = SearchBlueprintsTool(
                pipeline=active_retrieval,
                default_k=settings.retrieval_search_default_k,
                max_k=settings.retrieval_search_max_k,
                preview_row_count=settings.preview_row_count,
                observer=observer,
                tracer=tracer,
                disable_redaction=settings.otlp_disable_redaction,
                max_result_tokens=settings.max_tool_result_tokens,
            )
            runtime_tools["searchKnowledge"] = SearchKnowledgeTool(
                pipeline=active_retrieval,
                knowledge_k=settings.retrieval_search_knowledge_k,
                preview_row_count=settings.preview_row_count,
                observer=observer,
                tracer=tracer,
                disable_redaction=settings.otlp_disable_redaction,
                max_result_tokens=settings.max_tool_result_tokens,
            )
            runtime_tools["getBlueprint"] = GetBlueprintTool(
                vector_index=active_retrieval.vector_index,
                preview_row_count=settings.preview_row_count,
                observer=observer,
                tracer=tracer,
                disable_redaction=settings.otlp_disable_redaction,
                max_result_tokens=settings.max_tool_result_tokens,
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
            # Total-request fit budget (2026-08 fix): the FULL canonical request is
            # fit to (model_context_window - response_token_reserve) before every
            # send_turn so the leading base prompt is never front-truncated out.
            request_token_budget=settings.request_token_budget(),
            max_tool_calls_per_iteration=settings.max_tool_calls_per_iteration,
            observer=observer,
            runtime_tools=runtime_tools,
            blueprint_executor=blueprint_executor,
            discovery_emulation_provider=discovery_emulation_provider,
            progress_summarizer=progress_summarizer,
        )

    # Close the neo4j driver pool on shutdown (design §2.4, N1: lifespan not the
    # deprecated on_event). Only closes when this app OWNS a `Neo4jVectorIndex` —
    # an injected `retrieval` (tests) owns its own store lifecycle.
    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # The runtime is a pure READER: it never seeds/nukes the graph (the singleton
        # hydrator daemon owns that). The lifespan only closes the neo4j driver pool this
        # app OWNS on shutdown (an injected `retrieval` owns its own store lifecycle).
        try:
            yield
        finally:
            if vector_index is not None:
                await vector_index.close()

    app = FastAPI(title="data-agent-runtime", lifespan=_lifespan)

    # Readiness gate (singleton-hydrator redesign): an UNAUTHENTICATED probe the runtime
    # pod exposes so it stays out of the Service until the hydrator has seeded the graph.
    # "Ready" = the `:CorpusMeta.corpus_sha` singleton is present (a completed seed). When
    # THIS app owns no `Neo4jVectorIndex` (Neo4j absent / Phase-0 parity), there is
    # nothing to seed → always ready. Deliberately does NOT call `_extract_credentials`
    # (a k8s probe carries no JWT). Liveness stays a TCP probe (the process is up even
    # while the graph is cold), so hydrator lag never restarts a pod — only de-routes it.
    @app.get("/ready")
    async def ready() -> JSONResponse:
        if vector_index is None:
            return JSONResponse(status_code=200, content={"ready": True})
        ok = await vector_index.graph_ready()
        return JSONResponse(status_code=200 if ok else 503, content={"ready": ok})

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
                        "kind": s.attributes.get("openinference.span.kind"),
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
        agent_loop = _build_agent_loop(
            combine_observers(emitter.observe, _tracing_observer, *extra_observers)
        )

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
        agent_loop = _build_agent_loop(
            combine_observers(emitter.observe, _tracing_observer, *extra_observers)
        )

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
        # `blueprint_id -> terminal_sql` for every blueprint that ran successfully in
        # this session, so a turn whose answer table was designated BY BLUEPRINT can
        # still expose `answer_sql` and be re-paged after a reload. The de-reference
        # is done HERE, not in `project_history`, because that projection is a pure
        # function over the doc and the value lives behind a D46 KV pointer.
        #
        # Cost is bounded by the number of successful runBlueprint calls in the
        # session (typically 0-2), and a missing/expired ref simply leaves that id
        # unresolved — the turn then reports `answer_sql: null`, exactly as it did
        # before this existed.
        blueprint_terminal_sql: dict[str, str] = {}
        for entry in doc.tool_trail:
            if (
                entry.status != "ok"
                or entry.tool_name != "runBlueprint"
                or entry.result_full_ref is None
            ):
                continue
            try:
                result_full = await session_store.read_full_result(
                    x_session_id, entry.result_full_ref
                )
            except Exception:
                # DEGRADE, never 500. This is a pure read path whose job is to
                # rebuild a transcript; one unreadable blueprint result must cost
                # that turn its `answer_sql`, not the whole session's history. (A
                # store proxy missing this method did exactly that once.)
                _logger.warning(
                    "history: could not read full result %s for blueprint answer_sql "
                    "(session=%s) — that turn reports answer_sql=null",
                    entry.result_full_ref,
                    x_session_id,
                    exc_info=True,
                )
                continue
            if isinstance(result_full, dict):
                bp_id = result_full.get("blueprint_id")
                terminal_sql = result_full.get("terminal_sql")
                if isinstance(bp_id, str) and isinstance(terminal_sql, str) and terminal_sql:
                    blueprint_terminal_sql[bp_id] = terminal_sql
        body = project_history(
            doc.messages,
            doc.tool_trail,
            credentials.column_scope,
            doc.pause_checkpoint,
            blueprint_terminal_sql=blueprint_terminal_sql,
        )
        return JSONResponse(content={"session_id": x_session_id, **body})

    @app.post("/query/page")
    async def query_page(
        body: QueryPageRequest,
        authorization: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    ) -> JSONResponse:
        """Execute the model-designated `answer_sql` and return ONE page of rows.

        This is what replaced the old `result_table` field: instead of the runtime
        shipping a fixed ~20-row `ResultPreview` the user could not page past, the
        model designates the answer query via `presentTable` and the UI pages
        through it here.

        It adds NO authority. The query runs through the SAME
        `ToolDispatcher.dispatch("runQuery", ...)` the model uses, with THIS
        caller's credentials — so column-scope (D57/D80), read-only enforcement,
        row caps, denial mapping and provenance capture are the identical code
        path. A designated query can never read a column the same caller could not
        already reach by asking the agent. See `runtime/query_page.py`.

        Paging is applied by WRAPPING the SQL via sqlglot
        (`SELECT * FROM (<sql>) LIMIT n OFFSET m`), never by splicing a LIMIT onto
        model text — which also rejects multi-statement and non-SELECT payloads
        before dispatch. A rejection is a 400 with a STATIC message (never the
        offending SQL, never a raw parser message). A denial from the MCP is
        returned as the dispatcher's own canned `user_message`, exactly as the
        model would have seen it.
        """
        credentials = _extract_credentials(
            authorization=authorization, session_id=x_session_id, settings=settings
        )
        limit, offset = clamp_page_params(body.limit, body.offset)
        try:
            page_sql = build_page_sql(body.sql, limit=limit, offset=offset)
        except QueryPageError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

        dispatcher = _build_dispatcher(_tracing_observer)
        result = await dispatcher.dispatch("runQuery", {"sql": page_sql}, credentials)
        if result.status != "ok" or result.result_preview is None:
            # Denied/errored: surface the dispatcher's canned, PII-safe message —
            # the same one the model would have received. Never the raw backend text.
            return JSONResponse(
                status_code=403 if result.status == "denied" else 502,
                content={"error": result.user_message or "The query could not be run.",
                         "error_code": result.error_code},
            )
        preview = result.result_preview
        return JSONResponse(
            content={
                "columns": list(preview.columns),
                "rows": [list(row) for row in preview.preview_rows],
                "limit": limit,
                "offset": offset,
                # `has_more` is a HINT derived from a full page, not a total count:
                # counting all rows would mean a second aggregate query per page.
                # The UI shows "next" while a page comes back full.
                "has_more": len(preview.preview_rows) >= limit,
            }
        )

    return app


__all__ = ["QueryPageRequest", "ResumeRequest", "TurnRequest", "create_app"]
