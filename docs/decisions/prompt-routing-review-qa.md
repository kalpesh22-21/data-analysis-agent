# Prompt & Routing Review — Q&A and Decision Log

**Status:** SIGNED OFF, then **SCOPED DOWN to Release 1** (Lead, 2026-08-11).
**➡ To build, read [release-1-routing-and-intent-coverage.md](release-1-routing-and-intent-coverage.md)** — the clean, self-contained Release 1 spec. This document is the decision record and audit trail behind it; §R1–§R3 summarise the release plan and preserve the deferred findings.
**Opened:** 2026-08-11 (Session 25)
**Inputs:** (1) "Planning and System-Prompt Review for the HR/Payroll Data Agent" (reviewer document, §1–§21); (2) **"Planning, Routing, Runtime-State, and Validation Review — Consolidated Lead Decision Report" (2026-08-11, §1–§49)**, which supersedes the open state of this log.
**Scope of this doc:** map both documents onto the as-built runtime, record every question raised and its resolution, and record the eleven implementation decisions taken on top of the Lead report. This is the artifact for Lead sign-off.

**How to read:** **§R1–§R3 are the operative document** — the release plan, the Release 1 build contract, and the deferred work with its findings preserved. Read those to build. Everything from §A onward is the audit trail that produced them, retained deliberately (the Lead asked that superseded decisions be marked, not deleted): §C4 is the Lead-report reconciliation, §D the consolidated decision log with §D.1 recording explicitly rejected options, §E the pre-trim sequence, §F a dereference table for every `D<n>` cited, §G a worked example against the real corpus.

**Lead sign-off received 2026-08-11 (five inputs).** Sequential dispatch approved for this release (Decision 9); bounded concurrency becomes a separate future performance slice. Q26 resolved YES — `coverage` becomes a first-class result field (Decision 17). Decision 13's evidence set widened to include `getTableSchema`, with no new `analysisState` field. Decisions 1 and 4 marked SUPERSEDED in the log. The §G worked example corrected so the runtime, not the model, assigns intent IDs.

**Two standing items, both already accepted rather than open:**
- **Accepted risk (Decision 7):** an entitlement revocation does not take effect until the user's next session — up to 8 hours once long sessions ship.
- **Hard limit (§C4.4):** the MCP caps every query result at **1,000 rows** regardless of caller LIMIT, which bounds §7/§8 chaining to `20 < rows ≤ 1000`. Raising it is an MCP-wide change, not a runtime knob.

**Two consequences of the new inputs that need engineering attention** (neither blocks; both are recorded where they land): the widened evidence set weakens completion enforcement in a specific way (§C4.3, D13), and `coverage` must be **materialized at turn end** because its source is discarded (§C4.3, D17).

---

## R1. Release plan

**Scoped down 2026-08-11 on Lead review: the decision set had grown into an architecture-hardening programme rather than a fix for the two problems that started it.** Those two are (a) the prompt routes badly — blueprint preference comes too late even though candidates are already pre-injected; and (b) multi-part requests can lose an intent between rounds because the model's decomposition is discarded. Everything not serving those is deferred.

| | Scope | Decisions |
|---|---|---|
| **Release 1** | Blueprint-first routing · enriched blueprint cards · minimal original-intent `analysisState` · evidence-backed completion **and blocking** · batched state updates · finalization enforcement · minimal Layer 4 with detection + coverage metrics | 3, 8 *(trimmed)*, 10, 13 *(amended)*, 16 *(trimmed)*, **18 (new)** |
| *(withdrawn)* | Decisions 14 and 15 lapse with the v1 trim — both governed derived intents and late initialization, neither of which exists in v1. The reason-code enum they contributed survives in §R2.3. | 14, 15 |
| **Phase 2** | Advisory ad-hoc SQL validators (start with check 7, client-defined value resolution) · `coverage` as a first-class field · routing-telemetry projector | 2, 17 |
| **Phase 3** | Scratch chaining: auto-materialization, handles, provenance map, read-time subset check, truncation behaviour, paging restriction | 5, 6, 11, 12 |
| **Separate projects** | 8-hour session refresh · bounded concurrency | 7 *(refresh rule)*, 9 *(concurrency slice)* |

**Raw telemetry events ship in Release 1** even though the projector does not — the data must not be lost while behaviour is stabilising.

**Sequencing rationale.** Release 1 reduces the ad-hoc SQL surface (by routing more work to blueprints), which is why the ad-hoc validators can wait: Phase 2 gets to target the errors Layer 4 actually observes rather than the ten we guessed at. Coverage waits until `analysisState` is proven, because it is a durable-projection + `TurnOutcome` + history + backend-contract + UI change for something the model can state in prose meanwhile.

---

## R2. Release 1 specification

The buildable contract. Everything here is approved; §R3 is what was cut.

### R2.1 — `analysisState` v1

Exists **only for multi-intent requests**. A single-intent request creates no state at all.

```jsonc
{
  "turn_index": 7,
  "intents": [
    {
      "intent_id": "i1",                    // RUNTIME-ASSIGNED — the model never supplies ids
      "description": "headcount by department",   // an ORIGINAL user deliverable
      "status": "pending",                  // pending | completed | blocked
      "evidence_tool_call_id": "call_a1",   // required: completed, and model-declared blocked
      "reason_code": null                   // required for blocked only; closed enum
    }
  ]
}
```

**Cut from v1** (all previously specified, all removed): `route`, `depends_on`, derived intents and their depth/count rules, withdrawal, creation-evidence, late initialization, `late_init_self_completion`, the free-form `note`, and automatic blueprint-state flipping.

Two of those cuts are free rather than sacrificial. **`route` is derivable** from the evidence call's tool name (`runBlueprint` ⇒ blueprint, `runQuery` ⇒ ad-hoc, `getTableSchema` ⇒ metadata), so routing telemetry survives without the field. And **prerequisite work needs no managed entity** — the model simply does the work; only what the *user* asked for is tracked.

**Storage and lifecycle** (unchanged from Decision 8): a single mutable field on `SessionDoc` beside `pause_checkpoint`, latest-wins, `frozenset()` provenance, dropped cross-turn, re-rendered into context from the store on every round-trip.

### R2.2 — The tool

`updateAnalysisState`, always wired, no backing stack — the `recordAssumptions` class. **Exempt from `max_tool_calls_per_iteration`** (Decision 9), since calls beyond that cap are silently dropped with no error to the model.

The model proposes **descriptions only**; the runtime validates, assigns stable ids, and returns them in the tool result — which is how the model learns them. Declaration is full-replace; subsequent changes are **batched updates**, several intents per call. Batching is load-bearing: without automatic blueprint-flipping, per-intent update calls would reintroduce exactly the bookkeeping overhead this trim exists to remove.

### R2.3 — Evidence rules

| Transition | Requirement |
|---|---|
| → `completed` | `evidence_tool_call_id` referencing a **successful** call in the current turn, from `{runQuery, runBlueprint, getTableSchema}`. `runBlueprint` additionally requires `authoritative == true`. **A `getTableSchema` entry carrying the repeated-read guard marker (`IDEMPOTENT_READ_ALREADY_SERVED`) is not valid evidence** — it is a data-free nudge that fetched nothing. Reuse across intents is allowed and telemetry-flagged. |
| → `blocked`, **model-declared** | Closed-enum `reason_code` **and** `evidence_tool_call_id`; the runtime validates the evidence is appropriate to the reason. Without this, `blocked` is a free exit from any hard intent and the finalization guard degrades to "cannot silently drop an ask *unless it declares the ask blocked*". |
| → `blocked`, **runtime-forced** | `BUDGET_EXHAUSTED` / `USER_STOPPED` only. No evidence — the runtime sets these itself at the hard ceiling or on an explicit user stop. |

**Reason-code enum (v1):** `NO_ACCESS`, `NO_GROUNDED_SEMANTICS`, `NO_APPLICABLE_TOOL`, `REQUIRED_DATA_UNAVAILABLE`, `USER_DECLINED_CLARIFICATION`, `BUDGET_EXHAUSTED`, `USER_STOPPED`. Closed — never free prose. **The per-reason evidence predicates are specified in [release-1-routing-and-intent-coverage.md](release-1-routing-and-intent-coverage.md) §6.2**, including the `NO_APPLICABLE_TOOL` tightening (cited search returned zero cards **or** the turn contains a successful `getBlueprint`) and the `USER_DECLINED_CLARIFICATION` anchor (turn-level: ≥2 user messages, since `askUser` produces a `PauseCheckpoint`, not a `TrailEntry`).

**Two things the blocked-evidence validator must handle that the completion validator does not** (both surfaced while specifying this, both concrete):

- **Blocked evidence is drawn from a wider tool set, and may be a *failed* call.** `NO_ACCESS` is evidenced by a **denied** `runQuery` — `status == "denied"`, not `"ok"`. `NO_APPLICABLE_TOOL` is evidenced by a `searchBlueprints` call; `NO_GROUNDED_SEMANTICS` by `getTableSchema`/`searchKnowledge`. So the completion rule ("successful, and from the three-tool set") is the wrong validator here and must not be reused.
- **`USER_DECLINED_CLARIFICATION` has no `tool_call_id` to reference.** `askUser` is intercepted in the loop and never reaches the dispatcher, so it produces a `PauseCheckpoint`, **not** a `TrailEntry` — there is no call id for the model to cite. Either that reason becomes runtime-set (the runtime knows a pause was answered), or the evidence field accepts a checkpoint reference for this one code. Flagged as an implementation choice under Lead §47; it needs deciding before the validator is written.

### R2.4 — Finalization enforcement

No normal final answer and no `answerWithTable` while any tracked intent is `pending`. On refusal the runtime nudges the model for another round, **capped at one forced re-round per budget window** so enforcement cannot itself burn the window. At the hard ceiling or on an explicit "stop", the runtime marks remaining intents `blocked` with the forced reason codes, so no intent ever ends `pending`.

### R2.5 — Prompt

Remove SIMPLE/COMPLICATED. Behaviour only, no route names. Check offered blueprints first; `searchBlueprints` per unresolved intent; ad-hoc SQL only for what blueprints cannot satisfy; never re-derive an authoritative blueprint result. Retain the §17 reminder that successful execution is not proof of semantic correctness — that stays prompt-side until Phase 2 makes parts of it mechanical.

### R2.6 — Enriched blueprint cards

Derive from `resolves`, `slots`, `result_grain`, `status`. No corpus schema change, no re-seed, no learning-loop change. In Release 1 because per-intent `searchBlueprints` is becoming the default path, and thin cards cost a `getBlueprint` round-trip per candidate on exactly that path.

### R2.7 — Evaluation

Six cases, expanded later:

1. Complex wording satisfied by one blueprint
2. Multiple independent blueprint intents
3. One blueprint + one ad-hoc intent
4. Three intents, one of which would previously have been forgotten
5. Mixed metadata + analytical request
6. Authoritative blueprint result is **not** re-derived

Two metrics, both required:

- **Dropped-intent rate** — tracked intents not reaching a terminal status.
- **Multi-intent detection rate** — *of requests known to be multi-intent, what percentage created an `analysisState`?* This catches what dropped-intent rate structurally cannot see: an undetected multi-intent request has no state, so it has nothing to drop. Layer 4 supplies ground truth initially; production measurement later uses an offline classifier rather than trusting the agent's own judgement. This metric is also what tells us whether late initialization needs adding back in v2.

---

## R3. Deferred work — findings preserved

Deferred, **not deleted**. The analysis below was expensive to obtain and is needed verbatim whenever these resurface.

### R3.1 — Scratch chaining (Phase 3; Decisions 5, 6, 11, 12)

- **A hard 1,000-row ceiling bounds the whole feature.** `max_response_rows` defaults to 1,000 (`clickhouse-api/app/config.py:148`) and `_compact_result` truncates at it **regardless of any caller LIMIT**; `BlueprintExecutor` passes `query_limit=None`. `scratch_max_rows` is 10,000 and therefore never binds. The usable band is `20 < rows ≤ 1,000` — which fits §8's "500 departments" example but excludes any employee-grain intermediate. Raising it means raising `max_response_rows` MCP-wide, changing response size for every tool; it is not a runtime knob.
- **The truncation guard already exists, and its response is wrong for auto-materialization.** `_materialize_node` (executor.py:1057, itself a prior blocker fix) refuses to materialize a truncated intermediate, because a partial scratch table makes a downstream JOIN silently under-count — the wrong-answer class D56 exists to block. For a DAG intermediate, failing to the raw loop is correct. For §7 auto-materialization it is **not**: the blueprint's own result is valid and D56-verified, and only the handle is impossible. Correct behaviour there is to degrade the handle (`scratch_handle: null`, `materialization: "unavailable_truncated"`) and return the answer.
- **Provenance must be carried forward or lineage is lost.** `scratch.*` pairs are excluded from D44's allowlist (session-gated by D64, not scope-gated), so a downstream query reading `scratch.h1` computes provenance from its own SQL and sees no warehouse columns. Rule: store the blueprint's upstream provenance against the handle at materialization; any query reading a scratch table takes `direct warehouse provenance ∪ upstream provenance of every scratch handle read`.
- **The handle map cannot live in memory.** The runtime is stateless across pauses (D45) and multi-instance, so a process-local map breaks chaining on resume — and fails invisibly in single-instance dev. It belongs on the session doc, turn-scoped, fail-closed on a missing entry, reusing the existing `_prov_to_jsonable`/`_prov_from_jsonable` pair.
- **Scratch-backed queries must not be paged.** `answerWithTable` designates the blueprint, never the scratch join, or the paged table breaks at the scratch TTL.
- Materialization needs **its own threshold setting** defaulting to `preview_row_count`; coupling them lets a future preview-size change silently retune chaining.

