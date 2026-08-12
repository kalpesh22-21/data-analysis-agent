# 02 — Tools & API

The agent sees **15 tools** across four groups: 6 ClickHouse MCP data-plane tools, 4 runtime
composite tools (`resolveValues` D77, `recordAssumptions`, `answerWithTable`, `updateAnalysisState`),
4 knowledge-plane tools, and 1 control primitive. Writes are never tools — they happen offline in
the learning loop. The design follows OpenAI's "minimize tool proliferation / no overlap" lesson.

**Where the schemas come from.** The 6 data-plane schemas are **live-fetched** from the MCP
(`list_tools()`, translated verbatim — the MCP is the single source of truth for parameter shape);
the other **9 are locally authored** in `mcp/tool_schema.py::_LOCAL_TOOL_SCHEMAS` and appended after
them. That tuple is also the source of truth for "these names are ours": a name collision with an
MCP-advertised tool fails **loud at schema fetch** (`ToolNameCollisionError`), never a silent shadow
in the loop's `runtime_tools` interception.

## Injected vs. model-visible arguments

`session_id`, `jwt`, and `column scope` are **injected by runtime code at dispatch** into every tool
call. They are:

- **Not declared** in any tool schema exposed to the model (so the model never tries to fill them).
- **Not present** in the model's context (no token cost, no leakage).
- A **security property**: the model never holds the token, so it cannot forge or escalate scope.

So the model calls `runQuery(sql)`; the runtime executes `runQuery(sql, jwt, scope, session_id)`.
The same injection applies to `runQuery` calls made **inside** `runBlueprint`.

## Data plane — ClickHouse MCP (read-only)

| Tool | Model args | Purpose |
|---|---|---|
| `listDatabases` | — | Lists databases (respects `ALLOWED_DATABASES`). Discovery entry point. |
| `listTables` | `database` | Lists tables + storage engines. |
| `getTableSchema` | `database, table` | Returns the **merged + scope-filtered** schema (D83): the **MCP** joins **live ClickHouse introspection** (columns/types/engine/comments) with the **Semantic Catalog overlay** (entities, grain, primary key, column descriptions/synonyms/units, enum values, `rules`, `ambiguities`, `sensitive`/`client_defined` flags) from the git-backed YAML, then drops any column or block-entry (`grain`/`primary_key`/`join_keys`/`measures`/`temporal`/`rules`/`ambiguities`) referencing a column outside `column_scope` — so the model sees `introspection ⨝ catalog overlay`, scope-filtered, with the join computed **in the MCP**, not the runtime (D78, reversed by D83). The response also carries `catalog_sha` (D84) for version-skew observability. See [09-infrastructure.md](09-infrastructure.md) §Semantic Catalog and [mcp-overlay-design.md](decisions/mcp-overlay-design.md). **Grain is machine-usable** (per-table `grain`, per-measure `{agg, defined_over}`, `temporal`) so the blueprint fan-out/grain gate can check it statically — see [04-blueprints.md](04-blueprints.md) §Grain declaration (proposed D37). Table with no catalog entry → structural-only (no grain/rules/semantics); graceful degradation is now MCP-side (D83, restates D53(c)). |
| `sampleRows` | `database, table, limit(1–50, def 5)` | Small sample of raw rows. **Column-scope-enforced by reject** (D83): if any of the table's columns fall outside `column_scope`, the call is rejected with `COLUMN_SCOPE_VIOLATION` rather than executed — matching `runQuery`'s `SELECT *` treatment (D69/OQ-2) rather than silently projecting to in-scope columns. Previously unscoped (see WORKLOG.md gap note). |
| `runQuery` | `sql, limit(1–10000?)` | Read-only `SELECT/WITH/SHOW/DESCRIBE`. Enforces read-only, time limits, row caps. INSERT/UPDATE/DELETE/DDL/table functions blocked. |
| `explainQuery` | `sql` | `EXPLAIN` to validate SQL / inspect plan without executing. |

These 6 tools are provided by the existing `clickhouse-api` service (adopted + extended, D75). The MCP returns the merged, scope-filtered schema; the semantic-catalog overlay (D42) is applied **in the MCP** (D83 reverses D78) — it is once again a `clickhouse-api` extension item, not runtime work. After D77 + D83, the `clickhouse-api` extension scope is: D57 column-scope + D63 fail-closed + D64 scratch isolation + D5 scope injection, plus D83's `getTableSchema` overlay/scope-filter and `sampleRows` scope-reject. The MCP data plane is exactly 6 read tools — consistent with D4.

