# 02 — Tools & API

The agent sees **12 tools** across four groups: 6 ClickHouse MCP data-plane tools, 1 runtime
composite tool (`resolveValues`, D77), 4 knowledge-plane tools, and 1 control primitive. Writes are
never tools — they happen offline in the learning loop. The design follows OpenAI's "minimize tool
proliferation / no overlap" lesson.

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
the agent runtime**, not in the ClickHouse MCP service.

| Tool | Model args | Returns | Purpose |
|---|---|---|---|
| `resolveValues` | `table, column, concept, period?` | `{degraded, ranking, top_margin, values: [{value, description, score, freq}]}` | For **client-defined / time-varying code spaces** (`EarnCode`/`EarnDescription`, `TypeCode`, departments…): returns the **client-scoped** values matching a semantic `concept`, ranked by semantic similarity + frequency. `ClientCode`/scope is **injected** (D5) — the model asks "EarnCodes for *PTO*"; the runtime answers for *this* tenant. **Implementation (D77, BUILT Session 9 — see [decisions/resolvevalues-design.md](decisions/resolvevalues-design.md)):** the runtime issues an ordinary `runQuery` (`SELECT <column>, <descriptionCol>, count() AS freq … GROUP BY … ORDER BY freq DESC LIMIT N`, default LIMIT `N=200`), which passes through D57 column-scope enforcement and D5 tenant RLS automatically, then ranks the returned rows via the runtime embedding client (D71) as `0.7·cosine + 0.3·log-freq`, returning the **top-K=10**. `concept` never touches SQL; ranking is purely in-runtime (D10). No separate enforcement path in the MCP. `table`/`column`/`period.column` are fail-closed validated against the catalog allowlist. `period?` is an optional **structured** object `{column, start, end}` (concrete bounds only — `runQuery` has no bound-param surface; deictic/relative period resolution is deferred). **Result is a wrapper:** `ranking` is `"semantic+freq"` normally or `"freq_only"` when the embedding client is unavailable (**degrade, not fail** — D85: `degraded: true`, ranked by frequency alone; the model should confirm via `askUser` before filtering); `top_margin` is the score gap between the top two, to help the model decide accept-vs-clarify (D66(c)). **Error codes:** `RESOLVE_VALUES_UNKNOWN_TARGET` (unknown/ambiguous table/column/period-column, retryable), `RESOLVE_VALUES_INTERNAL_ERROR` (malformed inner result — degraded safely, never a raw crash); inner-`runQuery` denials (e.g. `COLUMN_SCOPE_VIOLATION`) pass through. Non-overlapping with `sampleRows`/`getTableSchema` — concept→client-specific-values is unique to it. |

## Knowledge plane (read)

| Tool | Model args | Purpose |
|---|---|---|
| `searchBlueprints` | `query, k` | **BUILT (Session 12, D88).** Returns reranked **thin cards** `{id, intent, slots_summary, score}` for blueprints in scope (transitive-USES pre-filter; `k` default 5, over-max clamps to 20; `degraded` flag when embed/rerank unavailable). For when the pre-injected 3 are off or intent is reformulated. |
| `getBlueprint` | `id` | **BUILT (Session 12, D88) as the thin D87 projection** `{found, id, intent, slots_summary, uses, status, drift_status, hit_count, catalog_sha}` — the full DAG (`resolves`, typed `slots`, `uses_rules`, `composes`, `sql_template`) arrives additively with `runBlueprint`. **Non-oracle:** out-of-scope ⇒ `{found: false}`, indistinguishable from a real miss. The "expand" step in progressive disclosure. |
| `runBlueprint` | `id, slot_bindings` | **BUILT (Session 13, D89).** Deterministic runtime execution of the stored full DAG (see [04-blueprints.md](04-blueprints.md)). Schema: `runBlueprint(id, slot_bindings)` where `slot_bindings` is a **flat `{name: raw_value}` object** (the model supplies raw slot values; the runtime resolves + binds them as typed AST literals, injection-safe). **Returns** a **D56-verified result**, or a **pause** (mid-DAG `askUser` — slot clarify or approval gate), or a **clarify/fallback** (raw-loop on degrade). Only **scalar-converging** DAGs execute (table-intermediate rejected pre-dispatch, F2). **Error family:** `RUN_BLUEPRINT_*` + the shared `RUNTIME_TOOL_INTERNAL_ERROR` (registry-level containment, D88a). Counts as **1** `tool_calls_made` regardless of inner node count. |
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

- Data plane — ClickHouse MCP: **6** (`listDatabases`, `listTables`, `getTableSchema`, `sampleRows`, `runQuery`, `explainQuery`)
- Runtime composite (model-facing, runtime-implemented): **1** (`resolveValues`)
- Knowledge plane (read): **4**
- Control: **1**
- **Total agent-facing: 12**
- Writes: 0 (offline learning loop)

The 6 MCP count is consistent with [D4](decisions/DECISIONS.md#tools). `resolveValues` is a
model-facing tool implemented in the runtime over `runQuery` (D77), not an MCP data-plane tool.

---

**Status:** Locked
**Open questions:**
- ~~Exact `slot_bindings` shape for `runBlueprint` (typed object) — finalize with blueprint schema.~~ **RESOLVED (D89): a flat `{name: raw_value}` object** — the model supplies raw values; the runtime resolves per-type + binds as typed AST literals.
- ~~Whether `searchKnowledge` returns chunks vs. summarized facts~~ — **chunks for Phase 1 (D88/OQ-R3)**; revisit with the Track-B knowledge store.
