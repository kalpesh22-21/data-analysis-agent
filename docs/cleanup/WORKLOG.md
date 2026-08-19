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

## #11 — T5.4: TurnAccumulators (2026-08-17)

- **T5.4** collapsed the six `seed_*` params + seven window accumulator
  locals into one window-scoped `TurnAccumulators`
  (`runtime/loop/turn_accumulators.py`, 455L). Moved+de-underscored:
  capture_terminal_sql (kept module-level — third windowless call site in
  _compute_turn_answer_tables), answer_envelope/AnswerEnvelope,
  accumulate_enrichment; absorbed: _accumulate_answer_tables,
  _accumulate_assumptions. `_run_loop`/`_run_loop_body` now take ONE
  `accumulators` param — the mirror's historical seed_blueprint_terminal_sql
  forwarding hole is now structurally unexpressible (docstring rewritten to
  record the failure mode). One deliberate reorder: construction hoisted
  above the AnswerShapeCounter seed read (bool-equivalent, pinned).
  Deliberate delta: blueprint_use/verification seeds are now copies, not
  aliases (equal-not-identical on the approval-resume no-table path;
  reviewer verified no identity-dependent consumer exists). Envelope sites
  all position-preserved incl. the per-call pause-seam compute — queued as
  ISSUES.md **M1** rather than optimized here. 30 new unit tests.
- Review: APPROVE, 0 blockers, 0 suggestions; moved bodies AST-identical;
  2 optional nits (shallow-copy docstring clause — folded in; three
  past-tense prose refs in untouched test/fixture files — left as history).
- V0 **5794 passed / 225 skipped / 1 xfailed** (pre-test-file run matched
  the 5764 baseline exactly), ruff clean.
  V1: **9 passed, 16:52 — all green** (L3 2/3, L7 2/3 above floor, rest 3/3).
  V2: L4's three-intent turn live → **3 blueprint-verified tables** (ideal
  shape); same-session follow-up answered via fresh scoped runQuery
  (calendar-window "none" — the known J7 semantics, not a slice issue);
  traces in `data-agent-runtime` (00:10).
- Builder hit an API-error stall mid-build; resumed with mandatory
  containment re-verification (G1 protocol) — clean. Base trap caught by
  pinning again (worktree at 9558e9e → reset to 2a67102).

## #12 — T5.5: single _finish() for the in-body exits (2026-08-17)

- **T5.5** consolidated the epilogue of the FIVE in-body TurnOutcome exits
  (done/no-tool-calls, askUser pause, done/answerWithTable, hard ceiling,
  budget-cap pause) into one async `AgentLoop._finish` (checkpoint write →
  conditional message append tagged with provenance → envelope → event
  LAST → outcome). Per-exit variation stays at the sites: provenance-union
  calls (done-only), checkpoint construction, force-blocks (E6
  unconditional, E7 conditional). **Converts 5 of the plan's "7 sites"** —
  the resume-stop exit (no accumulators exist there) and
  _pause_from_runtime_tool (already a single-purpose finisher) are
  deliberate exclusions, documented in the docstring.
- **Pre-flight characterization tests** (9, all verified green on the
  UNMODIFIED base — reviewer re-ran them on base independently):
  resume-stop emits no loop_turn_done (+positive control), 3× pause/ceiling
  provenance-is-None, M1 pause-envelope pin, blank-final-answer persists
  nothing (written against a LIVE mutation of the `or None` guard — the
  full 5800-test suite passed with the mutation until this test existed),
  2× store-write-before-event order pins.
- **ISSUES M1 fixed in this slice** (explicitly called out): the
  per-tool-call envelope compute sunk into its only consumer (the pause
  branch); purity of envelope()/answer_envelope verified by builder AND
  reviewer.
- Review: APPROVE, 0 blockers, 0 suggestions (1 no-action format nit).
- V0 **5803 passed / 225 skipped / 1 xfailed**, ruff clean.
  V1: **1 failed (L3 1/3, the accepted oscillating red — J7 cause) /
  8 passed, 15:24**; all else 3/3, L7 2/3 above floor.
  V2: L2 live → 2 blueprint-verified tables, answer_sql present, done exit
  through _finish; traces in `data-agent-runtime` (02:21).
- Base trap caught by pinning again (worktree at 9558e9e → reset 1d02276).

## #13 — T5.6: mirror wrapper inlined — TIER 5 COMPLETE (2026-08-17)

- **T5.6** deleted `_run_loop`: its try/finally moved into `_run_loop_body`
  (try opens as the FIRST statement, so a tools_provider raise still hits
  the finally; both nesting levels verbatim — CancelledError at the
  sleep(0) yield still runs the cancel); three callers retargeted.
  Verified mechanically: `git diff -w` = only the intended hunks; AST
  comparison (builder's AND reviewer's independent scripts) — all 22 body
  statements, finalbody, signature dump-identical to base.
- **Mutation check on the drain tick**: deleting the sleep(0) fails 4
  existing tests in any realistic run (the tick is load-bearing in a warm
  process). Residual gap — single-test-in-fresh-process — proven
  un-pinnable deterministically (prototype passed under mutation; deleted);
  documented rather than papered over with a flaky test. Also noted: the
  four progress-summary tests are order-coupled through process-global
  warm-up (queue-worthy if it ever bites).
- Review: APPROVE (independent AST proof); 2 comment-only should-fixes
  folded in (turn_accumulators.py return-pointer repair; the misleading
  "byte-identical to _run_loop" feature-off comment reworded) plus a
  22-line mechanical prose rename of stale `_run_loop` references across
  src+tests (historical past-tense mentions deliberately kept).
- V0 **5803 passed / 225 skipped / 1 xfailed** (exact baseline — pure
  inline), ruff clean. V1: 8 passed + L3 accepted red (1/3; L7 3/3).
  V2: three-intent turn live → 3 blueprint-verified tables; traces 02:45.

