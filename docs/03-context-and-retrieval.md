# 03 — Context & Retrieval

The context-assembly layer runs **before** the agent loop and decides what the model sees. The goal
is progressive disclosure: cheap thin signals first, full detail only on demand.

## Retrieval pipeline (runtime, before the loop)

```
user question
   │
   ▼
embed question
   │
   ▼
vector recall  ──────────────┐  (candidate blueprints from neo4j index,
   │                          │   candidate knowledge from vector index)
   ▼                          │
SCOPE PRE-FILTER  ◀───────────┘  drop blueprints whose transitive (table,column)
   │                              dependency set ⊄ user scope  (see §Scope)
   ▼
cross-encoder RERANKER
   │
   ▼
pre-inject:
  • top-3 blueprint THIN CARDS  { id, intent, slots-summary }
  • relevant global knowledge
  • this user's memory (preferences, entity defaults)
```

### Thin cards
Pre-injected blueprints are **thin** — `{id, intent, slots-summary}` only. Cheap on context budget;
the model decides what to expand via `getBlueprint(id)`. If the 3 are off, the model reformulates
and calls `searchBlueprints(query, k)`.

### Reranker
A cross-encoder reranker sits between vector recall and pre-injection, for both blueprints and
knowledge. Vector recall is high-recall/low-precision; the reranker restores precision before we
spend context budget.

## Progressive disclosure flow

1. **Thin cards** (pre-injected, ~free).
2. Model judges fit → `getBlueprint(id)` for the full DAG (confirm intent, inspect `resolves`/`rules`/SQL).
3. Model extracts slot values from the conversation.
4. Model calls `runBlueprint(id, slot_bindings)` (fast path) **or** falls back to the raw agent loop
   (`getTableSchema` → write SQL → `explainQuery` → `runQuery` → verify).

## Context assembled per turn

| Source | Content | How loaded |
|---|---|---|
| Session history | messages + tool-call/result trail; **thinking discarded**; **results preview-only**, **token-budgeted** (D46) | Couchbase, by `session_id` |
| Blueprint thin cards | top-3 reranked, scope-filtered | neo4j vector index |
| Global knowledge | reranked RAG hits (entity-agnostic) | neo4j vector index (D60) |
| User memory | preferences, entity defaults (entity-bearing OK, personal) | user store |
| Schema | on demand only | `getTableSchema` |

## Context budget management (D46)

The trail has **two consumers with different needs**, and they diverge:

- **Model context (per-turn replay)** needs only enough to *continue the conversation*.
- **The learning loop (end of session)** needs the **full** tool I/O trail incl. complete results to
  mine blueprints/goldens ([05-memory-and-learning.md](05-memory-and-learning.md)).

So what is *persisted* ≠ what is *injected*:

1. **Results are preview-only in context.** `runQuery` may return up to 10k rows — never injected in
   bulk. Context gets `{SQL, column shape, row_count, truncated: bool, ≤N-row preview}`; the **full**
   result is persisted to Couchbase (for learning) and rendered in the UI. This removes the dominant
   growth term.
   - **Truncation is explicit (N6):** the `truncated` flag + `row_count` ensure the model never
     mistakes the preview for the whole answer.
   - **Norm — push computation into SQL (N6):** the model answers aggregate / superlative / ranking
     questions by **querying** for them (`ORDER BY … LIMIT`, `argMax`, `GROUP BY`), not by eyeballing
     returned rows. Preview-only *enforces* this discipline (which blueprints already encode) rather
     than breaking anything. The **user still sees all rows** (full result rendered in the UI); only
     the model reasons over a preview, re-querying with the computation pushed down when it must
     compute over the full set.
   - **Interaction with D47 (N6):** re-queries count against the per-turn loop budget, so the
     push-down norm is what keeps preview-only from inflating iterations. No pagination tool is added
     (keeps the tool surface minimal) — the agent re-queries with the computation in SQL.
2. **History is token-budgeted with summarized overflow.** A fixed token budget is allotted to
   history; newest turns are kept **verbatim**. On overflow, older turns are compacted into a
   **running summary** (LLM) that **preserves each turn's SQL verbatim** (cheap and high-value — the
   model frequently refines prior SQL) while summarizing prose. Long sessions stay within budget
   without losing continuity.

### Ordering: filter before compaction (D50)

The summary is a **per-turn derived view**, not a persisted artifact — the raw trail is already
persisted in Couchbase for the learning loop, so context assembly recomputes the view each turn in a
fixed order:

```
1. load raw trail from Couchbase
2. FILTER by current scope (D44)     ← entries still carry column-provenance here
3. BUDGET survivors (D46)            ← newest verbatim; overflow → compact
4. inject
```

Because the **scope-filter (D44) runs before compaction (D46)**, the summarizer only ever sees
in-scope entries — the summary is free of out-of-scope data **by construction**, so no per-column
provenance needs to survive into the prose. This resolves the otherwise-direct conflict between
D44 (per-entry provenance filtering) and D46 (provenance-destroying prose compaction). Recompaction
is **cached by `(scope, content-hash of the compacted set)`**, so it only re-runs when the set ages
or scope changes (rare mid-session) — steady-state cost ≈ an incremental summary.

(Budget value `N` / history-token budget are tunable parameters — see open questions.)

## Scope and retrieval

Blueprints store their `(tables, columns)` dependency set as `USES` edges in neo4j. Retrieval
**pre-filters** out blueprints whose **transitive** dependency set is not a subset of the user's
column scope — so the user never sees, and the model never reasons about, a blueprint they can't run.
This is defense-in-depth, not the security boundary: the hard gate remains the injected scope on
`runQuery` at execution time. See [06-security-and-governance.md](06-security-and-governance.md).

## What we do NOT assemble

- Table usage metadata, Codex enrichment, runtime/live-schema context (skipped — see overview).
- Anything entity-specific in the *global* layers (enforced by the learning-loop leakage gate).

---

**Status:** Locked
**Open questions:**
- Reranker model/threshold and `k` for recall vs. top-3 final — tune empirically.
- Embedding model choice (shared across question + blueprint intent + knowledge).