### R3.2 — Coverage as a first-class field (Phase 2; Decision 17)

Runtime-derived, original intents only. The non-obvious constraint: `analysisState` is discarded at turn end, so coverage **must be materialized before that** — it is precisely the "audit information" Decision 8 says to persist first. Without it, `session_history::project_history` has nothing to walk and the UI transcript loses coverage on reload. Needs no resume seeding, since the state lives on the session doc and survives a pause directly.

### R3.3 — Bounded concurrency (separate project; Decision 9)

§C4.2's five-row table is the specification for this slice. The hardest row is pause-mid-batch: it is the one case where "commit in call order" has no defined answer, and under auto-materialization a discarded `runBlueprint` entry orphans a scratch table.

### R3.4 — 8-hour session refresh (separate project)

There is **no refresh path at all today** — `token_ttl_seconds=3600`, mint-once at `POST /api/session` (`ui/server.py:225`), so a session cannot currently outlive an hour. Whoever builds refresh must **reuse the session's cached `column_scope`** rather than re-resolving `resolve_column_scope(identity)`, and fail closed on a mismatch. The dependency is invisible from inside the auth code and needs a comment at the mint site.

---

## A. Accepted as-is — the review and the build already agree

| Review § | Point | As-built status |
|---|---|---|
| §2.1 | Blueprint results are authoritative; do not re-derive | Built. `authoritative=true` is set only on a D56-verified result and rides an in-band marker + note into the model's tool message (`loop/agent_loop.py::_tool_trail_entry_to_canonical`). The prompt reinforces it. |
| §2.2 | Catalog semantics outrank model reasoning | Built. `getTableSchema` returns the merged, scope-filtered overlay (D83) with `rules`, `ambiguities`, `clarify_if`, per-column descriptions. |
| §2.3 | Never guess tenant-specific codes | Built. `resolveValues` (D77) + 65 `client_defined` columns flagged in the catalog. |
| §2.4 | Assumptions model | Built. `recordAssumptions` → first-class result field; cross-turn replay of the strings is deliberately suppressed (`_is_stale_assumptions_entry`). |
| §2.5 | `answerWithTable` lineage boundary | Built. Server-side resolution of `blueprint_id` → that blueprint's `terminal_sql`; one field on the wire, one paging route. |
| §2.6 | Tool output is data, not instructions | Built. Prompt "Trust boundary" section + structural sanitisation of retrieved text (`retrieval/render.py`). |
| §8 | Do **not** add a `plan()` tool | Agreed; none exists and none is proposed. See Q4 for the one nuance. |
| §17 | "The model does not own correctness" | This is already the architecture's operating principle (D5 credential invisibility, D44/D57 provenance + scope, D49 no-LLM-inside-runBlueprint, D56 verify gate). |

---

## B. Assumption gaps — where the review's model differs from the build

These do not invalidate the review's direction. They change what each recommendation costs.

**B1 — Blueprint routing material is already pre-injected; it does not cost a tool call.**
The review (§6) proposes moving blueprint lookup ahead of table discovery. In the build, the top-3 blueprint thin cards (plus knowledge and user memory) are already retrieved and injected as a `user` message immediately before the current question, before the model's first round-trip (`retrieval_top_k_blueprints=3`). So the model *starts* every turn holding candidate blueprints. Separately, emulated discovery already pre-answers `listDatabases` + `listTables` once per session, so table discovery costs zero round-trips too. **The real ordering problem is prompt text, not mechanism:** the Operating procedure leads with "once you know the table you need, call `getTableSchema`…" and the blueprint-preference bullet appears five bullets later. The cheap, correct fix is to reorder and re-weight the prompt — not to add a retrieval step.

**B2 — The review under-states the planning problem: the plan is not merely un-persisted, it is deleted.**
§8 argues against persisting plans. In the build it goes further. **D22** reads, in full:

> **D22.** Couchbase session store keyed by `session_id`; persist messages + tool I/O trail; discard thinking.

The implementation of that last clause is `_tool_trail_entry_to_canonical`, which synthesizes `assistant(content=None, tool_calls=[…])` — so **all** model free text around a tool call is discarded and never replayed. The model carries no reasoning at all between rounds — only calls and results. This strengthens the review's argument, but it also breaks something the review assumes works: **intent decomposition (§12) cannot survive a round either.** If round 1 identifies three intents and runs two blueprints, round 2 sees two tool results and the original question — never "there were three intents". Today the only recovery is re-reading the pinned user question every round. See Q4.

**B3 — Runtime composition of blueprints does not exist.**
§11 proposes that a plan's units should be blueprints (e.g. `overtime_by_department` → feeds `monthly_hires` + `payroll_expense` → comparison). In the build, composition is an **authoring-time** feature: a `composes` node may carry `ref: {blueprint, slots}`, which the corpus loader inlines at load. At runtime `runBlueprint(id, slot_bindings)` executes exactly one blueprint, and its result reaches the model as a ≤20-row preview in a tool message — not as anything a subsequent query can join to. There is no model-facing way to feed one blueprint's output into another operation. Note the surface partly exists: the executor materializes table intermediates to session `scratch` via the D93 side-channel, and `scratch.*` is readable by `runQuery` (session-gated, D64). But that side-channel is an internal HTTP client, not one of the model's tools. See Q1.

**B4 — There is no correctness gate on ad-hoc SQL at all.**
§10 reads as "strengthen" the ad-hoc gate. There is nothing to strengthen: D56 verification runs **only inside** `runBlueprint`. A raw-loop `runQuery` is checked for read-only-ness, column scope, and provenance — none of which is semantic. (A `future-raw-loop-verification-gate` was already spun off as a follow-up in an earlier session.) See Q2, Q3.

**B5 — Tool calls are dispatched sequentially, by design.**
§4 ROUTE 2 says independent blueprints should run "preferably concurrently". The loop batches up to `max_tool_calls_per_iteration=8` calls from one model response but dispatches them **serially** (`for tool_call in capped_tool_calls`), a deliberate deferral recorded in the loop docstring (parallel dispatch is an open §11 tunable). Prompt text promising parallelism would be inaccurate. See Q5.

**B6 — `searchBlueprints` returns thin cards by deliberate design.**
§7 proposes richer applicability metadata on search results. Today `searchBlueprints` returns `{id, intent, slots_summary, score}` and `getBlueprint` returns the D87 thin projection (`intent`, `uses`, `status`, `drift_status`, `hit_count`, per-slot `{name, type, gloss, required}`, plus a `composition` summary). This two-step narrowing is progressive disclosure (D88), not an oversight. Of the review's proposed fields: `required_slots`/`optional_slots` exist on `getBlueprint`; `validated` exists as `status`; `semantic_contract` is approximated by `resolves`; **`supported_dimensions` does not exist anywhere** and would be a new authored corpus field. See Q6.

**B7 — Layer 4 (eval/canary) is specified but not built.**
§19 proposes eight evaluation cases and §20 proposes eight metrics. `docs/11-testing.md` §Layer 4 specifies golden Q&A with result-set grading and LLM-as-judge, but no golden fixture or harness exists in the repo. Every metric in §20 that is trail-derivable (re-derivation rate, tool-call efficiency, plan depth) can ship now; every metric needing a labelled expectation (blueprint utilization, blueprint miss rate, semantic correction rate, clarification precision) needs Layer 4 first. See Q8, Q9.

**B8 — `answerWithTable` carries exactly one table.**
§5 step 6 ("treat that portion as solved and plan only the unresolved remainder") produces answers with two result sets — a blueprint's and a residual query's. The final-answer tool accepts one `sql` **or** one `blueprint_id`, and the UI pages exactly one query. See Q7.

**B9 — Learning-loop candidate types are a closed set.**
§15's mapping is close to the built routing, but two of its rows have no home: "resolver improvement" and "runtime/system-prompt policy". The candidate type enum is `blueprint | global_knowledge | user_knowledge | schema_edit`. See Q10.

---

## C. Open questions

> **⚠ AUDIT TRAIL — every question in §C and §C2 is now closed.** Resolutions are tabulated in **§C4.1**; several were settled by the Lead's consolidated report rather than here. Individual `Status:` lines below may predate that report. Retained because the Lead asked that superseded decisions be marked rather than removed, and because the reasoning that produced each resolution is only legible against the option set it chose from.
>
> **Terminology:** the audit trail uses `satisfied` and `satisfied_by` for what the current design calls **`completed`** and **`evidence_tool_call_id`**. The canonical names are the current ones (§C4, §D, §E, §G).

### Q1 — Is model-driven runtime composition of blueprints in scope?
**Context:** B3. §11's core recommendation ("prefer validated blueprints as the units of the plan") is not executable today for any composition where one blueprint's *rows* feed the next.
**Why it matters:** it is the difference between §11 being a prompt sentence and a multi-session build spanning the runtime, the MCP, and the corpus.
**Options:**
- **(a) Out of scope — composition stays offline.** The model runs blueprints side by side and composes only in prose; genuinely dependent chains are authored as composed blueprints by the learning loop. §11 becomes "prefer blueprints for the independent parts".
- **(b) Expose the scratch write surface to the model.** A new model-facing tool materializes a blueprint result to `scratch.<name>` so a later `runQuery` can join it. Reuses D93 + D64 isolation, but adds a model-facing write-shaped tool — a departure from "writes are never tools".
- **(c) Runtime-assembled DAG.** `runBlueprint` accepts several ids plus a join spec. Largest change; effectively lets the model author a blueprint at runtime, which the D56/authoring gates exist to prevent.
**Recommendation:** (a) for this iteration, with (b) recorded as the follow-on if evaluation shows dependent multi-blueprint requests are common. (c) not recommended — it re-opens the exact hole D56 closes.
**Status:** ✅ **DECIDED — (b) expose the scratch write surface to the model.** Recommendation not taken; §11 is treated as in-scope capability, not prose. Opens Q14, Q15, Q16.

### Q2 — Is the ad-hoc validation gate (§10) code-enforced or prompt-only?
**Context:** B4. §18 Priority 2 says "where possible, enforce pieces of this in code rather than only through prompting."
**Why it matters:** a fail-closed code gate changes the failure mode of the entire raw loop — the path every non-blueprint answer takes.
**Options:**
- **(a) Prompt-only.** Add §14's checklist to the prompt. Zero risk, zero teeth.
- **(b) Advisory code check.** Run mechanical checks, feed failures back to the model as a tool message it may act on. No answer is blocked.
- **(c) Fail-closed code gate.** A failing check withholds the rows and routes the model to correct, mirroring `RUN_BLUEPRINT_VERIFY_FAILED`.
**Recommendation:** (b) first, promoted to (c) per-check once the observed false-positive rate is known. Going straight to (c) on a check like "temporal dimension must be pinned" would block a large fraction of legitimate exploratory queries on day one.
**Status:** ✅ **DECIDED — (b) advisory code checks first**, each promotable to fail-closed once its false-positive rate is measured. Promotion of any individual check to fail-closed is a separate decision, not implied by this one.

### Q3 — Which of §10's ten checks are in the first cut?
**Context:** mapping each against what the catalog can actually ground today.

| # | §10 check | Mechanically checkable now? | Source |
|---|---|---|---|
| 1 | Business terms grounded in catalog | No — judgement | — |
| 2 | Grain matches | Only with a **declared** result grain; the probe machinery exists (`grain_probe.py`) but an ad-hoc query declares nothing | needs new input |
| 3 | Population matches request | No — judgement | — |
| 4 | Required default filters applied | **Yes** — catalog `rules[].predicate` are SQL booleans; check the AST | catalog `rules` |
| 5 | Sentinel/null handling | **Partly** — expressed as rules (e.g. `missing_date_sentinel`), not column flags | catalog `rules` |
| 6 | Joins use approved keys | **Yes** — compare join predicates to `join_keys[].join_on` | catalog `join_keys` |
| 7 | Codes resolved, not guessed | **Yes** — a literal compared against a `client_defined` column (65 exist) with no preceding `resolveValues` this turn | catalog `client_defined` |
| 8 | Time semantics correct | **Yes for the weak form** — "a table with ≥1 temporal dimension must have one constrained" (the D65 gate, specified for blueprint authoring as D37b, never built for ad-hoc) | catalog `temporal` |
| 9 | Output measure answers the question | No — judgement | — |
| 10 | Provenance establishable | **Already enforced**, fail-closed | D44/D57 |

**Options:** **(a)** ship 4/6/7/8 as the mechanical cut, 1/3/5/9 as prompt text; **(b)** mechanical cut of 7 + 8 only (highest value, lowest false-positive risk); **(c)** all ten as prompt text now, mechanics later.
**Recommendation:** (b) as the first code cut — 7 and 8 are the two that map directly to the wrong-answer classes this system exists to prevent — with 4 and 6 following once 7/8's false-positive rate is measured. §14's prose covers all ten meanwhile.
**Revised recommendation after Q2 = advisory:** widen to **(a) — ship 4/6/7/8 together.** The false-positive argument for starting narrow was an argument about *blocking* answers; an advisory check that misfires costs one wasted round-trip and a nudge the model may ignore. Shipping all four at once also gives four false-positive rates to compare before anything is promoted to fail-closed, which is the evidence Q2 defers to.
**Status:** OPEN — confirm the widened cut (4/6/7/8) or hold at 7/8.

