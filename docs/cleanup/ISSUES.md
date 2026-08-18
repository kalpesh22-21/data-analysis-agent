# Detected Issues Stack

Committed snapshot of the running issues queue (2026-08-17, end of the
cleanup phase — see docs/cleanup/WORKLOG.md). Source of truth going forward:
this file. Convention: append new findings to the relevant section with a
date; when one is fixed, delete the entry and note the fix in WORKLOG.md.


A **running stack of detected issues** in `data-analysis-agent`, opened 2026-08-11 (Session 25) during a read-only analysis pass over the agent loop, tool surface, catalog schema, and blueprint execution. Nothing here is fixed yet — this is the queue.

**Why:** these were found by reading code against docs, not by a failing test, so none of them is recorded anywhere else. Several are silent-behavior issues (no error, no test failure) that would be re-discovered from scratch next session.

**How to apply:** treat as a stack — append newly detected issues to the bottom of the relevant section with a date; when one is fixed, delete the entry and note it in `docs/WORKLOG.md` (not here). Confirm an entry still reproduces before acting on it; line numbers drift.

---

## A. Agent loop (`runtime/loop/agent_loop.py`)

**A1 — FIXED 2026-08-12.** Quadratic budget accounting → `max_window_token_spend=1_000_000` (`BudgetGuard.max_token_spend`), derived as 25 rounds × 40k mean request; occupancy was never at risk (fit_request_to_budget held). Cached reads counted at full weight deliberately.

**A2 — Mid-batch budget break can only fire on wall-clock.** The `if guard.exceeded: break` inside the per-tool-call loop runs *before* this round's `record_iteration`, so iterations/tokens for the current round are not yet recorded. Only the wall-clock arm can trip mid-batch. Probably fine, but it is not what the S3 comment claims.

**A3 — `load_trail` is re-read 3+ times per window.** Guard seeding, `ContextAssembler.assemble`, and `_compute_turn_provenance_union` each load the full trail, plus `_compute_turn_assumptions` / `_compute_turn_answer_sql` on resume paths. Against Couchbase this is repeated I/O per round-trip.

**A4 — D22 discards the model's free text around tool calls.** `_tool_trail_entry_to_canonical` synthesizes `assistant(content=None, tool_calls=[...])`, and `result.assistant_text` on a tool-calling round is kept only as `last_assistant_text` for pause/ceiling returns — never persisted. So the model carries no reasoning between rounds, only calls and results. Interacts directly with the prompt's Planning section (see B1).

**A5 — Retrieval memo is per-window, not per-turn.** `retrieval_memo` is created fresh in `_run_loop_body`, so a budget-cap "continue" or any resume re-embeds and re-recalls the same question.

---

## B. Agent prompt (`runtime/prompts.py`, 11,297 chars ≈ 2,824 tokens, sent every round-trip)

**B1 — Planning section is written against a memory the loop deletes.** It instructs a full decomposition before the first tool call, then admits the plan cannot be re-read (A4). The model is asked to produce a plan it structurally cannot retain, on every complicated request.

**B2 — No date anchor anywhere.** Neither the prompt nor `context/assembly.py` injects "today". Deictic period resolution is deferred and `period` slots resolve against the warehouse domain — yet the Answering section asks for trailing-window projections and forward figures. The model has no grounded "now".

**B3 — Instruction/mechanism mismatches.** Prompt says "issue those tool calls together in one turn" without naming the cap (`max_tool_calls_per_iteration=8`, silently truncated). Says call `recordAssumptions` "exactly ONCE" while `_accumulate_assumptions` is built to fold repeated calls. `answerWithTable` terminality is stated three times (prompt Presenting-a-table, prompt Answering, tool description).

**B4 — Answering section reads as a patch from one incident.** The forecast/projection policy is markedly more specific than its neighbours and duplicates guidance already in the tool descriptions.

**B5 — Slot-type glosses duplicated with no parity test.** The slot-type list in `prompts.py` ("Understanding blueprints") duplicates `SLOT_TYPE_GLOSS` in `runtime/blueprint/models.py`. The existing parity test only pins `SLOT_TYPES` ↔ `SLOT_TYPE_GLOSS`, so the prompt copy can silently drift.

---

## C. Tool surface

