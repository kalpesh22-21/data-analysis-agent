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
| `getTableSchema` | `database, table` | Returns **live ClickHouse introspection only** (columns/types/engine/comments). The **runtime** then applies the **Semantic Catalog overlay** (entities, grain, primary key, column descriptions/synonyms/units, enum values, `rules`, `ambiguities`, `sensitive`/`client_defined` flags) from the git-backed YAML — so the model sees `introspection ⨝ catalog overlay`, but the join happens in the runtime, not the MCP (D78). See [09-infrastructure.md](09-infrastructure.md) §Semantic Catalog. **Grain is machine-usable** (per-table `grain`, per-measure `{agg, defined_over}`, `temporal`) so the blueprint fan-out/grain gate can check it statically — see [04-blueprints.md](04-blueprints.md) §Grain declaration (proposed D37). Table with no catalog entry → structural-only (no grain/rules/semantics); runtime-side graceful degradation. |
| `sampleRows` | `database, table, limit(1–50, def 5)` | Small sample of raw rows. |
| `runQuery` | `sql, limit(1–10000?)` | Read-only `SELECT/WITH/SHOW/DESCRIBE`. Enforces read-only, time limits, row caps. INSERT/UPDATE/DELETE/DDL/table functions blocked. |
| `explainQuery` | `sql` | `EXPLAIN` to validate SQL / inspect plan without executing. |

These 6 tools are provided by the existing `clickhouse-api` service (adopted + extended, D75). The MCP returns introspection only; the semantic-catalog overlay (D42) is applied by the **agent runtime** (D78) — it is no longer an `clickhouse-api` extension item. After D77 + D78, the remaining `clickhouse-api` extension scope is enforcement-only: D57 column-scope + D63 fail-closed + D64 scratch isolation + D5 scope injection. The MCP data plane is exactly 6 read tools — consistent with D4.

## Runtime composite tools

These tools are **model-facing** (the model calls them like any other tool) but are **implemented in
the agent runtime**, not in the ClickHouse MCP service.

| Tool | Model args | Returns | Purpose |
|---|---|---|---|
| `resolveValues` | `table, column, concept, period?` | `[{value, description, score, freq}]` | For **client-defined / time-varying code spaces** (`EarnCode`/`EarnDescription`, `TypeCode`, departments…): returns the **client-scoped** values matching a semantic `concept`, ranked by semantic similarity + frequency. `ClientCode`/scope is **injected** (D5) — the model asks "EarnCodes for *PTO*"; the runtime answers for *this* tenant. **Implementation (D77):** the runtime issues an ordinary `runQuery` (`SELECT <column>, <descriptionCol>, count() AS freq … GROUP BY … ORDER BY freq DESC LIMIT N`), which passes through D57 column-scope enforcement and D5 tenant RLS automatically, then ranks the returned rows via the runtime embedding client (D71). `concept` never touches SQL; ranking is purely in-runtime (D10). No separate enforcement path in the MCP. Non-overlapping with `sampleRows`/`getTableSchema` — concept→client-specific-values is unique to it. |

## Knowledge plane (read)

| Tool | Model args | Purpose |
|---|---|---|
| `searchBlueprints` | `query, k` | Returns reranked **thin cards** `{id, intent, slots-summary}` for blueprints in scope. For when the pre-injected 3 are off or intent is reformulated. |
| `getBlueprint` | `id` | Full DAG: `resolves`, `slots`, `uses_rules`, `composes`, `sql_template`, `status`, `hit_count`. The "expand" step in progressive disclosure. |
| `runBlueprint` | `id, slot_bindings` | Deterministic runtime execution of the DAG (see [04-blueprints.md](04-blueprints.md)). Model supplies slot values; runtime does the rest. |
| `searchKnowledge` | `query` | RAG over global knowledge docs (institutional knowledge + lessons). |

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
- Exact `slot_bindings` shape for `runBlueprint` (typed object) — finalize with blueprint schema.
- Whether `searchKnowledge` returns chunks vs. summarized facts — finalize with knowledge store.
