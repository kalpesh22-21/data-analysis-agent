"""Persistent REAL agent-runtime server for the Phase-0 UI (`ui/`).

This is the production-shaped sibling of `scripts/run_ui_runtime.py`. That
launcher wires Layer-1 FAKES (a content-routed `DemoModelClient` + a scripted
`DemoMCPClient`) so the UI can be driven OFFLINE with no OpenAI key and no live
warehouse — every answer it returns is canned. THIS launcher keeps the SAME
`create_app`-based structure, the SAME host/port (:8000), and the SAME real
JWT-vs-l2-token verification posture, but injects the REAL components proven to
compose in `scripts/demo_runtime_turn_traced.py`, so the UI answers REAL
questions against real ClickHouse:

    - `model_client`  -> a REAL `build_openai_model_client` (key read/stripped
      from `.env`, model from `DEMO_MODEL` or a preflight-selected default). The
      model is picked once at startup by a synchronous OpenAI Responses preflight
      (like `demo_runtime_turn_traced.py`) so an account that lacks `gpt-5.5`
      transparently falls back to the first candidate it can actually call.
    - `mcp_client`    -> a REAL `RealMCPClient` -> the live l2-mcp
      (`http://localhost:18090/mcp`), so getTableSchema/runQuery hit real
      ClickHouse under the caller's JWT scope (D57/D80 enforced by the MCP).
    - `session_store` -> the REAL `CouchbaseSessionStore` (live l2-cb bucket
      `agent_sessions`), so sessions PERSIST across restarts and feed the
      learning loop. Built directly here at module import: the store's `__init__`
      does no I/O and needs no event loop (the `acouchbase` cluster is constructed
      by its first `_ensure_connected()`), so uvicorn's loop being absent at
      `app = build_real_app()` time is not a problem.
      Set `REAL_SESSION_STORE=memory` (or if the `couchbase` SDK is unimportable)
      to fall back to the in-memory store instead.
    - `catalog`       -> the REAL `CatalogHandle` rebuilt from the frozen
      catalog-export snapshot (D75 Wave 1b — `databaseSchemaDocs/` is gone; the
      snapshot is the SAME payload the MCP `GET /catalog/export` serves), so
      provenance for real warehouse tables is DETERMINED (a result with
      undetermined provenance is dropped by the D44 replay/scope filter). Set
      `CATALOG_FIXTURE_PATH` to a live `/catalog/export` dump to refresh.
    - JWT             -> REAL verification against the l2-token JWKS (NOT
      bypassed), exactly like `run_ui_runtime.py`. The BFF (`ui/server.py`) mints
      per-user-entitlement JWTs bound to the session id.
    - OTLP -> Phoenix : `otlp_endpoint=http://localhost:6006/v1/traces`, project
      `data-agent-runtime`. `otlp_hide_llm_content=True` is kept (the D25
      DEFAULT) — this is a real server, NOT the diagnostic demo, so the LLM
      span's raw prompt/completion is NOT revealed. Set `OTLP_DISABLE_REDACTION=1`
      to flip the master telemetry debug switch: Phoenix then shows the REAL tool
      calls (actual SQL WITH literals + the result preview) AND the LLM Q/A, for a
      debugging operator ONLY. It makes the Phoenix project entity-bearing, so
      access-control this server exactly like the audit store when the flag is on.
      Default OFF (D25 shape-only preserved). TELEMETRY-ONLY: it never weakens the
      MCP-enforced scope/PII posture (D5/D57) — only what Phoenix records.

Loop tunables are the PRODUCTION defaults (`max_loop_iterations=15`,
`max_wall_clock_seconds=60`, `max_budget_windows=3`) — NOT the scripted demo's
low `max_loop_iterations=3` — so a real multi-step question completes.

Retrieval (neo4j blueprint recall) is OFF by default: wiring it needs the neo4j
corpus seeded with an `embedding_model` that matches `RuntimeSettings`
(otherwise recall parity-filters to an empty corpus, see app.py's B2 warning),
which is a separate step. Set `REAL_RETRIEVAL=1` to opt in (points at l2-neo4j +
l2-embedding); leave it off for a plain real-turn server. This is a documented
follow-on, not a blocker for the core real turn.

Prerequisites (this launcher does NOT start/stop any container):
    - `.env` with `OPENAI_API_KEY=...` at the repo root.
    - The l2 integration stack UP: l2-mcp (:18090), l2-token (:19000),
      l2-cb (:8091/:11210), l2-phoenix (:6006). (l2-neo4j/l2-embedding only if
      `REAL_RETRIEVAL=1`.)

Run:
    uv run python scripts/run_ui_runtime_real.py
    # or, via the launcher, alongside the BFF:
    REAL=1 ./scripts/run_ui.sh
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from data_agent.http_daemon import run_http_daemon
from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings, effective_llm_hide
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.model.openai_client import build_openai_model_client
from data_agent.runtime.session.memory_store import InMemorySessionStore

# `_catalog` + `_e2e_harness` are sibling modules under `scripts/`. Put this script's
# own directory on `sys.path` so the imports resolve BOTH when run as
# `python scripts/x.py` AND when the file is loaded by path (importlib
# `spec_from_file_location`, e.g. tests).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _catalog import catalog_handle  # noqa: E402
from _e2e_harness import load_openai_key, pick_openai_model  # noqa: E402

# --- The live l2 stack (docker-compose.integration.yml), already running. We
# only READ its JWKS / call its MCP / write its Couchbase — nothing is modified.
_TOKEN_ISSUER = "http://token:8000/"  # must match the JWT's "iss" claim
_TOKEN_AUDIENCE = "clickhouse-api"
_JWKS_URL = "http://localhost:19000/.well-known/jwks.json"
_MCP_URL = os.environ.get("MCP_URL", "http://localhost:18090/mcp")

# Couchbase (l2-cb): bucket `agent_sessions`, collections `sessions` /
# `session_results`, seeded by scripts/couchbase-init.sh (admin/password).
_COUCHBASE_CONNECTION_STRING = "couchbase://localhost"
_COUCHBASE_USERNAME = "admin"
_COUCHBASE_PASSWORD = "password"

# OTLP -> self-hosted Phoenix (l2-phoenix); the runtime turn lands in this project.
_PHOENIX_OTLP = os.environ.get("OTLP_ENDPOINT", "http://localhost:6006/v1/traces")
_PHOENIX_PROJECT = "data-agent-runtime"

# Optional retrieval (REAL_RETRIEVAL=1) — l2-neo4j + l2-embedding (D71 mocks).
_NEO4J_URL = "bolt://localhost:7687"
_NEO4J_USERNAME = "neo4j"
_NEO4J_PASSWORD = "testpassword"
_EMBEDDING_API_URL = "http://localhost:18003/embed"

# The `.env` key read and the Responses-API model preflight are `_e2e_harness
# .load_openai_key` / `.pick_openai_model` — the SYNC preflight, which is what this
# launcher needs: it selects once at startup, before uvicorn's event loop exists, so a
# persistent server never re-selects per turn. `DEMO_MODEL` short-circuits the
# candidate list ("gpt-5.5", "gpt-4o", "gpt-4.1", "gpt-4o-mini").
_PREFLIGHT_LABEL = "[run_ui_runtime_real]"


def _build_session_store(settings: RuntimeSettings) -> tuple[Any, str]:
    """Prefer the real Couchbase store; fall back to in-memory on request or if
    the SDK is unavailable. Returns (store, human-readable choice).

    Constructed DIRECTLY: `CouchbaseSessionStore.__init__` does no I/O and touches
    no event loop, so building it here — at module import, before uvicorn's loop —
    is safe; the cluster is built by the store's first `_ensure_connected()`. This
    used to need a hand-written lazy proxy that re-declared every `SessionStore`
    method and drifted from the Protocol twice."""
    if os.environ.get("REAL_SESSION_STORE") == "memory":
        return InMemorySessionStore(), "InMemorySessionStore (REAL_SESSION_STORE=memory)"
    try:
        import acouchbase.cluster  # noqa: F401  (import probe only)
    except ImportError:
        return (
            InMemorySessionStore(),
            "InMemorySessionStore (couchbase SDK not importable)",
        )
    from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

    return CouchbaseSessionStore(settings), "CouchbaseSessionStore (l2-cb, lazy-connect)"


def build_real_app():
    api_key = load_openai_key()
    model = pick_openai_model(api_key, label=_PREFLIGHT_LABEL)

    retrieval_on = os.environ.get("REAL_RETRIEVAL") == "1"
    # Access-controlled TELEMETRY DEBUG switch (default OFF = D25 shape-only). When
    # OTLP_DISABLE_REDACTION=1, Phoenix shows the REAL tool calls (actual SQL WITH
    # literals + the result preview) AND the LLM Q/A — for a debugging operator
    # only. It makes the Phoenix project entity-bearing, so treat this server like
    # the audit store (access-controlled) when the flag is on.
    disable_redaction = os.environ.get("OTLP_DISABLE_REDACTION") == "1"

    settings = RuntimeSettings(
        _env_file=None,  # explicit wiring only — don't double-read .env
        mcp_url=_MCP_URL,
        openai_api_key=api_key,
        openai_model=model,
        openai_base_url="",
        # Real JWT verification against the live l2-token JWKS (NOT bypassed).
        jwks_url=_JWKS_URL,
        jwt_issuer=_TOKEN_ISSUER,
        jwt_audience=_TOKEN_AUDIENCE,
        # Couchbase session store (consulted only by the lazy store below).
        couchbase_connection_string=_COUCHBASE_CONNECTION_STRING,
        couchbase_username=_COUCHBASE_USERNAME,
        couchbase_password=_COUCHBASE_PASSWORD,
        # OTLP -> Phoenix, project data-agent-runtime, D25 default posture
        # (LLM span content HIDDEN — this is a real server, not the demo).
        otlp_endpoint=_PHOENIX_OTLP,
        otlp_project_name=_PHOENIX_PROJECT,
        otlp_hide_llm_content=True,
        # Default OFF: keep the D25 shape-only posture. OTLP_DISABLE_REDACTION=1
        # flips the master telemetry debug switch (real SQL + values + result +
        # LLM Q/A into Phoenix) — access-controlled debugging only.
        otlp_disable_redaction=disable_redaction,
        # Production loop tunables (NOT the scripted demo's low caps).
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        # Retrieval / scratch: both off unless REAL_RETRIEVAL=1.
        #
        # `scratch_enabled` was hardcoded False here while the comment claimed it
        # followed the flag. The effect was that a COMPOSED (table-intermediate)
        # blueprint could never run in the UI: `runBlueprint` reported "not supported
        # in this environment" and the model silently degraded to the raw loop. It is
        # gated on the SAME flag because the two are only useful together — a
        # scratch-join blueprint has to be FOUND by retrieval before it can
        # materialize anything.
        retrieval_enabled=retrieval_on,
        scratch_enabled=retrieval_on,
        neo4j_url=_NEO4J_URL if retrieval_on else "",
        neo4j_username=_NEO4J_USERNAME,
        neo4j_password=_NEO4J_PASSWORD,
        embedding_api_url=_EMBEDDING_API_URL if retrieval_on else "",
    )

    session_store, store_choice = _build_session_store(settings)

    print(f"[run_ui_runtime_real] mcp_url        = {settings.mcp_url}")
    print(f"[run_ui_runtime_real] session_store  = {store_choice}")
    # Print the EFFECTIVE hide (disable_redaction overrides otlp_hide_llm_content),
    # never the raw setting — otherwise the banner claims hide_llm_content=True while
    # OTLP_DISABLE_REDACTION=1 has actually revealed the LLM content.
    print(
        f"[run_ui_runtime_real] otlp_endpoint  = {settings.otlp_endpoint} "
        f"(project={settings.otlp_project_name}, "
        f"effective_llm_hide={effective_llm_hide(settings)})"
    )
    if settings.otlp_disable_redaction:
        print(
            "[run_ui_runtime_real] otlp_disable_redaction = ON — Phoenix will show "
            "REAL tool calls (SQL+values), results, and LLM Q/A. ACCESS-CONTROL this server."
        )
    print(f"[run_ui_runtime_real] retrieval      = {'ON (neo4j)' if retrieval_on else 'OFF'}")
    print(f"[run_ui_runtime_real] model          = {settings.openai_model}")

    # REAL components — mirrors demo_runtime_turn_traced.py's proven wiring. Every
    # dependency `create_app` would build itself from settings is built here too,
    # explicitly, so the wiring is auditable at one glance.
    return create_app(
        settings=settings,
        session_store=session_store,
        mcp_client=RealMCPClient(settings.mcp_url),
        model_client=build_openai_model_client(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
        ),
        catalog=catalog_handle(),
    )


app = build_real_app()

_logger = logging.getLogger(__name__)


if __name__ == "__main__":
    # `run_http_daemon`, not `uvicorn.run`: uvicorn RE-RAISES the SIGTERM it captured
    # once `serve()` returns, and the restored default disposition kills the process
    # right there — exit 143, with nothing after `serve()` reachable. The wrapper chains
    # that re-raise onto a handler of ours so a stop is exit 0, matching the four
    # non-HTTP workers (C3). See `data_agent/http_daemon.py`.
    #
    # `app` stays built at module scope (this launcher is also served as
    # `uvicorn scripts.run_ui_runtime_real:app`) and is passed as a factory returning it.
    raise SystemExit(
        run_http_daemon(
            lambda: app,
            host="0.0.0.0",
            port=8000,
            logger=_logger,
            process="ui-runtime-real",
            log_level="info",
        )
    )