**C1 — Tool count doc drift.** `docs/02-tools-and-api.md` says 12; code advertises **14** (6 MCP + 8 in `_LOCAL_TOOL_SCHEMAS`), and the agreed `updateAnalysisState` takes it to **15**. The comment in `mcp/tool_schema.py` says "count 6 → 13". `recordAssumptions` and `answerWithTable` are undocumented there — and `answerWithTable` being a **second terminal exit** of the loop is architecturally significant enough to belong in that doc. Scheduled as step 1 of the implementation sequence in the Q&A doc.

**C2 — Full 14-tool schema list is re-sent every round-trip**, with no `tool_choice`, `temperature`, `parallel_tool_calls`, or reasoning params set on either the Responses or Chat path (`model/openai_client.py`).

---

## D. Blueprints (`runtime/blueprint/`)

**D1 — `04-blueprints.md` stale on F2.** It states table-intermediate DAGs are "rejected pre-dispatch"; the executor now materializes them to session scratch via the D93 side-channel when `scratch_client` is wired, and the fixture corpus has two blueprints exercising it. Rejection is now *conditional* on scratch being unwired.

**D2 — `semantic_catalog` is accepted-but-unread in `BlueprintExecutor`.** The D56 gate verifies only against the blueprint's **own declared** `result_grain`, never against the catalog's table grain/measures — so a blueprint declaring a wrong-but-self-consistent grain passes verification. This is the D37 authoring-gate gap, still open.

**D3 — `signature_checked: False` everywhere.** The signature half of D56 is vacuous (no declared signature is stored); only the grain row-count check has teeth. Honestly reported, but it means "verified" is weaker than it reads.

---

## F. Hard limits worth knowing before designing

**F1 — The MCP hard-caps every query result at 1,000 rows.** `max_response_rows` defaults to 1,000 (`clickhouse-api/app/config.py:148`) and `_compact_result` truncates at it **regardless of any caller LIMIT**; `BlueprintExecutor` passes `query_limit=None`. `scratch_max_rows` is 10,000, so it never binds. Any design that chains a query result into further analysis has a usable band of only `20 < rows ≤ 1000`. Raising it changes response size for every tool — not a runtime-side knob. Found 2026-08-11.

**F2 — A truncated result is already guarded, but only on the internal path.** `_materialize_node` (executor.py:1057) refuses to materialize a truncated intermediate — a prior blocker fix, because a partial scratch table makes a downstream JOIN silently under-count. Any *new* materialization path needs the same predicate; note the correct response differs by context (fail the DAG vs. degrade only the handle).

## E. Auth / session lifetime

**E1 — There is no token-refresh path; a session cannot outlive one hour.** The BFF mints once at `POST /api/session` (`ui/server.py:225`) into `_SESSIONS[session_id]`; the only other write to that map is the test-only `POST /api/session/scope` narrowing affordance. `token_ttl_seconds` is **3600** (`clickhouse-api/app/token_service.py:61`) and `verify_jwt` requires `exp` with 60s leeway — so at ~61 minutes the next turn 401s with no recovery. Product expects 8-hour sessions. Found 2026-08-11 while answering Lead Q2.

**E2 — When refresh is built, it must reuse the session's cached `column_scope`.** Not re-resolve `resolve_column_scope(identity)`. A re-resolve would let an entitlement change land mid-session, which breaks the same-scope invariant that Decision 7 (see the Q&A doc) relies on — and that invariant is what justified dropping scope-hash stamping on materialized scratch tables. The dependency is non-obvious from inside the auth code, so it needs a comment at the mint site.

---

## G. R4→R6 live sweeps (2026-08-12/13) — most items FIXED same-day, kept here for the record