## Runtime composite tools

These tools are **model-facing** (the model calls them like any other tool) but are **implemented in
the agent runtime**, not in the ClickHouse MCP service. Each is **intercepted in the agent loop**
(`runtime_tools`, `app.py`) and never dispatched to the MCP under its own name — only any inner
`runQuery` it issues is. Each counts as exactly **1** `tool_calls_made` (`updateAnalysisState` is
the one exception to the per-iteration cap — see below). The last three carry **no backing stack**
(`recordAssumptions`/`answerWithTable` echo the model's own text; `updateAnalysisState` needs only
the session store), so unlike `resolveValues` and the knowledge-plane tools they are **always wired**
and never take the advertised-but-unwired `*_UNAVAILABLE` path.

| Tool | Model args | Returns | Purpose |
|---|---|---|---|
| `resolveValues` | `table, column, concept, period?` | `{degraded, ranking, top_margin, values: [{value, description, score, freq}]}` | For **client-defined / time-varying code spaces** (`EarnCode`/`EarnDescription`, `TypeCode`, departments…): returns the **client-scoped** values matching a semantic `concept`, ranked by semantic similarity + frequency. `ClientCode`/scope is **injected** (D5) — the model asks "EarnCodes for *PTO*"; the runtime answers for *this* tenant. **Implementation (D77, BUILT Session 9 — see [decisions/resolvevalues-design.md](decisions/resolvevalues-design.md)):** the runtime issues an ordinary `runQuery` (`SELECT <column>, <descriptionCol>, count() AS freq … GROUP BY … ORDER BY freq DESC LIMIT N`, default LIMIT `N=200`), which passes through D57 column-scope enforcement and D5 tenant RLS automatically, then ranks the returned rows via the runtime embedding client (D71) as `0.7·cosine + 0.3·log-freq`, returning the **top-K=10**. `concept` never touches SQL; ranking is purely in-runtime (D10). No separate enforcement path in the MCP. `table`/`column`/`period.column` are fail-closed validated against the catalog allowlist. `period?` is an optional **structured** object `{column, start, end}` (concrete bounds only — `runQuery` has no bound-param surface; deictic/relative period resolution is deferred). **Result is a wrapper:** `ranking` is `"semantic+freq"` normally or `"freq_only"` when the embedding client is unavailable (**degrade, not fail** — D85: `degraded: true`, ranked by frequency alone; the model should confirm via `askUser` before filtering); `top_margin` is the score gap between the top two, to help the model decide accept-vs-clarify (D66(c)). **Error codes:** `RESOLVE_VALUES_UNKNOWN_TARGET` (unknown/ambiguous table/column/period-column, retryable), `RESOLVE_VALUES_INTERNAL_ERROR` (malformed inner result — degraded safely, never a raw crash); inner-`runQuery` denials (e.g. `COLUMN_SCOPE_VIOLATION`) pass through. Non-overlapping with `sampleRows`/`getTableSchema` — concept→client-specific-values is unique to it. |
| `recordAssumptions` | `assumptions: string[]` | `{recorded: <count>}` confirmation | **BUILT** ([decisions/ui-assumptions-contract.md](decisions/ui-assumptions-contract.md)). Called **ONCE, just before the final answer**, to surface the plain-English assumptions behind that answer as a first-class response field (alongside `sql`/`result_table`). **MODEL-DECLARED, PLAIN ENGLISH ONLY** — never SQL, codes, or column names; that contract is enforced by the **schema + system prompt**, not the runtime (`clean_assumptions` deliberately does not try to detect or strip SQL). The tool itself is stateless: the loop folds `clean_assumptions(arguments["assumptions"])` into the turn's `assumptions` accumulator from the call **arguments**, and `session_history.project_history` reconstructs the same set from the trail with the **same** helper, so live answer and history agree. Cleaning is lenient (drop non-strings/blanks, dedupe first-occurrence, cap 50 items × 2,000 chars) — safety guards, not a contract. Provenance is **determined-empty** (`frozenset()`, never `None` — it reads no warehouse data; `None` would strand the result behind the D94 "result withheld" sentinel). Assumption text is kept out of a *later* turn's context by name (`context/assembly.py::_is_stale_model_text_entry`), not by provenance. |
| `answerWithTable` | `answer, sql?, blueprint_id?` | `{answered, table_designated}` confirmation | **BUILT — the TERMINAL tool.** Called **instead of** a plain final message when the answer is a table: it carries the final prose **and** designates the query whose rows the user should see, so the turn ends on it (see §`answerWithTable` — terminal exit below). Two ways to designate, one wire contract: `sql` (a read-only `SELECT`, written **without** a `LIMIT` — the UI adds its own paging; it need **not** be a query the agent ran, since the executed one usually carries a reading LIMIT) or `blueprint_id` (a blueprint run **successfully this turn**, resolved server-side to its captured `terminal_sql`). **`sql` wins if both are given.** Either way the UI receives **one** field, `answer_sql`, and pages it through `POST /query/page` — the blueprint path is resolved server-side precisely so the UI never re-executes a DAG (which would re-run every node and re-materialise scratch on every scroll). `answer` is **required**: an optional one would let a habitual mid-turn call end the turn with an empty answer. Naming a blueprint that did **not** run this turn is turned into a retryable `ANSWER_TABLE_BLUEPRINT_NOT_RUN` nudge rather than terminating the turn with no table. **Known limit:** a scratch-backed composed blueprint's terminal SQL references a session-scoped `scratch.*` table with a TTL — paging works until that lapses, then returns an ordinary query error. |
| `updateAnalysisState` | `intents: [{description} \| {intent_id, status, evidence_tool_call_id?, reason_code?}]` | The **full** state: `{turn_index, intent_count, intents: [{intent_id, description, status, evidence_tool_call_id, reason_code}]}` | **BUILT (Release 1)** — the per-turn **intent ledger** for a request that asks for more than one thing, so no deliverable is silently dropped. `composite/analysis_state.py`; design in [decisions/release-1/03-analysis-state.md](decisions/release-1/03-analysis-state.md). The model declares each deliverable once, then updates each to `completed`/`blocked` **citing the tool call that shows it**; the loop refuses a terminal `answerWithTable` while any tracked intent is still `pending`. A single-deliverable request should not call it at all — tracking is **opt-in**, and no live state means no enforcement. Full rules below. |