**Tier 5 complete.** Six slices, all reviewed-APPROVE, all V0+V1+V2:
ReadGuard (20c7c48) → BlueprintGate (7f2d51f) → finalization decision layer
(2a67102) → TurnAccumulators (1d02276) → _finish() (db79498) → this.
`_run_loop_body` decomposed from a 1,484-line monolith threading ~20
parallel locals into a ~1,270-line driver coordinating five window-scoped
collaborators (ReadGuard, BlueprintGate, AnswerShapeCounter,
FinalizationGate, TurnAccumulators) with 121 new unit tests through their
interfaces. agent_loop.py 4326→3385. Line-count note: Tier 5 is
restructuring, not deletion — src net +871 (module docs + interfaces);
true deletions were the duplicated epilogues, the wrapper, and absorbed
helper copies. Deferred-by-design: force-block stays a method (resume()
caller), E1/E2 exits unconverted (documented), ISSUES M2 product question.

## #14 — H4+H7: worker entrypoint hardening (2026-08-17)

Issue-stack triage began (ease × impact; user picked the easy/high quadrant
first). This slice: the two production-blocking entrypoint defects.

- **H4** — the four PID-1 workers (hydrator, learning consumer/sweeper/
  scheduler) dropped SIGTERM → every K8s rollout waited out the grace period
  then SIGKILLed mid-work, skipping finally cleanup. Fix: shared
  `src/data_agent/daemon.py::run_daemon` (plane-neutral Tier-1 home) —
  SIGTERM cancels the main task so existing finallys run; exit 0 on
  SIGTERM shutdown; second SIGTERM (and SIGTERM-during-SIGINT-cleanup)
  logged + ignored, never re-cancelled mid-cleanup; SIGINT propagation
  preserved. Before/after smoke: cleanup never ran → runs, exit 0.
- **H7** — inbox service built its app at module import. DIAGNOSIS
  CORRECTED during the build: the crash trigger (eager acouchbase Cluster
  in CouchbaseCandidateStore.__init__) was already defused incidentally by
  Tier 3's lazy seam (b7b21c1) — the mechanism is real (reproduced on
  SDK 4.6.2/py3.14) but was latent, not blocking. Fix removes the CLASS:
  app construction moved inside the running loop (`_serve` driving
  uvicorn.Server), module import is side-effect-free (AST-pinned,
  mutation-verified), startup-failure exits 3 (uvicorn.run parity),
  dev Ctrl-C caught. Full-plane + offline smokes green; Helm invocations
  unchanged.
- Review: APPROVE; 2 should-fixes + 2 nits folded (exit-3 parity, tightened
  AST invariant — both mutation-verified; SIGINT/SIGTERM interleave guard
  with its own deterministic test; Ctrl-C catch). 15 new tests total.
- New follow-ups queued: ISSUES C3 (uvicorn workloads still exit 143 on
  SIGTERM — policy decision), plus reviewer notes: inbox service still uses
  deprecated on_event("shutdown"); no terminationGracePeriodSeconds set for
  the consumer.
- V0 5837 green (+ the expected cross-repo parity red from the in-flight
  canon slice, proven by stash-test); ruff clean. V1 not required
  (no loop/dispatch code); live smokes above are the V2.

## #15 — E1+E2 token refresh, M2 stop-path parity (2026-08-17)

- **E1** — BFF sessions died at ~61 min (single mint, TTL 3600, product
  expects 8h). Lazy re-mint at the single token-attach point
  (`_jwt_for_session`, now async): age > TOKEN_REFRESH_AFTER_SECONDS
  (default TTL−300) → re-mint with the CACHED claims and swap; stale +
  mint-fail → serve held token + WARNING (shock-absorber band); past-TTL +
  mint-fail → loud 502, nothing proxied; rolling refresh, no session cap.
  Env: TOKEN_TTL_SECONDS (BFF cannot learn the service TTL — mint response
  carries no expires_in), TOKEN_REFRESH_AFTER_SECONDS; startup warning if
  the band is misconfigured. **E2 enforced by construction**: the refresh
  path cannot reach entitlement resolution (reviewer traced the full call
  graph); the Decision-7 rationale (scratch tables are not scope-stamped —
  a mid-session scope change would leak) lives as a load-bearing comment at
  the refresh site. 14 tests incl. raise-style laundering traps
  (mutation-verified) and exact boundary pins (operator flips each fail
  exactly one test). LIVE-verified: rolling refresh kept a session
  answering 200s past its original 30s TTL against the real token service.
- **M2** (user-approved behaviour change) — resume()'s budget-cap "stop"
  now returns what "continue" rebuilds: the same two trail producers feed a
  locally-built TurnAccumulators, so the stop outcome carries assumptions +
  answer_tables + their envelope projections (internally consistent; a grid
  without its SQL cannot page). Force-block + no-event contracts unchanged
  (test_turn_exit_contract untouched). 5 tests.
- Review: APPROVE, 0 blockers; 1 suggestion + 2 nits folded (env sanity
  warning, boundary tests, TurnAccumulators call-site count).
- V0 5837 green (+ the expected in-flight canon parity red, stash-proven);
  V1 **8 passed + L3 accepted red** (L1/L2/L4–L7 3/3, multi-intent 3/3).

## #16 — J3b + J1 canon slice, end-to-end verified on the real database (2026-08-17)

User directive mid-wave: verify end-to-end against the real database before
committing. Full stack stood up (rebuilt l2-mcp from the new canon, corpus
re-seeded); real questions asked through the BFF:

- **J3b-a PASS** — ambiguity default flipped to `hire_date`; ad-hoc "hires
  by year" now keys on the populated column (2/2/3). Builder swept all 45
  ambiguity defaults across 11 catalog files: only this one was a genuine
  defect (`supervisor`'s default is all-NULL but so is every alternative —
  warehouse seed gap, left alone).