**FIXED 2026-08-13, live-verified in R6+kt runs (uncommitted on top of `37344be`):**
- **G1 bare-text finish** → answer-shape exit gate in `agent_loop.py` (refuse once per window when trail holds a successful runQuery/runBlueprint with row_count>1 and no answerWithTable; ephemeral nudge; never hard-locks). Gate went 7-for-8 nudges live. Each gate has its OWN grant — `claim_finalization_block(kind=...)`, key `"turn:window:kind"` — after shared-grant starvation hit 2/4 three-part runs.
- **G2 q7 death loop** → no-re-run prompt rule (Operating procedure) + G4 extractor fix removed the trigger; q7 now completes in ~9 calls, both runs.
- **G4 CTE alias fail-closed** → clickhouse-api case-A escape via `_is_output_alias_reference` + `prefer_column_name_to_alias=0` pinned in `readonly_settings()` (scope-enforcement setting, not perf). Deployed in rebuilt l2-mcp.
- Also fixed same pass: `updateAnalysisState` slimmed to `{description}`/`{intent_id,status}` (reason_code derived runtime-side from tagged evidence; citation field removed; auto-bind backstop with validator-derived pools — dormant in all live runs, tags always landed); date anchor injected per-turn in `context/assembly.py` (from turn-open ts, D45-stable; NOTE: UTC — reads a day ahead of local in the evening, open choice).

**G3 — trail records `status="ok"` with non-null `error_code`.** STILL OPEN. e.g. refused `getBlueprint` `IDEMPOTENT_READ_ALREADY_SERVED` logged as ok — trail-based metrics over-count success.

**G5 — `numbers()`/table functions still fail closed.** STILL OPEN. `7bcea15` fixed `SELECT 1` but `SELECT number FROM numbers(6)` rejects ("table '_0' not in catalog"). Table functions need catalog-exempt handling.

**G7 — FIXED 2026-08-14 (`c96a077`): fail-to-review built, live-verified, see WORKLOG.** Residual hazard kept open: stale-consumer zombies (a 2-day-old consumer silently raced and ate deliveries; group pruned 2026-08-13, **no auto-prune exists**). Hint-vocabulary work stays deferred — live e2e showed the model's six failures were value-normalization mismatches (quoted `'DDUCT'`, `toDate(...)` wrappers, wrong column for a derived alias vs the totality walk's normalized values), a concrete target for that deferred slice.

**G6 — watch item: `loop_answer_shape_exhausted` rate.** The gate is honor-system (any second bare-text finish passes). Live so far: 1/8 nudges declined (a 2-row breakdown the model judged prose-worthy). If the rate climbs, the nudge wording or a designated-answer requirement is the next lever. Also documented: when the INTENTS grant is exhausted and prose is untabled, force-block finalizes without consulting the shape allowance — untabled prose ships (deliberate never-hard-lock).

---

## H. Learning-plane / runtime seam

