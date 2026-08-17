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

from _catalog import catalog_dict  # noqa: E402
from _e2e_harness import (  # noqa: E402
    CATALOG,
    DEPT_COL,
    EMBEDDING_MODEL,
    OLD_TS,
    PHOENIX_GRAPHQL,
    PHOENIX_OTLP,
    PHOENIX_UI,
    SALARY_COL,
    TABLE,
    CapturingExtractor,
    apply_live_env,
    build_infra,
    load_openai_key,
    mint_bound_token,
    neo4j_auth,
    neo4j_uri,
    park_foreign_idle_sessions,
    pick_openai_model_async,
    print_model_emission,
    span_attrs,
    teardown,
)

_OPENAI_KEY = load_openai_key()

apply_live_env(
    # Turn the D25 verbose gate ON for this DIAGNOSTIC run so the spans carry the
    # human-readable content (question / accepted SQL / learned intent). This makes
    # the learning-loop Phoenix project entity-bearing — a controlled demo posture.
    LEARNING_TRACE_VERBOSE="1",
)

import httpx  # noqa: E402
from openinference.semconv.trace import OpenInferenceSpanKindValues  # noqa: E402

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
from data_agent.learning.models import LearningStatus, compute_content_hash  # noqa: E402
from data_agent.learning.observability import (  # noqa: E402
    configure_learning_tracing,
    context_from_traceparent,
    get_learning_tracer,
    learning_recall_span,
)
from data_agent.learning.promotion.landing import landing_id  # noqa: E402
from data_agent.learning.promotion.models import policy_from_settings  # noqa: E402
from data_agent.learning.sweeper import LearningSweeper  # noqa: E402
from data_agent.runtime.model.openai_client import build_openai_model_client  # noqa: E402
from data_agent.runtime.observability.tracing import span  # noqa: E402
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex  # noqa: E402
from data_agent.runtime.session.models import SessionDoc, TrailEntry, TurnMessage  # noqa: E402

# --------------------------------------------------------------------------- consts
# The live-stack wiring (env block, stores, queue, mint, teardown, model preflight)
# lives in `_e2e_harness`; what stays here is what THIS demo is about.

_QUESTION = "what is the total annual salary for the Sales department?"
_ACCEPTED_SQL = f"SELECT sum(AnnualSalary) AS total_salary FROM {TABLE} WHERE Department = 'Sales'"
_RELATED_QUESTION = "how much total salary does a department pay its employees"


def _closed_session(sid: str) -> SessionDoc:
    return SessionDoc(
        session_id=sid,
        created_at=OLD_TS,
        last_activity=OLD_TS,
        learning_status=LearningStatus.ACTIVE,
        messages=[
            TurnMessage(0, "user", _QUESTION, OLD_TS, frozenset()),
            TurnMessage(0, "assistant", "Sales earned 147000 in total.", OLD_TS, frozenset()),
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
                ts=OLD_TS,
            )
        ],
    )


