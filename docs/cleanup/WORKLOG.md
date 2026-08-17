# Cleanup Work Log

Newest entry last. One entry per slice: what changed, review outcome, V0/V1/V2
verification results vs baseline.

---

## #1 — Slice 0: baseline (2026-08-16)

State of the world before any cleanup change, on `cleanup/2026-08` (=
`phase0/provenance-extractor` @ 9558e9e).

- Stack: l2-mcp, l2-token, l2-ch, l2-neo4j, l2-embedding, l2-reranker,
  l2-redis, l2-phoenix all Up; **l2-cb unhealthy** (6 days) — does not affect
  V0/V1 (in-memory session store); restart before any V2 that needs Couchbase.
- V0 (offline `uv run pytest -q`): **5580 passed, 225 skipped, 1 xfailed** (18.9s). Green.
- V1 (7-question live gate, `RUN_LIVE_EVAL=1`, RUNS=3): **7 passed, 2 FAILED**
  (189s). **PRE-EXISTING reds, before any cleanup change:**
  - L3: 0/3 — "the residual half never ran a query" (×3, consistent)
  - L5: 0/3 — "not every intent reached a terminal disposition" (×3, consistent)
  - L1, L2, L4, L6, L7: ≥ the 67% gate.
  The 0/3 consistency = real routing defect predating this branch, not model
  noise. Cleanup gate for later slices: no case below its baseline rate; L3/L5
  are red at baseline and stay out-of-scope for refactor slices.
  **Diagnosed same day (detected-issues stack K1/K2): never passed, not a
  regression.** L3 = eval-harness defect (LiveEvalMCPClient advertises MCP
  tools with EMPTY input_schema → the model cannot issue ad-hoc SQL; faithful
  schemas flip L3 green, proven live). L5 = model never declares intents
  (`serves_intent` tags silently dropped with no feedback; late-init lock) +
  the same harness defect. Fix plan: harness-fidelity slice (tests/eval only,
  F1/F2/F3 + G3) then re-baseline all 7; runtime feedback fix (G1) + prompt
  (G2) as separate behaviour-change slices, not inside refactors.

---

## #2 — Tier 1: drift hazards (2026-08-16)

Deduplicated every multi-copy drift hazard from PLAN Tier 1. 40 files
(35 modified, 5 new). Built in an isolated worktree, reviewed
(verdict: APPROVE; should-fix + 2 nits applied), then landed here.

New shared homes:
- `src/data_agent/canonical.py` — `canonical_json`, THE serialization
  convention for dedup canonical keys + blueprint structural keys
  (hash-critical; `tests/test_canonical_json.py` pins literal sha256 digests).
- `src/data_agent/timeutil.py` — `now_iso()` (was 7 byte-identical copies).
- `src/data_agent/runtime/mcp/_transport.py` — `SideChannelError` base,
  `side_channel_headers`, `auth_headers`, `error_from_response` shared by
  catalog/corpus/scratch HTTP clients (exception names preserved as
  subclasses; `MCPToolError` deliberately NOT in the hierarchy).
- `src/data_agent/learning/entrypoint.py` — `configure_daemon_process`
  (was 3 copied preambles; now also used by `run_inbox_service`).

