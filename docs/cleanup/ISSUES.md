# Detected Issues Stack

Committed snapshot of the running issues queue (2026-08-17, end of the
cleanup phase — see docs/cleanup/WORKLOG.md). Source of truth going forward:
this file. Convention: append new findings to the relevant section with a
date; when one is fixed, delete the entry and note the fix in WORKLOG.md.


A **running stack of detected issues** in `data-analysis-agent`, opened 2026-08-11 (Session 25) during a read-only analysis pass over the agent loop, tool surface, catalog schema, and blueprint execution. Fixed entries are deleted (with a WORKLOG pointer) or annotated; the rest is the queue.

**Why:** these were found by reading code against docs, not by a failing test, so none of them is recorded anywhere else. Several are silent-behavior issues (no error, no test failure) that would be re-discovered from scratch next session.

**How to apply:** treat as a stack — append newly detected issues to the bottom of the relevant section with a date; when one is fixed, delete the entry and note it in `docs/WORKLOG.md` (not here). Confirm an entry still reproduces before acting on it; line numbers drift.

---

## A. Agent loop (`runtime/loop/agent_loop.py`)

**A1 — FIXED 2026-08-12.** Quadratic budget accounting → `max_window_token_spend=1_000_000` (`BudgetGuard.max_token_spend`), derived as 25 rounds × 40k mean request; occupancy was never at risk (fit_request_to_budget held). Cached reads counted at full weight deliberately.

**A3 — `load_trail` is re-read 3+ times per window.** Guard seeding, `ContextAssembler.assemble`, and `_compute_turn_provenance_union` each load the full trail, plus `_compute_turn_assumptions` / `_compute_turn_answer_sql` on resume paths. Against Couchbase this is repeated I/O per round-trip.

**A4 — D22 discards the model's free text around tool calls.** `_tool_trail_entry_to_canonical` synthesizes `assistant(content=None, tool_calls=[...])`, and `result.assistant_text` on a tool-calling round is kept only as `last_assistant_text` for pause/ceiling returns — never persisted. So the model carries no reasoning between rounds, only calls and results. Interacts directly with the prompt's Planning section (see B1).

**A5 — Retrieval memo is per-window, not per-turn.** `retrieval_memo` is created fresh in `_run_loop_body`, so a budget-cap "continue" or any resume re-embeds and re-recalls the same question.

---

## B. Agent prompt (`runtime/prompts.py`, 11,297 chars ≈ 2,824 tokens, sent every round-trip)

**B1 — Planning section is written against a memory the loop deletes.** It instructs a full decomposition before the first tool call, then admits the plan cannot be re-read (A4). The model is asked to produce a plan it structurally cannot retain, on every complicated request.

**B2 — Date anchor is UTC, so it reads a day ahead of a local evening (residual; the core was FIXED 2026-08-13, see §G).** `context/assembly.py` now injects `Today's date is YYYY-MM-DD.` as one `user`-role message per turn, taken from the turn's own first `user` message `ts` (D45-stable across rebuilds, validated-not-trusted, absent rather than wrong). That `ts` is `datetime.now(UTC)`, so for a user west of UTC the anchor rolls over during their evening and the model is told it is already tomorrow — it will resolve "yesterday"/"this month" off a date the user does not recognise. **Open choice, not a defect to patch blindly:** the fix is a tenant/user timezone (none is carried today), and switching to server-local would just relocate the skew. Decide whether the anchor is UTC-by-contract (and say so in the anchor text) or timezone-aware.

**B3 — Instruction/mechanism mismatches.** Prompt says "issue those tool calls together in one turn" without naming the cap (`max_tool_calls_per_iteration=8`, silently truncated). Says call `recordAssumptions` "exactly ONCE" while `_accumulate_assumptions` is built to fold repeated calls. `answerWithTable` terminality is stated three times (prompt Presenting-a-table, prompt Answering, tool description).

**B4 — Answering section reads as a patch from one incident.** The forecast/projection policy is markedly more specific than its neighbours and duplicates guidance already in the tool descriptions.

---

## C. Tool surface