async def _confirm_phoenix(sid: str) -> None:
    query = (
        "{ projects { edges { node { name traceCount recordCount "
        "spans(first: 1000, sort: {col: startTime, dir: desc}) { edges { node { "
        "name spanKind spanId parentId attributes context { traceId spanId } } } } "
        "} } } }"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(PHOENIX_GRAPHQL, json={"query": query})
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
        print(f"  Open the Phoenix UI to inspect: {PHOENIX_UI}")
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

    session_traces = {_trace_id(s) for s in spans if span_attrs(s).get("session.id") == sid}
    if not session_traces:
        print(
            f"  no spans found for session.id={sid!r} yet (ingestion lag?). "
            f"Open {PHOENIX_UI} (project: learning-loop)."
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
            attrs = span_attrs(s)
            hits = {k: attrs[k] for k in wanted if k in attrs}
            if not hits:
                continue
            if not printed_header:
                print("    human-readable (verbose) attrs:")
                printed_header = True
            for k, v in hits.items():
                print(f"      {s['name']}.{k} = {v!r}")

    print(f"\n  Open the Phoenix UI: {PHOENIX_UI}  (project: learning-loop)")
    print("=" * 70)


async def _run() -> int:
    if not learning_enabled():
        raise SystemExit("LEARNING_ENABLED must be truthy")

    model = await pick_openai_model_async(_OPENAI_KEY)

    # ---------------------------------------------------------------- tracing
    provider = configure_learning_tracing(otlp_endpoint=PHOENIX_OTLP)
    tracer = get_learning_tracer(provider)
    print(f"[TRACE] Phoenix OTLP exporter -> {PHOENIX_OTLP} (service.name=learning-loop)")

    infra = await build_infra(stream_prefix="demo:e2e")
    tag = uuid.uuid4().hex[:10]
    sid = f"demo-{tag}"

    try:
        # ============================================================ STAGE 1
        probe_session = f"demo-probe-{tag}"
        probe_jwt = await mint_bound_token([SALARY_COL, DEPT_COL], probe_session)
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
        parked = await park_foreign_idle_sessions(infra, sid)
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
        # ONE parse of the frozen export, projected two ways: the id SET the `rule`
        # role is validated against, and the INDEX the unknown-id matcher reads. Both
        # must come from the same catalog or they can disagree about what exists.
        demo_catalog = catalog_dict()
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
            catalog_schema=CATALOG,
            embedder=infra.embedder,
            # Rule-role grounding from the frozen catalog-export snapshot (D75 Wave 1b;
            # `databaseSchemaDocs/` is gone). Dev demo → reads the committed fixture.
            known_rules=known_rule_ids_from_catalog(demo_catalog),
            # Without this the hint machinery is INERT here (the factory says so at
            # INFO and carries on): a plan citing a rule id that does not exist
            # declines `missing_rule` terminally, never re-asked, even when the
            # catalog names the same concept under another id.
            rule_index=rule_index_from_catalog(demo_catalog),
            sampler=lambda _env: False,
        )
        # Wrap the factory-built real extractor to capture what the model emits.
        capturing = CapturingExtractor(consumer._extractor)
        consumer._extractor = capturing

        consumed = await consumer.run_once()
        print(
            f"[STAGE 3] CONSUMED (done={consumed.done}) with the REAL {model!r} extractor [traced]"
        )

        print_model_emission(capturing.last_result)

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

        # =================================================== STAGE 4 — ROUTE, APPROVE, LAND
        # The SHIPPED policy (plan §4): threshold 1, and the cron routes to `in_review`
        # rather than validating. The two extra increments stay because the demo's whole
        # point is showing the cross-session counter accrue.
        policy = policy_from_settings(infra.settings)
        await infra.corpus_store.increment_hit_count(ckey)
        await infra.corpus_store.increment_hit_count(ckey)

        scheduler, inbox = build_promotion_write_plane(
            infra.settings,
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
            policy=policy,
            tracer=tracer,  # REAL scheduler tracer seam: promote/land emit their own
            # spans STARTED under the candidate's traceparent → the SAME session trace.
        )
        routed = None
        last_decision = None
        for _ in range(30):
            promo = await scheduler.run_once()
            if promo.disabled:
                raise SystemExit("scheduler disabled")
            d = next((x for x in promo.decisions if x.candidate_id == cid), None)
            if d is not None:
                last_decision = d
            routed = await infra.candidate_store.get(cid)
            if routed is not None and routed.status == "in_review":
                break
            await asyncio.sleep(0.5)
        if last_decision is not None:
            print(
                f"[STAGE 4] scheduler decision for {cid}: action={last_decision.action} "
                f"to_status={getattr(last_decision, 'to_status', None)} "
                f"reason={getattr(last_decision, 'reason', None)}"
            )
        if routed is None or routed.status != "in_review":
            reason = getattr(last_decision, "reason", None) if last_decision else None
            print(
                f"[STAGE 4] candidate did NOT reach 'in_review' "
                f"(status={None if routed is None else routed.status}, "
                f"decision_reason={reason}). Reporting the traced run; skipping recall."
            )
            await _flush_and_confirm(provider, model, sid, stages_done=4, extractor_real=True)
            return 0

        # The human step the demo is FOR: approve → land. Standing in for a reviewer
        # clicking approve in the inbox, which is now the only edge that reaches the graph.
        validated = None
        try:
            validated = await inbox.approve(cid)
        except Exception as exc:  # noqa: BLE001 - a demo reports, never crashes
            print(f"[STAGE 4] approve did not land: {exc}")
        if validated is None or validated.status != "validated":
            print(
                f"[STAGE 4] candidate did NOT reach 'validated' after approve "
                f"(status={None if validated is None else validated.status}). "
                "Reporting the traced run; skipping recall."
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
            url=neo4j_uri(),
            auth=neo4j_auth(),
            expected_model=EMBEDDING_MODEL,
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
        await teardown(infra)


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