### Q4 — How does a multi-intent request survive across rounds?
**Context:** B2. §12's "intent decomposition" is exactly the state D22 deletes.
**Why it matters:** without it, a three-intent request can silently answer two. This is a correctness issue, not an efficiency one.
**Options:**
- **(a) Rely on re-reading the question.** The originating question is pinned in context by `fit_request_to_budget`; the prompt instructs the model to re-derive what is still missing from visible tool results each round. Zero build.
- **(b) Persist the intent set only.** A minimal, non-reasoning `analysisState` (§9) holding requested intents and their resolution status — the review's Priority 5, pulled forward in its narrowest form.
- **(c) Stop deleting assistant text on tool-calling rounds.** Reverses D22 for the current turn only. Cheapest in code, largest change to replay semantics and token cost.
**Recommendation:** (a) for this iteration, with the prompt making the re-derivation instruction explicit and intent-shaped ("check the original question for parts you have not yet answered"), and a Q9 metric to detect dropped intents. Escalate to (b) if the metric shows real loss.
**Status:** ⛔ **SUPERSEDED by Decision 8 — (b), the narrow `analysisState`.** Briefly decided as (c) on 2026-08-11 and reversed the same day by the Lead. Retained here for the audit trail: the reversal is what closed Q17/Q18/Q19, and the reasoning only makes sense against what (c) would have required.

### Q5 — ROUTE 2 says "concurrently". Do we implement concurrent dispatch, or reword?
**Context:** B5.
**Options:** **(a)** reword to "in one batch" (accurate today, zero build); **(b)** implement bounded-concurrency dispatch — the loop docstring flags the accounting/ordering determinism this would disturb.
**Recommendation:** (a) now. (b) is a real latency win for ROUTE 2 but should be its own slice with its own review, since it touches budget accounting and trail ordering.
**Status:** OPEN

### Q6 — Blueprint contract metadata: new authored fields or derived?
**Context:** B6. §7 asks blueprint applicability to be judged on declared capability, not similarity.
**Options:**
- **(a) Derive from existing fields.** Enrich the search card from `resolves`, `slots` (name/type/required), `result_grain`, `status`. No corpus schema change; no re-seed; no learning-loop change.
- **(b) Add authored fields** (`supported_dimensions`, `semantic_contract`). Touches the corpus schema, the loader's write-time validation, the learning extractor and generalizer (which must now emit them), and requires a re-seed. Every existing blueprint would need backfilling.
**Recommendation:** (a). It delivers most of §7's value — the model matches on declared slots and pinned term resolutions rather than prose similarity — at a fraction of the cost, and it does not put a new authoring burden on the learning loop before we know the routing change works.
**Status:** ✅ **DECIDED — (a) derive from existing fields.** No corpus schema change, no re-seed, no learning-loop change this round. `supported_dimensions` is not added; if routing evaluation later shows the derived card is insufficient, (b) returns as its own proposal.

### Q7 — How does a mixed blueprint + residual answer present its table?
**Context:** B8, from §5 step 6.
**Options:** **(a)** the model designates whichever single table is the primary answer and describes the rest in prose; **(b)** extend `answerWithTable` to multiple designations (UI, contract, and paging route all change); **(c)** prompt the model to prefer a single composite residual query when a mixed answer would need two tables.
**Recommendation:** (a), stated explicitly in the prompt. (b) is a UI contract change that should be driven by observed need.
**Status:** OPEN

### Q8 — Does Layer 4 get built as part of this work?
**Context:** B7. §19's eight cases are eval cases, and there is no eval harness.
**Options:** **(a)** build a minimal Layer-4 harness (golden Q&A + expected route + result-set grading) as part of this change; **(b)** encode §19's cases as Layer-1/2 tests with a scripted model, proving *routing* without proving *answer correctness*; **(c)** defer entirely.
**Recommendation:** (b) now — routing is deterministic enough to assert with a scripted client, and Case H (no re-derivation after an authoritative result) in particular should be a hard regression test — with (a) as the follow-on. Deferring entirely means shipping a routing policy with no evidence it changed behaviour.
**Status:** OPEN

### Q9 — Routing telemetry: model-declared or runtime-inferred?
**Context:** §18 Priority 4.
**Options:** **(a)** infer from the trail (which tools ran, in what order, was there a `runQuery` after an authoritative result) — zero model burden, no new tool, works retroactively on existing sessions; **(b)** have the model declare its chosen route — accurate about *intent* but costs a tool call or an argument and is self-reported.
**Recommendation:** (a). Every §20 metric except "blueprint miss rate" and "semantic correction rate" is derivable from the trail, and those two need Layer 4 regardless.
**Status:** OPEN

### Q10 — Do we add learning-candidate types for §15's two unhoused rows?
**Context:** B9 — "resolver improvement" and "runtime/system-prompt policy" have no candidate type.
**Options:** **(a)** leave the enum closed; route both to a human channel; **(b)** add candidate types (touches the state machine, inbox UI, promotion scheduler, landing writer).
**Recommendation:** (a). A system-prompt policy change is not a thing that should auto-promote.
**Status:** OPEN

### Q11 — Does the prompt keep the SIMPLE/COMPLICATED vocabulary anywhere?
**Context:** §4/§13 replace it with routes. §16 asks for a net token reduction.
**Note:** the proposed §13 replacement is shorter than the two sections it replaces, but §14 adds a ten-point checklist; expect net-neutral to slightly larger. The prompt is 2,824 tokens and is re-sent on every round-trip, so growth is multiplied by round count.
**Options:** **(a)** drop SIMPLE/COMPLICATED entirely, adopt route vocabulary; **(b)** keep a one-line complexity cue as a cheap fast path for trivially atomic requests.
**Recommendation:** (a) — the review's central argument is that semantic complexity is the wrong criterion, and keeping the vocabulary invites the model to keep using it.
**Status:** OPEN

### Q12 — Are the six routes named in the prompt, or only their behaviour?
**Context:** §4 names ROUTE 1–6. §13's proposed text describes behaviour without naming routes.
**Why it matters:** named routes are useful for telemetry (Q9) and for the eval cases (Q8), but naming them in the prompt invites the model to narrate them to the user.
**Options:** **(a)** behaviour only in the prompt; names live in the runtime's telemetry vocabulary; **(b)** name them in the prompt too.
**Recommendation:** (a).
**Status:** OPEN

### Q13 — Does this change ship as one prompt slice, or prompt + code together?
**Context:** §18 orders the priorities but not the release boundary.
**Options:** **(a)** Priority 1 (prompt/routing) ships alone first, measured with Q9 telemetry, before any code gate; **(b)** Priorities 1+2 ship together so the ad-hoc gate lands with the policy that increases reliance on ad-hoc paths' correctness.
**Recommendation:** (a). The routing change should *reduce* ad-hoc usage; measuring it alone tells us whether the gate's scope is even what we think.
**Status:** OPEN

---

## C2. Questions opened by the decisions above

> **⚠ AUDIT TRAIL — see the note on §C.** Resolutions are in §C4.1.

### Q14 — What is the shape of the scratch-materialization tool? *(from Q1 = expose scratch)*
**Context:** the write surface already exists — `clickhouse-api`'s `scratch_ingest.materialize` behind `POST /scratch/v1/...`, wrapped by `runtime/mcp/scratch_client.py::ScratchClient`, already used by `BlueprintExecutor._materialize_node` with `scratch_max_rows=10_000` / `scratch_max_columns=256`. What is missing is a *model-facing* entry point. So this is runtime + tool-schema work, not MCP work.
**Sub-questions:**
- **Source of rows:** a completed `runBlueprint` result only, a `runQuery` result, or an arbitrary SELECT the tool executes and materializes in one step?
- **Naming:** runtime-generated handle (`scratch.s_<sid>_bp_<uuid>`, as the executor does) versus a model-supplied alias. Runtime-generated is safer; model-supplied is far easier for the model to then write a JOIN against. A runtime-generated name returned in the tool result and referenced verbatim is probably the middle path.
- **Is it terminal or chainable:** does materializing count as one `tool_calls_made` (yes, by the existing rule) and can the model materialize several within a turn?
**Recommendation:** materialize from a **completed blueprint result by id** in the first cut — that keeps the rows D56-verified and makes the feature exactly what §11 asked for, without turning it into a general "write my query output to a table" primitive.
**Status:** ✅ **CLOSED — collapsed into Decision 5.** There is no new tool; materialization is a `runBlueprint` behaviour, sourced only from a D56-verified result, with a runtime-generated handle returned in the result. The remaining sub-question (trigger: explicit flag vs. auto on preview overflow) is carried as Q21.

### Q15 — How does provenance survive materialization? *(from Q1 — this is the fail-open risk)*
**Context:** D44's scope filter deliberately **excludes** `scratch.*` pairs from the column-scope allowlist — they are session-gated (D64), not scope-gated. Inside `BlueprintExecutor` that is safe because `_union_provenance` folds the *producer* node's warehouse provenance into the final result, so the warehouse lineage is never lost. If the model materializes and then issues its own `runQuery` against `scratch.x`, that downstream query's provenance is computed from its SQL alone — which references only `scratch.*` — so **the warehouse lineage of the underlying rows disappears from the trail**.
**Why it matters:** two concrete consequences. (1) The turn's `_compute_turn_provenance_union` no longer reflects the real columns read, so the D44 replay gate under a later-narrowed scope is weakened. (2) `column_scope` can narrow *within* a session; a table materialized under a wide scope stays readable under a narrower one.
**Options:** **(a)** carry the producer's provenance forward — the runtime records `scratch.x → {upstream warehouse pairs}` for the session and unions it into any downstream query's provenance that reads `scratch.x`; **(b)** stamp the materialized table with the scope hash it was created under and refuse reads under a different scope; **(c)** both.
**Original recommendation:** (c) — (a) preserves lineage honesty, (b) closes the narrowing window.

