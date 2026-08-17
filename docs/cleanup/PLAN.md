# Cleanup Plan — design debt + duplication (2026-08)

Branch: `cleanup/2026-08`. Source: four-agent design/duplication review (2026-08-16).
Work log: [WORKLOG.md](WORKLOG.md) — one entry per slice, appended as work lands.

## Rules of engagement

- One slice at a time; each slice is independently shippable and committed only
  after reviewer + qa pass (never mid-flight).
- **Verification protocol after every sizable change (V0 → V1 → V2):**
  - **V0 — offline suite:** `uv run pytest -q` must be green (same failures as
    baseline at worst; no new reds).
  - **V1 — live 7-question gate:** with the l2 stack up (incl. Phoenix :6006),
    `RUN_LIVE_EVAL=1 uv run pytest tests/eval/test_routing_live.py -q -s`.
    This runs the 7 questions L1–L7 (the Arize Phoenix question set) against the
    REAL model + real prompt + whole AgentLoop, reported as pass-rates.
    Gate: every case ≥ `LIVE_EVAL_MIN_PASS_RATE` (default 0.67) and no case
    below its baseline rate.
  - **V2 — live agent spot-check:** stand up the real agent
    (`scripts/run_ui_runtime_real.py` :8000 + `ui/server.py` BFF :3000), ask a
    sample of the 7 questions through the running service, confirm answers and
    that fresh traces land in Phoenix project `data-agent-runtime`
    (http://localhost:6006). Required for slices touching the loop, dispatch,
    tracing, session store, or wiring (`app.py`, launchers); optional for
    pure-dedup slices whose V0+V1 are green.
- Stack health first: `docker ps` — l2-mcp, l2-token, l2-ch, l2-neo4j,
  l2-embedding, l2-reranker, l2-redis, l2-phoenix, l2-cb. Known traps
  (memory): RLS seed expires daily; `l2-cb` currently unhealthy (V2 restart it
  or `REAL_SESSION_STORE=memory`).
- No pushes unless asked. No behaviour changes intended anywhere — this is
  refactoring; any intentional behaviour delta must be called out in the
  WORKLOG entry and approved first.

## Baseline (Slice 0)

Record in WORKLOG before any change: V0 full-suite result, V1 per-case
pass-rates. All later slices compare against this.

## Tier 1 — drift hazards (small diffs, kills a recurring bug class)

- **T1.1 `_canonical_json` × 3 → one home.** `learning/models.py:85`,
  `learning/dedup/canonical_key.py:38`, `runtime/blueprint/structural_key.py:106`
  (public `canonical_json`). Hash-critical: digests must stay byte-identical —
  add a parity test asserting the digest of a fixed corpus doesn't change.
- **T1.2 closed-set constant mirrors → import downward.** `SLOT_TYPES` +
  `NODE_KINDS` (`learning/extractor/models.py` ← `runtime/blueprint/models.py`),
  `_TRUTHY`, `AcceptedSignal`, `CandidateKind`/`PriorArtKind`, vector-index name
  map, `_DATA_TOOLS`, `rules.py` re-importing `SLOT_TOKEN` from `template.py`.
- **T1.3 `mask_sql` unification (this repo).** `sqlparse/oracle.py` imports
  `observability/redaction.py::mask_sql`. (clickhouse-api copy: separate repo,
  flag only.)
- **T1.4 `_error_from_response` / `_headers` / `_auth_headers` / error classes →
  one `runtime/mcp/_transport.py`** shared by export_client, corpus_client,
  scratch_client, real_client.
- **T1.5 daemon preamble parity.** Extract the tracing/env-warning preamble
  (consumer/scheduler/sweeper × 3) and add it to `run_hydrator` +
  `run_inbox_service`, which silently lack it.
- Small: `_now_iso` × 7, `_resolve_catalog` × 2, `_default_observer` × 2.

## Tier 2 — deletions (pure subtraction)

- **T2.1 dead compaction limb**: `context/budget.py` compact/render/CompactionResult/
  SummaryCache + the `app.py:493` summarizer wiring + 3 inert
  `ContextAssembler` params.
- **T2.2 `Redactor` class** (redaction.py:138) — zero callers.
- **T2.3 dead `databaseSchemaDocs/` path**: dir-based loaders in
  `catalog/loader.py`, `load_catalog_handle`, `load_known_rule_ids`, stale
  docstrings pointing at them; migrate the `tests/catalog/` users to the
  export-based path.
- **T2.4 learning factory shims**: `build_promotion_scheduler`,
  `build_review_inbox` (fold into `build_promotion_plane`),
  `_loader_triage_kwargs`.
- **T2.5** `get_or_create_session ≡ create_session` (drop one Protocol method,
  4 impls), `_compute_turn_answer_sql` (test-only), `new_budget_window`,
  `_begin_model_turn`, dead `__init__.py` re-export shims × 4.

## Tier 3 — the Couchbase seam

- **T3.1 `CouchbaseSessionStore` accepts/defers its `Cluster`** (lazy connect via
  `CouchbaseConnectGate`), then **delete both** 122-line
  `_LazyCouchbaseSessionStore` proxies in `run_ui_runtime.py` /
  `run_ui_runtime_real.py` and the AST proxy test.
- **T3.2 `CouchbaseStoreBase`**: import guard + cluster construction + TTL +
  `_get_or_none` shared by the 5 stores; collapse the 5 duplicated
  credential blocks in config behind one `CouchbaseBucketSettings` with
  env prefixes (env var names must not change).

## Tier 4 — structural moves (one PR-sized slice each)

- **T4.1 extract the pure blueprint compiler/validator out of
  `corpus_loader.py`** (~lines 1037–2681: reference inlining, sqlglot analysis,
  `_validate_blueprint_dag` split into per-gate functions) into
  `runtime/blueprint/compiler.py` (no Neo4j/yaml deps); seeds/DTOs into their
  own module so `learning/` stops importing the loader; promote the two
  privates `hydrator.py` imports.
- **T4.2 collapse `executor.execute` into the DAG path** (single-node = 1-node
  DAG); fix the `_execute_single` ghost comments.
- **T4.3 HTTP client dedup**: `_JsonPostClient` base for embedding/reranker;
  side-channel clients onto T1.4's `_transport.py`.
- **T4.4 tool span envelope**: `@traced_tool`/`RuntimeToolBase` replacing the 5
  hand-rolled `_emit_tool_span` copies; `RunBlueprintTool` reuses `_ReadTool`'s
  guard/error shape.
- **T4.5 token minting**: everything calls `HttpTokenMinter` (ui/server.py
  `_mint_jwt`, 3 scripts).
- **T4.6 scripts harness**: `scripts/_e2e_harness.py` absorbing the ~413
  shared demo lines + `_load_openai_key`/`_pick_openai_model` × 4.
- **T4.7 untrusted-JSON coercers**: one `_untrusted.py`
  (`as_str/as_float(lo,hi)/as_str_list/as_bool_or_none`); replace the divergent
  `_str_list`/`_float` copies (derive-the-guard).

## Tier 5 — the loop (highest risk, last; V2 mandatory per step)

Decompose `agent_loop._run_loop_body` in dependency order, one extraction per
slice, each with its own unit tests through the new interface:
**T5.1 `ReadGuard`** → **T5.2 `BlueprintGate`** → **T5.3 `FinalizationGate`** →
**T5.4 `TurnAccumulators`** (absorbs the 6 `seed_*` params + 4 accumulate
helpers) → **T5.5 single `finish()`** (7 `TurnOutcome` sites) →
**T5.6 delete the `_run_loop` mirror wrapper** (inline the `finally`).

## Deferred / decisions needed (not started without a call)

- Hypothetical seams (`UserMemoryProvider`, `AnswerTableHooks`, null-object
  Protocols, `RecurrenceCountReader`): delete vs keep-dormant — user decision.
- `AgentLoop.__init__` 19→ value objects; `build_learning_consumer` 18-param
  shrink; `RuntimeSettings` split; `CandidateEnvelope` per-stage views;
  scheduler class split; `validation.py` concern split — design-heavy, propose
  separately after Tiers 1–4.
- clickhouse-api `mask_sql` drift — other repo.

## Status

| Slice | State | WORKLOG |
|---|---|---|
| 0 baseline | done (L3/L5 pre-existing reds, diagnosed → issues stack K1/K2) | #1 |
| Tier 1 drift hazards | done, reviewed, V0+V1 green | #2 |
| Tier 2 deletions | done, reviewed, V0+V1+V2 green (−1208 LOC) | #3 |
| Eval harness fidelity (K1 F1/F2/F3 + G3) | proposed, awaiting user go | — |