- **J1 — failed first, root-caused, fixed, PASS.** Guidance in
  `annual_salary.description` was IGNORED 2/2 live runs; Phoenix payload
  proof: the guidance text was absent from 170KB request payloads while the
  schema was in context. Root cause: `_cap_nontabular_result` keeps only
  the head of a wide table's column list — late columns lose descriptions
  entirely (NEW ISSUE **C5**; also affects ambiguities). Fix that works:
  `kn-clickhouse-median` knowledge entry (recall-surfaced, schema-width
  independent) — next run the model wrote
  `(quantileExactLow+quantileExactHigh)/2` unprompted; values match ground
  truth exactly (125000/62500/115000 vs the old wrong 130000/75000/115000).
  Column description kept too (correct, visible on narrow tables).
- **J3b-b/c** — rehire caveat on the three hires intents (byte-identical);
  blueprint route live: getBlueprint→runBlueprint, data-anchored answer,
  no re-derivation. Sidecar freshness gate landed as a live-dirs suite test
  with non-vacuity + served-SHA layers (teeth proven both directions);
  historical correction: a2c8cb9 was a not-re-run gate, and this repo has
  NO CI — the suite is the enforcement point.
- Review (canon): APPROVE. clickhouse-api commit `f838a7f`; this commit
  carries the fixture mirrors (blueprints intents, knowledge entry, catalog
  export regenerated via scripts/regen_catalog_fixture.py — note the
  catalog direction has NO runtime-side guard, reviewer nit, queued with
  C5). V0 with synced mirrors: **5838 passed / 225 skipped / 1 xfailed**.

## #17 — H1+H2: derived enforcement classification + judge brief decrowding (2026-08-17)

- **H1** — `DenialInfo.enforcement: bool` (default False = substantive, so
  an unclassified new code can never silently STOP counting); all 21
  `_DENIAL_TABLE` codes classified on the line "was the model's data work
  judged, or only its call protocol"; `ENFORCEMENT_DENIAL_CODES` derived
  from the table; the learning set = derived ∪ {IDEMPOTENT_READ marker
  (assembly-owned, not a table entry)}. **The predicted drift had already
  happened**: ANSWER_TABLE_NO_TABLE_DESIGNATED (08 §O) was registered
  after the hand list and silently counted substantive. Two additions
  (that one + RETRIEVAL_TOOL_INVALID_ARGS), zero removals, no counted
  metric moves today (reviewer-verified against both readers); no wire
  leak of the new field (no DenialInfo serialization anywhere). Old
  subset-drift test replaced by a derivation test + a per-code rationale
  map that fails when a new code lands unclassified.
- **H2** — judge `session_brief`: bookkeeping calls (shared
  `BOOKKEEPING_TOOLS` in learning/summary/models.py; extractor's private
  mirror DELETED) filtered before the 12-call cap; `truncated` computed
  over substantive calls only; `bookkeeping_calls_omitted` always states
  the omission. Pre-fix repro: 17 recordAssumptions ate 11 of 12 slots and
  flipped truncated. Extractor behaviour byte-identical after the
  constant move.
- Review: APPROVE, 0 blockers; both judgement-call classifications
  independently confirmed at raise sites. 1 nit queued as **H8** (the
  enforcement boolean is load-bearing for two readers with different
  semantics; outage codes strain it).
- V0 **5869 passed / 225 skipped / 1 xfailed**, ruff clean. Learning-plane
  slice: no live gate required (classify_denial output unchanged;
  reviewer-verified).

**Easy×high triage quadrant complete** (H4+H7, E1/E2+M2, J3b+J1, H1+H2 —
WORKLOG #14–#17). Remaining stack = discussion items: H3/H5/H6 rewrite
family, J7 product call, Wave B, C5, I1/I2, D2/D3, plus hygiene batch.

## #18 — the rewrite-fragility family (H3/H5/H6) + keep-and-annotate (2026-08-18)

Landed in two acts. The deep-map found the family WIDER than filed: beyond
H5 (all-rule WHERE → unparseable → ParseError escapes → SESSION-level
poison: never-ACK → redelivery loop → dead_letter, and a permanent 500 on
the reviewer completion path) and H6 (rule inside sumIf → one-arg sumIf +
byte-identical metrics + silently narrowed `uses` scope), measurement
showed 5 unparseable shapes, a raw KeyError escape (OR parent), and a
SILENT CROSS JOIN (sole join-ON deletion). Also corrected the record:
H6 was NOT "mitigated by approve-path static validation" — nothing
recomputes S4 (the approve gate reads the stored ok stamp) and golden
replay misattributes the real-warehouse failure as probe_unavailable.

Act 1 built an allowlist around the deletion surgery; review REQUEST-
CHANGES with two proven blockers (OR-arm/NOT context lost by immediate-
parent checks; one-member locator deleting a whole multi-member IN).

Act 2 — **USER DECISION (2026-08-18): `role: rule` keeps the filter.**
The deletion design assumed platform re-application that exists only for
RLS-backed tenancy, not catalog-default rules (executor contract: static
rule = "authored SQL, no action"). The rewriter now keeps the predicate
verbatim and annotates uses_rules — matching canon and the executor; the
whole deletion path (and both blockers, and H5/H6 themselves) is deleted,
not fixed. 15/15 shape matrix kept verbatim; the deductions-ratio
candidate that crashed the pipeline now generalizes cleanly. Executor
chain verified (reviewer, link-by-link): learned uses_rules are bare
strings → parse_rule None → always static → dynamic-rule hazard
unreachable. Cross-tier structural keys CONVERGE — the known-gap test is
now a convergence pin DERIVED through the real rewrite (tautology caught
by review; reworked; fails against the old deleting rewriter, stash-
proven). G7's hint concern is RESOLVED by the semantics change (the hint's
role:rule advice is now correct).

