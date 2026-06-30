# 02 — Tools & API

The agent sees **11 tools** across two planes. Writes are never tools — they happen offline in the
learning loop. The design follows OpenAI's "minimize tool proliferation / no overlap" lesson.

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
| `getTableSchema` | `database, table` | A **join of two sources**: live ClickHouse introspection (columns/types/engine/comments) ⨝ the **Semantic Catalog** overlay (entities, grain, primary key, column descriptions/synonyms/units, enum values, `rules`, `ambiguities`). The structural half is live; the semantic half is curated git-backed YAML — see [09-infrastructure.md](09-infrastructure.md) §Semantic Catalog. This is our "human annotations" layer. **Grain is machine-usable** (per-table `grain`, per-measure `{agg, defined_over}`, `temporal: {period_col, semantics}`) so the blueprint fan-out/grain gate can check it statically — see [04-blueprints.md](04-blueprints.md) §Grain declaration (proposed D37). |
| `sampleRows` | `database, table, limit(1–50, def 5)` | Small sample of raw rows. |
| `runQuery` | `sql, limit(1–10000?)` | Read-only `SELECT/WITH/SHOW/DESCRIBE`. Enforces read-only, time limits, row caps. INSERT/UPDATE/DELETE/DDL/table functions blocked. |
| `explainQuery` | `sql` | `EXPLAIN` to validate SQL / inspect plan without executing. |
| `resolveValues` | `table, column, concept, period?` | For **client-defined / time-varying code spaces** (`EarnCode`/`EarnDescription`, `TypeCode`, departments…): returns the **client-scoped** values matching a semantic `concept`, ranked `[{value, description, score, freq}]`. `ClientCode`/scope is **injected** (D5) — the model asks "EarnCodes for *PTO*"; the runtime answers for *this* tenant. **Live** resolution (`DISTINCT (code, description)` within scope → semantic rank); no tenant cache yet (index later, D66). Non-overlapping with `sampleRows`/`getTableSchema` — concept→client-specific-values is unique to it. |

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

- Data plane: 7 (incl. `resolveValues`)
- Knowledge plane (read): 4
- Control: 1
- **Total agent-facing: 12**
- Writes: 0 (offline learning loop)

---

**Status:** Locked
**Open questions:**
- Exact `slot_bindings` shape for `runBlueprint` (typed object) — finalize with blueprint schema.
- Whether `searchKnowledge` returns chunks vs. summarized facts — finalize with knowledge store.