*(C1 — tool-count doc drift — ALREADY CLOSED, verified 2026-08-18. The fix landed with the Release-1 work (`37344be`/`e8cf36e`), ahead of this queue: `docs/02-tools-and-api.md` now says **15** throughout (header, per-group tables, count summary), `recordAssumptions`/`answerWithTable`/`updateAnalysisState` are all documented, and `answerWithTable`'s second-terminal-exit role has its own section. The `mcp/tool_schema.py` comment reads "count 6 → 15". **Re-counted from code:** 6 MCP (`listDatabases`, `listTables`, `getTableSchema`, `sampleRows`, `runQuery`, `explainQuery` — the 6 `@mcp.tool`s in `clickhouse-api/app/mcp_server.py`) + 9 in `_LOCAL_TOOL_SCHEMAS` (`askUser`, `resolveValues`, `searchBlueprints`, `getBlueprint`, `searchKnowledge`, `runBlueprint`, `recordAssumptions`, `answerWithTable`, `updateAnalysisState`) = **15**, appended unconditionally by `fetch_function_schemas` with no downstream filtering, and pinned by three `len(schemas) == 15` assertions in `tests/runtime/mcp/test_tool_schema.py`. Entry deleted.)*

**C3 — uvicorn workloads exit 143 on SIGTERM. FIXED (hygiene wave 2026-08-18, closed 2026-08-19).** `capture_signals` re-raises the captured SIGTERM onto the restored `SIG_DFL` after `serve()` returns, killing the process where no code can see it (verified: returncode=-15). `data_agent/http_daemon.py::run_http_daemon` chains that re-raise onto a handler installed first, builds the app via `Config(factory=True)` so composition happens INSIDE the captured region (mid-composition SIGTERM smoked graceful), and preserves uvloop via uvicorn's own runner. The last three holes were the chart's own `command:` — `runtime`/`ui`/`inbox-ui` deployed as `uvicorn <module>:<app>`, where uvicorn owns `main()` and nothing in this repo brackets `serve()`. They now run `python scripts/run_runtime_api.py` and `python scripts/run_ui_bff.py` (one launcher for both UIs — they are one app split by env), each handing its app to `run_http_daemon` as a FACTORY that defers the heavy import too, so even a stop arriving mid-boot is graceful; image, env, ports and probes are untouched and the Dockerfile's default CMD moved with them. Smoked: the BFF launcher answers on its port and exits **0** on SIGTERM. Guarded at all three seams — `tests/test_http_daemon.py` parameterizes the adoption check over all five launchers, `tests/deploy/test_helm_launcher_commands.py` fails any rendered workload that goes back to the uvicorn CLI or names a script the repo does not carry, and `tests/test_http_launchers.py` pins the heavy import INSIDE each factory (hoisting it to module scope re-opens the mid-boot window while passing every other test — verified, which is why that assertion exists).

**C4 — `schema_notes` is authored across the semantic catalog but has ZERO readers (2026-08-17, found during J1 placement analysis).** `build_table_schema_response` returns a fixed key set that omits it; it ships in `/catalog/export` verbatim but nothing renders it anywhere in either repo. Guidance authored there is invisible. Wire it into the schema response or mark the field inert in the authoring docs. **DOCUMENTED AS INERT 2026-08-18** (this repo can only document — the field and its only would-be reader live in `clickhouse-api`): a ⚠️ bullet in `09-infrastructure.md` §Semantic Catalog tells authors it never reaches the model and to use `columns[].description`/`rules`/`ambiguities` instead, and the `getTableSchema` row in `02-tools-and-api.md` notes the response key set excludes it. Re-verified: zero hits for `schema_notes` in this repo's `src/`+`tests/`, and `overlay.py::build_table_schema_response` builds from an explicit `entry.get(...)` list that omits it. **The wire-or-remove decision is still OPEN** and is a `clickhouse-api` change.

**C2 — Full 14-tool schema list is re-sent every round-trip**, with no `tool_choice`, `temperature`, `parallel_tool_calls`, or reasoning params set on either the Responses or Chat path (`model/openai_client.py`).

---

## D. Blueprints (`runtime/blueprint/`)

*(D1 — F2 doc drift — CLOSED 2026-08-18. `04-blueprints.md` was already corrected ahead of this queue (release-1 checklist) and re-verified line-by-line against `blueprint/executor.py`: the `scratch_client is None` pre-dispatch check, `_materialize_node`'s truncation/no-column/over-cap fail-closed arms, and the un-materialized-on-resume `SLOT_INVALID` all match the prose. **The same stale claim survived in four other docs and was fixed in this batch:** `11-testing.md` (both the Layer-1 and Layer-2 rows), `decisions/DECISIONS.md` (D89 preamble + D89(d), dated SUPERSEDED-by-D93 markers), `decisions/TRACEABILITY.md` (the D59 and D89/F2 rows), `decisions/OPEN-QUESTIONS.md` (execution-semantics bullet). Entry deleted.)*

**D2 — `semantic_catalog` is accepted-but-unread in `BlueprintExecutor`.** The D56 gate verifies only against the blueprint's **own declared** `result_grain`, never against the catalog's table grain/measures — so a blueprint declaring a wrong-but-self-consistent grain passes verification. This is the D37 authoring-gate gap, still open.

**D3 — `signature_checked: False` everywhere.** The signature half of D56 is vacuous (no declared signature is stored); only the grain row-count check has teeth. Honestly reported, but it means "verified" is weaker than it reads.

---

## F. Hard limits worth knowing before designing

**F1 — The MCP hard-caps every query result at 1,000 rows.** `max_response_rows` defaults to 1,000 (`clickhouse-api/app/config.py:148`) and `_compact_result` truncates at it **regardless of any caller LIMIT**; `BlueprintExecutor` passes `query_limit=None`. `scratch_max_rows` is 10,000, so it never binds. Any design that chains a query result into further analysis has a usable band of only `20 < rows ≤ 1000`. Raising it changes response size for every tool — not a runtime-side knob. Found 2026-08-11.

**F2 — A truncated result is already guarded, but only on the internal path.** `_materialize_node` (executor.py:1057) refuses to materialize a truncated intermediate — a prior blocker fix, because a partial scratch table makes a downstream JOIN silently under-count. Any *new* materialization path needs the same predicate; note the correct response differs by context (fail the DAG vs. degrade only the handle).

## E. Auth / session lifetime

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

**H8 — FIXED (outage codes strained the binary enforcement taxonomy; found 2026-08-17 as an H1 review nit).** `DenialInfo.enforcement` was a boolean serving two readers with different semantics: `CLICKHOUSE_UNAVAILABLE` (enforcement=False) on a `runBlueprint` made `_blueprint_usages` infer `outcome="corrected"` — "this blueprint was wrong" from a warehouse outage, which flowed to `SessionSignals.corrected_blueprint`, a 0.5 inbox ranking penalty and a triage K4 keep. The real shape was three-way collapsed into two.

The taxonomy is now a `DenialKind` StrEnum (`GATE` / `WORK_JUDGED` / `INFRA_FAILED`, default `WORK_JUDGED`) on `DenialInfo`, at the same derivation seam H1 established — registering a code and classifying it stay one edit, and `test_denial_mapping.py::_EXPECTED_KIND` still fails until a human states the classification in words. `enforcement` survives as a DERIVED property (`kind is GATE`), so no H1-era reader changed shape; `INFRA_FAILURE_CODES` is the new derived export beside `ENFORCEMENT_DENIAL_CODES`, and `loader.py::INFRA_ERROR_CODES` unions it with `INTERNAL_TRANSPORT_ERROR_CODE` exactly as the enforcement set unions the idempotent-read marker.

ONE behaviour change: `_blueprint_usages` now skips infra failures as well as gates, so an outage emits NO usage (not `accepted` — that would be the opposite conflation). `_failed_fixed_pairs` is UNCHANGED and still pairs them, and that asymmetry is the point: an outage is genuinely analyst friction (they hit a wall and re-ran) while being no evidence at all about the blueprint's SQL, and one boolean could not say both. Metric movement is prospective only — `SessionSignals` is stamped at envelope-build, so no existing inbox row re-scores. A second patch registers the six codes that reach the trail unregistered (`RUN_BLUEPRINT_NOT_FOUND` / `_SLOT_INVALID` / `_UNSUPPORTED` / `_VERIFY_FAILED` / `_ABORTED`, `INTERNAL_TRANSPORT_ERROR`), which moves metrics deliberately.

---

## I. UI disclosure surface — remaining after the 2026-08-13 hardening slice (`1885a8f`)

**I2 — DECIDED 2026-08-18 (user): full transparency KEPT.** The `result` SSE frame continues to carry sql_executed/answer_sql/provenance chips/blueprint_use verbatim (D56 transparency is the product posture), and `POST /query/page` keeps accepting SQL from the browser. Revisit only before an external-facing deployment. Note the boundary with I1: transparency applies to the STRUCTURED payload; the model's PROSE is scrubbed (I1 slice).

*(I3 — hermetic corpus fixture drift — RESOLVED en route by the J3/J7 canon+mirror work and deleted 2026-08-18. `tests/runtime/retrieval/test_corpus_loader_structural_key_qa.py` verified green at `f9845f5`: 14 passed, 0 failed.)*

---

## J. R8 live regression sweep (2026-08-13, post-`1885a8f`, 11 turns, transcripts in session scratchpad `sweep/`)

**J2 — PTO multi-intent question dropped from `answerWithTable` to prose.** Baseline (08-13 03:44) answered with a table + verification badge; R8 returned exact-match numbers as prose with `answer_tables: null` — UI loses paging and the verification badge on a genuinely tabular per-employee result. Only route regression in the sweep.

**J3 — Hires blueprint is structurally empty on this dataset. CONFIRMED LIVE 2026-08-17, fix in flight.** It keys off `most_recent_hire_date` (all-NULL, see trap #2 in [[l2-dev-stack-traps]]) AND anchors its window to `max()` of that same column, so it can never return rows here. Real-database sweep (REAL_RETRIEVAL=1, gpt-5.5): L4 and L7 both SHIPPED the empty table as designated + blueprint-backed + grain-checked (0 rows, verification badge on); L1's projection consumed the same empty data and forecast "roughly 0 people... based on 0 hires" — coincidentally right on this dataset, mechanically wrong (any tenant with real hires gets a confident wrong forecast). Fix: re-key blueprint (and the L1 projection input) to `hire_date`; slice launched 2026-08-17 — 3 blueprints re-keyed (incl. `bp-hires-in-range`, same defect), canon patch applied to clickhouse-api + sidecars regenerated.

**J6 — D56 verification is vacuous at 0 rows; empty-but-verified ships to the user (design decision needed, added 2026-08-17).** The grain check passes trivially on an empty result, so a structurally-empty blueprint (J3) presents a verified badge on a 0-row table, and the no-re-derivation rule (correct per L6) then forbids the model from sanity-checking with fresh SQL. **DECIDED 2026-08-17 (user): option (a)** — badge reports `row_count=0` as "empty — unverifiable" instead of verified; **BUILT, reviewed (APPROVE), live-verified, committed 3df9e76 same day.** Options (b) re-derivation-lock release and (c) prompt nudge deliberately NOT taken. Sibling of D2/D3 (verification weaker than it reads).

**J7 — blueprint period semantics: data-relative windows collide with calendar-relative questions (2026-08-17, from the L3 trace read).** `bp-hires-per-month` counts "last N months" back from `max(hire_date)` (data-relative by design → answers about 2021 on this warehouse); the user's "last six months" means calendar months, and since the date-anchor injection (08-13) the model HAS a grounded today — so in 2 of 3 L3 re-baseline runs it ran the blueprint, judged the 2021-window result unresponsive, re-derived with `toDate(today)`-anchored SQL, and completed the intent with its OWN query as evidence. The re-derivation metric (newly judgeable under gpt-5.5 intent tracking) correctly flags it. Every component is defensible; the collision is corpus design. **Option (b) BUILT + landed 2026-08-17 (25429a8 + clickhouse-api 11258a9): PARTIAL EFFECT.** Delivery proven (note in model payloads, all 3 conclusive runs, after fixing the eval conftest field-drop 6b30465); behaviour moved 1-of-3 only — two runs re-derived DESPITE reading the static note. Next levers (queued, on hold): surface the CONCRETE anchor date ("window ends 2021-03-01") on the result so the model can present the discrepancy instead of resolving it with SQL; and/or decide the product question — an answer giving BOTH windows ("calendar: 0; as of latest data: 3 in Mar-2021") may be the ideal, but the no-re-derivation contract forbids the query that produces it. L3 stands red (1/3) with this understood cause. Also re-confirmed in the same traces: raw-SQL table designation whenever ad-hoc SQL is in the turn (0 blueprint-backed/verified — the badge-loss pattern from the harness re-baseline), and one run exhausted the answer-shape gate.
**Concrete-anchor lever BUILT (2026-08-19).** The first of the two queued levers is in: `_stamp_window_anchor` now derives the window's real extent from the terminal rows the executor already holds — the declared single-column result grain mapped to its output column (the same `map_grain_columns` the D56 gate uses), min/max over that column's values, stamped as `window_start`/`window_end` on `result_full` (no new query, additive keys). `window_note_for_result` renders the concrete variant when `window_end` is present: "…counts back from the latest data on record, which ends 2021-06-01 — not from today's date. Present it as 'as of the latest data (2021-06-01)' and state in prose when that differs from the calendar period the user asked about…". FAIL-CLOSED to the previous behaviour (anchor string only, static note) on: no declared grain (`bp-hires-projection`'s shape), a multi-column grain, an unmappable grain column, a truncated result, no rows, or a non-ISO-date value — a wrong date in the note is worse than no date. Calendar-anchored and grainless blueprints are byte-identical to before. Behaviour NOT re-measured yet: the L3 baseline statement above (1-of-3, red) stands as written until the next V1 run.

**J4 — accrual→employee join under-keyed.** Ad-hoc PTO SQL joins `accrual_events` to `employee` on `employee_code` alone (no `client_code`) and groups by `employee_name`, which duplicates across codes (EMP001/EMP006 both "Anderson, Alice A"). Harmless on current data; conflates people on wider data.

**J5 — Metadata answer humanizes the table inventory 1:1.** Post-hardening, "what tables are available" answers with zero physical identifiers (verified) but the subject-area bullets mirror `SHOW TABLES` order with underscores→spaces — letter of I1's rule holds, spirit only partly. Judgement call: tighten the prompt to describe capability areas, not enumerate per-table.

---

## K. A2 live-eval baseline (2026-08-16, cleanup branch baseline run) — DIAGNOSED same day

*(K1 — harness infidelity, empty MCP tool schemas in the A2 live gate — FIXED by the harness-fidelity slice (`3d6d6d1`, WORKLOG #5): byte-exact `tools/list` fixture committed and reviewer-verified against the regen recipe, real `getTableSchema`/`sampleRows` shapes, L3 residual re-worded, three-valued `re_derivation`, `MIN_PASS_RATE` default corrected; all 7 A2 cases re-baselined on gpt-5.5 in the same slice. Entry deleted 2026-08-18.)*

**K2 — residual only: the `no_live_state` tag-drop is still silent (RESOLVED IN PRACTICE, not by construction).** The original entry was "L5 red 0/3, model never declares intents". That is closed by the model, not by the code: under gpt-5.5 the model declares intents unprompted and L5 re-baselined **3/3** (WORKLOG #5). What remains: when the model tags `serves_intent` before any state is live, `strip_serves_intent` still drops the tag **silently** (`loop_intent_tag_dropped{no_live_state}`, degrade-not-fail) and nothing tells the model — so the failure mode is latent behind a model behaviour that could regress with any model change. The **G1 corrective-note slice** (append "tags dropped; declare intents before your next substantive call" at the drop seam) is **BUILT and reviewed but UNLANDED**, contained in its own worktree — optional robustness, land it if a model change re-opens L5. G2 (decomposition trigger under-fires on non-enumerated conjunctive questions, "X and Y" vs "I need three things:") stays open as a prompt nit. G3 (report state-None distinctly) landed with WORKLOG #5. **Do NOT relax late-init.**

---

## L. Cleanup follow-ups (Tier-2 review, 2026-08-16 — queued, not gating)

*(L3 — decision docs describe deleted APIs — DONE 2026-08-18, by annotation (history preserved, nothing rewritten). Dated SUPERSEDED markers added at all four sites: `phase0-runtime-design.md` §5 (the compaction seam — `budget.compact`/`render_messages`/`CompactionResult`/`SummaryCache`/`llm_summarizer.py`/`Redactor` are gone; `fit_request_to_budget` is what ships, and the `assemble` signature shown is stale — Tier 2, WORKLOG #3); `learning-loop-wave3-wiring-design.md` §1 factory table + the §6 prose (`build_promotion_scheduler`/`build_review_inbox` folded into `build_promotion_plane` — Tier 2, WORKLOG #3); `OPEN-QUESTIONS.md` (the D46 compaction-trigger bullet, now also pointing at the dead `history_token_budget` config in L1; and the Observability "Redactor implementation" bullet); `release-1/03-analysis-state.md` (the two `scripts/` `_LazyCouchbaseSessionStore` proxy rows + `test_launcher_session_store_proxies.py` — deleted in **Tier 3**, WORKLOG #4, successor `test_launcher_session_store.py`; the incident note beneath them kept deliberately, since it is why the seam was fixed). Also corrected there: the proposed `runtime/context/sanitize.py` shipped as `runtime/sanitize.py::sanitize_text`. Entry deleted.)*

## M. Tier-5 follow-ups (2026-08-17 — queued, not gating)

**M4 — flywheel demo PART C predates the governed-corpus trust gate (2026-08-18).**
`demo_flywheel_inbox_e2e.py` PART C claims a just-landed learning blueprint
autoplays; recall serves `source='mcp'` ONLY (vector_index.py trust gate,
governed-corpus Phase 2), so an inbox-approved blueprint sits in Neo4j
staging until PROMOTED to canon. Verified live 2026-08-18: the landed
candidate ranks #1 in the raw vector index for the variant question and is
correctly filtered by the trust gate. **NARRATIVE FIXED 2026-08-18** (prints +
docstring only, zero logic): PART C now presents the RAW-path answer plus the
trust gate holding — module docstring rewritten, PART C banner states the
staging boundary, STAGE C2's three branches re-worded (a fast path that DOES
fire is attributed to the MCP-canon corpus, not to what PART B landed, with the
id to compare), and STAGE C3's backstop no longer blames "semantic distance":
it names `AND node.source = 'mcp'` in the recall Cypher as the reason, reports
the miss as CORRECTLY WITHHELD / "THE GATE HELD", and flags the found-branch as
what a trust-gate failure would look like. The SUMMARY line no longer calls the
expected raw path "not reached". **The "promotion hop does not exist" claim was
wrong** (2026-08-19): the SERVICE hop has existed all along —
`inbox/service.py` `verify` (flips `verified=true` on the staging node; source
stays `learning`) and `promote` (emits the MCP canon YAML + PR metadata via
`promotion/mcp_export.py`, moves the candidate to terminal `promoted`), with
`validated` already listable. What did not exist was any way to REACH it: the
BFF allowlists stopped at `{approve, reject, retract, complete}` /
`{in_review, rejected, needs_parameterization}`, so no browser could call it.
**M4-P1 exposes it**: `verify`/`promote` added to `_INBOX_ACTIONS`, `validated`
to `_INBOX_LIST_STATUSES`, a fourth "Promotable" tab on the inbox page whose
cards carry a verify-state badge, offer Verify + Promote, and render the returned
YAML with a copy button. P1b also made the TERMINAL state listable (`promoted`,
in both the service's `_LISTABLE_STATUSES` and the BFF's) behind a fifth tab
whose one action is the service's idempotent re-emit ("Re-emit YAML"): that
affordance existed in `inbox.py` but no list surface could return a promoted
row, so the YAML behind an abandoned PR was recoverable by curl and nothing else.
**Still open (by design, not a gap):** the last leg stays manual — a human takes
the emitted YAML to a PR against the MCP corpus repo, reruns
`tools/check_corpus_parity.py --write`, and rebuilds the mcp image before the
node is served as canon. The inbox has no git access and is not getting any.
PART C can demo the hop up to the emit; the autoplay it wants still needs that
PR merged and the image rebuilt.

**M1 — FIXED in T5.5 (WORKLOG #12): per-tool-call envelope rebuild feeds a branch that almost never fires.**
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