Kept from act 1 (all still live): the output gate (render wrap + template
re-parse via the SLOT_TOKEN colon-trick + function-arity census — belt),
H3 strict-inline verification pre+post pass (locatability = _find_literal
with comma-split IN ∪ the S3 literal_predicates enumerator, so
BETWEEN/boolean inline entries don't false-fail), canonical_ast_norm
fail-soft at both builder sites, the stage-level never-raise belt, and
the completion path returning in-band declines instead of 500s.

Reviews: act-1 REQUEST-CHANGES (both blockers proven end-to-end);
act-2 APPROVE, 0 blockers (2 suggestions + 2 nits folded: the convergence
pin rework, docstring/message prose, validation.py threat-model comment).
36 new tests, base-first-proven. V0 **5905 passed / 225 skipped /
1 xfailed**, ruff clean. Learning-plane slice; no live gate required.

Process finding (reviewer): worktree venvs can resolve the editable
install to the MAIN checkout's src — every landed slice is unaffected
(each was re-verified with full V0 in main after patch-apply), but
builder/reviewer briefs now require verifying `data_agent.__file__`
resolves inside the worktree before trusting a run.

## #19 — C5 schema preview + I1 answer scrub, e2e-verified on the real database (2026-08-18)

User decisions: C5 solve-the-truncation; I1 build the prose scrub (+ blueprint
ids); I2 full transparency KEPT (structured payload untouched, recorded in
ISSUES).

- **C5** — `dispatch/schema_preview.py::fit_schema_under_cap` owns the policy:
  lossless null-key compaction (–34% payload) → {name,type} skeleton for EVERY
  column → base sections protected → relevance-ranked detail upgrades
  (per-table IDF scorer; annual_salary first for the salary question;
  plural stemming ies→y) → honest budgeted marker (no false re-fetch advice).
  question threaded as an ordering-only hint (D25 leak test). Before: 35/130
  employee columns visible, annual_salary ABSENT. After: 130/130 named,
  detail follows the question. Prompt ceiling raised 17,000→17,400 (dated
  tripwire rationale) for the two-tier teaching; byte-pinned draft doc moved
  with it. Review: APPROVE; folds incl. the silent base-drop event gap.
- **I1** — `runtime/answer_scrub.py`: one-pass alternation (corpus ids →
  [saved analysis]; qualified ≥2-dotted incl. 3-part, underscored, quoted →
  [schema detail withheld]); @-adjacent and data-file-extension carve-outs
  (R0 consumes-but-preserves); KN-95/BP-1042 value protection; scrub at
  _finish (one string → persist + outcome: live/history parity), pause path,
  askUser question; count-only event. Review: APPROVE WITH FIXES, all folded
  (3-part leak, carve-outs, letter-required prefix). The sentinel test
  rename judged correct by review.
- **Workflow fix that outlives the slice**: pytest `pythonpath=["src"]` — a
  worktree suite could silently import MAIN's src (reviewer-proven);
  now structurally impossible.
- Verification (user directive: e2e on the real DB before committing):
  V0 **5972**; V1 **9 passed** (L1/2/5/6 3/3, L3/L4/L7 2/3 ≥ floor);
  C5 live: correct medians + "130 of 130 columns are listed" marker PROVEN in
  the model's request payload (eval AND live service); I1 live: 3 adversarial
  probes → 100% model compliance (it refused to name identifiers, on accuracy
  grounds once), zero redaction events on a verified-working channel;
  mechanism pinned at the seam by 33 tests. ISSUES: C5/I1 popped; M3 (C5b
  user spec), M4 queued.

## #20 — demo harness snake-case debt + learning loop verified live (2026-08-18)

User directive: prove candidates are learnt properly. First flywheel run
stopped honestly at PART A — root cause PRE-EXISTING harness staleness
(scripts/_e2e_harness.py still minted a CamelCase column scope from before
the warehouse snake-migration → MCP scope-filtered the schema to zero columns
→ model hallucinated names → PARSE_FAILED_CLOSED, fail-closed guard correct).
Fixed: SALARY_COL/DEPT_COL/CATALOG → real snake_case + types; scope widened
to 5 columns (each proven load-bearing live — the model wrote the
exclude-not-hired rule predicate the 2-column scope would have hidden);
_ACCEPTED_SQL in the sibling demo (executed live) fixed; flywheel /turn
result contract caught up (sql → sql_executed/answer_sql/answer_tables).

**Learning loop VERIFIED with a novel candidate** (by-status average salary —
no corpus coverage): live ask $700k → rectify $100k avg → sweep
(scanned=1 claimed=1) → REAL gpt-5.5 extractor → blueprint candidate
(confidence 0.95, snake_case citations, enum slot with real values) → S4
template clean (static_validation ok) → review inbox → HUMAN APPROVE →
VALIDATED LANDING in Neo4j (created_by=learning). Prior-art logic also
demonstrated: the covered question produced a knowledge candidate instead of
a duplicate blueprint. PART C non-autoplay root-caused to the DELIBERATE
governed-corpus trust gate (recall serves source='mcp' only; the landed node
ranks #1 in the raw index — will serve on promotion). ISSUES M4 queued for
the stale demo narrative.

## #21 — C5b + getTableSchema columns argument, full-loop verified live (2026-08-18)

- **C5b (user five-point spec)**: grouped presentation (relevance-ordered
  detailed prefix, primary/join-key columns pinned, skeleton tail in
  original order, marker states which variant); tenancy strip ALWAYS
  (client_code + proc_center; the 4 RLS tables were already MCP-hidden —
  the strip is load-bearing for the 7 OTHER tables that exposed
  client_code; no rule/ambiguity references them; result_full untouched);
  base sections NEVER truncated (degradation ladder deleted); columns-only
  budget `schema_columns_token_budget=6000` (generic 4k untouched);
  null-strip UNCONDITIONAL (user point 5 overrode the reviewer-endorsed
  byte-identity deviation). Employee: 17→114 of 130 detailed; preview
  7,197 tok; 3 employee-sized schemas ≈24% of request budget (accepted).
  Review: APPROVE; folds incl. the ungated tenancy operator event,
  fail-closed bare-string strip, both marker variants.
- **columns: narrowing argument** (clickhouse-api 26d2644): overlay-layer
  filter after both authorization gates; hidden ≡ nonexistent in the echo;
  base always complete; runtime pass-through verified (no arg filtering;
  narrowed call = new read-guard signature by construction); marker +
  prompt (17,246/17,400) now teach the actionable contract. Also fixed the
  red test f838a7f landed (closed _KNOWLEDGE_IDS pin never learned
  kn-clickhouse-median — my process gap: the knowledge entry was added
  after that slice's suite run and the suite was not re-run).
- **Live verification (user directive — full end-to-end incl. learning):**
  V0 5987; V1 8 passed + L3 0/3 low-tail → isolated rerun 1/3 = exactly
  its accepted baseline (oscillation, not regression). Probes: accrual
  events schema clean of client_code in the model payload (after unmasking
  a STALE-PROCESS false alarm — the serving runtime predated the code;
  lesson recorded: verify process start time vs code under test); grouped
  RELEVANCE marker live in payload with pinned employee_code leading;
  median exact; narrowed fetch live (full docs for named columns;
  client_code/no_such_column adversarial → identical fail-closed echo).
- **Learning loop verified on this tree** (learning plane byte-identical
  since d4fd911): one novel candidate landed CLEAN end-to-end this morning
  (avg-by-status: extract → S4 ok → inbox → human approve → validated
  Neo4j landing, ranks #1 in the raw index; recall correctly trust-gated
  pending canon promotion, M4). Three further novel candidates today each
  stopped by a DIFFERENT legitimate gate: H3 strict-inline (IS NOT NULL
  planned as inline value 'NULL'), golden-replay grain-probe (slot-less
  group-by candidate — pre-existing edge, worth an ISSUES entry if it
  recurs), strict slot-miss (plan over-covered a literal absent from the
  accepted SQL). All routed to review; zero garbage landed; zero crashes —
  the exact shapes that session-poisoned a week ago now decline in-band.

## #21b — flywheel demo learning-plane tracing (2026-08-18)

User observation: no learning spans in Phoenix. Root cause: the flywheel
demo never installed the learning tracer — the learning collaborators take
`tracer=` as an explicit seam (sweeper/consumer/write-plane all accept it;
the sibling demo_learning_e2e_openai wires it at :238) and run NO-OP
without it, so every flywheel run today traced its runtime turns
(`data-agent-runtime`) while the learning stages ran dark. Fixed:
configure_learning_tracing + get_learning_tracer at run start, tracer
threaded into LearningSweeper / build_learning_consumer /
build_promotion_write_plane, provider.force_flush() in the finally (both
KEEP paths). Verified live: `learning-loop` project received the full
chain — learning.sweep → enqueue → consume → triage → leakage → dedup →
extract (+ judge Response spans) at 20:10-20:11.

## #22 — hygiene wave (2026-08-18)

Two parallel batches, both reviewed (code: APPROVE + factory=True fold;
docs: verified baseline-exact).

**Code batch:** J7c — window_anchor threaded through both promotion
projections via Blueprint.parse (single fail-closed gate) + a DERIVED
seed-field-parity tripwire, mutation-verified. L1 — dead config deleted
(history_token_budget method + ratio field; env var proven inert via
extra="ignore"; counter-pin added). L2 — context_summary_cache deleted
(reader tolerance proven empirically; content hash measured identical —
allowlist-built; migration = stray key ignored on read, dropped on next
write). B5 — two derived prompt-gloss parity tests (bidirectional token-set
equality; verbatim-shared set pinned both ways with the rewording-vs-
contradiction limitation stated). A2 — the S3 comment now states the
wall-clock-only mid-batch truth with its derivation. C3 —
`http_daemon.run_http_daemon`: root cause = uvicorn re-raises captured
SIGTERM onto SIG_DFL post-shutdown; fix = chained handler + Config(
factory=True) so app composition sits inside the captured region +
uvicorn's own runner (uvloop restored — the first draft had silently
downgraded the UI launchers to stdlib asyncio). All three launchers +
mid-composition smoked at exit 0; counterfactual -15 recorded. Residual:
the uvicorn-CLI deployments need a chart change (C3 entry updated).

**Docs batch:** C1 verified ALREADY closed (real count 15, pinned ×3) —
tombstoned. D1's stale claim lived in FOUR other docs (11-testing,
DECISIONS D89, TRACEABILITY, OPEN-QUESTIONS) — fixed/marked, 04-blueprints
itself was correct. L3 — dated SUPERSEDED markers (history preserved);
corrected the entry's own error (the deleted proxies were Tier 3/#4, not
Tier 2). C4 — schema_notes documented inert at the authoring surfaces.
M4 — flywheel PART C narrative now states the staging/trust-gate truth
(the "semantic distance" misattribution fixed); a near-miss SUMMARY-line
change was caught and reverted by the builder itself. ISSUES housekeeping:
I3 verified green and tombstoned (its "2 failures" were 4 env-induced
ones), K1 tombstoned, B2/K2 reduced to their true residuals.