**H1 — Enforcement-vs-substantive denial classification is maintained by hand in learning (2026-08-13).** `learning/summary/loader.py::ENFORCEMENT_ERROR_CODES` filters Release-1 enforcement denials (BLUEPRINT_DEFINITION_NOT_READ, FINALIZATION_BLOCKED_PENDING_INTENTS, ANALYSIS_STATE_*, etc.) out of `failed_fixed_count`/`corrected_blueprint`. A drift test pins the codes against `denial_mapping.KNOWN_DENIAL_CODES`, so renames fail loudly — but a **new** enforcement code added to `_DENIAL_TABLE` silently counts as a substantive failure (quiet re-introduction of the negative bias; no mechanical property distinguishes the classes — `retryable` doesn't). Fix when runtime unfreezes: add `enforcement: bool` to `DenialInfo` in `runtime/dispatch/denial_mapping.py` and derive the learning-side set from the table, moving ownership to where codes are registered. Reviewer finding, 2026-08-13 session (learning-plane Release-1 fixes).

**H3 — Strict rewrite skips `role=inline` plan entries, tolerating hallucinated inline literals (2026-08-13).** `learning/generalize/rewrite.py:123`: `rewrite_sql_to_template(strict=True)` `continue`s past `role == "inline"` parameterization entries, so an inline entry whose literal does not exist in the accepted SQL passes S4 with `outcome: ok` and ships a plan that disagrees with the template. Affects ALL candidates (pre-existing, not Release-1). The multi-table variant of this hole was closed at the GeneralizeStage collapse (subset check, 2026-08-13 slice); the general fix — require inline literals to be locatable (`_find_literal` must succeed) under strict mode — is a deliberate S4 behavior change: it would start failing candidates currently tolerated, so check fixtures and decide as its own slice. Reviewer finding with live repro, 2026-08-13.

**H2 — Judge `session_brief` tool-call cap crowded by state-tool churn (2026-08-13).** `learning/judge/prompt.py::session_brief` caps `tool_calls` at `_MAX_TOOL_CALLS = 12`; Release-1 sessions' `updateAnalysisState`/`recordAssumptions` churn competes for those slots and can flip `truncated=True`, making the judge discount its own verdict. Same crowding the extractor payload seam fixed with `_PAYLOAD_EXCLUDED_TOOLS`, but here the cap makes exclusion behavioural, so it was deliberately not applied in the 2026-08-13 slice. Decide together with H1.

**H5 — `generalize_blueprint` raises unhandled `sqlglot.ParseError` on all-rule-predicate WHERE (2026-08-13).** When a SQL's entire `WHERE` consists of `role=rule` parameterization entries, `rewrite_sql_to_template` emits `... WHERE  GROUP BY ...` (and can drop a `sumIf` argument); `canonical_ast_norm` then raises `sqlglot.ParseError` OUT of `GeneralizeStage.process` — `generalize_blueprint` catches `RewriteError` only. Reachable from the ordinary consumer path (any valid candidate of that shape, e.g. `_COVERED` in `tests/learning/extractor/test_totality_hint.py`); dead-letters/aborts the pipeline for that candidate. Found by the fail-to-review builder 2026-08-13 while writing the completion happy-path test (fixture switched to the payroll example to avoid it). Sibling of [[H3]]'s rewrite fragility; fix as its own slice.

**H4 — Worker scripts ignore SIGTERM as PID 1; every K8s rollout stalls then SIGKILLs mid-work (2026-08-13).** The Helm chart execs `python scripts/run_{learning_consumer,learning_sweeper,learning_scheduler,hydrator}.py` directly, so Python is PID 1 with no SIGTERM handler anywhere in `scripts/` or `src/data_agent` (`asyncio.run` handles SIGINT only). PID 1 *drops* unhandled SIGTERM → pod termination waits out the full grace period, then SIGKILLs the consumer mid-extraction/mid-stream-drain, skipping the `finally` cleanup (e.g. neo4j driver close in `run_learning_consumer.py`). uvicorn workloads (runtime/ui/inbox) are fine. Fix in each worker's `_main()`: `loop.add_signal_handler(signal.SIGTERM, task.cancel)` so existing `finally` blocks run; `tini` as ENTRYPOINT is the blunter alternative. Surfaced by the Dockerfile review (image itself is fine) — pre-existing scripts issue, fix before this image serves production traffic.

**H6 — Rule-role rewrite strips the condition argument out of `sumIf(...)` (2026-08-14).** When a `role=rule` parameterization entry's literal lives inside an aggregate-function condition (not a bare WHERE conjunct), `rewrite_sql_to_template` removes it and destroys the expression: live-completed deductions-ratio candidate landed `sumIf(coalesce(p.amount,0)) AS total_deductions` / `... AS total_earnings` — one-argument `sumIf` is invalid ClickHouse AND both metrics became byte-identical. The decline hint steers reviewers toward `role: rule` for exactly these predicates. Mitigated: approve-path static validation + golden replay stand between it and a landing. Same rewrite-fragility family as [[H3]]/H5; hits the model path identically. Found in the fail-to-review live e2e, 2026-08-14.

**H7 — `scripts/run_inbox_service.py:28` builds the app at module import; full write plane cannot start (2026-08-14).** `create_inbox_app()` runs at import, and the full-plane branch constructs `CouchbaseCandidateStore` → `acouchbase.Cluster(...)` which raises `RuntimeError: Event loop is not running` on the current SDK/Python. Pre-existing (`inbox/service.py:373` predates the fail-to-review slice); QA had to hand-build the same app inside a running loop to test live. Fix: defer construction into an ASGI lifespan/startup hook. Same entrypoint-wiring family as H4.

---

## I. UI disclosure surface — remaining after the 2026-08-13 hardening slice (`1885a8f`)

**I1 — Answer-text schema disclosure is prompt-only; no runtime scrub.** The `1885a8f` slice hardened the system prompt (business-terms answers, no table/column/DDL), the progress summarizer (default-deny arg allowlist + structural output filter), and gated internal dispatch progress (`emit_progress=False`: executor ×4, resolveValues, discovery emulation). But nothing inspects the model's final answer prose — a model ignoring the instruction is caught by nobody. Next layer if needed: scrub answer text against catalog identifiers seen in the turn's trail. Only a live sweep measures prompt compliance.

**I2 — The `result` SSE frame still carries full SQL/structure by design (D56 transparency).** `sql_executed` (every query verbatim incl. blueprint node SQL), `answer_sql`/`answer_tables[].sql`, `provenance` (`db.table.column` chips), `blueprint_use` slots — straight projection in `app.py::_outcome_to_dict`, no redaction layer; `redaction.py` is telemetry-only by stated design. Also `POST /query/page` accepts SQL *from* the browser. Curbing these is a product decision (e.g. a config flag gating transparency payloads), deliberately not made in the hardening slice.

**I3 — Hermetic corpus fixture drifted from canon (committed in `e8cf36e`).** `tests/runtime/retrieval/test_corpus_loader_structural_key_qa.py` — 2 standing failures: canon's `bp-active-headcount-by-department.sql_template` gained `ORDER BY headcount`, hermetic fixture lacks it. Only failures in the runtime suite (3036 pass otherwise).

---

## J. R8 live regression sweep (2026-08-13, post-`1885a8f`, 11 turns, transcripts in session scratchpad `sweep/`)

**J1 — Median is wrong for even-sized groups.** "Median annual salary by department" fell through to ad-hoc `runQuery` using `quantileExact(0.5)`, which in ClickHouse returns the UPPER middle value, not the average of the two: Engineering 130,000 (should be 125,000), Operations 75,000 (should be 62,500). Not blueprint-backed (`blueprint_use: null`), so nothing gated the aggregate choice. Standing correctness bug, not introduced by the hardening slice.

**J2 — PTO multi-intent question dropped from `answerWithTable` to prose.** Baseline (08-13 03:44) answered with a table + verification badge; R8 returned exact-match numbers as prose with `answer_tables: null` — UI loses paging and the verification badge on a genuinely tabular per-employee result. Only route regression in the sweep.

**J3 — Hires blueprint is structurally empty on this dataset. CONFIRMED LIVE 2026-08-17, fix in flight.** It keys off `most_recent_hire_date` (all-NULL, see trap #2 in [[l2-dev-stack-traps]]) AND anchors its window to `max()` of that same column, so it can never return rows here. Real-database sweep (REAL_RETRIEVAL=1, gpt-5.5): L4 and L7 both SHIPPED the empty table as designated + blueprint-backed + grain-checked (0 rows, verification badge on); L1's projection consumed the same empty data and forecast "roughly 0 people... based on 0 hires" — coincidentally right on this dataset, mechanically wrong (any tenant with real hires gets a confident wrong forecast). Fix: re-key blueprint (and the L1 projection input) to `hire_date`; slice launched 2026-08-17 — 3 blueprints re-keyed (incl. `bp-hires-in-range`, same defect), canon patch applied to clickhouse-api + sidecars regenerated.

**J3b — semantic catalog still steers ad-hoc SQL to the broken column (2026-08-17; scope grown by J3 review).** `clickhouse-api/app/semantic_catalog/data/employee.yaml` ambiguity entry maps term "hire_date" → `default: most_recent_hire_date`, so every NON-blueprint hires question the model writes ad-hoc lands on the all-NULL column even after the J3 re-key — same question now gives a populated blueprint answer but an empty ad-hoc answer. Fix in the MCP repo: flip the ambiguity default to `hire_date` (the `rehired_not_terminated` rule's use is legitimate — leave it); ALSO add the one-line rehire caveat to the 3 hires-blueprint intents ("counts by original hire date; rehires not re-counted" — J3 review should-fix), and add a suite/CI test running tools/check_corpus_parity.py against the LIVE data dirs (a stale sidecar committed green at a2c8cb9; found during J3). All three touch the same canon area — one slice.

**J6 — D56 verification is vacuous at 0 rows; empty-but-verified ships to the user (design decision needed, added 2026-08-17).** The grain check passes trivially on an empty result, so a structurally-empty blueprint (J3) presents a verified badge on a 0-row table, and the no-re-derivation rule (correct per L6) then forbids the model from sanity-checking with fresh SQL. **DECIDED 2026-08-17 (user): option (a)** — badge reports `row_count=0` as "empty — unverifiable" instead of verified; **BUILT, reviewed (APPROVE), live-verified, committed 3df9e76 same day.** Options (b) re-derivation-lock release and (c) prompt nudge deliberately NOT taken. Sibling of D2/D3 (verification weaker than it reads).

**J7c — learned windowed blueprints will silently lose `window_anchor` at promotion (2026-08-17, J7b review nit; inert today).** Neither `learning/generalize/mapping.py::blueprint_seed_from_candidate` (~:124) nor `learning/promotion/mcp_export.py::_blueprint_doc` (~:127 whitelist) carries `window_anchor` — verified inert (candidates cannot produce the field yet), but the first learning-extracted windowed blueprint will promote to canon anchor-less and reintroduce the J7 failure for learned corpus. When the extractor grows window awareness, thread the field through both projections. Derive-the-guard class.

**J7 — blueprint period semantics: data-relative windows collide with calendar-relative questions (2026-08-17, from the L3 trace read).** `bp-hires-per-month` counts "last N months" back from `max(hire_date)` (data-relative by design → answers about 2021 on this warehouse); the user's "last six months" means calendar months, and since the date-anchor injection (08-13) the model HAS a grounded today — so in 2 of 3 L3 re-baseline runs it ran the blueprint, judged the 2021-window result unresponsive, re-derived with `toDate(today)`-anchored SQL, and completed the intent with its OWN query as evidence. The re-derivation metric (newly judgeable under gpt-5.5 intent tracking) correctly flags it. Every component is defensible; the collision is corpus design. **Option (b) BUILT + landed 2026-08-17 (25429a8 + clickhouse-api 11258a9): PARTIAL EFFECT.** Delivery proven (note in model payloads, all 3 conclusive runs, after fixing the eval conftest field-drop 6b30465); behaviour moved 1-of-3 only — two runs re-derived DESPITE reading the static note. Next levers (queued, on hold): surface the CONCRETE anchor date ("window ends 2021-03-01") on the result so the model can present the discrepancy instead of resolving it with SQL; and/or decide the product question — an answer giving BOTH windows ("calendar: 0; as of latest data: 3 in Mar-2021") may be the ideal, but the no-re-derivation contract forbids the query that produces it. L3 stands red (1/3) with this understood cause. Also re-confirmed in the same traces: raw-SQL table designation whenever ad-hoc SQL is in the turn (0 blueprint-backed/verified — the badge-loss pattern from the harness re-baseline), and one run exhausted the answer-shape gate.

**J4 — accrual→employee join under-keyed.** Ad-hoc PTO SQL joins `accrual_events` to `employee` on `employee_code` alone (no `client_code`) and groups by `employee_name`, which duplicates across codes (EMP001/EMP006 both "Anderson, Alice A"). Harmless on current data; conflates people on wider data.

**J5 — Metadata answer humanizes the table inventory 1:1.** Post-hardening, "what tables are available" answers with zero physical identifiers (verified) but the subject-area bullets mirror `SHOW TABLES` order with underscores→spaces — letter of I1's rule holds, spirit only partly. Judgement call: tighten the prompt to describe capability areas, not enumerate per-table.

---

## K. A2 live-eval baseline (2026-08-16, cleanup branch baseline run) — DIAGNOSED same day

**K1 — L3 red 0/3, root cause = HARNESS INFIDELITY (never passed, not a regression).** `LiveEvalMCPClient.list_tools` (test_routing_live.py:106-119) advertises all 6 MCP tools with `description=""` and EMPTY `input_schema` — the model is told `runQuery()` takes no `sql` and `getTableSchema()` takes no table, so A2 is a blueprint-only arena; only the 2 cases needing a non-blueprint route (L3, L5) are red. Counterfactual proven live: patching faithful schemas (from clickhouse-api/app/mcp_server.py:205-330) → L3 passes. Fixes: F1 commit a `tools/list` export fixture (like `fixture_catalog`); F2 re-word L3's residual (bp-active-headcount already covers it → will flap post-fix); F3 `metrics.re_derivation` returns False vacuously when the turn is untracked. **F1 changes every A2 case's behaviour → re-baseline all 7 in the same slice.** Also seen: with real schemas the model designated raw-`sql` answer tables (0 verified) instead of `blueprint_id` — L7 can't catch verified-rate drops.

**K2 — L5 red 0/3, root cause = MODEL NEVER DECLARES INTENTS + silent tag drop (never passed).** `analysisState` is None in every run — predicate detail "not every intent reached terminal disposition" is a misreport (predicate conflates state-None with pending). The model DID tag `serves_intent` on calls, but `strip_serves_intent` drops tags silently (`no_live_state`, degrade-not-fail by design) and nothing tells the model → it never calls `updateAnalysisState`; when it tries later it hits `ANALYSIS_STATE_LATE_INIT` (natural order search→run→bookkeep vs SUBSTANTIVE_TOOLS lock). 0 of 4 diagnostic runs ever tracked. Fixes: G1 (cheapest/highest-value, RUNTIME change) at the `loop_intent_tag_dropped{no_live_state}` seam (agent_loop.py:~3429) append a corrective note to the tool result — "tags dropped; declare intents before next substantive call"; G2 prompt: decomposition trigger under-fires on non-enumerated conjunctive questions ("X and Y" vs "I need three things:"); G3 predicate: report state-None distinctly. Do NOT relax late-init. L5 needs BOTH the K1 schema fix and the declaration fix.

---

## L. Cleanup follow-ups (Tier-2 review, 2026-08-16 — queued, not gating)

**L1 — `RuntimeSettings.history_token_budget` + `history_token_budget_ratio` are dead config.** After Tier 2 removed `ContextAssembler(history_token_budget=...)` (its only reader), the property (`runtime/config.py:449`) and field (`:798`) have zero readers; the comment at ~:458 describes deleted machinery. `HISTORY_TOKEN_BUDGET_RATIO` set in env is silently ignored. Deleting is a config-surface change → own slice: delete property+field+stale comment, or annotate INERT. `tests/runtime/test_config.py` still pins them.

**L2 — `SessionDoc.context_summary_cache` is a permanently-None persisted field.** `runtime/session/models.py:523,555,578` — nothing can populate it post-Tier-2; round-trips None forever; old Couchbase docs may carry the key, so removal is a persisted-schema change (needs tolerant reader or migration note). `tests/learning/test_content_hash.py:142` still exercises its exclusion.

**L3 — decision docs describe deleted APIs.** `docs/decisions/phase0-runtime-design.md:362-374` presents the compaction seam (budget.compact/render_messages/Redactor) as live architecture; `learning-loop-wave3-wiring-design.md` §25/26 documents `build_promotion_scheduler`/`build_review_inbox`; `OPEN-QUESTIONS.md` and `release-1/03-analysis-state.md` also name deleted APIs. Docs-only commit; decision docs are partly historical — mark superseded sections rather than rewrite history.

## M. Tier-5 follow-ups (2026-08-17 — queued, not gating)

**M1 — per-tool-call envelope rebuild feeds a branch that almost never fires.**
`agent_loop._run_loop_body` computes the answer envelope (a
`rollup_verification` + N `table.to_doc()` calls) unconditionally for EVERY
tool call in a batch, solely to feed `_pause_from_runtime_tool` when
`tool_result.pause is not None` (the T5.4 map's site "3161"; after T5.4 the
same shape is `accum.envelope()` at the same position). Moving the call
inside the `if pause` branch is behaviour-neutral for outputs but was kept
verbatim in T5.4 to stay strictly behaviour-identical. Fix in a follow-up
slice: one-line move + a test that a pause still carries the envelope.

A reviewer document ("Planning and System-Prompt Review") landed the same session, proposing blueprint-first routing over the current SIMPLE/COMPLICATED planning policy. Its questions, the four decisions taken so far, and the follow-on questions those opened are tracked separately in **`docs/decisions/prompt-routing-review-qa.md`** (in-repo, for Lead sign-off) — not here. Issues **A4** (D22 deletes assistant text) and **B1/B3** above are now owned by that document's Decision 4 and Priority-1 workstream.

Related: [[base-prompt-drop-context-budget]] (why the request-fit budget exists), [[l2-dev-stack-traps]], [[phase0-runtime-scope]], [[commit-after-review]].
