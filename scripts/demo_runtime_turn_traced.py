#!/usr/bin/env python
"""demo_runtime_turn_traced — a REAL normal user interaction driven through the
online agent runtime (`create_app`) and traced to Phoenix.

This is NOT a pytest test (the real LLM is nondeterministic). It stands up the
REAL runtime composition root with REAL dependencies:

  1. a REAL OpenAI model client (`build_openai_model_client`, key from `.env`,
     model `gpt-5.5` with a small preflight fallback like the learning demo),
  2. a REAL `RealMCPClient` -> the live l2-mcp (streamable-HTTP), which runs a
     REAL ClickHouse query against the seeded `dbpcm_warehouse.employee` table
     the MCP live tests use,
  3. the REAL catalog rebuilt from the frozen catalog-export snapshot (D75 Wave
     1b — `databaseSchemaDocs/` is gone; `catalog_source="fixture"` reads the
     committed export, provenance) + a real minimal in-memory session store
     (`InMemorySessionStore` — the production store, not a fake),
  4. a scoped JWT minted via the live l2-token IdP (bound to the session id,
     like the MCP live tests / conftest).

Tracing is exported to Phoenix (`OTLP_ENDPOINT=http://localhost:6006/v1/traces`)
and lands in the `data-agent-runtime` project — the project name is now set IN
CODE by `configure_tracing(project_name=...)` (no OTEL_RESOURCE_ATTRIBUTES hack).

Unlike the learning loop, a runtime TURN is already ONE connected trace: the
`agent.turn` AGENT span wraps the whole turn and the TOOL/CHAIN spans (plus the
OpenInference OpenAI LLM span from `instrument_openai`) nest under it via the
ambient context — so NO cross-process propagation is needed.

D25: the MANUAL runtime spans (AGENT/TOOL/CHAIN/GUARDRAIL) are SHAPE-ONLY (no
verbose flag; SQL literals + bound-slot values already redacted). The auto
OpenAI LLM span's raw prompt/completion is hidden BY DEFAULT in the library
(`RuntimeSettings.otlp_hide_llm_content=True`). This DIAGNOSTIC demo OPTS IN to
revealing it (`otlp_hide_llm_content=False`) — like the learning-loop verbose
gate — so the question/answer are visible in Phoenix. Set DEMO_HIDE_LLM_CONTENT=1
to run the D25-safe DEFAULT posture (LLM span content-free) instead.

Run (from the repo root, the l2 stack + Phoenix UP):

    # opt-in reveal (LLM content visible on the span):
    DEMO_MODEL=gpt-5.5 uv run python scripts/demo_runtime_turn_traced.py
    # D25-safe default (LLM content hidden on the span):
    DEMO_MODEL=gpt-5.5 DEMO_HIDE_LLM_CONTENT=1 uv run python scripts/demo_runtime_turn_traced.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

# `_e2e_harness` is a SIBLING module under `scripts/`. Put this script's own directory
# on `sys.path` so the import resolves BOTH when run as `python scripts/x.py` AND when
# the file is loaded by path (importlib `spec_from_file_location`, e.g. tests).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _e2e_harness import (  # noqa: E402
    PHOENIX_GRAPHQL,
    PHOENIX_OTLP,
    PHOENIX_UI,
    load_openai_key,
    mint_bound_token,
    parse_sse,
    pick_openai_model_async,
    span_attrs,
)

_OPENAI_KEY = load_openai_key()

# The runtime reads the OTLP endpoint from settings; export the online turn's
# spans to Phoenix. Set BEFORE any RuntimeSettings is constructed.
os.environ["OTLP_ENDPOINT"] = PHOENIX_OTLP

import httpx  # noqa: E402

from data_agent.runtime.app import create_app  # noqa: E402
from data_agent.runtime.config import RuntimeSettings  # noqa: E402
from data_agent.runtime.observability import tracing  # noqa: E402
from data_agent.runtime.session.memory_store import InMemorySessionStore  # noqa: E402

# The live l2-mcp (docker-compose.integration.yml); the l2-token IdP endpoints and the
# tenant claims the mint carries live in `_e2e_harness`.
_JWKS_URL = "http://localhost:19000/.well-known/jwks.json"
_TOKEN_ISSUER = "http://token:8000/"
_TOKEN_AUDIENCE = "clickhouse-api"
_MCP_URL = os.environ.get("MCP_TEST_URL", "http://localhost:18090/mcp")

_QUESTION = "How many employees are in the Sales department?"

_LLM_CONTENT_KEYS = (
    "llm.input_messages",
    "llm.output_messages",
    "input.value",
    "output.value",
    "llm.prompts",
)


async def _confirm_phoenix(trace_id: str | None, *, hide_content: bool) -> None:
    query = (
        "{ projects { edges { node { name traceCount recordCount "
        "spans(first: 1000, sort: {col: startTime, dir: desc}) { edges { node { "
        "name spanKind spanId parentId startTime attributes context { traceId spanId } } } } "
        "} } } }"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(PHOENIX_GRAPHQL, json={"query": query})
        resp.raise_for_status()
        data = resp.json()
    projects = data.get("data", {}).get("projects", {}).get("edges", [])
    print("=" * 70)
    print(">>> PHOENIX TRACE CONFIRMATION")
    print("=" * 70)
    target = next((e["node"] for e in projects if e["node"]["name"] == "data-agent-runtime"), None)
    if target is None:
        print("  project 'data-agent-runtime' NOT found. Projects present:")
        for edge in projects:
            print(f"    - {edge['node']['name']} (traces={edge['node']['traceCount']})")
        print(f"  Open the Phoenix UI to inspect: {PHOENIX_UI}")
        return
    spans = [e["node"] for e in target["spans"]["edges"]]
    print(
        f"  project: 'data-agent-runtime'  traceCount={target['traceCount']} "
        f"recordCount={target['recordCount']}  spans retrieved={len(spans)}"
    )

    def _tid(s):  # noqa: ANN001
        return (s.get("context") or {}).get("traceId")

    by_trace: dict[str, list] = {}
    for s in spans:
        by_trace.setdefault(_tid(s), []).append(s)

    # Prefer THE trace we captured at emit time; else the newest trace that has
    # an `agent.turn` AGENT root (the runtime turn we just drove).
    chosen = trace_id if trace_id in by_trace else None
    if chosen is None:
        agent_spans = [s for s in spans if s["name"] == "agent.turn"]
        if agent_spans:
            chosen = _tid(agent_spans[0])  # spans are startTime-desc → newest first
    if chosen is None:
        print(
            "  no `agent.turn` trace found yet (ingestion lag?). "
            f"Open {PHOENIX_UI} (project: data-agent-runtime)."
        )
        return

    members = by_trace.get(chosen, [])
    print(f"\n  ── traceId {chosen}  ({len(members)} spans) ──")
    by_span = {s["spanId"]: s for s in members}
    children: dict[str | None, list] = {}
    for s in members:
        parent = s.get("parentId")
        parent = parent if parent in by_span else None
        children.setdefault(parent, []).append(s)

    def _print(node_id, depth):  # noqa: ANN001
        for s in children.get(node_id, []):
            indent = "    " + "  " * depth
            attrs = span_attrs(s)
            extra = ""
            if s["name"].startswith("tool."):
                extra = f"  (status={attrs.get('tool.status')})"
            print(f"{indent}└─ {s['name']} [{s.get('spanKind')}]{extra}")
            _print(s["spanId"], depth + 1)

    _print(None, 0)

    # Is there a real OpenInference OpenAI LLM span? (Phoenix may return spanKind
    # lower-cased, so compare case-insensitively.)
    llm = [s for s in members if (s.get("spanKind") or "").upper() == "LLM"]
    print(
        f"\n  OpenInference OpenAI LLM span(s) in this trace: {len(llm)}"
        + (f"  ({', '.join(sorted({s['name'] for s in llm}))})" if llm else "")
    )

    # D25 proof: does the LLM span carry raw prompt/completion content?
    print(
        f"\n  D25 posture: otlp_hide_llm_content={hide_content} "
        f"({'DEFAULT (content hidden)' if hide_content else 'OPT-IN reveal (content shown)'})"
    )
    for s in llm:
        attrs = span_attrs(s)
        blob = json.dumps(attrs)
        present = [k for k in _LLM_CONTENT_KEYS if k in attrs]
        carries_question = _QUESTION in blob
        carries_sales = "Sales" in blob
        print(
            f"    - {s['name']}: content_attr_keys_present={present or 'NONE'} | "
            f"carries_question_text={carries_question} | carries_'Sales'={carries_sales} | "
            f"non-content shape kept: model_name={attrs.get('llm.model_name')!r} "
            f"total_tokens={attrs.get('llm.token_count.total')}"
        )
    print(f"\n  Open the Phoenix UI: {PHOENIX_UI}  (project: data-agent-runtime)")
    print("=" * 70)


async def _run() -> int:
    model = await pick_openai_model_async(_OPENAI_KEY)

    settings = RuntimeSettings(
        _env_file=None,
        mcp_url=_MCP_URL,
        openai_api_key=_OPENAI_KEY,
        openai_model=model,
        otlp_endpoint=PHOENIX_OTLP,
        otlp_project_name="data-agent-runtime",
        # D25 opt-in reveal (like the learning-loop verbose gate): this DIAGNOSTIC
        # demo defaults to SHOWING the LLM prompt/completion on the span so the
        # question/answer are visible in Phoenix. The library/production default is
        # hide-on (content-free). Flip to hide-on with DEMO_HIDE_LLM_CONTENT=1 to
        # prove the D25-safe default posture.
        otlp_hide_llm_content=os.environ.get("DEMO_HIDE_LLM_CONTENT") == "1",
        jwks_url=_JWKS_URL,
        jwt_issuer=_TOKEN_ISSUER,
        jwt_audience=_TOKEN_AUDIENCE,
        # Catalog from the frozen export snapshot (D75 Wave 1b — `databaseSchemaDocs/`
        # is gone). This demo has no MCP catalog JWT wiring, so read the committed
        # fixture rather than the live `/catalog/export`; `create_app` then rebuilds
        # the CatalogHandle from it. Point `CATALOG_FIXTURE_PATH` at a live dump to refresh.
        catalog_source="fixture",
        # Minimal: no retrieval/embedding/scratch wiring for this bare count turn.
        retrieval_enabled=False,
        scratch_enabled=False,
    )

    # Capture the TracerProvider create_app builds internally so we can
    # force_flush it after the turn (create_app owns it and does not return it).
    captured: dict[str, object] = {}
    _real_configure = tracing.configure_tracing

    def _capturing(**kwargs):  # noqa: ANN003
        provider = _real_configure(**kwargs)
        captured["provider"] = provider
        return provider

    tracing.configure_tracing = _capturing  # type: ignore[assignment]
    try:
        app = create_app(settings=settings, session_store=InMemorySessionStore())
    finally:
        tracing.configure_tracing = _real_configure  # type: ignore[assignment]

    provider = captured["provider"]
    print(f"[TRACE] Phoenix OTLP exporter -> {PHOENIX_OTLP} (project=data-agent-runtime)")

    sid = f"runtime-turn-{uuid.uuid4().hex[:16]}"
    # Allow-all (D80b `[]`), session-bound: this demo drives ONE count question and
    # the MCP still enforces the tenant claims + row policies the token carries.
    jwt = await mint_bound_token([], sid, allow_unscoped=True)

    # Capture the traceId of THIS turn's agent.turn root by wrapping agent_span.
    trace_id_holder: dict[str, str] = {}
    _real_agent_span = tracing.agent_span

    def _capturing_agent_span(tracer, **kwargs):  # noqa: ANN001, ANN003
        cm = _real_agent_span(tracer, **kwargs)

        class _Wrap:
            def __enter__(self):
                span = cm.__enter__()
                ctx = span.get_span_context()
                trace_id_holder["trace_id"] = format(ctx.trace_id, "032x")
                return span

            def __exit__(self, *a):
                return cm.__exit__(*a)

        return _Wrap()

    tracing.agent_span = _capturing_agent_span  # type: ignore[assignment]

    # Drive ONE real user turn through the HTTP surface (POST /turn, SSE).
    from fastapi.testclient import TestClient

    print(f"\n[TURN] POST /turn  session={sid!r}")
    print(f"[TURN] question: {_QUESTION!r}")
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/turn",
                headers={"Authorization": f"Bearer {jwt}", "X-Session-Id": sid},
                json={"message": _QUESTION},
            )
    finally:
        tracing.agent_span = _real_agent_span  # type: ignore[assignment]

    progress, result, error = parse_sse(resp.text)
    print(f"[TURN] HTTP {resp.status_code}; progress steps: {[p.get('step') for p in progress]}")

    print("\n" + "=" * 70)
    print(">>> THE REAL RUNTIME ANSWER")
    print("=" * 70)
    if error is not None:
        print(f"  ERROR event: {error}")
    if result is not None:
        print(f"  status         : {result.get('status')}")
        print(f"  tool_calls_made: {result.get('tool_calls_made')}")
        print(f"  assistant_text : {result.get('assistant_text')!r}")
    print("=" * 70)

    # Flush the BatchSpanProcessor so spans export before we query Phoenix.
    provider.force_flush()  # type: ignore[attr-defined]
    print(
        f"\n[SUMMARY] model={model!r} | traceId={trace_id_holder.get('trace_id')} | "
        f"otlp_hide_llm_content={settings.otlp_hide_llm_content}"
    )
    await asyncio.sleep(5.0)  # Phoenix ingestion lag
    await _confirm_phoenix(
        trace_id_holder.get("trace_id"), hide_content=settings.otlp_hide_llm_content
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