Suite after the wave: **6019 passed / 225 skipped / 1 xfailed**; ruff
clean. Net: two genuine deletions (L1, L2), one new lifecycle module,
+32 tests, and the doc surface re-aligned with reality.

NEXT (user 2026-08-18): docstring trim wave (src/ only; contracts kept,
narratives move to WORKLOG/git), then Tier 4 Wave B.

## #23 — docstring trim wave (2026-08-18)

User decision: minimal docstrings across the code. Three parallel slices
(runtime/ · learning/+top-level · scripts/+ui/), one shared contract: one
summary line + contract essentials (invariants, caller constraints,
security notes); narratives, incident history and design argument deleted
— they live in this WORKLOG and git history. Booby-trapped invariants kept
as one terse line (+ a bare WORKLOG pointer only where an entry actually
describes them). Comments (#) deliberately out of scope.

Numbers: **−6,670 lines** total (runtime −2,704, docstring mass −29.6%;
learning+top −3,551, −43%; scripts/ui −415, −37%). ~1,150 docstrings
rewritten, none deleted (AST position pins). Every slice PROVED
docstring-only: AST-with-docstrings-blanked and comment-token comparisons
against base — zero code/comment differences; byte-pinned constants
(AGENT_SYSTEM_PROMPT, tool-schema descriptions) untouched. Suite exactly
baseline (6019/225/1) in all three worktrees and after the merge; ruff
clean. Incidental accuracy fixes: two stale scope claims deleted
(retrieval/__init__, reranker "NOT WIRED YET"), one dead file pointer
dropped, the generalize package's pre-keep-and-annotate claim corrected.

## #24 — T4.2: executor leaf path collapsed into the DAG path (2026-08-18)

- 193-line leaf body deleted; single-node blueprints synthesize a 1-node
  DAG at execute-time (never in Blueprint.parse — the stored shape stays
  honest for resume()'s guard, _all_referenced_slots and the learning
  plane) and delegate to _execute_dag. executor.py −134 net. The ~90
  existing single-path tests passed UNCHANGED — the equivalence proof —
  plus 2 new (the gate-ordering trap with a binds_to slot; sql ==
  [terminal_sql]). Review: APPROVE after an adversarial arm-by-arm
  equivalence audit; fold: the silenced SLOT_INVALID warning restored.
- Declared behaviour deltas (all approved-by-record): (1) single-node step
  event `executing` → `executing_node`/0 — UI- and span-invisible;
  (2) **the pre-bind read-only gate now covers DAG node templates** —
  strictly safer; closes the hole where a poisoned DDL node reached
  dispatch and died as RUN_BLUEPRINT_INTERNAL_ERROR via an uncaught
  TemplateBindError from the grain-probe assert (reviewer independently
  confirmed the unguarded raise path); (3) verify-fail log variant.
  Precedence note (review nit): the rule/slot name-collision flip is
  write-time-guarded (corpus_loader rejects the shape), reachable only for
  poisoned records, and fail-safe in both orders.
- **Cross-binary persistence probe PASSED** (the risk V0/V1 cannot touch):
  a mid-DAG approval checkpoint produced by the PRE-collapse binary
  (upstream node run, awaiting_node=1, completed_nodes_json persisted)
  resumed on the COLLAPSED binary — verified result, sql len 2, upstream
  NOT re-run, grain pass, provenance determined.
- **Incidental find + fix: the fake-launcher trigger routing has been dead
  since the 08-13 date-anchor injection** — assembly splices anchor/
  retrieval/state as user-role messages before the question, so
  DemoModelClient's first-user-message matching hit the anchor and every
  turn fell through to the fallback (live-proven: bare "ask" fell
  through). Fixed by filtering the runtime-authored inserts before
  routing and turn-count heuristics; 56 demo-suite tests green (they
  drive the loop pre-assembly, which is why CI never saw it).
- V0 **6021 passed / 225 skipped / 1 xfailed**; V1 8 cases at/above
  baseline (L3 its accepted 1/3); ruff clean.

## #25 — T4.4: tool span envelope (2026-08-18)

- `RuntimeToolBase(ABC)` in `dispatch/tool_envelope.py`: observer events +
  `in_tool_span` + guarded dispatch behind one seam. Honest scope: 3 clean
  adoptions (retrieval read tools −45, blueprint tool −43, analysis_state
  −21) + 1 partial (resolve_values takes only the span-half via
  `in_tool_span`; deliberately not subclassed, rationale in-file) + 1
  leave-in-place. Review: APPROVE after tracing all six outcome paths
  (ok/denied/error/raise/guarded-raise/tracer-None) byte-equivalent
  against the old per-site code.
- Fail-closed by construction: `_span_args` is abstract with NO default —
  forgetting it is a TypeError at instantiation, not a silent D25 leak;
  `__init_subclass__` makes empty `tool_name`/internal-error identity fail
  at class-definition time (reviewer's snippet corrected by the builder:
  `__abstractmethods__` is unreadable inside `__init_subclass__`, so it
  probes the base's abstract names for still-abstract methods).
- Review fold: (1) a guarded-hook re-raise used to escape `_guarded`
  entirely (Python never consults the sibling except arm), landing in the
  loop's outer guard with observer symmetry broken — now wrapped, routing
  to the tool's own single-sourced `_internal_error` arm, comments in
  analysis_state corrected to match; (2) `_EMITS_OWN_PROGRESS` deleted
  (zero users — the flag failed the deletion test).
- R1 belt proven real, not theoretical: under the old default a SQL-shaped
  secret in an exception landed verbatim on span events;
  `record_exception=False` on envelope spans withholds the text and keeps
  ERROR status. Both belt tests kept incl. the positive control
  (dispatcher post-hoc markers keep the recording default — per-caller,
  not global). `_ERROR_PROVENANCE` frozenset-vs-None pins D44 replay
  semantics per tool.
- V0 **6029 passed / 225 skipped / 1 xfailed**; V1 7/9 with the two known
  oscillators red → isolated rerun L3 2/3 (accepted baseline 1/3), L7 3/3
  — low-tail, not regression (the slice is span-plumbing only); ruff
  clean. Follow-up on record: R7 resume-path span (agent_loop) still
  unspanned.

## #26 — T4.1: blueprint compiler extracted from corpus_loader (2026-08-18)

- `corpus_loader.py` 3,249 → 1,396: the compile bucket (~1,640 lines)
  moved to NEW `runtime/blueprint/compiler.py` (1,668); seed DTOs +
  `CorpusLoadError`/`DimensionMismatchError` + the entry→seed parsers
  moved to NEW cycle-proof `data_agent/corpus/seeds.py` (252, deps:
  dataclasses/typing/logging only). Pure move proven twice, mechanically:
  builder's re-extract-and-byte-compare against the base sha, AND the
  reviewer's independent full-coverage AST byte-diff — zero body diffs
  modulo five name promotions (`validate_blueprint_uses`,
  `validate_blueprint_dag`, `dag_properties`, `fetch_existing_models`,
  `fetch_existing_vector_dims`). The 356-line `validate_blueprint_dag`
  god-gate moved unsplit (per-gate split = its own future slice).
- The point of the slice: `learning/generalize/mapping.py`'s edge to the
  loader is fully cut (DTOs from `corpus.seeds`); `promotion/landing.py`
  keeps only the legitimate `load_corpus` writer edge. Honest caveat
  (reviewer-measured): package-level `import data_agent.learning` still
  pulls the loader via `learning/__init__` eager re-exports → landing —
  pre-existing property, not this slice's defect.
- Traps dodged by design: `_warn_on_catalog_skew` deliberately NOT moved
  (skew tests filter caplog by the literal loader logger name — moving it
  would make their negative assertions pass vacuously); `CorpusLoadError`
  single-class-object invariant verified (`is` across all three modules);
  compiler imports nothing from `runtime.retrieval.*` (the pre-existing
  retrieval↔blueprint import cycle is order-load-bearing); cold-import
  gates run in three separate processes. Identity tests edited honestly
  (consumers map → compiler) instead of re-exporting dead constants.
- Review: APPROVE, zero blockers/suggestions. Two informational nits on
  record: four moved warnings now log under
  `data_agent.runtime.blueprint.compiler` (ops filters keyed on the old
  name go quiet for those messages); the grammar parity sweep no longer
  watches corpus_loader (unreachable today — the loader has zero grammar
  references left).
- Worktree env note for future slices: fresh worktrees need
  `uv run --extra dev pytest` (pytest-asyncio lives in the dev extra).
- V0 on main after compose with T4.4: **6029 passed / 225 skipped /
  1 xfailed**; cold imports + ruff clean; V1 **8/9 at/above baseline,
  L3 its accepted 1/3**. Wave B complete — Tier 4 closed.

## #27 — R7: resume-path tool span (2026-08-19)

- The blueprint RESUME path (approval/askUser checkpoint → /turn/resume →
  executor.resume) ran spanless — the pause and the answer appeared in
  Phoenix with nothing between. `_resume_blueprint` now lifts the
  executor re-entry + outcome mapping verbatim into a closure run under
  `in_tool_span` (the T4.4 envelope discipline: optimistic ok,
  record_exception=False, single-expression stamp), placed between the
  PRE-EXISTING tool_dispatch_start/ok/error events. Span name stays
  `tool.runBlueprint` (groups with the first call); the resume half is
  `tool.args.resumed=True`; allowlist = id/resumed/awaiting_node/
  slot_count — reviewer confirmed the approval answer has no path onto
  the span under any posture and awaiting_node is int-coerced at
  checkpoint load before this code can run.
- `AgentLoop.__init__` gained keyword-only `tracer=None` (Layer-1 tests
  unaffected); app.py passes the composition-root tracer, so the resume
  span nests under the turn's agent_span. Review: APPROVE, zero
  blockers — closure byte-identical to the pre-change code, exception
  flow unchanged in both directions.
- V0 **6034 passed / 225 skipped / 1 xfailed**; ruff clean. V1 deferred
  to the combined J7+R7 gate (next entry) — the resume path is not
  exercised by the routing eval; the L3 re-measure covers the shared
  surfaces.

## #28 — C3 closed: production workloads off the uvicorn CLI (2026-08-19)

- The residual from the hygiene wave: runtime/ui/inbox-ui deployed as bare
  `uvicorn <module>:<app>` — no repo code brackets serve(), so SIGTERM
  exits 143 and orchestrators read a rollout as a crash. NEW
  `scripts/run_runtime_api.py` + `scripts/run_ui_bff.py` (one launcher
  serves ui AND inbox-ui — they ran the identical command, split by env)
  on `run_http_daemon`; chart commands + Dockerfile CMD migrated; helm
  README table updated. Rendered output: zero uvicorn across all 8
  workloads; helm lint clean.
- Load-bearing detail: each launcher DEFERS the heavy import into the
  factory body — uvicorn calls the factory from config.load() inside
  serve(), i.e. inside the SIGTERM-captured region; a module-scope
  import would boot seconds of app outside any handler. Review fold: the
  AST guard now pins that placement (module-scope Import/ImportFrom
  naming ui/data_agent.runtime = failure; the in-factory import must
  exist) — proven to fail on both hoisted-import forms while every other
  test stayed green, which is why it exists. Guarded at three seams:
  adoption, rendered-command, placement.
- Real SIGTERM smoke (builder + reviewer independently): BFF serves,
  SIGTERM → "graceful shutdown complete, exiting 0" in stdout, exit 0.
  Reviewer ruled all four flagged assumptions SOUND (argv host/port,
  Dockerfile CMD in scope, run_ui.sh stays dev-only, process-name change
  harmless). Note for ops: root logger now INFO in pods (was
  WARNING-via-lastResort) — deliberate, matches daemon workers.
- V0 **6051 passed / 225 skipped / 1 xfailed**; ruff clean; review
  APPROVE.

## #29 — J7: concrete window anchor on data-anchored results (2026-08-19)

- The queued J7 lever: the static "window is data-anchored" note left
  responsiveness an inference, and 2-of-3 live runs re-derived with
  calendar SQL despite reading it. Now `_stamp_window_anchor` computes
  `window_start`/`window_end` from the terminal rows' grain column
  (max/min of the mapped column — the executor holds the FULL rows;
  runBlueprint passes no query_limit) and the note names the date:
  "…ends {window_end} … state in prose when that differs from the
  calendar period the user asked about; do not re-derive". Zero new
  queries, no prompt change (154-char ceiling headroom untouched), no
  YAML change — the ledger had already pre-authorized detail-on-result.
- Fail-closed everywhere derivation isn't safe: no/multi-column grain,
  unmappable column (reachable via verifiable:false grains — reviewer
  caught the reachability claim), truncated, empty/short rows, NULL or
  non-date values (a NULL aborts the WHOLE derivation — naming a window
  while dropping rows would lie), impossible dates. Values normalized to
  ISO strings BEFORE min/max (no mixed-type compare). Read side:
  `data_anchored_result_note` treats persisted `window_end` as untrusted
  — regex shape + `date.fromisoformat` calendar check, 11-case poisoned
  parametrize (Unicode digits, 9999-99-99, 2021-02-30, injection text).
  D45 resume parity by construction (shared mapper); pre-slice
  checkpoints render the static note byte-identically.
- Review: APPROVE, zero blockers; both should-fixes + both nits folded.
  bp-hires-projection (grainless) stays on the static note by design.
  L3 re-measure lands with the combined V1 gate (next entries).
- V0 **6077 passed / 225 skipped / 1 xfailed** (+26 for the slice); ruff
  clean.

## #30 — M4: the promotion hop, exposed end to end (2026-08-19)

- ISSUES M4's "the promotion hop does not exist" was STALE: the mapping
  pass found verify (service.py:545) and promote (:561) built and tested
  at the service layer, the YAML emitter preserving the landing id so
  the canon reseed MERGE flips the SAME Neo4j node learning→mcp in
  place, and the naive flip-source-in-place shortcut proven self-erasing
  (GC deletes source='mcp' nodes with stale corpus_sha — the design
  routes promotion through canon for exactly this reason). The gap was
  one allowlist: the BFF rejected the verbs and never listed
  `validated`.
- Landed: BFF actions += verify/promote (promote may carry the optional
  {doc_id,title} body; its PromotionEmit response passes through
  untouched — six string fields, reviewer confirmed nothing sensitive
  rides in it); list statuses += validated AND promoted; inbox page
  gains Promotable + Promoted tabs, verify/promote buttons, a YAML panel
  (textContent-only sinks, zero HTML injection surface, clipboard copy
  with honest role=status feedback), node_stamped=false re-verify
  warning, a verified badge on promotable cards, verify disabled after a
  successful promote, and "Re-emit YAML" on promoted cards — the
  service's idempotent re-emit means a lost PR is always regenerable.
  New drift guard: BFF and service status allowlists asserted EQUAL (the
  exact drift that hid validated).
- Review: APPROVE; both named follow-ups (promoted-list recovery,
  verified badge) folded as P1b per the reviewer's own prescriptions.
  The remaining seam is DESIGNED manual: promote returns YAML → human PR
  into clickhouse-api canon + parity --write → mcp image rebuild →
  hydrator reseed flips the node. No git access in the inbox, by design.
- V0 **6094 passed / 225 skipped / 1 xfailed**; ruff clean. Live
  proof (verify → promote → canon → recall serves) lands with the
  campaign-closing mining test.

## #31 — H8: three-way denial taxonomy, two commits (2026-08-19)

- Patch 1 (2b01d3e): `DenialKind` (GATE / WORK_JUDGED / INFRA_FAILED)
  replaces `DenialInfo.enforcement` at the single H1 derivation seam;
  `enforcement` survives as a derived property (kind is GATE) so every
  reader keeps its shape; GATE == exactly the old True set, zero
  movement. The ONE behavior change: `_blueprint_usages` also skips
  infra failures — a CLICKHOUSE_UNAVAILABLE outage no longer records
  `outcome="corrected"` against a blueprint's reputation (triage K4 keep
  + 0.5 inbox ranking penalty stop firing on outages) while
  `_failed_fixed_pairs` deliberately still counts it as analyst
  friction. The asymmetry the boolean could not express, pinned
  four-ways in one parametrized test over the whole derived infra set.
- Patch 2: the map's bigger find — SIX codes landing on
  runQuery/runBlueprint trail entries were never registered
  (RUN_BLUEPRINT_NOT_FOUND/SLOT_INVALID/UNSUPPORTED/VERIFY_FAILED/
  ABORTED, INTERNAL_TRANSPORT_ERROR), so the unknown-code fallback
  filed them all work-judged→"corrected" — UNSUPPORTED/NOT_FOUND being
  the dominant real-world source of false corrections, worse than the
  outage H8 named. Registered with kinds (UNSUPPORTED/ABORTED → GATE,
  reviewer upheld both with reasoning; INTERNAL_TRANSPORT_ERROR →
  infra; rest work-judged), honest replay user_messages byte-identical
  to the live production strings (import cycle → strings spelled +
  byte-equality test importing both sides), which also fixes the latent
  bug where all six rendered the generic "Something went wrong" on
  replay. Deliberate metric movement, stated.
- Review: APPROVE ×2; folds: SUBSTANTIVE_DATA_CODES extended with the
  three new work-judged codes (the set's own derivation rule demanded
  it); GATE comment reworded to "no verdict was formed on the work"
  (mid-run when-abort runs nodes before stopping). Deferred, on record
  in ISSUES: RUN_BLUEPRINT_INVALID_ARGS/INTERNAL_ERROR/UNAVAILABLE
  registrations + executor.py:762 retryable reconciliation.
- V0 after patch 1 **6124**, after patch 2 **6156 passed / 225 skipped /
  1 xfailed**; ruff clean at both points.