**Status:** ✅ **RESOLVED — (a) adopted; (b) dropped.** Under Decision 7 (UI guarantees one scope for a session's lifetime) the narrow-then-read threat is unreachable, so the scope hash is redundant. The adopted rule:

```
on materialization of scratch.h1
    → store the blueprint's upstream warehouse provenance against the handle

on any query reading scratch.h1
    query provenance = direct warehouse provenance
                     ∪ upstream provenance of every scratch table read
```

This is what keeps `_compute_turn_provenance_union` and the D44 replay filter honest once scratch is in the chain. Recorded as Decision 6.

**Residual, not yet decided (Q23):** whether to also enforce the subset test at read time. The lineage rule is a *recording* rule — it fixes what the trail says about a query after the fact, but does not stop rows reaching the model. Under Decision 7 nothing needs to. The open question is whether to enforce anyway as defence-in-depth.

### Q16 — What happens when a final answer pages a scratch-backed query? *(from Q1)*
**Context:** `answerWithTable` resolves to one query the UI re-runs per page via `POST /query/page`. A scratch-backed answer works until the scratch TTL, then silently breaks. The runtime already anticipates this: `hooks/answer_table.py::ON_ANSWER_TABLE_EPHEMERAL` fires when a resolved answer query references scratch — but it is **dormant** (empty registry, no hook registered).
**Options:** **(a)** register a hook that rewrites a scratch-backed designation to a durable equivalent; **(b)** allow it and surface the expiry to the user; **(c)** forbid designating a scratch-backed query as the answer table, forcing the model to designate the durable upstream query instead.
**Recommendation:** **(c)** for the first cut — it needs no new machinery and the seam already detects the case. Revisit if it proves too restrictive.
**Status:** ✅ **RESOLVED — (c).** The model designates the **blueprint** in `answerWithTable`, never a scratch-backed join, so `POST /query/page` always resolves to durable SQL and the dormant `ON_ANSWER_TABLE_EPHEMERAL` seam stays dormant. Recorded as part of Decision 5.

### Q17 — Where is mid-turn assistant text persisted, and what provenance does it carry? *(from Q4 = reverse D22)*
**Context:** today there is nowhere to put it. `TrailEntry` has no field for the assistant text accompanying a tool call, and an assistant `TurnMessage` is appended only once, at turn end. Assistant messages are scope-gated by `is_message_in_scope` against a provenance tag, so mid-turn text needs one.
**Why it matters:** mid-turn reasoning can quote cell values it just saw ("Warehouse is the top department at $412k"), so an untagged assistant message is a replay leak under a narrowed scope — the same class the final answer's `_compute_turn_provenance_union` tag exists to prevent.
**Options:** **(a)** add a field to `TrailEntry` and tag it with the same provenance as the tool result it accompanies; **(b)** append a per-round assistant `TurnMessage` tagged with the running provenance union so far; **(c)** persist untagged but drop cross-turn unconditionally (the `_is_stale_assumptions_entry` precedent).
**Recommendation:** **(b)** — it reuses the existing message stream, the existing scope gate, and the existing fail-closed union, and it interleaves chronologically for free. (c) is cheaper but repeats the mistake that memo already documents: using the wrong channel to express "do not replay this later".

**Status:** ✅ **CLOSED by Decision 8.** No free-form prose is retained, so there is no prose to tag. The residue that survives — `analysisState` is still model-authored data reaching model-facing context — is handled by three constraints now folded into Decision 8:
1. **Reference, not payload.** `satisfied_by` carries `tool_call_id` / `blueprint_id` only. Naming "the authoritative tool result that satisfied an intent" as a *result* would have put warehouse values straight into the state.
2. **`frozenset()` provenance** — determined-empty, exactly the `recordAssumptions` posture after its fix.
3. **Cross-turn drop** via the `_is_stale_assumptions_entry` pattern.

Why (3) carries the weight rather than the tool-description contract: intent labels are model-authored free text written *after* the model has seen the rows, so *"hiring trend for Warehouse and Logistics, $412k combined"* is a reachable label. The structural drop bounds the blast radius to the turn in which the values were already visible. Contract-only enforcement on model-authored JSON is the failure mode recorded in the learning-plane guard lesson.

### Q18 — What is the token cost of replaying mid-turn reasoning, and is it droppable? *(from Q4)*
**Context:** `fit_request_to_budget` pins the whole current turn except tool pairs older than K=3. Per-round assistant text would land in the pinned region and be **undroppable**, growing every round inside a budget that already trips early (see the loop's quadratic token-accounting issue). A 15-round turn could carry 15 blocks of reasoning prose that cannot be trimmed.
**Options:** **(a)** classify mid-turn assistant text as its own droppable tier, dropped before current-turn tool pairs; **(b)** keep only the most recent N reasoning blocks; **(c)** pin it all and accept the cost.
**Recommendation:** **(a)** plus **(b)** at N≈2 — reasoning goes stale much faster than tool results do, so it should be the *first* thing to go under pressure, not the last.

**Status:** ✅ **CLOSED by Decision 8 — conditionally.** A structured state is bounded (N intents × a small record) where prose was not, so no new budget tier is needed. **The condition is load-bearing and is now part of Decision 8:** `analysisState` must be a **single mutable field on `SessionDoc`, rewritten in place (latest-wins)** — *not* an append-only series of trail entries. Written as trail entries, N rounds put N copies in the pinned region and the unbounded-growth problem returns in structured form. `pause_checkpoint` is the precedent for exactly this storage shape.

### Q19 — Does reversing D22 need its own ADR — or is this an implementation correction? *(from Q4)*
**Context:** D22 in full is:

> **D22.** Couchbase session store keyed by `session_id`; persist messages + tool I/O trail; discard thinking.

**The decision text and the implementation are not the same width.** D22 says discard *thinking*. The implementation discards **all** assistant free text accompanying a tool call — a narration of what the model is about to do, a statement of the intents it has identified, and genuine chain-of-thought are treated identically and all dropped. So Decision 4 can be framed two ways, and the framing changes what needs signing off:
- **as a reversal** of D22 — requiring an ADR amending a locked decision; or
- **as an implementation correction** — the code over-applied "discard thinking" to material that was never thinking, and we are narrowing it back to the decision as written.

The second reading is defensible and cheaper to govern, but it should be an explicit call rather than something assumed, because the replay contract (`_tool_trail_entry_to_canonical`, `_assembled_to_canonical`, and the D45 byte-identical-rebuild guarantee) is written against the wider behaviour either way.
**Recommendation:** record it as an **amendment** to D22 regardless of framing.

**Status:** ✅ **CLOSED by Decision 8 — no amendment needed.** D22's "discard thinking" clause is **untouched and explicitly reaffirmed**: free-form narration and chain-of-thought continue to be discarded exactly as today. `analysisState` is neither a message, nor tool I/O, nor thinking — it is coordination metadata, and `SessionDoc` already carries a non-message, non-trail structured field (`pause_checkpoint`) as precedent. What remains is a **one-line note in `DECISIONS.md`** recording that the session doc gained a field — not an amendment to a locked decision. The width discrepancy found above (decision says "thinking", implementation discards *all* assistant prose) is moot in practice now, but is left recorded so the next reader does not re-derive it.

### Q20 — Decision 1 contradicts two locked decisions as written. Amend, or re-scope? *(from Q1)*
**Context:** exposing the scratch write surface to the model runs against the explicit wording of two locked decisions:

> **D21.** Read-only agent path; scratch writes via privileged side-channel only.

> **D93** (locked, 2026-07-02, Session 14). The scratch-*write* surface: a privileged, **non-tool**, session-scoped write side-channel on the MCP, confined to the `scratch` database *structurally* (not merely by grant). … **(a) Two non-`@mcp.tool` routes** (`POST /scratch/v1/materialize`, `POST /scratch/v1/drop`) behind the same `JWTAuthMiddleware`…

"Non-tool" is not incidental phrasing in D93 — it is the property being decided, and D21 states the agent path is read-only. It is also reflected in `docs/02-tools-and-api.md` ("Writes are never tools — they happen offline in the learning loop") and in the tool-count invariant.
**Why it matters:** this is a governance question, not a technical blocker. The technical path is straightforward (the routes, the auth middleware, the session-scoping and the caps all exist). What does not currently exist is permission to put a write-shaped verb in the model's hands.
**Options:**
- **(a) Amend D21 + D93** to permit exactly one narrowly-scoped model-facing materialize verb, with the read-only property re-stated as "read-only *with respect to the warehouse*; `scratch` is session-scoped scratch space, not data of record."
- **(b) Re-scope so the write stays runtime-owned:** the model never calls a materialize tool; instead `runBlueprint` gains an opt-in that materializes its own verified result and returns the handle. The model receives a joinable table name without ever invoking a write. This satisfies §11 and keeps D21/D93 literally true.
- **(c) Reverse Decision 1** back to Q1 option (a).
**Recommendation:** **(b).** It delivers the decided capability, keeps the model's tool surface read-only, keeps the rows D56-verified (which Q14 wanted anyway), and needs no amendment to D21 or D93 — the write is still runtime-issued through the privileged side-channel, exactly as D93 describes. If the Lead wants the general primitive rather than the blueprint-scoped one, (a) is the honest path and the amendment should be explicit.
**Status:** ✅ **RESOLVED — (b)**, via Lead Q1 (§C3), which independently proposed the same shape. **No longer a governance blocker: D21 and D93 need no amendment**, because the write stays runtime-issued through the privileged non-tool side-channel and the model's tool surface stays read-only. Recorded as Decision 5.

### Q21 — Materialization trigger: explicit flag or automatic on preview overflow? *(from Decision 5)*
**Context:** the Lead's proposal is `runBlueprint(id, materialize_result=true)`. That makes the model predict, *before* running, whether it will later need to join — and a wrong guess costs a re-run (safe and idempotent, but a wasted call).
**Alternative:** the runtime already knows the condition under which a handle is useful — **the result does not fit in the preview**. Materialize automatically iff `row_count > preview_row_count` (default 20) and always return `scratch_handle` when it did. No model-facing parameter, no wrong-guess path, and a write is spent only when the model demonstrably cannot see all the rows. Caps already exist (`scratch_max_rows=10_000`, `scratch_max_columns=256`).
**Trade-off:** the explicit flag is more predictable and easier to trace; auto is fewer moving parts for the model and never needs a re-run.
**Recommendation:** auto-materialize on the preview-overflow condition, with the explicit flag retained as a fallback if traces show the model wanting handles for small results too.
**Status:** OPEN — with the Lead.

### Q22 — The same-scope invariant becomes a constraint on a session-refresh path that does not exist yet *(from Decision 7)*
**Context:** Decision 7 holds today only because the BFF mints once and there is no re-mint path — and because `token_ttl_seconds=3600` means a session cannot outlive an hour anyway. An 8-hour session requires adding refresh.
**Why it matters:** a refresh implementation must **reuse the session's cached `column_scope`**, not re-resolve `resolve_column_scope(identity)`. If it re-resolves, an entitlement change lands mid-session and re-opens the exact threat Q15(b) was dropped against — in a component whose author has no reason to know a scratch table depends on it.
**Options:** **(a)** record the constraint alongside the session-lifetime spec and in `ui/server.py` at the mint site, so the future refresh path inherits it; **(b)** additionally assert it in code (a refresh that produces a different scope than the cached one fails closed); **(c)** revisit Q15(b) when refresh is built.
**Recommendation:** (a) now, (b) when refresh is built. The cost of (a) is two comments and a line in the spec.
**Status:** OPEN

### Q23 — Enforce the scratch subset test at read time as defence-in-depth? *(from Q15)*
**Context:** Decision 6's lineage rule is a *recording* rule: it fixes what the trail says about a query that read `scratch.h1`, but it acts after the fact — the rows still reach the model. Under Decision 7 nothing needs to stop them. The question is whether to enforce anyway.
**Shape if adopted:** on a `runQuery` whose parsed tables include scratch handles, union their stored upstream provenance and test it against the current `credentials.column_scope` before dispatch — the same subset test `scope_filter.is_provenance_in_scope` already implements, over data Decision 6 already stores. Roughly ten lines in the dispatch path.
**For:** it converts "safe because the UI behaves" into "safe because the runtime checks". Every other layer here is defence-in-depth — D44 exists even though the MCP already enforces D57 — so skipping it makes this the one place that trusts a caller-supplied invariant. It also removes the coupling in Q22.
**Against:** under Decision 7 it can never fire; it is code and a test for an unreachable branch.
**Recommendation:** adopt. Small, reuses data already stored, and it is the difference between an invariant that is *documented* and one that is *enforced*.
**Status:** OPEN — noted as recommended-not-yet-decided; **this is the one item where the record currently differs from my recommendation.**

### Q24 — Who triggers per-intent retrieval? *(from Decision 8)*
**Context:** the routing weakness Decision 8 exposes. Today retrieval embeds the **entire user question as one string** (`_insert_retrieval` passes `user_message` straight to `retrieve(question=…)`, memoized on `(user_message, scope_hash)`) and returns `retrieval_top_k_blueprints=3` cards. For a four-intent request that is structurally insufficient twice over: three cards cannot cover four intents, and one vector for a compound sentence is a poor query for any single clause. In the worked example (§G), the blueprint answering intent 3 may not surface at all.
**Why it matters:** Priority 1 tells the model to route each intent to a blueprint. If retrieval only ever answers the whole question, per-intent routing depends on the model spending a `searchBlueprints` call per intent — which the current prompt frames as a *fallback* ("if none of the blueprints offered to you fit"), not as per-intent practice.
**Options:**
- **(a) Model-driven.** The prompt instructs `searchBlueprints` per unresolved intent. No runtime change; costs up to one tool call per intent, but batchable in a single response alongside the declaration.
- **(b) Runtime-driven off the declaration.** The runtime reads the declared intents and fires retrieval per intent, injecting cards for each. Better recall per intent and makes the top-3 cut sane again (3 per intent, not 3 per question) — but the cards can only arrive in the **following** round, so round 1 becomes declare-only and costs a round-trip.
- **(c) Both** — model batches `searchBlueprints` in round 1; runtime enriches from round 2 onward.
**Recommendation:** (a) first. It needs no runtime change and no extra round-trip, and it tells us from real traces whether per-intent recall is actually the bottleneck before we build (b). This is also the strongest argument that `analysisState` earns its keep beyond bookkeeping — worth revisiting once (a) is measured.
**Status:** OPEN

### Q25 — What happens when an intent is unsatisfied at turn end? *(from Decision 8)*
**Context:** the runtime can now assert `all(i.status == "satisfied")` when the turn closes. Nothing has been decided about what it does when that fails.
**Options:** **(a)** nothing — telemetry only (feeds the Q9 dropped-intent metric); **(b)** nudge the model with another round ("intent i4 is still pending"), costing a round-trip and risking a loop near the budget cap; **(c)** surface the gap to the user as a first-class field (see Q26).
**Recommendation:** (a) + (c). (b) re-creates the "pressure to complete persisted steps" the review's §8 warns about, and an intent can be legitimately `dropped` — as i4 is when its dependency returns zero rows.
**Status:** OPEN

### Q26 — Should coverage become a first-class result field? *(from Decision 8)*
**Context:** today, an uncovered part of a request reaches the user only as prose — the prompt says *"if you could not complete every part, say which part is missing and why."* Trusting model prose is precisely what `answerWithTable` and `recordAssumptions` exist to stop doing. With `analysisState`, the runtime knows which intents finished `pending` or `dropped`, and why (`note`).
**Shape if adopted:** a `coverage` field on `TurnOutcome` beside `assumptions`, sourced from the ledger rather than from narration; the UI renders it like the assumptions strip.
**Recommendation:** adopt — but as its own slice after Decision 8 lands, since it is a UI contract change. It is the clearest user-visible payoff of the whole decision.
**Status:** OPEN

---

## C3. Lead review thread

> **⚠ AUDIT TRAIL — see the note on §C**, including the `satisfied` → `completed` terminology change.

### Lead Q1 — Does the requirement need model-controlled writes, or only blueprint-result chaining?
*Asked 2026-08-11. Proposes `runBlueprint(id, materialize_result=true)` returning `{result, authoritative, scratch_handle}` as an alternative to a model-called `materializeResult(...)`.*

**Answer: the flag satisfies the requirement. Model-controlled writes are not needed, and are worse on five counts.** This is Q20 option (b), and it is the recommended path.

The real requirement is *chaining* — a verified blueprint result usable as input to subsequent analysis. Nothing about that requires the model to own the write verb.

**Why the flag is strictly better than a separate `materializeResult`:**

1. **Verification containment.** The flag can only materialize a **D56-verified** blueprint result. A general `materializeResult(sql)` would let the model persist *unverified* ad-hoc output and then build on it — a strictly larger and less trustworthy surface, and precisely the "re-derive with fresh probabilistic reasoning" the review's §2.1 argues against.
2. **Q15 becomes tractable instead of hard.** The provenance gap is the one genuinely dangerous part of Decision 1. Under the flag, the executor is *already holding* the union provenance of the whole DAG (`_union_provenance`) at the moment it materializes — so recording `scratch.h1 → {warehouse column pairs}` is a few lines with the lineage in hand. Under a standalone tool the runtime would have to reconstruct lineage from a handle after the fact, which is exactly the kind of reconstruction that fails open.
3. **No governance amendment.** D21 ("read-only agent path; scratch writes via privileged side-channel only") and D93 ("non-tool… two non-`@mcp.tool` routes") both stay literally true. The write is still runtime-issued through the privileged side-channel. Q20 dissolves.
4. **Tool surface stays read-only.** No write-shaped verb in the model's hands; the `docs/02-tools-and-api.md` invariant ("writes are never tools") holds unchanged.
5. **One tool call, not two.** `runBlueprint` already counts as exactly one `tool_calls_made` regardless of inner node count; materializing inside it stays within that.

**Honest scope of what it covers.** Chaining splits into three tiers, and the flag addresses the middle one:

| Tier | Case | Covered by |
|---|---|---|
| **1** | Cohort small enough to read off the result (≤ `preview_row_count`, default 20) and pass as a slot | **Already works today, no build.** The blueprint result carries `preview_rows`; the `list` slot type resolves element-wise and binds as an `IN (…)` tuple (`slots.py::_resolve_list`). `runBlueprint(overtime_by_dept)` → read top 5 → `runBlueprint(monthly_hires, {department: [d1…d5]})`. |
| **2** | Intermediate too large to read, composed by JOIN in a final ad-hoc query | **The flag.** Materialize each blueprint, then one `runQuery` joining the handles. |
| **3** | Large cohort that must **parameterize a downstream blueprint**, where filtering the blueprint's output afterwards is not equivalent — a rank, a percentage-of-total, a top-N | **Neither.** Would need slot bindings sourced from a scratch handle (`{department: {from_scratch: "h1.department"}}`), which is materially more machinery. |

Tier 3 is a real residue and should be recorded as out of scope rather than assumed away. For a per-group aggregate (the common case, and the review's §11 example) filtering after the join is equivalent, so tier 2 is genuinely sufficient there — it just computes more groups than needed.

**One design refinement worth considering: drop the parameter entirely.** The flag forces the model to predict, before running, whether it will later need to join — and if it guesses wrong it must re-run the blueprint (safe and idempotent, but a wasted call). The condition under which a handle is actually needed is knowable by the runtime without asking: **the result does not fit in the preview.** So the runtime could materialize automatically iff `row_count > preview_row_count` and always return `scratch_handle` when it did. That removes a model-facing decision, removes a wrong-guess path, and spends a scratch write only when the model demonstrably cannot see all the rows. Caps already exist (`scratch_max_rows=10_000`, `scratch_max_columns=256`).

Trade-off: an explicit flag is more predictable and easier to trace; auto-materialize is fewer moving parts for the model and never needs a re-run. Recommend **auto-materialize on the preview-overflow condition**, with the explicit flag as the fallback if traces show the model wanting handles for small results too.

**Consequential updates:** Q20 resolves to (b) and stops blocking. Q14 (tool shape) collapses to "no new tool — a `runBlueprint` behaviour". Q16 gets a clean answer: the model designates the **blueprint** in `answerWithTable`, never the scratch-backed join, so the D46 paging route keeps resolving to durable SQL. Q15 drops from hard to tractable — see Lead Q2 below, which changed its resolution further.

**Status:** ✅ **ACCEPTED** → Decision 5. Open sub-question carried forward: explicit `materialize_result` flag vs. auto-materialize on preview overflow (Q21).

### Lead Q2 — Is column scope frozen for the session, or only for the duration of one request?
*Asked 2026-08-11, in response to the Q15 lineage/scope analysis.*

**Answer as-built: neither — scope is rebuilt per request, and the architecture actively assumes it can change between requests in one session.**

- `_extract_credentials` (`app.py:140`, documented as "the ONLY place a `RuntimeCredentials` is constructed") runs inside the `/turn` and `/turn/resume` handlers, reads the `Authorization` header, and derives `column_scope` from the token's `column_scope` claim on **every** call. `RuntimeCredentials`' own docstring: "constructed exactly once per inbound HTTP turn request." Nothing caches scope at session level.
- **D44(C) exists for precisely this case** — *"Mid-session scope change — provenance-filter: … every turn, the runtime drops trail entries whose provenance ⊄ current scope."* If scope were session-frozen, D44(C) would be dead code.
- A shipped endpoint narrows scope mid-session: `POST /api/session/scope` (`ui/server.py:231`) re-mints the session JWT with a narrower scope, same `session_id`. Test-gated (`UI_TEST_AFFORDANCES=1`), but its docstring says a real product "would drive [it] from its identity provider", and it enforces **monotonic narrowing** (`[]` refused, new scope must be a subset) — a guard that only makes sense if mid-session narrowing is expected. Layer-3 covers it (`tests/e2e/test_conformance.py::TestScopeNarrowingDropsReplay`).

**Product answer given: "The UI will guarantee same scope."** → Decision 7.

**Verified accurate for the code as written**, with one caveat that materially shapes the decision: the BFF mints once at `POST /api/session` (`ui/server.py:225`) into `_SESSIONS[session_id]` and has **no re-mint-on-expiry path** — the only other write to that map is the test affordance. So scope genuinely cannot change within a session today.

**But the 8-hour session does not exist yet.** `token_ttl_seconds` is **3600** (`clickhouse-api/app/token_service.py:61`) and `verify_jwt` requires `exp` (60s leeway). At 61 minutes the next turn 401s; there is no refresh. The invariant therefore currently holds *partly because sessions cannot outlive an hour*. Supporting 8-hour sessions means adding a refresh path — and that path is exactly where the invariant becomes a rule someone must remember to honour. See Q22.

**Status:** ✅ **ANSWERED** → Decisions 6 and 7.

### Lead Q3 — Replace retained assistant text with a minimal structured `analysisState`
*Direction given 2026-08-11, reversing Decision 4:* "Persist a minimal structured `analysisState` for the current turn only; continue discarding free-form model thinking/narration… constrained to coordination metadata — intent, status, dependency, perhaps the authoritative tool result that satisfied an intent — and not allow copied warehouse values or model reasoning into it."

**Accepted → Decision 8.** The Lead's claim that this removes the original reasons for Q17/Q18/Q19 holds, with the residues folded into the decision as constraints (see each question's closing note). It is also the better option for a reason not in the original framing: **a structured ledger makes unresolved intents machine-detectable.** At turn end the runtime can assert every intent reached `satisfied`, which turns "the agent silently answered three of four asks" from a Layer-4 judgement call into a trail assertion. Retained prose could never provide that. It opens Q24–Q26.

**One phrase needed tightening.** *"Perhaps the authoritative tool result that satisfied an intent"* — if that is the **result**, warehouse values enter the state and the constraint the Lead set in the same sentence is violated. Recorded as a **reference** (`tool_call_id` / `blueprint_id`), never a payload.

**How the model populates it — a tool, following the `recordAssumptions` pattern.** The alternatives fail concretely: structured text in the assistant message is discarded by the very behaviour this decision preserves; a runtime that infers *status* needs an intent↔tool-call link only the model has, and supplying it would mean an `intent_id` argument on every tool — impossible for the six MCP tools, whose schemas are forwarded verbatim (`translate_tool_spec` passes `input_schema` through unchanged).

So: one always-wired, no-backing-stack runtime tool, `updateAnalysisState(intents=[…])`, **full-replace** (latest-wins matches the storage model and avoids merge semantics).

**Cost, which is smaller than it first appears.** It counts as one `tool_calls_made`, but **costs no round-trip** — the loop dispatches up to `max_tool_calls_per_iteration=8` calls from a single model response, so round 1 of the worked example is one response carrying `updateAnalysisState(4 intents)` + three `runBlueprint` calls. Gated like `recordAssumptions` ("single intent → do not call"), so simple requests pay nothing.

**A refinement that removes most update calls:** the runtime can flip `status` to `satisfied` for **blueprint-routed** intents unaided. `_capture_terminal_sql` already builds `blueprint_id → terminal_sql` for successfully-run blueprints in the window, so matching declared ids against run ids is free. That leaves only ad-hoc intents needing an explicit update — two calls for the four-intent example.

**Validation posture.** Model-authored JSON reaching model-facing context, so the guard is derived from downstream reads rather than spot-patched per field: closed enum on `status`, bounded intent count, `depends_on` must reference declared ids, `satisfied_by` accepts reference keys **only**, and **unknown keys are rejected rather than passed through**. A validation failure returns a retryable error the model can correct, never a silently-stored malformed state.

**`recordAssumptions` is unaffected and still required** — the two are orthogonal on every axis:

| | `recordAssumptions` | `updateAnalysisState` |
|---|---|---|
| Audience | the **user** (first-class result field) | the **model** + the runtime's turn-end check |
| Timing | once, before the final answer | mid-turn, mutable |
| Content | plain-English interpretation | coordination metadata |
| Lifecycle | persisted; replayed by `project_history`; seeded across resumes | current turn only; dropped cross-turn; never user-visible |
| Also feeds | the learning loop (weak catalog semantics) | — |

Merging either direction breaks something: absorbing assumptions would require surfacing the state to the user, keeping it past the turn, and allowing prose — undoing all three constraints just set; and `recordAssumptions` fires once at the end, so it cannot carry mid-turn state.

**Two costs on the record.** Tool count goes **14 → 15**, and three of those are now bookkeeping verbs the model must invoke at the right moment under call-once/only-if discipline (`recordAssumptions`, `answerWithTable`, `updateAnalysisState`). Review §16 asked to *reduce* the model's project-management overhead; this spends some of what Priority 1 was meant to free. Defensible as a trade, but it should be conscious rather than drift. Second: all three are always-wired, no-backing-stack runtime tools folding model-authored structured data into `TurnOutcome` — same validation posture, same `frozenset()` provenance, same fold-on-success pattern. Worth building against one shared seam.

**Explicit non-goals.** `analysisState` does **not** decide routes, does **not** enforce dependencies (nothing stops a dependent intent dispatching early), and does **not** preserve anything beyond intents and their status — why a table was chosen, what a schema ruled out, all still evaporate each round. That last is consistent with §13's "work out what is still missing from the tool results you can see", and tool results are retained. It is the right trade, but not a free one.

**Status:** ✅ **ACCEPTED** → Decision 8.

---

## C4. Lead consolidated report — reconciliation

The Lead's "Planning, Routing, Runtime-State, and Validation Review — Consolidated Lead Decision Report" (2026-08-11, §1–§49) supersedes the open state of §C/§C2. This section records where every question landed, the single divergence, and the implementation decisions taken on top.

### C4.1 — Where every question landed

| Q | Resolution | Source |
|---|---|---|
| Q1 | `runBlueprint` materializes its own verified result; no model-facing write tool | Lead §6.1 (= Decision 5) |
| Q2 | Advisory checks first, promotable per-check on measured evidence | Lead §15 (= Decision 2) |
| Q3 | Ship checks **4/6/7/8 together** | Lead §16 |
| Q4 | `analysisState`; free-form narration still discarded | Lead §19 (= Decision 8) |
| Q5 | Lead §18 adopts bounded concurrency; **sequential retained and SIGNED OFF** — concurrency deferred to a separate performance slice | Decision 9, Lead input 1 |
| Q6 | Derive from existing fields; no new corpus fields | Lead §5 (= Decision 3) |
| Q7 | One designated table; secondary findings in prose | Lead §14 |
| Q8 | Build Layer 4 now | Lead §41 |
| Q9 | Runtime-inferred telemetry; no model-facing route declaration | Lead §42 |
| Q10 | Candidate enum stays closed | Lead §40 |
| Q11 | SIMPLE/COMPLICATED removed | Lead §3 |
| Q12 | Behaviour-only prompt; route names live in telemetry | Lead §3 |
| Q13 | Routing + advisory validation ship **together**, Layer 4 gates release | Lead §43 |
| Q14 | Collapsed into Decision 5 — no new tool | Lead §6.1 |
| Q15 | Lineage carry-forward; scope-hash dropped | Decision 6, Lead §9 |
| Q16 | No scratch-backed answer table — designate the blueprint | Lead §13 |
| Q17 / Q18 / Q19 | Obsolete under Decision 8; D22 needs no amendment | Lead §19 |
| Q20 | Resolved without amending D21/D93 | Lead §6.1 |
| Q21 | **Automatic** materialization on overflow | Lead §7 |
| Q22 | Scope resolved once at session creation, cached; refresh reuses it; mismatch fails closed | Lead §12 |
| Q23 | **Yes** — read-time subset check, code-enforced | Lead §10 |
| Q24 | Lead §4 keeps this a **prompt-ordering change** and adds no retrieval mechanism → resolves to option (a), model-driven per-intent `searchBlueprints`. **Residual risk recorded below.** | Lead §4 |
| Q25 | Finalization blocked while any intent is `pending` | Lead §33 + Decision 10 |
| Q26 | **YES** — `coverage` is a first-class `TurnOutcome` field, runtime-derived, original intents only | Decision 17, Lead input 2 |

**Q24 residual risk (recorded, not blocking).** Retrieval still embeds the **whole question as one string** and returns `retrieval_top_k_blueprints=3` cards. For a four-intent request that cannot cover the intents, and a single vector over a compound sentence is a weak query for any one clause. §4's prompt-ordering change is therefore necessary but not sufficient: per-intent routing now depends on the model spending a `searchBlueprints` call per unresolved intent. Layer-4 cases 1–4 should be read as the test of whether that holds; if blueprint miss-rate is high, the runtime-driven variant (retrieval fired off the declared intents) is the fix.

### C4.2 — Sequential dispatch (signed off; concurrency deferred)

**Lead §18/§37/§45 adopt bounded concurrent execution of up to 8 substantive tool calls, with results committed in call order. This release retains sequential dispatch — approved by the Lead on 2026-08-11, with bounded concurrency becoming a separate future performance slice.** Recorded as Decision 9. The rationale below is retained because it is the specification for what that future slice must solve.

**What is unaffected.** The routing policy is unchanged: the prompt still instructs the model to emit independent blueprints together, and batching still saves round-trips. Only wall-clock changes, from `max()` across a batch to `sum()`.

**What retaining sequential dispatch buys.** Five implementation problems disappear rather than being solved:

| Problem under concurrency | Under sequential dispatch |
|---|---|
| **Pause mid-batch is undefined.** `runBlueprint` can return a `ToolPause`; the loop returns *before* persisting a trail entry or counting the call. With 8 in flight, 7 completed results are discarded — and under §7 auto-materialization a discarded `runBlueprint` entry **orphans a scratch table**. Pauses become more expensive under concurrency than they were serially. | Today's semantics hold exactly: earlier calls persisted, pausing call not, later calls never dispatched. |
| **Concurrency amplifies into the MCP.** The cap of 8 is on *model-facing* calls, but `runBlueprint`/`resolveValues` issue inner `runQuery` calls through the same dispatcher — 8 concurrent blueprints is 8 concurrent DAGs, dozens of in-flight MCP calls. Needs a separate MCP-level cap; design §11 OQ-J (connection pooling) was deferred. | No amplification beyond today. |
| **CAS thrash.** `append_trail_entry` is a `_mutate_with_cas_retry` read-modify-write of the whole session doc. 8 concurrent appends contend. | Serial appends, as today. |
| **`ts` ordering.** `ts` is the context orderer (`assembly.py` interleaves on `(turn_index, ts, stream_rank)`). Entries built as results arrive but appended in call order would have `ts` contradicting array order. | Construction is already in call order. |
| **In-batch duplicate reads.** `seen_read_calls` is checked and mutated inside the dispatch loop; concurrently, two identical `getTableSchema` calls in one batch both miss the guard. | Guard works as built. |

The loop's own docstring gives this as the original rationale — sequential dispatch "keeps `BudgetGuard` iteration accounting and trail ordering trivially deterministic", with parallel dispatch deferred as an open §11 tunable. Nothing in the Lead report changes that trade; it only raises the value of the latency saved.

**This is now the scope of the deferred performance slice.** All five rows above must be solved for concurrency to land, against the stable base this release produces — rather than alongside a prompt rewrite and a new state contract. The pause-mid-batch row is the hardest: it is the one case where "commit in call order" has no defined answer, and under Decision 11's auto-materialization a discarded `runBlueprint` entry orphans a scratch table.

**One fragment of §37 survives in altered form.** Its point was that `updateAnalysisState` should not consume a concurrency slot. With sequential dispatch the binding limit is `max_tool_calls_per_iteration` (default **8**) — the per-iteration batch cap — and calls beyond it are **silently dropped, never dispatched, with no error to the model**. So state + 8 substantive calls loses the eighth. Decision 9 therefore exempts `updateAnalysisState` from that cap.

### C4.3 — Implementation decisions (Decisions 9–17)

Taken item-by-item after reconciling the Lead report against the runtime. Full statements in §D.

- **D9 — Sequential dispatch retained** (§C4.2), `updateAnalysisState` exempt from `max_tool_calls_per_iteration`.
- **D10 — Finalization has terminal escapes.** §33's contract cannot terminate as written: `stopped_hard_ceiling` and a budget-cap "stop" answer are both reachable with pending intents and must return something. Surviving `pending` intents are **rewritten by the runtime to a terminal status** (`blocked`, with new reason codes `BUDGET_EXHAUSTED` / `USER_STOPPED`) so §46's "no intent ends pending" holds literally rather than by carve-out. The refuse-and-nudge path is capped at **one forced re-round per budget window**, so enforcement cannot itself burn the window.
- **D11 — Truncated results degrade the handle, never the answer.** The guard already exists for the internal path (`_materialize_node`, executor.py:1057, itself a prior blocker fix) but its response — `UNSUPPORTED` → raw loop — is wrong for §7, where the blueprint's result is valid and D56-verified and only the *handle* is impossible. §7 returns the verified result and preview with `scratch_handle: null` + `materialization: "unavailable_truncated"`. Same truncation predicate (`_unpack_result`'s `truncated`), opposite consequence. Materialization gets **its own threshold setting**, defaulting to `preview_row_count` — coupling them means a future preview-size change silently retunes chaining.
- **D12 — The scratch→provenance map is turn-scoped, on the session doc.** In-memory is not viable: the runtime is stateless across pauses by design (D45) and multi-instance, so a process-local map breaks chaining on resume and fails invisibly in single-instance dev. Entries carry `turn_index`; a read from another turn is rejected. Reuses the existing `_prov_to_jsonable`/`_prov_from_jsonable` pair rather than a second encoding. **Fail-closed on a missing entry** — the map, not the table, is the authority on whether a handle works.
- **D13 — Evidence sets are split, and narrower than the locking set.** §27's locking set answers "does this commit the task contract"; it is the wrong test for "does this answer an intent". `resolveValues` maps codes and `sampleRows` explores — neither answers anything.

  **AMENDED by Lead input 3 (2026-08-11):** completion-evidence is **`{runQuery, runBlueprint (authoritative only), getTableSchema}`**. `resolveValues`, `sampleRows`, `searchBlueprints` and `searchKnowledge` remain insufficient. **No `intent.kind` field is added** — this is how the metadata-intent gap (§C4.4) is closed instead.

  `authoritative == True` remains required for blueprint evidence: a blueprint *can* return `ok` with an unclean verify block, so this catches a case "completed successfully" does not.

  **Two consequences of admitting `getTableSchema`, recorded rather than blocked:**
  1. **Enforcement weakens in a specific, nameable way.** Any intent can now be marked `completed` by citing a schema fetch — including an analytical one. *"Headcount by department"* → `getTableSchema(employee)` → `completed` → finalization allowed → the user gets no number. This is the escape hatch §35 exists to close, reopened one tool wide. Mitigation without a new field: **flag `metadata_evidence_completion` in telemetry** for every completion whose evidence is a `getTableSchema`, and cover it in Layer 4. If abuse shows up, the tightening also needs no new field — reject `getTableSchema` evidence for an intent in a turn that contains any successful `runQuery`/`runBlueprint`, which implies the intent was analytical. The analytical-vs-metadata distinction `intent.kind` would have stored is **derivable at read time** from the evidence call's tool name.
  2. **A guarded repeat is not evidence.** `getTableSchema` is in `IDEMPOTENT_READ_TOOLS`, so a duplicate call is not re-dispatched — it persists a data-free `TrailEntry` with `status == "ok"` and `error_code == IDEMPOTENT_READ_ALREADY_SERVED`. That entry satisfies "exists, current turn, completed successfully" while having fetched nothing. **Validation must reject any evidence entry carrying that marker**, or the model can complete an intent by citing a deduped no-op.

  **Creation-evidence for a derived intent stays permissive** — any successful call in the turn. **Evidence reuse across intents is allowed** — one query can genuinely answer two intents — and flagged in telemetry only.

- **D17 — `coverage` must be materialized at turn end.** *(Phase 2 — see §R3.2.)* Decision 17 derives coverage from `analysisState`, but Decision 8 discards that state at the end of the logical turn ("may be discarded after necessary telemetry/audit information is persisted"). So coverage cannot be re-derived later, and `session_history::project_history` — which reconstructs `assumptions` for the UI transcript by walking the persisted trail — has nothing to walk. Coverage is exactly the "audit information" Decision 8 refers to: **project it into durable storage at turn end, before the state is discarded.** Follows the `assumptions` precedent in every other respect (nullable, `[] → None`, rendered beside the answer). Unlike `assumptions` and `answer_sql`, it needs **no resume seeding** — `analysisState` lives on the session doc and survives a pause directly.
- **D14 — §30 is enforced structurally, judged semantically.** Runtime enforces: parent resolves to an existing intent; `reason_code` in the closed enum; **depth 1** (derived intents parent only to originals); **max 2 derived per original**; derived intents **excluded from user-facing coverage** (which is §30's "the final answer remains organized around original user intents", made mechanical). The prerequisite-vs-deliverable judgement is prompt + Layer 4. **Derived intents are withdrawable** (terminal `withdrawn` + reason); originals are not. Without this, a derived intent the model adds and then doesn't need is stuck `pending`, blocks finalization, and cannot be marked `unsupported` because §35 requires evidence it doesn't have — deadlocking every such turn to the D10 escape. Withdrawal dodges nothing: the original it served is still `pending`.
- **D15 — Late init is accepted, and instrumented.** §26 and §28 are **mutually exclusive paths**: the pre-execution reset requires that no tenant-data call has occurred, which is never true at late init. The residual risk is that late init lets the model author the original intent *after seeing results* — declaring precisely what it already achieved and completing it with the call it just made, satisfying every rule while the enforcement does nothing. Accepted, because the *reason* for late init is the derived prerequisite, and both the derived-intent rules and pending-blocks-finalization still bite on that work. Made observable via a `late_init_self_completion` telemetry flag (evidence call precedes state creation) and a Layer-4 case where the honest decomposition is broader than the first query's return.
- **D16 — Layer 4 splits by what it needs.** Cases **1–8** need golden Q&A and answer grading. Cases **9–15** are runtime-contract assertions needing no golden answer and land as Layer 1/2, so the contract tests are not gated on the grading harness and vice versa.

### C4.4 — Two scope facts for the Lead

**The MCP hard-caps results at 1,000 rows.** `max_response_rows` defaults to 1,000 (`clickhouse-api/app/config.py:148`) and `_compact_result` truncates at it **regardless of any caller LIMIT**; `BlueprintExecutor` passes `query_limit=None`. `scratch_max_rows` is 10,000, so it never binds. The usable chaining band is therefore:

```
row_count ≤ 20            → no handle needed (fits preview)
20 < row_count ≤ 1,000    → materialize, handle returned
row_count > 1,000         → truncated; no handle (D11)
```

§8's tier-2 example — 500 departments — fits. Chaining over any employee-grain intermediate does not. Raising the ceiling means raising `max_response_rows` in the MCP, which changes response size for every tool; it is not a runtime-side knob.

**A metadata-shaped intent could not be completed** under D13's original strict rule — *"what fields do we track for employees?"* is answered by `getTableSchema` and by nothing in `{runQuery, runBlueprint}`, so finalization would block until D10's escape. **Closed by Lead input 3:** `getTableSchema` is admitted as completion evidence, and no `intent.kind` field is added. The trade is recorded under D13 — enforcement now permits any intent to be completed by a schema fetch, mitigated by telemetry rather than by structure, with a field-free tightening available if traces show abuse.

---

## D. Decision log

| # | Question | Decision | Rationale given | Date |
|---|---|---|---|---|
| ~~1~~ | ⛔ **SUPERSEDED by Decision 5** — Q1, runtime blueprint composition | ~~Expose the scratch write surface to the model (option b)~~ — **never implemented.** Replaced by `runBlueprint` materializing its own verified result through the existing privileged side-channel; no model-facing write tool. Retained for audit history only. | §11 treated as in-scope capability rather than prose; the D93 side-channel and D64 isolation already exist, so the gap is a model-facing entry point | 2026-08-11 |
| 2 · *Phase 2* | Q2 — ad-hoc validation enforcement | **Advisory code checks first** (option b), each promotable to fail-closed on measured evidence | Avoids blocking legitimate exploratory queries before false-positive rates are known | 2026-08-11 |
| 3 · **Release 1** | Q6 — blueprint contract metadata | **Derive from existing fields** (option a) | Most of §7's value without a corpus schema change, re-seed, or new learning-loop authoring burden | 2026-08-11 |
| ~~4~~ | ⛔ **SUPERSEDED by Decision 8** — Q4, multi-intent survival | ~~Stop deleting assistant text, current turn only (option c)~~ — **never implemented.** Replaced by the structured `analysisState`; free-form thinking/narration continues to be discarded and **D22 is unamended**. Retained for audit history only. | Superseded same day by Lead direction; the reversal is what closed Q17/Q18/Q19. | 2026-08-11 |
| 5 · *Phase 3* | Q1 / Q14 / Q16 / Q20 — how the model chains a blueprint result | **`runBlueprint` materializes its own verified result and returns a `scratch_handle`.** No `materializeResult` tool; no model-facing write verb. Source is a D56-verified result only; handle is runtime-generated; `answerWithTable` designates the **blueprint**, never the scratch-backed join. | Delivers §11's capability while keeping the model's tool surface read-only. **D21 and D93 need no amendment** — the write stays runtime-issued through the privileged non-tool side-channel. Also keeps materialization inside the executor, where the DAG's union provenance is already in hand (which is what makes Decision 6 cheap). | 2026-08-11 |
| 6 · *Phase 3* | Q15 — provenance across materialization | **Lineage carry-forward adopted; scope-hash stamping dropped.** Store the blueprint's upstream warehouse provenance against the handle at materialization; any query reading a scratch table takes `direct warehouse provenance ∪ upstream provenance of every scratch table read`. | Keeps `_compute_turn_provenance_union` and the D44 replay filter honest once scratch is in the chain. The scope hash is redundant under Decision 7. | 2026-08-11 |
| 7 · *separate project* | Lead Q2 — scope lifetime | **The UI guarantees one `column_scope` for a session's lifetime.** | Product guarantee. Verified true of the code as written: the BFF mints once (`ui/server.py:225`) with no re-mint path. **Note:** the runtime does *not* assume this — scope is rebuilt per request and D44(C) exists specifically for mid-session change — so this is an invariant supplied by the caller, not enforced by the runtime. Carries two consequences: Q22 (constrains any future refresh path) and the revocation window below. | 2026-08-11 |

| 8 · **Release 1** *(trimmed — see §R2.1)* | Q4 (redo) / Lead Q3 — multi-intent survival | **Persist a minimal structured `analysisState` for the current turn only; continue discarding free-form thinking/narration.** Supersedes Decision 4. Constrained to coordination metadata (`intent_id, kind, description, status, route, depends_on, evidence_tool_call_id, note`) — **ids assigned by the runtime, not the model** (Lead §23). **No copied warehouse values, no model reasoning.** Written by the model via one always-wired `updateAnalysisState(intents=[…])` runtime tool, **full-replace**; stored as a **single mutable field on `SessionDoc`** beside `pause_checkpoint`, latest-wins; `frozenset()` provenance; **dropped cross-turn**; evidence is a **reference only**. The runtime auto-flips blueprint-routed intents to `completed`. | Solves the dropped-intent problem at its root without retaining prose — which closes Q17/Q18/Q19 outright. Adds a capability retained prose could not: unresolved intents become **machine-detectable** at turn end. D22's "discard thinking" stays intact. | 2026-08-11 |

| 9 ✅ · *separate project* | Q5 / Lead §18, §37, §45 — dispatch model — **SIGNED OFF 2026-08-11; concurrency deferred to a separate performance slice** | **Sequential dispatch retained for this release; bounded concurrency NOT adopted.** `updateAnalysisState` is exempt from `max_tool_calls_per_iteration` (calls beyond that cap are silently dropped, never dispatched). | **Diverges from the Lead report** (§C4.2). Concurrency leaves pause-mid-batch undefined, amplifies inner `runQuery` fan-out into the MCP with no cap, contends on the session doc's CAS append, breaks `ts` ordering, and defeats the in-batch read guard. Sequential dispatch makes all five disappear rather than solving them alongside a prompt rewrite and a new state contract. Costs wall-clock only. | 2026-08-11 |
| 10 · **Release 1** | Lead §33 — finalization contract | **Terminal escapes + forced terminal status.** Surviving `pending` intents are rewritten to `blocked` with `BUDGET_EXHAUSTED` / `USER_STOPPED` at `stopped_hard_ceiling` and on a budget-cap "stop". Refuse-and-nudge capped at one forced re-round per budget window. | §33 as written cannot terminate — both exits are reachable with pending intents and must return something. Rewriting keeps §46's "no intent ends pending" literally true rather than carved out, and gives coverage accurate data. | 2026-08-11 |
| 11 · *Phase 3* | Lead §7 — truncated materialization | **Degrade the handle, never the answer.** `scratch_handle: null` + `materialization: "unavailable_truncated"`; verified result and preview returned as normal. Materialization gets its own threshold setting, defaulting to `preview_row_count`. | The truncation guard already exists (`_materialize_node`, executor.py:1057) but its `UNSUPPORTED` response is right for a DAG intermediate and wrong here, where only the handle is impossible. Coupling the threshold to preview size would let a preview change silently retune chaining. | 2026-08-11 |
| 12 · *Phase 3* | Lead §9, §10 — scratch provenance map | **Turn-scoped, on the session doc**, reusing `_prov_to_jsonable`/`_prov_from_jsonable`; fail-closed on a missing entry. | In-memory breaks D45 statelessness and multi-instance resume, and fails invisibly in single-instance dev. Turn-scoping bounds growth and matches chaining's actual use. The map, not the table, is the authority on whether a handle works. | 2026-08-11 |
| 13 · **Release 1** | Lead §32 — evidence validation — **AMENDED by Lead input 3 (2026-08-11)** | **Completion-evidence = `{runQuery, runBlueprint (authoritative only), getTableSchema}`**; `resolveValues`, `sampleRows`, `searchBlueprints`, `searchKnowledge` remain insufficient. **No `intent.kind` field added.** A `getTableSchema` entry carrying the repeated-read guard marker is **not** valid evidence (see D13 note). **Creation-evidence permissive**; **reuse allowed**, telemetry-flagged. | §32's "substantive evidence" clause is not mechanically decidable. §27's locking set answers a different question — `resolveValues` and `sampleRows` touch tenant data but answer nothing. The two evidence roles need different rules. | 2026-08-11 |
| ~~14~~ · ⛔ **LAPSED with the v1 trim** | Lead §29, §30 — derived intents | **Structural enforcement:** parent resolves; closed reason enum; **depth 1**; **max 2 per original**; excluded from user-facing coverage. **Derived intents withdrawable**, originals not. Semantic rule → prompt + Layer 4. | Scope is judgement, but depth and count bound the blast radius, and coverage-exclusion makes §30's "organized around original intents" mechanical. Without withdrawal, an unneeded derived intent deadlocks the turn — it blocks finalization and cannot be marked `unsupported` for want of evidence. | 2026-08-11 |
| ~~15~~ · ⛔ **LAPSED with the v1 trim** | Lead §26, §28 — late initialization | **Accepted and instrumented.** §26 and §28 are mutually exclusive. `late_init_self_completion` telemetry flag + a Layer-4 case. | Late init lets the model author the original intent after seeing results, satisfying every rule while enforcement does nothing. Accepted because the derived-prerequisite work — the reason for late init — still faces the full contract. | 2026-08-11 |
| 16 · **Release 1** *(6 cases — see §R2.7)* | Lead §41 — Layer 4 scope | **Split:** cases 1–8 need golden Q&A + grading; cases 9–15 are runtime-contract assertions and land as Layer 1/2. | The contract tests need no golden answer, so neither half gates the other. | 2026-08-11 |
| 17 · *Phase 2* | Q26 / Lead input 2 — coverage | **`coverage` becomes a first-class `TurnOutcome` field, surfaced in the UI.** Runtime-derived from `analysisState`; **original intents only**, derived prerequisites excluded. Nullable, following the `[] → None` fork used by `sql_executed` / `assumptions`. **Materialized at turn end** (see D17 note) because its source is discarded. | Makes §30's "the final answer remains organized around original user intents" mechanical rather than a prompt rule, and stops an uncovered part of a request reaching the user only if the model remembers to narrate it — the same reason `answerWithTable` and `recordAssumptions` exist. | 2026-08-11 |
| 18 · **Release 1** | Release-1 trim — the `blocked` escape | **Model-declared `blocked` requires a closed-enum `reason_code` AND an `evidence_tool_call_id`**, validated as appropriate to the reason. Runtime-forced `blocked` (`BUDGET_EXHAUSTED` / `USER_STOPPED`) requires no evidence. **The blocked validator is not the completion validator** — blocked evidence may be a *failed* call (`NO_ACCESS` cites a **denied** `runQuery`) and comes from a wider tool set (`searchBlueprints`, `searchKnowledge`). | Without it, `blocked` is a free exit from any hard intent and the finalization guard degrades to "cannot silently drop an ask unless it declares the ask blocked" — reopening exactly the §35 escape hatch. **CLOSED 2026-08-11:** `USER_DECLINED_CLARIFICATION` uses a turn-level anchor instead of an id — the turn must carry ≥2 user messages, since `resume_checkpoint` appends the clarification answer as a `TurnMessage` at the same `turn_index` (`couchbase_store.py:274`) while the checkpoint field itself is overwritten per pause. `NO_APPLICABLE_TOOL` additionally requires that the cited search returned zero cards **or** the turn contains a successful `getBlueprint`, so the reason proves the search *failed* rather than merely that it happened. Full predicate table in the Release-1 spec §6.2. | 2026-08-11 |

**Accepted risk on the record (Decision 7).** Freezing scope for a session's lifetime means **an entitlement revocation does not take effect until the user's next session**. Bounded at one hour today by `token_ttl_seconds=3600`; at eight-hour sessions it becomes an eight-hour window. This should be signed off explicitly as a posture choice, not inherited silently as a side effect of the scratch-chaining design.

**All questions Q1–Q26 are closed.** Resolutions are tabulated in §C4.1. Two carry residual risk that is accepted rather than open: **Q24** (retrieval still embeds the whole question once and returns three cards, so per-intent routing depends on the model spending a `searchBlueprints` call per intent — Layer-4 cases 1–4 are the test of whether that holds) and **Decision 7**'s revocation window, above.

### D.1 — Explicitly rejected options

Recorded so they are not re-proposed as "obvious" improvements later.

| Option | Why rejected |
|---|---|
| A model-facing `materializeResult(...)` write tool | Would let the model persist **unverified** ad-hoc output and build on it, and would contradict D21/D93. Replaced by `runBlueprint` materializing its own verified result (Decision 5). |
| Retaining mid-turn assistant narration | Superseded by `analysisState` (Decision 8); D22 stays unamended. |
| Scope-hash stamping on materialized scratch tables | Redundant under Decision 7's session-frozen scope. Lineage carry-forward (Decision 6) plus the read-time subset check (Q23) cover it. |
| Adding `supported_dimensions` / `semantic_contract` corpus fields | Derivable from `resolves` / `slots` / `result_grain` / `status` (Decision 3). |
| An `intent.kind: analytical \| metadata` field | Closed instead by admitting `getTableSchema` as completion evidence (Lead input 3, D13). |
| **"Reject `getTableSchema` evidence when any `runQuery`/`runBlueprint` succeeded in the turn"** | Proposed as a field-free tightening for D13's widened evidence set; **rejected on review.** The test is per-*turn* but the concern is per-*intent*, so it breaks the legitimate mixed case — a request combining a metadata intent with an analytical one would have its metadata intent's schema evidence rejected merely because a blueprint succeeded elsewhere in the same turn. The §G worked example is itself close to this shape. **Telemetry (`metadata_evidence_completion`) plus a Layer-4 case is the whole mitigation for the first cut.** |

---

## E. Cost estimate and implementation sequence

Costs revised against Decisions 1–8; the sequence in **§E.1** reflects Lead §48 as amended by Decisions 9–17.

| Priority | Review § | As-built delta | Estimated cost |
|---|---|---|---|
| **P1 — routing policy** | §4, §5, §6, §13 | Prompt text only, given B1 (cards already pre-injected, discovery already free). Reorder + reweight; drop SIMPLE/COMPLICATED. | **Small** — one slice, prompt + tests |
| **P2 — ad-hoc validation** | §10, §14 | New; no gate exists (B4). **Decision 2 = advisory**, so: four AST checks against catalog `rules` / `join_keys` / `client_defined` / `temporal`, results fed back as a tool message. No blocking path. | **Medium** — pending Q3's cut |
| **P3 — blueprint contracts** | §7 | **Decision 3 = derived.** Enrich the search card from `resolves`, `slots`, `result_grain`, `status`. No corpus change, no re-seed. | **Small** |
| **P4 — routing telemetry** | §18.4, §20 | Trail-derivable per Q9; needs an offline projector over session docs | **Small–medium** |
| **Decisions 5+6 — blueprint chaining** | §11 | Write surface, session isolation and caps all exist (D93/D64), and materialization happens inside the executor where the DAG's union provenance is already in hand. Needs: the materialize step + handle in the `runBlueprint` result, a session-scoped `handle → upstream provenance` map, provenance union on scratch reads, a trigger rule (Q21), and no new tool. | **Medium** — reduced from medium–large; **no longer governance-blocked** |
| **Decision 8 — `analysisState`** | §9, §12 | New `SessionDoc` field (`pause_checkpoint` precedent) + one always-wired runtime tool + validation + render-into-context + cross-turn drop + runtime auto-flip for blueprint-routed intents + a turn-end assert. No change to the replay-synthesis path — `_tool_trail_entry_to_canonical` keeps emitting `content=None`, so its tests stand. | **Medium** — unblocked; smaller than Decision 4 would have been |
| **P5 — execution state** | §9, §12 | **This *is* P5**, pulled forward in its narrowest form as Decision 8 | folded in above |
| **(implied) Layer 4** | §19, §20 | Specified, never built (B7) | **Medium** — gates half of §20's metrics |

### E.1 — Implementation sequence (Lead §48, as amended) — **SUPERSEDED by §R1**

> **⚠ This eleven-step sequence predates the 2026-08-11 Release-1 trim.** It is retained as the record of the full programme; **§R1 is the sequence to build against.** Steps 3, 4 and part of 2b are Phase 3; step 5 is Phase 2; step 11 is a separate project.


Step 3 is struck by Decision 9; the remainder is renumbered and annotated with what each step actually touches. Per Lead §43, **everything below is pre-release** — routing and advisory validation ship together, and Layer 4 gates the combined system.

| # | Step | Notes |
|---|---|---|
| 1 | Revise decision log / ADRs; remove superseded decisions | This document + a one-line `DECISIONS.md` note that `SessionDoc` gained a field (**not** a D22 amendment — Q19) |
| 2 | `analysisState` contract + finalization enforcement | Tool #15; new `SessionDoc` field; **runtime-assigned intent ids** (Lead §23 — model proposes descriptions only); validation; render-into-context; cross-turn drop; runtime auto-flip for blueprint-routed intents; turn-end assert; **D10** escapes + one-nudge cap; **D13** evidence rules incl. the guard-marker exclusion; **D14** derived-intent rules |
| 2b | `coverage` field | **D17** — project coverage from `analysisState` **at turn end, before the state is discarded**; add to `TurnOutcome`, `docs/ui-backend-contract.md`, `session_history::project_history`, and the UI. Original intents only |
| ~~3~~ | ~~Bounded concurrency = 8 with deterministic trail ordering~~ | **Struck — Decision 9.** If reinstated, land it after step 11 against a stable base |
| 3 | Automatic blueprint overflow materialization | **D11** — own threshold setting; truncated ⇒ `scratch_handle: null` + `materialization: "unavailable_truncated"` |
| 4 | Scratch upstream-provenance propagation + read-time subset enforcement | **D12** — turn-scoped map on the session doc, fail-closed on missing entry |
| 5 | Advisory raw-query checks 4/6/7/8 | AST vs catalog `rules` / `join_keys` / `client_defined` / `temporal`. Advisory only; each promotable later on measured evidence |
| 6 | Rewrite the routing portion of the system prompt | Drop SIMPLE/COMPLICATED; behaviour-only, no route names; per-intent `searchBlueprints` (**Q24**); §17's semantic-correctness reminder |
| 7 | Enrich blueprint search cards from existing metadata | **Decision 3** — `resolves`, `slots`, `result_grain`, `status`. No corpus change |
| 8 | Runtime-inferred routing telemetry | Offline projector over session docs; adds `late_init_self_completion` (**D15**) and evidence-reuse flags (**D13**) |
| 9 | Layer-4 golden evaluation harness | **D16** — cases 1–8 here; cases 9–15 land as Layer 1/2 alongside steps 2–5 |
| 10 | Run routing + validation + state contract through Layer 4 | Release gate (Lead §43) |
| 11 | 8-hour session refresh using cached scope, mismatch fails closed | **Q22.** Independent of the rest; note there is **no refresh path at all today** — `token_ttl_seconds=3600`, mint-once, so sessions cannot currently outlive an hour |

**Also folded in:** `docs/02-tools-and-api.md` is corrected to **15 tools** (it still says 12; `recordAssumptions`, `answerWithTable` and now `updateAnalysisState` are undocumented, and `answerWithTable` being a second terminal exit belongs there). `docs/04-blueprints.md`'s F2 note is corrected — table intermediates are no longer rejected pre-dispatch when a scratch client is wired.

---

## F. Decisions referenced in this document

Dereferenced from `docs/decisions/DECISIONS.md` so this document reads standalone. Texts are condensed except **D21**, **D22** and **D93**, which are quoted in full above because Decisions 1 and 4 turn on their exact wording.

| ID | Status | What it says |
|---|---|---|
| **D5** | locked | `session_id`, JWT and column scope are **injected by code** into every tool call — not in tool schemas, not in model context. Security property: the model cannot forge or escalate scope. |
| **D21** | locked | *"Read-only agent path; scratch writes via privileged side-channel only."* — quoted in full in Q20. |
| **D22** | locked | *"Couchbase session store keyed by `session_id`; persist messages + tool I/O trail; discard thinking."* — quoted in full in B2 and Q19. Decision 4 would have amended the last clause, but was **superseded by Decision 8** — **D22 is unamended**; free-form thinking/narration continues to be discarded. |
| **D37 / D37b** | **proposed** (not built) | Grain is a declared invariant owned at storage and enforced at **authoring**: (a) declare grain/measures/`temporal` in the catalog; (b) a static fan-out/grain gate in the learning loop that rejects coarse-aggregate-without-de-fan and period-join-without-period-filter; (c) temporal slot types. **D37b is the authoring-time gate — still Phase 2, never built**, which is why the runtime D56 gate is the only teeth today. |
| **D44** | locked | The replayed session trail is a scope layer with a retention policy. Every stored tool result carries a **column-provenance set** (`USES` semantics, so a derived aggregate over a forbidden column is gated too); every turn, entries whose provenance ⊄ current scope are dropped before context assembly. Underlies Q15 and Q17. |
| **D45** | locked | In-flight turns are durable across restarts via a **pause checkpoint** (pending prompt, partial trail, mid-DAG `blueprint_id` + `slot_bindings` + completed nodes + `awaiting_node` + CAS `consumed` flag). The runtime is stateless across pauses — any instance resumes. Its byte-identical-rebuild guarantee is what Q17/Q18 must not break. |
| **D49** | locked | Slot resolvers are **deterministic code — no LLM inside `runBlueprint`**. Anything not deterministically resolvable → `askUser`, never an LLM guess. |
| **D56** | locked, **BUILT** | **No silent path** — every `runBlueprint` result passes a mandatory agent-side verification gate before the user sees it: code-computed grain-integrity assertions (`row count == COUNT(DISTINCT declared grain)`) plus signature checks, then LLM review. The user is never asked to verify. **Applies only inside `runBlueprint`** — this is the asymmetry §10 addresses. |
| **D57** | locked | Column-level scope is enforced **at the MCP by parsing the SQL**: referenced columns are extracted with the ClickHouse-dialect parser and the query is rejected if they ⊄ scope. The parser is the live security boundary. |
| **D64** | locked | Scratch-table access is **bound to the owning `session_id`**, enforced at the MCP by the same SQL parse that enforces D57 — a naming convention is not an access boundary. This is why `scratch.*` is excluded from the D44 column-scope allowlist, which is the root of Q15. |
| **D65** | **proposed** (revises D40) | `temporal` is a **list of dimensions**, each with a `role` ∈ {`period`, `as_of`, `event`} plus a table-level `default_pin`. **Gate:** a query reading a table with ≥1 temporal dimension must constrain at least one — pinning any bounds the window; pinning none is the failure. This is the rule behind §10 check #8. |
| **D77** | locked, **BUILT** | `resolveValues` is an agent-runtime composite over `runQuery` (not an MCP data-plane tool): concept → client-scoped values, ranked in-runtime, with D57 scope and D5 tenant RLS inherited from the inner query. |
| **D83** | locked (reverses D78) | `getTableSchema`'s semantic-catalog overlay lives **in the MCP**: introspection ⨝ catalog YAML (grain, measures, `temporal`, rules, ambiguities, enum values, `sensitive`/`client_defined` flags), then scope-filtered before returning. The runtime receives it already overlaid and filtered. |
| **D87** | locked | The neo4j corpus schema: `:Blueprint`/`:KnowledgeChunk` with native 768-dim cosine vector indexes, and a blueprint's transitive USES set stored **denormalized** as byte-exact `database.table.column` strings for zero-traversal scope pre-filtering. |
| **D88** | locked, **BUILT** | The three model-facing read tools ship as **runtime tools behind a generalized registry** (`RuntimeTool` protocol + `AgentLoop.runtime_tools`), with a non-oracle scope posture and footprint-split provenance. `askUser` remains the sole hardcoded terminal branch. This registry is where a materialize tool would land under Q20(a). |
| **D93** | locked | The scratch-**write** surface is a privileged, **non-tool**, session-scoped side-channel on the MCP, structurally confined to the `scratch` database — two **non-`@mcp.tool`** routes behind the JWT middleware. Quoted in full in Q20; Decision 1 turns on the word "non-tool". |

## G. Worked example — Decisions 5 and 8 against the real corpus

Built entirely from the committed fixtures (`tests/fixtures/corpus/blueprints.yaml`, `tests/fixtures/catalog_export.json`) — real blueprint ids, real slots, real catalog rules. It is the shortest realistic request that exercises multi-intent routing, a genuine dependency, both chaining tiers, and the ad-hoc residue.

> **"Give me headcount and average salary by department, and for the departments paying above the company average, show their hiring trend over the last 6 months."**

| Intent | Blueprint | Fits? |
|---|---|---|
| i1 — headcount by department | `bp-active-headcount-by-department` (`department` optional → omit = all) | ✅ |
| i2 — average salary by department | `bp-average-salary-by-department` (`department` optional → omit) | ✅ |
| i3 — departments above company average | `bp-departments-above-company-average-salary` (no slots; 2-node scalar-converging DAG) | ✅ |
| i4 — hiring trend, last 6 months, **for those departments** | `bp-hires-per-month(window_months=6)` | ❌ |

i4 fails for a concrete, checkable reason: `bp-hires-per-month`'s `uses` is `employee_code`, `most_recent_hire_date`, `employee_status` — **no `department_name`**, and no department slot. It answers company-wide only. **This is the tier-3 residue from Lead Q1 occurring on the first realistic multi-intent request:** the cohort from i3 cannot parameterize the blueprint for i4, so i4 falls to grounded ad-hoc.

> **Note on ids.** Per Lead §23, **the model never supplies intent ids.** It proposes descriptions; the runtime validates them and assigns the stable ids, which come back in the tool result and are used by every later update. The `i1…i4` labels in the table above are this document's shorthand for the reader, not something the model writes.

**Round 1** — the model *proposes* intents and batches the three independent blueprints in one response (`updateAnalysisState` + 3 × `runBlueprint` = 4 tool calls, **one round-trip**). Note there are no ids, and the dependency is expressed by **position in the proposed array**, since no ids exist yet:

```json
// model → updateAnalysisState
{"intents": [
  {"description": "headcount by department",                          "route": "blueprint"},
  {"description": "average salary by department",                     "route": "blueprint"},
  {"description": "departments paying above the company average",     "route": "blueprint"},
  {"description": "hiring trend over the last 6 months for those departments",
   "depends_on_proposed": [2]}
]}
```

The runtime validates, assigns ids, and returns the authoritative state — this result is how the model learns the ids:

```json
// runtime → tool result
{"turn_index": 7, "intents": [
  {"intent_id":"i1","kind":"original","description":"headcount by department","status":"pending","route":"blueprint","depends_on":[]},
  {"intent_id":"i2","kind":"original","description":"average salary by department","status":"pending","route":"blueprint","depends_on":[]},
  {"intent_id":"i3","kind":"original","description":"departments paying above the company average","status":"pending","route":"blueprint","depends_on":[]},
  {"intent_id":"i4","kind":"original","description":"hiring trend over the last 6 months for those departments","status":"pending","route":null,"depends_on":["i3"]}
]}
```

**Round 2** — the runtime auto-flips i1–i3 to `completed` (declared blueprint ids matched against blueprints that actually ran); the model marks i4's route, referencing the **runtime-assigned** id:

```json
// runtime-derived
{"intent_id":"i1","status":"completed","route":"blueprint",
 "evidence_tool_call_id":"call_a1","evidence_blueprint_id":"bp-active-headcount-by-department"}
…
// model → updateAnalysisState
{"updates":[{"intent_id":"i4","route":"ad_hoc","note":"no blueprint covers hires by department"}]}
```

**The load-bearing line:** evidence is a reference. The department names and the company average from i3 are *not* in the state — they are in the tool result, which the model can already see. What the constraint forbids:

```json
"evidence": {"departments":["Engineering","Warehouse"], "company_avg": 87342.19}   ❌ warehouse values
"note": "chose most_recent_hire_date because the hire_date ambiguity defaults to it"  ❌ reasoning
"intent_id": "my-hiring-intent"                                                       ❌ model-supplied id
```

**Round 3** — the model grounds i4 with `getTableSchema(employee)` and meets three real catalog constraints: the **`hire_date` ambiguity** (`hire_date` / `most_recent_hire_date` / `rehire_date`, default `most_recent_hire_date`, `clarify_if` the question distinguishes original from most-recent); the **`exclude_not_hired_default`** rule (`employee_status != 'N' OR employee_status IS NULL`); and **`missing_date_sentinel`** (`1900-01-01 00:00:00` on DateTime64 date fields). These are §10 checks #1/#8, #4 and #5 respectively — the ad-hoc validation gate has real work to do here. i4 flips to `completed`; `answerWithTable` closes the turn.

**Both chaining tiers appear in one request.** The i3 cohort is a handful of departments, inside the 20-row preview cap — so the model reads them off the result and binds an IN-list via a `list` slot (**tier 1, already works today, no scratch spent**). Had it been 40 departments, past the cap, Decision 5's `scratch_handle` is what makes i4 possible at all: `JOIN scratch.h3` (**tier 2**).

**And the revisability case.** If i3 returns zero rows — uniform salaries, nothing above average — i4 is unanswerable as posed. Its entry goes terminal (`blocked`, `REQUIRED_DATA_UNAVAILABLE`, with the evidence §35 requires), the runtime's turn-end assert sees it, and under **Decision 17** it reaches the user as a `coverage` entry rather than depending on the model to narrate it. That is the review's §8 concern handled in practice: the ledger is revised, not completed because it was written down.

**One open implementation detail this example surfaces.** Lead §23 has the runtime assign ids, but a dependency must be expressible *at proposal time*, before any id exists. The example uses `depends_on_proposed: [2]` — a 0-based index into the array the model just submitted, which the runtime rewrites to real ids. Referencing by description would be the alternative and is more fragile. Either way it needs pinning in the tool schema; §47 leaves the exact JSON to engineering discretion, and this is one of the choices that falls under it.

## H. Sources checked

`runtime/loop/agent_loop.py`, `runtime/prompts.py`, `runtime/context/{assembly,budget,discovery_emulation}.py`, `runtime/dispatch/tool_dispatcher.py`, `runtime/mcp/tool_schema.py`, `runtime/blueprint/{executor,models,verify,grain_probe,slots,template,when,tool}.py`, `runtime/retrieval/{pipeline,render}.py`, `runtime/model/openai_client.py`, `runtime/config.py`, `runtime/app.py`, `tests/fixtures/catalog_export.json` (11 tables, `catalog_sha 1d86876c`), `tests/fixtures/corpus/blueprints.yaml` (11 blueprints), `docs/{02-tools-and-api,04-blueprints,11-testing}.md`.
