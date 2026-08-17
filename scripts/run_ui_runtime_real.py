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
      learning loop. Constructed lazily (the `acouchbase` cluster connects
      eagerly and needs a running event loop, but this app is built at module
      import before uvicorn's loop is up — see `_LazyCouchbaseSessionStore`).
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

import os
import sys
from pathlib import Path
from typing import Any

import openai
import uvicorn

from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings, effective_llm_hide
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.model.openai_client import build_openai_model_client
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import FinalizationBlockKind

# `_catalog` is a sibling module under `scripts/`. Put this script's own directory
# on `sys.path` so the import resolves BOTH when run as `python scripts/x.py` AND
# when the file is loaded by path (importlib `spec_from_file_location`, e.g. tests).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _catalog import catalog_handle  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent

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

# Model preflight candidates (mirrors demo_runtime_turn_traced.py): the first
# the account can actually call on the Responses API wins.
_MODEL_CANDIDATES = ("gpt-5.5", "gpt-4o", "gpt-4.1", "gpt-4o-mini")


def _load_openai_key() -> str:
    """Read + strip the OPENAI_API_KEY from `.env` (quotes tolerated)."""
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


def _pick_openai_model(api_key: str) -> str:
    """First candidate the account can call on the Responses API (sync preflight).

    Run once at startup (before uvicorn's loop) so a persistent server does not
    have to re-select per turn. `DEMO_MODEL` short-circuits the list.
    """
    client = openai.OpenAI(api_key=api_key)
    candidates = (os.environ["DEMO_MODEL"],) if os.environ.get("DEMO_MODEL") else _MODEL_CANDIDATES
    last_err: Exception | None = None
    for model in candidates:
        try:
            client.responses.create(model=model, input=[{"role": "user", "content": "ping"}])
            print(f"[run_ui_runtime_real] model preflight OK: {model!r}")
            return model
        except openai.NotFoundError as exc:
            print(f"[run_ui_runtime_real] {model!r} unavailable (404) — trying next")
            last_err = exc
        except Exception as exc:  # noqa: BLE001 - preflight is best-effort selection
            print(f"[run_ui_runtime_real] {model!r} errored ({type(exc).__name__}) — trying next")
            last_err = exc
    raise SystemExit(f"No OpenAI model candidate worked: {last_err}")


class _LazyCouchbaseSessionStore:
    """Construct the real `CouchbaseSessionStore` on FIRST async use.

    The `acouchbase` cluster connects EAGERLY at construction and requires a
    RUNNING event loop, but this launcher builds the app at module import
    (`app = build_real_app()`), before uvicorn's loop is up. This thin proxy
    defers the real construction to the first awaited method (always inside a
    request, where the loop is running) and delegates every `SessionStore` call
    to it verbatim. Same pattern as `run_ui_runtime.py`'s wrapper.

    MAINTENANCE: this class is a HAND-WRITTEN stand-in for the `SessionStore`
    Protocol, and nothing in `tests/` imports this launcher (module import builds
    the app, which reads `.env` and preflights OpenAI). A method added to the
    Protocol and forgotten here is therefore invisible to CI and fails only against
    the live server — which is exactly what happened twice (`read_full_result`, then
    Release 1's `apply_analysis_state`). `tests/runtime/test_launcher_session_store_proxies.py`
    now DERIVES the required surface from the Protocol and fails if it is missing
    here; keep the delegation complete rather than relying on anyone noticing.
    """

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings
        self._inner: Any = None

    def _store(self) -> Any:
        if self._inner is None:
            from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

            self._inner = CouchbaseSessionStore(self._settings)
        return self._inner

    async def get_or_create_session(self, session_id: str) -> Any:
        return await self._store().get_or_create_session(session_id)

    async def load_trail(self, session_id: str) -> Any:
        return await self._store().load_trail(session_id)

    async def append_message(self, session_id: str, message: Any) -> None:
        await self._store().append_message(session_id, message)

    async def append_trail_entry(self, session_id: str, entry: Any) -> None:
        await self._store().append_trail_entry(session_id, entry)

    async def bump_last_activity(self, session_id: str) -> None:
        await self._store().bump_last_activity(session_id)

    async def write_full_result(
        self, session_id: str, result_id: str, result_full: dict[str, Any]
    ) -> str:
        return await self._store().write_full_result(session_id, result_id, result_full)

    async def read_full_result(self, session_id: str, result_full_ref: str) -> Any:
        # The read-back half of `write_full_result`. It was missing while the write
        # half was present, so every caller of the read path hit an AttributeError
        # here rather than in the real store — `GET /session/history` 500'd once it
        # started de-referencing blueprint results. A hand-maintained proxy silently
        # drifts from the protocol it stands in for; add new SessionStore methods
        # here too.
        return await self._store().read_full_result(session_id, result_full_ref)

    async def write_pause_checkpoint(self, session_id: str, checkpoint: Any) -> None:
        await self._store().write_pause_checkpoint(session_id, checkpoint)

    async def apply_analysis_state(self, session_id: str, turn_index: int, merge: Any) -> Any:
        # Release 1 (03 §B.1). Missing here for the whole of Release 1's first live
        # run: every `updateAnalysisState` call raised AttributeError on THIS class
        # and surfaced as RUNTIME_TOOL_INTERNAL_ERROR, while the suite stayed green
        # because nothing in `tests/` imports this launcher. `merge` is forwarded as
        # the CALLBACK it is — never called here — so the real store's CAS retry
        # re-invokes it against its own fresh read.
        return await self._store().apply_analysis_state(session_id, turn_index, merge)

    async def claim_finalization_block(
        self,
        session_id: str,
        turn_index: int,
        window_count: int,
        kind: FinalizationBlockKind,
    ) -> bool:
        # Release 1 (05 §C.1). Same omission, same run: without it the forced
        # finalization re-round could not be claimed at all. `kind` (05 §J.3) is
        # REQUIRED and forwarded: it selects WHICH per-window allowance is being
        # claimed, so a proxy that dropped it would collapse the two gates back
        # onto one budget against the real server only.
        return await self._store().claim_finalization_block(
            session_id, turn_index, window_count, kind
        )

    async def get_session_with_cas(self, session_id: str) -> Any:
        return await self._store().get_session_with_cas(session_id)

    async def resume_checkpoint(self, session_id: str, cas: Any, answer: str) -> Any:
        return await self._store().resume_checkpoint(session_id, cas, answer)

    async def scan_idle_sessions(
        self, *, statuses: list[str], last_activity_before: str, limit: int
    ) -> Any:
        return await self._store().scan_idle_sessions(
            statuses=statuses, last_activity_before=last_activity_before, limit=limit
        )

    async def transition_learning_status(
        self,
        session_id: str,
        expected_from: str,
        to: str,
        cas: Any,
        *,
        content_hash: str | None = None,
        assert_from: bool = True,
    ) -> Any:
        return await self._store().transition_learning_status(
            session_id,
            expected_from,
            to,
            cas,
            content_hash=content_hash,
            assert_from=assert_from,
        )


def _build_session_store(settings: RuntimeSettings) -> tuple[Any, str]:
    """Prefer the real Couchbase store; fall back to in-memory on request or if
    the SDK is unavailable. Returns (store, human-readable choice)."""
    if os.environ.get("REAL_SESSION_STORE") == "memory":
        return InMemorySessionStore(), "InMemorySessionStore (REAL_SESSION_STORE=memory)"
    try:
        import acouchbase.cluster  # noqa: F401  (import probe only)
    except ImportError:
        return (
            InMemorySessionStore(),
            "InMemorySessionStore (couchbase SDK not importable)",
        )
    return _LazyCouchbaseSessionStore(settings), "CouchbaseSessionStore (l2-cb, lazy-connect)"


def build_real_app():
    api_key = _load_openai_key()
    model = _pick_openai_model(api_key)

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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