Constant mirrors now import downward (SLOT_TYPES, NODE_KINDS, AcceptedSignal,
PriorArtKind, corpus index map — now a shared read-only `MappingProxyType`,
DATA_TOOLS, SLOT_TOKEN, READ_CORPUS_META_QUERY, TRUTHY_ENV_VALUES); guard
tests tightened `==` → `is`. `sqlparse/oracle.py` imports `mask_sql` from
redaction (its copy's stated cycle was stale; verified cold-import clean).

Flagged behaviour changes (reviewed + approved):
1. `error_from_response` unified on broad `except Exception` (was
   ValueError-only in catalog/corpus) — strict superset, keeps HTTP status.
2. `run_inbox_service.py` gained the daemon preamble (tracing posture line +
   LEARNING_* typo warning). Inert unless OTLP_ENDPOINT is set. Known gap
   (pre-existing): bypassed under `uvicorn scripts.run_inbox_service:app`.

Left for follow-up: `validation.py::_ACCEPTED_SIGNAL_DOMAIN` is a 4th copy of
the AcceptedSignal set (derive via `typing.get_args`); `run_hydrator.py` has
no runtime-plane env-typo warning equivalent (needs a naming convention
first); `tool_dispatcher._estimate_tokens` deliberate cycle-breaking copy kept.

Verification:
- V0: **5587 passed, 225 skipped, 1 xfailed** (baseline 5580 + 7 net new).
- V1: **identical to baseline** — L1 3/3, L2 3/3, L4 3/3, L6 3/3, L7 3/3;
  L3 0/3, L5 0/3 (the two known pre-existing reds, same failure text).
- ruff clean; `ruff format --check` set byte-identical to baseline.
- V2: skipped per protocol (pure-dedup slice, no loop/dispatch/session/wiring
  semantics touched; V0+V1 green).

---

## #3 — Tier 2: deletions, pure subtraction (2026-08-16/17)

Removed production-dead code per PLAN Tier 2. **93 files, −1208 net LOC**
(549 insertions / 1764 deletions). Built in an isolated worktree, reviewed
(verdict: APPROVE, no blockers; deletion safety independently re-verified by
the reviewer; 3 nits applied), landed here.

Deleted: (T2.1) the dead compaction limb in `context/budget.py`
(compact_trail/render_messages/CompactionResult/SummaryCache + transitive
helpers), the whole `llm_summarizer.py` module + its `app.py` wiring, 3 inert
`ContextAssembler` params, `AssembledContext.compaction_applied` field (the
CHAIN **span attribute** kept — outward telemetry); (T2.2) `Redactor` class;
(T2.3) the dead `databaseSchemaDocs/` dir-path loader chain
(catalog/loader.py, catalog_handle.py entry points, grounding
.load_known_rule_ids) + stale docstrings; (T2.4) factory shims
`build_promotion_scheduler`/`build_review_inbox` folded into
`build_promotion_plane` (policy default + split-store guard preserved
verbatim), `_loader_triage_kwargs` inlined; (T2.5)
`SessionStore.create_session` dropped from the Protocol, both impls, and both
script proxies (couchbase keeps a private `_create_session`);
`_compute_turn_answer_sql`, `new_budget_window`, `_begin_model_turn` inlined;
4 package `__init__` re-export shims reduced to docstrings.

Follow-ups queued in detected-issues stack L1–L3 (dead
`history_token_budget` config, permanently-None `SessionDoc
.context_summary_cache`, decision-doc drift naming deleted APIs).

Verification:
- V0: **5564 passed, 225 skipped, 1 xfailed** (Tier-1 5587; −23 fully
  reconciled: 28 dead-path tests deleted, 10 added, −4 proxy-Protocol
  parametrizations, −1 connect-gate case).
- V1: **no case below baseline** — L1/L2/L4/L6/L7 3/3; L3 **1/3** (above its
  0/3 baseline — first-ever L3 pass, still under the 67% gate = expected red);
  L5 0/3 (= baseline, known K2).
- V2 (required: slice touches app.py/loop/session store): **PASS.**
  Stack: re-applied the daily RLS seed (hr-demo-expansion.sql — the K/trap
  "no data while healthy" hit first attempt exactly as documented; l2-cb
  restarted, healthcheck still reports unhealthy due to a stale compose
  healthcheck probe but the cluster + all 5 buckets answer). Real runtime
  :8000 (gpt-5.5 preflight OK, CouchbaseSessionStore lazy-connect, OTLP →
  Phoenix `data-agent-runtime`) + BFF :3000.
  - L2-style question through the BFF: correct 3-department answer, **2
    answer_tables** (headcount + avg salary), clean SQL, entitlement caveat.
  - L5-style question: correct columns-summary + "7 currently active", 5 tool
    calls, business-terms answer (no raw identifiers — I1 posture held).
  - Phoenix: fresh spans landed (Response OK, tool.updateAnalysisState,
    loop_intent_completed, loop_analysis_state_transition).
  - Couchbase: session doc `session::s309b…` persisted in
    `agent_sessions._default.sessions`.
  Services shut down after verification.

---

## #4 — Tier 3: the Couchbase seam (2026-08-17)

Fixed the missing seam that forced hand-written SessionStore proxies into
scripts/, then shared the construction boilerplate across all 5 Couchbase
stores. 26 files; net ≈ −100 LOC. Reviewed (APPROVE; 1 suggestion applied —
half-built-store poisoning fixed with a fails-without-fix test; 2 nits:
demo-launcher SDK-missing misconfig now fails at boot instead of first
request — strictly better, noted here; stale proxy memory retired).

- `CouchbaseStoreBase` (runtime/couchbase_connect.py): `Cluster(...)`
  construction deferred into `_ensure_connected()` — `__init__` does no I/O,
  needs no event loop; injected-cluster (test/fake) path stays eager,
  byte-compatible. Race-safe (no await between check and registration,
  verified against acouchbase 4.6.2 source); failed build leaves the store
  retriable; `_cluster` assigned only when handles bind (review hardening).
- Both `_LazyCouchbaseSessionStore` proxies deleted (~228 lines); launchers
  construct the real store directly. AST proxy test replaced by
  tests/runtime/test_launcher_session_store.py (Protocol-derived surface,
  anti-vacuity, per-store "no cluster at construction / built on first
  await" cases).
- All 5 stores (session/candidate/audit/user/corpus) converted; public
  signatures unchanged; 3 TTL variants preserved; `get_or_none` KV helper;
  availability-error text identical (verified). Config-side
  `CouchbaseBucketSettings` collapse deliberately skipped (env-var risk).

Verification:
- V0: **5550 passed, 225 skipped, 1 xfailed** (−14 vs Tier-2, reconciled:
  −59 proxy test, +31 launcher test, +14 gate tests).
- V1: first run invalidated — a parallel agent had contaminated the main
  checkout's agent_loop.py mid-run (quarantined; see below) and its live
  eval runs contended (24:43 vs ~4:00). Clean rerun after containment:
  **L2 3/3, L4 3/3, multi-intent 2/3** — all at baseline; L1/L6/L7 3/3 and
  L3 0/3, L5 0/3 from the invalidated run stand (L3/L5 = known K1/K2).
- V2 (mandatory — session store): **PASS.** run_ui_runtime_real constructed
  CouchbaseSessionStore directly at import, no loop (the deleted proxies'
  whole reason to exist); first request lazily built the cluster; turn 1
  answered with 2 verified-shape tables; session doc persisted; turn 2
  answered from replayed Couchbase history with **0 tool calls**; Phoenix
  `data-agent-runtime` spans carried real SQL (OTLP_DISABLE_REDACTION=1 per
  user request).

Process incident, recorded: the parallel L5-fix agent (G1/G2) escaped its
worktree after a stall-resume and edited the MAIN checkout's agent_loop.py
during Tier-3's V1. Contained: agent stopped, partial diff quarantined to
scratchpad (g1-partial.diff), file restored from HEAD, rerun clean. Lesson:
after resuming a stalled worktree agent, verify its cwd containment before
letting live verification run; serialize live-eval workloads (API/CPU
contention turned a 4-min gate into 25 min and produced flaky reds).

---

## #5 — Harness fidelity + gpt-5.5 gate (2026-08-17)

Committed `3d6d6d1`. The A2 live gate had been lying to the model (empty MCP
tool schemas → blueprint-only arena) and gating gpt-4.1 while the real
server runs gpt-5.5. Fixed: committed byte-exact tool-spec fixture (reviewer
re-ran the regen recipe and diffed), real getTableSchema/sampleRows shapes,
L3 residual re-worded to a genuinely uncovered aggregate, three-valued
re_derivation (unjudgeable ≠ pass), state-None reported distinctly,
MIN_PASS_RATE default fixed (0.67 > 2/3 made the documented one-red-run
tolerance unreachable). V1 protocol now pins OPENAI_MODEL=gpt-5.5 + OTLP
export to Phoenix `cleanup-eval` with redaction off (user requests).

Official gpt-5.5 re-baseline: L1/L2/L4/L5/L6/L7 3/3, multi-intent 3/3;
**L5 3/3 — gpt-5.5 declares intents unprompted; K2 resolved by model**
(G1/G2 slice built + contained in its worktree, ready but UNLANDED —
optional robustness now). L3 1/3: new failure mode, the intent-scoped
re-derivation metric (live for the first time) flags the model re-deriving
the hires intent alongside its residual — needs a trace read; queued.

## #6 — J3 + J6(a): hires blueprints + empty-result honesty (2026-08-17)

`90f1908` (+ clickhouse-api `a1d39da`) and `3df9e76`. Both reviewed
(APPROVE ×2), both live-verified through the real stack (REAL_RETRIEVAL=1,
rebuilt l2-mcp, re-seeded corpus):

- **J3**: bp-hires-per-month / bp-hires-in-range / bp-hires-projection
  re-keyed most_recent_hire_date (all-NULL) → hire_date, canon + mirror in
  lockstep (drift guards green); sidecars regenerated (incidentally
  repairing a pre-existing stale manifest entry — parity gate never ran
  against live dirs; follow-up in J3b). Live: "hires per month last six
  months" now answers **3 new hires, March 2021** (predicted exactly from
  seed data) vs structurally-empty before. Rehire reading (original hire
  date) accepted; caveat + catalog-ambiguity retarget + parity-gate test
  bundled into J3b (issues stack).
- **J6(a)**: empty blueprint results no longer wear a verified badge —
  wire carries `{passed:false, empty_result:true, status:"empty —
  unverifiable"}`; pre-J6 persisted sessions re-read honestly; model-facing
  note withdraws "verified" but keeps authoritative + no-re-derivation
  (option (b) explicitly rejected by user). Live: empty 2023 hires window
  returned the new badge end-to-end.

Verification stack state: l2-mcp rebuilt from canon a1d39da; neo4j corpus
re-seeded (11 blueprints, model all-mpnet-base-v2); RLS seed fresh.

---

## #7 — J7(b) window_anchor + Tier 4 Wave A + harness field-drop fix (2026-08-17)

Three landings, then a HOLD (user):

- **J7(b)** `25429a8` (+ clickhouse-api `11258a9`, sidecars regenerated in the
  same commit — review blocker: the B1 sha fast path would otherwise skip the
  re-seed and the field would never reach the graph). Blueprints declare
  `window_anchor: data|calendar`; threaded YAML→seed→neo4j (retraction-safe)→
  detail→executor stamp (both paths)→runBlueprint window_note→TrailEntry→
  canonical rendering→getBlueprint gloss + 1 prompt line (16,929/17,000).
  56 new tests. l2-mcp rebuilt, corpus re-seeded with anchors.
- **Eval conftest field-drop** `6b30465` (tests-only, mutation-verified):
  conftest.blueprint_detail hand-copied a field list and dropped
  window_anchor (so the first "conclusive" L3 run never delivered the note —
  invalidated) plus status/drift_status (latent). Fixed + a derived
  SHARED_FIELDS tripwire (never hand-enumerated).
- **Tier 4 Wave A** `ae17803`: JsonPostClient base (embedding/reranker
  byte-identical wire), all 4 hand-rolled POST /token onto HttpTokenMinter
  (ttl_seconds None-omits; keyword-only allow_unscoped for the two deliberate
  D80b allow-all callers; blank TENANT_* now fails the session mint loudly,
  naming the one env var; transport failures pinned 502),
  scripts/_e2e_harness.py (−838 duplicated demo lines, env reads lazy),
  src/data_agent/untrusted.py (strictest-variant coercers; audit floats
  deliberately unclamped — forensic record). −798 duplicated lines, +43 tests.
  Suite after all landings: **5682 passed, 225 skipped, 1 xfailed**; ruff clean.

**L3 final verdict (user-requested live verification):** with delivery fixed,
all 3 runs carried the anchor note in the model payload (vs 0/3 before the
conftest fix — clean attribution). Behaviour moved 1-of-3: one clean pass
(blueprint kept as i1 evidence), two runs re-derived DESPITE reading the
note (one calendar-anchored query; one completed i1 with its own runQuery
evidence). Conclusion: J7(b) delivery chain proven; a static semantic note
is insufficient against the model's calendar reading of "last six months".
Follow-ups queued in ISSUES.md J7 (surface the concrete anchor date; or the
product decision that a both-windows answer is ideal and the
no-re-derivation contract needs an exception shape). L3 stands red 1/3 with
understood cause.

**Issues stack committed to `docs/cleanup/ISSUES.md`** (source of truth
going forward). **Wave B (T4.1/T4.2/T4.4) and Tier 5 are HELD** per user;
the G1/G2 robustness slice remains built-but-unlanded in its worktree.

## #8 — T5.1: ReadGuard extraction (2026-08-17)

Tier 5 un-held by user ("Lets start Tier 5"); Wave B stays held. First loop
slice, strictly behaviour-identical:

- **T5.1** extracted the repeated-idempotent-read guard + trim-aware re-fetch
  exemption from `_run_loop_body` into a window-scoped `ReadGuard` class in
  `runtime/loop/read_guard.py` (73→~440 lines, still a stdlib-only leaf).
  The guard owns the seven formerly-loop-local state vars (seen signatures,
  serving pointers, exemption counts, per-round served set, emulation seeds);
  interface: `observe_prior_read / seed_emulation / begin_round / classify →
  ReadDecision / record_served`. The body keeps the effects: single shared
  tool_trail walk, marker TrailEntry build + append-then-emit ordering,
  `expanded_this_round` fold for deduped getBlueprint (the T5.1↔T5.2
  coupling), `tool_calls_made`, budget break/continue. The four pure event
  helpers moved verbatim (payload keys allowlist-pinned, unchanged).
  `agent_loop.py` 4326→~4080; `_run_loop_body` 1484→~1410. +25 unit tests
  through the new interface (incl. post-cap event repetition, per-review).
- Review: APPROVE, 0 blockers; polish applied (public
  `repeated_read_guard_event`, AbstractSet contract + honest `.update()`,
  two stale comment pointers, the post-cap repetition test).
- V0 **5707 passed / 225 skipped / 1 xfailed**, ruff clean.
  V1 (gpt-5.5, cleanup-eval): **9 passed, 17:40** — L1–L6 3/3 (L3 3/3 this
  run — first fully green L3; the anchor note carried all three runs; not
  attributable to this behaviour-identical slice, but recorded), L7 2/3
  (prose-answer flake, ≥ floor), multi-intent 3/3, 0 false positives.
  V2 (mandatory for Tier 5): real runtime :8000 (retrieval ON, gpt-5.5,
  memory session store — l2-cb still unhealthy after restart) + BFF :3000;
  L2 live → 2 blueprint-verified tables; L6 live → getBlueprint→runBlueprint,
  no re-derivation, verified table; fresh traces in Phoenix
  `data-agent-runtime` (21:58).

Process trap, recorded: the builder's isolated worktree was created from
`phase0/provenance-extractor` (the repo's registered main), NOT the cleanup
branch — 13 commits stale. Symptoms that unmasked it: 2 "pre-existing"
canon-parity failures (stale pre-J3 fixture) and ~102 fewer collected tests.
Recovery: patch export + `git apply --3way` onto `0f8836a` (one import
conflict: Tier 2's BudgetGuard rename), targeted + full suites re-run green.
Lesson: pin/verify the worktree base in every builder brief; treat
"pre-existing failure" claims from a worktree as unverified until reproduced
on the real base.

## #9 — T5.2: BlueprintGate extraction (2026-08-17)

- **T5.2** extracted the blueprint-definition gate from `_run_loop_body` into
  `runtime/loop/blueprint_gate.py` (window-scoped `BlueprintGate` + the moved
  refusal builder + `BLUEPRINT_DEFINITION_NOT_READ_CODE`). Interface:
  `observe_prior_definition_read / begin_round / note_definition_in_context /
  check_run_blueprint → ToolResult|None / commit_round`. Builder verified the
  membership test is committed-set-only (same-batch get→run still refused) and
  unified the two staging sites after proving same semantics; raw-id hooks
  preserve the original decline/ok asymmetry. Body keeps: shared trail walk
  (dual dispatch comment intact), unwired-tool carve-out, summary skip,
  refusal short-circuit, fold position. `_MAX_SURPLUS_STATE_REJECTIONS`
  deliberately stayed (03 §E.2 machinery, not this gate). 20 new unit tests.
- Review: APPROVE, 0 blockers, 0 suggestions; AST-verified refusal
  byte-identity; 2 comment nits folded in (loader.py cross-ref, test
  docstring impossible-path claim).
- V0 **5727 passed / 225 skipped / 1 xfailed**, ruff clean.
  V1: **1 failed (L3) / 8 passed, 18:24** — L3 1/3 is the ACCEPTED red
  (J7 calendar re-derivation, understood cause; T5.1's 3/3 was variance).
  All other cases 3/3 incl. L7. No dip below baseline.
  V2: L2 live — both getBlueprints before both runBlueprints, 2 verified
  tables, correct figures; L6 live — getBlueprint→runBlueprint, verified,
  no re-derivation; fresh traces in `data-agent-runtime` (22:32).
- The worktree-base trap fired again and was CAUGHT by the brief's pinning
  step (worktree spawned at 9558e9e; builder reset to 20c7c48 before work).
  Patch applied to main with zero conflicts.

Next: T5.3 re-scoped per explorer deep-map — the finalization machinery is
four decision sites sharing two allowances + shared mutable analysis_state +
a store-writing method with callers outside the body. T5.3a extracts the
pure helpers (`loop/finalization.py`), an `AnswerShapeCounter`
(multi_row_answer_calls + answer_table_succeeded — the only cleanly-owned
state), and a thin `FinalizationGate` (per-round refused flag + allowance
claims). `_force_block_pending_intents`, terminal exits, analysis_state
ownership, tool_result assignments, nudge splice/clear all STAY until
T5.5 finish(). Eight ordering invariants documented in the builder brief.

## #10 — T5.3a: finalization decision layer extracted (2026-08-17)

- **T5.3a** (re-scoped from T5.3 per the deep-map; the full gate is four
  decision sites + shared analysis_state + a method resume() also calls):
  `runtime/loop/finalization.py` (~709 lines) now holds the moved pure
  helpers (pending_intents, finalization_blocked,
  answer_table_no_table_designated, the two nudge builders — renamed *_text
  to dodge a real local-shadowing UnboundLocalError — refreshed_analysis_state,
  the shape events), `AnswerShapeCounter` (multi_row_answer_calls +
  answer_table_succeeded; trail-seed asymmetry preserved with rationale),
  and `FinalizationGate` (per-round refused flag + may_refuse(kind), which
  absorbed _grant_forced_reround incl. the one-flag-for-both-kinds and
  no-store-call-no-event short-circuit semantics). Body keeps: both gate
  emissions, the if/elif precedence, tool_result rewrites, nudge
  set/splice/clear, force-block, terminal exits, budget branches.
  `agent_loop.py` 4031→3561. 37 new unit tests. All 8 ordering invariants
  from the map verified by review.
- Review: APPROVE, 0 blockers; all moved helpers AST-identical; fixes
  folded: docstring caller-count (my brief's error — force-block has ONE
  caller outside the body, resume()'s USER_STOPPED, not three; T5.5 scoped
  against the real number), stale claim-key comment → (turn_index, window,
  kind), defensive status=="ok" guard in observe_prior_entry.
- V0 **5764 passed / 225 skipped / 1 xfailed**, ruff clean.
  V1: **9 passed, 17:28 — ALL cases green** (L3 2/3 above floor, L4 3/3,
  L7 3/3, multi-intent 3/3, 0 false positives).
  V2: L4's three-intent question live — 3 searches → 3 getBlueprints →
  3 runBlueprints → done; 2 designated verified tables + prose (L7
  re-measure variance, shape contract satisfied); no refusal loop; fresh
  traces in `data-agent-runtime` (23:19).
- Base trap caught again by the pinning step (worktree spawned at 9558e9e,
  reset to 7f2d51f). Patch applied to main with zero conflicts.