### `answerWithTable` — terminal exit

A successful `answerWithTable` **ends the turn**. The full condition the loop applies is all of:

- `tool_result.status == "ok"`,
- `isinstance(tool_call.arguments, dict)`, and
- `clean_answer_text(arguments["answer"])` is **not `None`** — i.e. a `str` that is non-blank after
  `.strip()` (it is then truncated to 20,000 chars).

**A blank `answer` does not terminate.** Neither does a call the runtime turned into an error — the
`ANSWER_TABLE_BLUEPRINT_NOT_RUN` nudge and the finalization refusal below are both non-`ok` precisely
so the turn continues and the model can fix it.

The check is recorded per call but **acted on only after the whole tool batch drains**, so a model
that batches `recordAssumptions` + `answerWithTable` still gets both folded before the turn closes.
The exit then mirrors the no-tool-calls exit exactly — same `_compute_turn_provenance_union`, same
persisted assistant `TurnMessage` — because replay, `/session/history` and the D44 scope gate all
read that message. **The safe default is unchanged:** a scalar answer still ends the old way (a model
turn with no tool calls), so a model that never calls this tool cannot hang.

**It can be refused.** Under Release 1 finalization enforcement
([05-finalization-enforcement.md](decisions/release-1/05-finalization-enforcement.md)), a call that
meets the terminal condition while a tracked intent is still `pending` is replaced — **before** the
trail entry is written and **before** the designation is resolved — with a retryable
`FINALIZATION_BLOCKED_PENDING_INTENTS` error whose `denial_detail` **names the pending intents**. The
turn continues, the batch drains normally, and the model is expected to resolve each intent and
re-send. That refusal is granted **once per budget window**; when the allowance is spent the runtime
instead **force-blocks** the remaining pending intents with `ENFORCEMENT_EXHAUSTED` (a runtime-only
reason code meaning *"enforcement could not establish a disposition"*, **not** that the intent was
proven impossible) and lets the answer through.

### `updateAnalysisState` — the intent ledger

**Two modes, both INFERRED, never model-declared.** There is no mode parameter. The tool loads the
state governing this turn (`live_analysis_state(doc, turn_index)` — a state from an *earlier* turn is
history and is invisible here) and:

