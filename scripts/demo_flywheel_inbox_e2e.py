#!/usr/bin/env python
"""demo_flywheel_inbox_e2e — a REAL, human-in-the-loop end-to-end walk of the
Track-B learning FLYWHEEL against the LIVE stack (real OpenAI + l2-mcp +
Couchbase + Neo4j + ClickHouse, all already UP).

This is NOT a pytest test (the real LLM is nondeterministic) — it is a DEMO
driver, a sibling of `scripts/demo_learning_e2e_openai.py` (with which it shares
its whole live rig — the env block, `build_infra`, `teardown`, the catalog, the
mint, the sweep helper, the model preflight, the emission printer — through
`scripts/_e2e_harness.py`) and of `scripts/run_ui_runtime_real.py` (whose
`create_app` runtime wiring it drives in-process). It walks THREE parts, each
failing HONESTLY (print + return) if the real model does not cooperate — a valid
real-LLM outcome, not a hard error:

  PART A — a LIVE runtime turn: ask -> rectify intent -> a real session trail.
  PART B — learn from that live session, then HUMAN-ACCEPT via the review inbox
           (the `sampler=True` coin routes the blueprint to `in_review`, so a
           human `inbox.approve(...)` — not an auto-promotion — lands it).
  PART C — a VARIANT question (a DIFFERENT filter) through the SAME in-process
           runtime, and the GOVERNED-CORPUS TRUST GATE holding.

           An inbox-approved blueprint lands in the `source='learning'` STAGING
           tier (`learning/promotion/landing.py`), NOT the trusted MCP canon,
           and agent recall serves `source='mcp'` ONLY — the fail-closed gate in
           `runtime/retrieval/vector_index.py`. So the learned blueprint is
           deliberately NOT recallable end-to-end yet, whatever its
           status/drift_status: it must first be PROMOTED to canon (a human
           verify -> re-source to mcp; the governed-corpus Phase-3 hop, not
           built). PART C therefore shows the RAW-path answer plus the gate
           doing its job — not an autoplay. If the fast path DOES fire, it fired
           off the pre-existing MCP-canon corpus, not off what PART B landed.

Run (from the repo root, the l2 stack UP):

    uv run python scripts/demo_flywheel_inbox_e2e.py
    # keep the landed blueprint + inbox state for inspection (skip teardown):
    KEEP=1 uv run python scripts/demo_flywheel_inbox_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

# `_e2e_harness` + `_catalog` are SIBLING modules under `scripts/`. Put this script's
# own directory on `sys.path` so the imports resolve BOTH when run as
# `python scripts/x.py` AND when the file is loaded by path (importlib
# `spec_from_file_location`, e.g. tests).
sys.path.insert(0, str(Path(__file__).resolve().parent))

# --------------------------------------------------------------------------- env
# Load OPENAI_API_KEY from .env (strip surrounding quotes) and set the FULL live
# env block the E2E test documents, BEFORE any settings object is constructed.

from _catalog import catalog_dict, catalog_handle  # noqa: E402
from _e2e_harness import (  # noqa: E402
    CATALOG,
    DEMO_COLUMN_SCOPE,
    EMBEDDING_MODEL,
    OLD_TS,
    CapturingExtractor,
    Infra,
    apply_live_env,
    build_infra,
    embedding_url,
    load_openai_key,
    mcp_url,
    mint_bound_token,
    neo4j_auth,
    neo4j_uri,
    park_foreign_idle_sessions,
    parse_sse,
    pick_openai_model_async,
    print_model_emission,
    teardown,
)

_OPENAI_KEY = load_openai_key()

apply_live_env()

import httpx  # noqa: E402

from data_agent.learning.candidate.models import mint_candidate_id  # noqa: E402
from data_agent.learning.config import learning_enabled  # noqa: E402
from data_agent.learning.extractor.grounding import (  # noqa: E402
    known_rule_ids_from_catalog,
    rule_index_from_catalog,
)
from data_agent.learning.factory import (  # noqa: E402
    build_learning_consumer,
    build_promotion_write_plane,
)
from data_agent.learning.inbox.inbox import InboxTransitionError  # noqa: E402
from data_agent.learning.models import LearningStatus, compute_content_hash  # noqa: E402
from data_agent.learning.observability import (  # noqa: E402
    configure_learning_tracing,
    get_learning_tracer,
)
from data_agent.learning.promotion.landing import landing_id  # noqa: E402
from data_agent.learning.sweeper import LearningSweeper  # noqa: E402
from data_agent.runtime.app import create_app  # noqa: E402
from data_agent.runtime.config import RuntimeSettings  # noqa: E402
from data_agent.runtime.model.openai_client import build_openai_model_client  # noqa: E402
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex  # noqa: E402

# --------------------------------------------------------------------------- consts
# The live-stack wiring (env block, stores, queue, mint, teardown, model preflight)
# lives in `_e2e_harness`; what stays here is what THIS demo is about.

# Runtime JWT verification against the live l2-token JWKS (mirrors
# run_ui_runtime_real.py). The MCP still enforces scope/session live (D57/D80).
_JWKS_URL = "http://localhost:19000/.well-known/jwks.json"
_TOKEN_ISSUER = "http://token:8000/"
_TOKEN_AUDIENCE = "clickhouse-api"
_PHOENIX_OTLP = os.environ.get("OTLP_ENDPOINT", "http://localhost:6006/v1/traces")

# PART A: ask, then rectify intent, in ONE session -> a real correction trail.
_QUESTION_A = "what is the total annual salary for the Sales department?"
# A schema-supported intent rectification that stays within the bound column scope
# (`DEMO_COLUMN_SCOPE`): swap the metric total->average on the same column
# and filter the model already used, so turn 2 converges cleanly (no new schema,
# no out-of-scope column) and the session stays learnable.
_RECTIFY_A = "actually, I want the AVERAGE annual salary per employee in Sales, not the total."
# PART C: the VARIANT — same shape, DIFFERENT filter. It does NOT autoplay what PART B
# landed: that blueprint is in the `source='learning'` staging tier and recall serves
# `source='mcp'` only (the trust gate), so the raw path is the CORRECT outcome here.
_QUESTION_C = "what is the average annual salary per employee in the Engineering department?"

# --------------------------------------------------------------------------- runtime


def _build_runtime_app(infra: Infra, model: str):
    """Build the REAL in-process runtime `AgentLoop` (via `create_app`) EXACTLY as
    `run_ui_runtime_real.py` wires it, with retrieval turned ON so a landed
    blueprint is recallable. The SAME live Couchbase session store + RealMCPClient
    that `build_infra` opened are reused (one event loop, one bucket) so the
    session a runtime turn writes is the SAME doc the learning sweeper later reads.
    `create_app` builds the Neo4jVectorIndex + RetrievalPipeline + runBlueprint
    executor itself from these settings (retrieval_enabled + neo4j_url + embedder)."""
    settings = RuntimeSettings(
        _env_file=None,
        mcp_url=mcp_url(),
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
        # the corpus/landing stamp (EMBEDDING_MODEL) or recall parity-filters to an empty set.
        retrieval_enabled=True,
        scratch_enabled=False,
        neo4j_url=neo4j_uri(),
        neo4j_username=neo4j_auth()[0],
        neo4j_password=neo4j_auth()[1],
        embedding_api_url=embedding_url(),
        embedding_model=EMBEDDING_MODEL,
    )
    print(
        f"[RUNTIME] create_app(retrieval=ON, mcp={mcp_url()}, model={model!r}, "
        f"embedding_model={EMBEDDING_MODEL!r})"
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
    _progress, result, error = parse_sse(resp.text)
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
    _progress, result, error = parse_sse(resp.text)
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
    # The `/turn` result contract (runtime/app.py): `sql` was split into
    # `sql_executed` (EVERY query the turn ran — the audit list) and `answer_sql`
    # (the ONE query the model designated via `presentTable`), and `result_table`
    # — a runtime-chosen 20-row preview — was replaced by `answer_tables`.
    executed = result.get("sql_executed") or []
    print(f"  sql_executed   : {len(executed)} query(ies)")
    for q in executed:
        print(f"    - {q}")
    print(f"  answer_sql     : {result.get('answer_sql')}")
    for i, tbl in enumerate(result.get("answer_tables") or []):
        print(
            f"  answer_tables[{i}]: sql={tbl.get('sql')!r} caption={tbl.get('caption')!r} "
            f"verification={tbl.get('verification')}"
        )
    assumptions = result.get("assumptions") or []
    for a in assumptions:
        print(f"  assumption     : {a}")
    bpu = result.get("blueprint_use")
    if bpu is not None:
        print(f"  blueprint_use  : {bpu}")


def _looks_like_ran_query(result: dict | None) -> bool:
    """A turn 'actually ran a query and answered' iff it EXECUTED read-only SQL (or
    played a blueprint) and came back with prose.

    Reads `sql_executed`, not the retired `sql`/`result_table` pair. The check is
    deliberately NOT `answer_sql`/`answer_tables`: those carry the model's
    `presentTable` designation, which is advisory and is legitimately absent for a
    SCALAR answer — and "the total annual salary for Sales" is exactly that. Gating
    on the designation made a perfectly good turn (three tool calls, a real number
    in the prose) read as "the model did not run a query".
    """
    if result is None:
        return False
    ran = bool(result.get("sql_executed")) or bool(result.get("blueprint_use"))
    return ran and bool(result.get("assistant_text"))


# --------------------------------------------------------------------------- run


async def _run() -> int:
    if not learning_enabled():
        raise SystemExit("LEARNING_ENABLED must be truthy")

    model = await pick_openai_model_async(_OPENAI_KEY)

    # Learning-plane tracing (Phoenix project `learning-loop`). The runtime app
    # below wires its OWN provider (`data-agent-runtime`); the learning stages
    # take the tracer as an explicit seam and run NO-OP without it — which is
    # exactly what this demo silently did until 2026-08-18.
    provider = configure_learning_tracing(otlp_endpoint=_PHOENIX_OTLP)
    tracer = get_learning_tracer(provider)
    print(f"[TRACE] Phoenix OTLP exporter -> {_PHOENIX_OTLP} (service.name=learning-loop)")

    infra = await build_infra(stream_prefix="demo:flywheel")

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

        jwt_a = await mint_bound_token(DEMO_COLUMN_SCOPE, sid_a)
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
        doc_a.last_activity = OLD_TS
        doc_a.learning_status = LearningStatus.ACTIVE
        await infra.session_store._upsert_doc(sid_a, doc_a)
        parked = await park_foreign_idle_sessions(infra, sid_a)
        print(f"[STAGE B1] idled session for sweep; parked {parked} foreign idle session(s)")

        sweeper = LearningSweeper(infra.session_store, infra.queue, infra.settings, tracer=tracer)
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
        # ONE parse of the frozen export, projected two ways (the id SET the `rule`
        # role is validated against + the INDEX the unknown-id matcher reads) — they
        # must come from the same catalog or they can disagree about what exists.
        demo_catalog = catalog_dict()
        consumer = build_learning_consumer(
            infra.settings,
            tracer=tracer,
            session_store=infra.session_store,
            queue=infra.queue,
            model_client=build_openai_model_client(api_key=_OPENAI_KEY, model=model, base_url=""),
            audit_store=infra.audit_store,
            candidate_store=infra.candidate_store,
            blueprint_corpus=infra.corpus_store,
            user_store=infra.user_store,
            catalog_schema=CATALOG,
            embedder=infra.embedder,
            known_rules=known_rule_ids_from_catalog(demo_catalog),
            # Without this the hint machinery is INERT here (the factory says so at
            # INFO and carries on) and an unknown rule id declines terminally.
            rule_index=rule_index_from_catalog(demo_catalog),
            sampler=lambda _env: True,  # route the blueprint to the HUMAN inbox
        )
        capturing = CapturingExtractor(consumer._extractor)
        consumer._extractor = capturing
        consumed = await consumer.run_once()
        print(
            f"[STAGE B3] CONSUMED (done={consumed.done}) with the REAL {model!r} extractor "
            "(sampler=True -> in_review)"
        )
        print_model_emission(capturing.last_result)

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
            tracer=tracer,
            candidate_store=infra.candidate_store,
            hit_counts=infra.corpus_store,
            mcp_client=infra.mcp_client,
            token_minter=infra.token_minter,
            neo4j_driver=infra.neo4j_driver,
            embedding_client=infra.embedder,
            model_id=EMBEDDING_MODEL,
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
        print("# [PART C] a VARIANT question through the RUNTIME + the TRUST GATE holding")
        print("#   the PART B blueprint landed in the source='learning' STAGING tier;")
        print("#   recall serves source='mcp' ONLY, so it is correctly NOT served until")
        print("#   promoted to canon. Expect the RAW path — that is the gate working.")
        print("#" * 72)

        jwt_c = await mint_bound_token(DEMO_COLUMN_SCOPE, sid_c)
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
                f"\n[STAGE C2] FAST PATH FIRED: blueprint_id={bpu.get('blueprint_id')} "
                f"slots={slots}"
            )
            print(
                "           NOTE: recall serves source='mcp' only, so this came from the "
                "MCP-CANON corpus — it is NOT the blueprint PART B just landed (that one "
                "is source='learning' staging). Compare the id above with keys['blueprint_id']."
            )
            if any(str(v).lower() == "engineering" for v in slots.values()):
                print(
                    "           slots bound to department=Engineering — a canon blueprint "
                    "generalized across the filter."
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
                "\n[STAGE C2] the runtime took the RAW path (no runBlueprint) — the EXPECTED "
                "outcome. What PART B landed is a source='learning' STAGING node and recall "
                "serves source='mcp' only, so the learned blueprint is correctly not served "
                "until it is PROMOTED to canon. The question is still answered, from raw tools."
            )

        # Deterministic recall BACKSTOP (like the learning demo's STAGE 5): does the
        # variant question vector recall the just-landed blueprint node at all?
        print("\n[STAGE C3] deterministic recall backstop (Neo4jVectorIndex.recall):")
        print(
            "           NOTE: this is the GATED recall the agent uses — its Cypher carries "
            "`AND node.source = 'mcp'`. A source='learning' staging node is filtered out "
            "BEFORE scoring, so absence here is the trust gate, not a ranking result."
        )
        query_vector = (await infra.embedder.embed([_QUESTION_C]))[0]
        index = Neo4jVectorIndex(
            url=neo4j_uri(),
            auth=neo4j_auth(),
            expected_model=EMBEDDING_MODEL,
            timeout_seconds=15.0,
        )
        try:
            recalled = await index.recall(query_vector=query_vector, kind="blueprint", k=30)
            landed = next((c for c in recalled if c.id == node_id), None)
            if landed is None:
                print(
                    f"           CORRECTLY WITHHELD: the learned blueprint {node_id} was not "
                    f"served for {_QUESTION_C!r} — it is source='learning' staging and this "
                    f"recall serves source='mcp' only. Recall returned {len(recalled)} canon "
                    f"node(s). It ranks in the RAW vector index (verified live 2026-08-18); the "
                    f"trust gate, not semantic distance, is what keeps it out. THE GATE HELD."
                )
            else:
                print(
                    f"           RECALLED: {_QUESTION_C!r} surfaced {node_id} "
                    f"(uses={sorted(landed.uses)}) — meaning this node is source='mcp' CANON. "
                    f"For a freshly-landed learning node that would be a TRUST-GATE FAILURE; "
                    f"expected only once the promotion hop (staging -> canon) exists and ran."
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
            await teardown(infra)
            print("\n[TEARDOWN] cleaned created session/candidate/corpus/neo4j artifacts.")
        provider.force_flush()


async def _finish(infra, keys, part_a, part_b, part_c) -> None:  # noqa: ANN001
    print("\n" + "=" * 70)
    print(">>> [SUMMARY]")
    print("=" * 70)
    print(f"  PART A (live runtime turn + rectify): {'DONE' if part_a else 'not reached'}")
    print(f"  PART B (learn + human inbox approve): {'DONE' if part_b else 'not reached'}")
    # `part_c` tracks ONLY whether the blueprint fast path fired — which, under the
    # source='mcp' trust gate, can only ever be a CANON blueprint, never what PART B
    # landed. A raw-path PART C is the expected, correct outcome (see STAGE C2/C3),
    # so this line must not read as a failure; it is also False when PART C never ran.
    print(f"  PART C (variant turn; canon fast path): {'FIRED' if part_c else 'not fired'}")
    print("  key ids:")
    for k, v in keys.items():
        print(f"    {k:14s}: {v}")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