- **`None` → initialize.** Each item carries **only** `description`. Every intent starts `pending`.
- **live state → update.** Each item carries `intent_id` + `status` (+ evidence). Merge is **by id**.

The two shapes do not overlap, so a mis-inferred mode is visible rather than silent: an item with an
`intent_id` on the first call is rejected (`model_supplied_intent_id`), and a payload shaped entirely
like an initialize sent while a state is live is rejected as a `second_initialize`.

**One flat item shape, and the payload is normalised first — a key carrying no information is
absent.** Found live (gpt-5.5): the model **cannot omit keys**. It emits every property the item
schema declares and fills the unused ones with placeholders — `""` for a string, the **first enum
member** for an enum. A semantically correct first call arrived as
`{"description": "…", "intent_id": "", "evidence_tool_call_id": "", "reason_code": "NO_ACCESS",
"status": "pending"}`; the runtime rejected it (and five retries) as `model_supplied_intent_id`, so
no state was created, six tool calls were burned, and — because the turn still answered — the whole
feature was inert **silently**. Wording could not fix it (the description already said *"do not
invent ids"*), so the validator tolerates the only shape the model can produce. The rule has one
clause per JSON type, derived from what downstream **reads** require:

- **A string with no content is absent** — `""`, whitespace, `null`. 04 §B.2 already requires
  `evidence_tool_call_id` to be a *non-empty* string, so `""` is definitionally not a value there;
  the same holds for `intent_id` and `description`. Applied to **both** modes, before mode inference.
- **An enum the item's shape cannot read is a placeholder**, because an enum has no empty member.
  `status` cannot be read on a declaration (every intent starts `pending`) and `reason_code` is read
  only for a `blocked` intent — so both are ignored on initialize, and `reason_code` is ignored on
  any non-`blocked` update.

Only fields the **schema declares** are elided: an unknown key is still rejected whatever its value.
And a value that is a *claim* rather than a filler still fails — a **non-empty** `intent_id` on
initialize (`model_supplied_intent_id`), a non-`pending` `status` on initialize
(`status_on_initialize`), a non-empty `description` on update (`description_rewrite`), a non-empty
`evidence_tool_call_id` on a `pending` intent (`evidence_presence`).

| Rule | Behaviour |
|---|---|
| **Ids** | **Runtime-assigned**, sequential in proposal order (`i1`, `i2`, …). The model never supplies one; the call's result is how it learns them, and the rendered state block repeats them every round-trip so they survive a context rebuild. |
| **Descriptions** | **Frozen** at declaration. An update carrying `description` is rejected (`description_rewrite`) — rewriting one is how a hard ask would be laundered into an easy one. |
| **Add / remove** | Structurally impossible. Merge-by-id means an intent the model stops mentioning keeps its disposition, and an unknown id is rejected rather than appended — there is no "full replace" shape to shorten. |
| **`status`** | Closed enum: `pending` \| `completed` \| `blocked`. |
| **`reason_code`** | Closed enum, **model-declarable only**: `NO_ACCESS` \| `REQUIRED_DATA_UNAVAILABLE`. The runtime-only codes (`BUDGET_EXHAUSTED`, `USER_STOPPED`, `ENFORCEMENT_EXHAUSTED`) are written by the runtime's forced-block paths and are rejected by the allowlist if declared. |
| **Presence** | `completed` ⇒ non-empty `evidence_tool_call_id`, no `reason_code`. `blocked` ⇒ both. `pending` ⇒ neither. (A placeholder `reason_code` on a non-`blocked` intent is elided by the normalisation above, not rejected.) |
| **Evidence** | Must be a call from an **EARLIER** model message: state calls are dispatched **first** in their batch, so a call made in the same message has not run yet. `completed` needs a **successful** `runQuery`/`runBlueprint`/`getTableSchema` (a `runBlueprint` must additionally be `authoritative`, and a deduped idempotent-read marker is refused). `NO_ACCESS` needs a **failed** call with `COLUMN_SCOPE_VIOLATION`/`SCRATCH_SESSION_VIOLATION`; `REQUIRED_DATA_UNAVAILABLE` needs a **successful** call that returned **0 rows**. Completion evidence may be **reused** across intents (one query can answer two asks — telemetry-flagged only); **block evidence must be distinct per intent**. |
| **Bounds** | ≤ 8 intents, ≤ 500 chars per description — **rejected, never truncated** (the description is the frozen record enforcement reads). ≤ 2 calls per model response; the surplus is rejected with a trail entry, not silently dropped. |
| **Call budget** | **Exempt** from `max_tool_calls_per_iteration` (it is bookkeeping, not work) — the partition runs on the **raw** tool-call list, because capping silently discards the overflow. State calls are also committed **before** an `askUser` pause short-circuits the rest of the batch. |

**Late-init boundary.** A **first** declaration is refused once the analysis is under way. The
locking set is exactly four tools — `runQuery`, `runBlueprint`, `sampleRows`, `resolveValues`
(`SUBSTANTIVE_TOOLS`). **Everything else is non-locking**, including `listDatabases`, `listTables`,
`getTableSchema`, `explainQuery`, `searchBlueprints`, `getBlueprint`, `searchKnowledge`, `askUser`,
`recordAssumptions`, `answerWithTable` and `updateAnalysisState` itself — so discovery does not close
the door. The failure is **asymmetric**: a refused initialization does not error the turn, it leaves
the turn running **untracked**, which is why the boundary is watched via
`loop_analysis_state_late_init_rejected` rather than assumed correct. The **tool description and the
base prompt must both name all four** — they are re-sent every round-trip, so a description that
omitted `runBlueprint` (the most likely first substantive call in a blueprint-first release) sent the
model into the non-retryable refusal on every multi-deliverable turn. A test derives the check from
`SUBSTANTIVE_TOOLS` so a future addition cannot drift out of the model-facing text.

**Error codes.** Both are registered in `denial_mapping.py` and carry a `denial_detail` (the only
channel that reaches the model):

- **`ANALYSIS_STATE_INVALID`** — **retryable**. Every payload/state/evidence rejection (unknown key,
  missing or over-long description, unknown or duplicate `intent_id`, bad status or reason code, a
  non-`pending` status on a declaration, missing/invalid/reused evidence, surplus call). The
  rejection **reason** is emitted as an enum on
  `loop_analysis_state_rejected`; the offending value never is (D25).
- **`ANALYSIS_STATE_LATE_INIT`** — **not retryable**. The late-init boundary has passed and no retry
  helps. The detail still echoes the proposed descriptions so the model can see what it was about to
  track.

A crash that slips both guards returns the shared `RUNTIME_TOOL_INTERNAL_ERROR` (not retryable)
rather than an unregistered code that would render as generic text.

## Knowledge plane (read)

| Tool | Model args | Purpose |
|---|---|---|
| `searchBlueprints` | `query, k` | **BUILT (Session 12, D88; cards enriched in Release 1).** Returns reranked cards for blueprints in scope (transitive-USES pre-filter; `k` default 5, over-max clamps to 20; `degraded` flag when embed/rerank unavailable). Each card carries `{id, intent, slots_summary, score}` plus, when the blueprint stores a DAG, `resolves` (pinned term→column resolutions), a `{name, type, required}` `slots` summary (with `slots_omitted` when the per-card cap dropped some) and `result_grain` — enough to choose between candidates **and** to fill `runBlueprint`, so a `getBlueprint` round-trip is normally unnecessary. **Called once per DELIVERABLE — normal practice, not a fallback.** The model calls it for every analytical deliverable the request contains, in its own words, **whether or not one of the pre-injected cards fits**: those cards were recalled from the **whole question as one string**, so on a multi-part request they under-serve every part of it. One search per deliverable, not one for the whole question. (`status` is deliberately absent — recall already filters to `validated`, so it would be a constant.) |
| `getBlueprint` | `id` | **BUILT (Session 12, D88) as the thin D87 projection** `{found, id, intent, slots_summary, uses, status, drift_status, hit_count, catalog_sha}` — the full DAG (`resolves`, typed `slots`, `uses_rules`, `composes`, `sql_template`) arrives additively with `runBlueprint`. **Non-oracle:** out-of-scope ⇒ `{found: false}`, indistinguishable from a real miss. The "expand" step in progressive disclosure. |
| `runBlueprint` | `id, slot_bindings` | **BUILT (Session 13, D89).** Deterministic runtime execution of the stored full DAG (see [04-blueprints.md](04-blueprints.md)). Schema: `runBlueprint(id, slot_bindings)` where `slot_bindings` is a **flat `{name: raw_value}` object** (the model supplies raw slot values; the runtime resolves + binds them as typed AST literals, injection-safe). **Returns** a **D56-verified result**, or a **pause** (mid-DAG `askUser` — slot clarify or approval gate), or a **clarify/fallback** (raw-loop on degrade). Scalar-converging DAGs execute by scalar passing; a **table intermediate** is materialized to a session-scoped scratch table and JOIN-rewritten (D93) **when a scratch client is wired**, and stays `RUN_BLUEPRINT_UNSUPPORTED` → raw loop when it is not (F2 — see [04-blueprints.md](04-blueprints.md)). A `resolve_via` rule (D67) expands a concept → code-set via `resolveValues.resolve()` and binds it as an IN-list of typed literals — **binding only the concept-matching subset (gap-cut + sub-floor drop, D91), never the full ranked domain** (a wrong-"verified"-answer guard; top score below the confidence floor → raw-loop fallback). **Error family:** `RUN_BLUEPRINT_*` + the shared `RUNTIME_TOOL_INTERNAL_ERROR` (registry-level containment, D88a). Counts as **1** `tool_calls_made` regardless of inner node count. |
| `searchKnowledge` | `query` | **BUILT (Session 12, D88).** RAG over global knowledge docs (institutional knowledge + lessons) — reranked chunks `{id, title, text, score}`; bypasses column scope (entity-agnostic, write-gated D58(a)). |

## Control-flow primitive

| Primitive | Model args | Behaviour |
|---|---|---|
| `askUser` | `question, options?` | **Pauses** the agent loop, renders in the UI (free text or chips), **resumes** with the answer threaded into context. The pause writes a **durable checkpoint** to the session doc so a restart mid-pause doesn't lose the turn (D45, [04-blueprints.md](04-blueprints.md) §Pause / resume durability). Not a data tool — a runtime control primitive. |

### `askUser` firing rules (be disciplined)
1. A catalog `clarify_if` rule triggers (e.g. `salary` + "take home").
2. A matched blueprint has a **missing required slot** it cannot infer.
3. **Low retrieval/intent confidence** — ambiguous table or metric.
4. **Expensive/broad** query confirmation (e.g. full-warehouse scan, no period filter).
5. **Per-turn budget cap reached** (D47) — max loop iterations / tokens / wall-clock hit; ask whether
   to continue, refine, or stop, returning the best partial result so far. **"Continue" grants a fresh
   budget window** (N8). Never a silent runaway.

Otherwise prefer **reasonable default + stated assumption**, surfaced as a one-tap correction,
rather than a blocking question.

## Tool count summary

- Data plane — ClickHouse MCP, **live-fetched**: **6** (`listDatabases`, `listTables`, `getTableSchema`, `sampleRows`, `runQuery`, `explainQuery`)
- Runtime composite (model-facing, runtime-implemented): **4** (`resolveValues`, `recordAssumptions`, `answerWithTable`, `updateAnalysisState`)
- Knowledge plane (read): **4** (`searchBlueprints`, `getBlueprint`, `runBlueprint`, `searchKnowledge`)
- Control: **1** (`askUser`)
- **Total agent-facing: 15** — 6 MCP-advertised + **9 locally authored** (`_LOCAL_TOOL_SCHEMAS`)
- Writes: 0 (offline learning loop)

The 6 MCP count is consistent with [D4](decisions/DECISIONS.md#tools). The other 9 are model-facing
but implemented in the runtime — `resolveValues` over `runQuery` (D77), the 4 knowledge-plane tools,
`askUser`, and the 3 answer-shaping/bookkeeping tools — not MCP data-plane tools. Five of them can
be **advertised-but-unwired** and return a clean local unavailable error (`resolveValues`,
`searchBlueprints`, `getBlueprint`, `searchKnowledge`, `runBlueprint`); `recordAssumptions`,
`answerWithTable` and `updateAnalysisState` never can.

---

**Status:** Locked
**Open questions:**
- ~~Exact `slot_bindings` shape for `runBlueprint` (typed object) — finalize with blueprint schema.~~ **RESOLVED (D89): a flat `{name: raw_value}` object** — the model supplies raw values; the runtime resolves per-type + binds as typed AST literals.
- ~~Whether `searchKnowledge` returns chunks vs. summarized facts~~ — **chunks for Phase 1 (D88/OQ-R3)**; revisit with the Track-B knowledge store.
