# 01 — Architecture

## The core split: data plane vs. knowledge plane

Everything hangs off keeping two planes separate so that a bad knowledge write can never corrupt
the data path, and the model is never tempted to "write" mid-conversation.

```
                          ┌──────────────────────────────┐
                          │            UI                 │
                          │  session_id (generated here)  │
                          │  + JWT, column scope          │
                          └───────────────┬──────────────┘
                                          │  request (session_id, jwt, scope)
                                          ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │                       AGENT RUNTIME                               │
        │                                                                   │
        │   ┌────────────────┐   ┌──────────────────┐   ┌───────────────┐   │
        │   │ Context        │   │   Agent loop     │   │  Control-flow │   │
        │   │ assembly       │──▶│ (one LLM/turn)   │◀─▶│  askUser      │   │
        │   │ (retrieval,    │   │ model↔tools loop │   │  (pause/resume)│  │
        │   │  reranker)     │   └────────┬─────────┘   └───────────────┘   │
        │   └────────────────┘            │                                 │
        │   code injects jwt/scope/session_id into EVERY tool call here     │
        └───────────────┬───────────────────────────────┬──────────────────┘
                        │                                │
              DATA PLANE (read-only)            KNOWLEDGE PLANE (read)
        ┌───────────────▼──────────┐      ┌──────────────▼─────────────────┐
        │ ClickHouse MCP           │      │ searchBlueprints / getBlueprint │
        │ listDatabases listTables │      │ runBlueprint                    │
        │ getTableSchema sampleRows│      │ searchKnowledge                 │
        │ runQuery explainQuery    │      └──────────────┬─────────────────┘
        └───────────────┬──────────┘                     │
                        │                      ┌──────────▼──────────┐
                ┌───────▼────────┐             │ neo4j (blueprints)  │
                │  ClickHouse     │            │ vector idx + RAG    │
                │  + scratch schema│           │ (global knowledge)  │
                └─────────────────┘            └─────────────────────┘

         OFFLINE (async, after session ends) — never a model-callable tool:
         ┌──────────────────────────────────────────────────────────────┐
         │ Learning-loop write router: extractor → leakage gate → dedup  │
         │ → promotion → {blueprint | global knowledge | user knowledge  │
         │ | schema-edit proposal}                                       │
         └──────────────────────────────────────────────────────────────┘
```

### Data plane
The 6 ClickHouse MCP tools. Read-only, deterministic, never writes. See [02-tools-and-api.md](02-tools-and-api.md).

### Knowledge plane
Split into **read** (used during a chat) and **write** (the learning loop, fully offline/async).
- Read: `searchBlueprints`, `getBlueprint`, `runBlueprint`, `searchKnowledge`.
- Write: never a model-callable tool — runs after the session in the write router
  (see [05-memory-and-learning.md](05-memory-and-learning.md)).

## Components

| Component | Responsibility |
|---|---|
| **UI** | Generates `session_id`, holds JWT + column scope, streams plan/SQL/results, renders clarification chips and the review inbox. |
| **Agent runtime** | Context assembly, the per-turn agent loop, tool dispatch, `askUser` control flow, blueprint execution. |
| **Context assembly** | Pre-loop retrieval: embed → vector recall → cross-encoder rerank → top-3 thin blueprint cards + knowledge + user memory. |
| **ClickHouse MCP** | The read-only data plane. Implemented by the existing `clickhouse-api` service (FastAPI + MCP, JWT/OIDC auth), adopted and extended — not built from scratch (D75). |
| **Scratch schema** | Session-scoped, TTL'd ClickHouse schema for external uploads and large blueprint intermediates. Loaded via a privileged side-channel, not an agent tool. |
| **Semantic Catalog** | Git-backed YAML: the curated semantic layer (entities, grain, measures, `temporal`, rule definitions, ambiguities, synonyms). Applied by the **MCP** as an overlay over its own introspection, then scope-filtered, before the model sees schema (D83 reverses D78's runtime-side placement). Single source of truth for grain/rules that blueprints inherit. See [09-infrastructure.md](09-infrastructure.md). |
| **neo4j** | Blueprint DAG store, including `USES` edges to tables/columns for scope filtering. |
| **Vector index + RAG** | Global knowledge retrieval. |
| **Couchbase** | Session store keyed by `session_id`: messages + tool-call/result trail (thinking discarded). |
| **Learning-loop write router** | Offline: mines the transcript into gated candidates. |

## Request lifecycle (one turn)

1. UI sends `{message, session_id, jwt, scope}`.
2. Runtime loads session context from Couchbase (messages + tool I/O trail; no thinking), then
   **re-filters the trail against the current scope** — drops any past result whose column-provenance
   ⊄ scope, so a mid-session scope narrowing can't leak previously-authorized data (D44, [06](06-security-and-governance.md)).
3. **Context assembly** runs retrieval, pre-injects top-3 thin blueprint cards + relevant knowledge + user memory.
4. **Agent loop**: the LLM reasons and emits tool calls. The runtime injects `jwt/scope/session_id`
   into every call (model never sees them), dispatches, feeds results back, repeats.
   - Fast path: a matching blueprint → `getBlueprint` (confirm) → `runBlueprint` → **execute then
     pass the mandatory verification gate** (code-computed grain-integrity + invariant assertions +
     LLM review; fall back to the raw loop on any failure) before returning. **No silent path** (D56):
     verification always runs and the user is never asked to verify; it simply doesn't pause. The D43
     drift attestation (Phase 2) adds periodic + authoring-time defense on top of this runtime gate.
   - Clarification: `askUser` pauses the loop and resumes with the answer. The pause persists a
     **durable checkpoint** (D45), so the runtime is stateless across pauses and a restart mid-pause
     resumes cleanly on any instance.
   - **Budget cap (D47):** the raw loop runs under per-turn ceilings (max tool-call iterations,
     tokens, wall-clock). On hitting a cap the runtime **pauses via `askUser`** — "this is taking a
     while; continue, refine, or stop?" — and emits a Phoenix event. **"Continue" grants a fresh
     budget window** (N8), so the loop can proceed and re-prompts only if the next window is exhausted.
     Never a silent infinite loop; the blueprint fast path is already bounded by DAG size.
5. **Step-level progress streams throughout the turn** (D61) — retrieval, blueprint pick, each DAG
   node, the D56 verify gate — to mask latency; then the final answer + SQL + results stream to the
   UI. Progress events reuse the [10](10-observability.md) span boundaries (PII-safe: step/shape, not
   values). Turn's tool I/O is persisted to Couchbase (thinking discarded).
6. **On session end**: the transcript is handed to the offline learning-loop write router.

---

**Status:** Locked
**Open questions:**
- Full component diagram polish (this is the working version).

## Session catalog lifecycle

The live runtime initializes a catalog per user session through backend
`POST /session/catalog`, using the verified user JWT and `X-Session-Id`. The backend
calculates permissions once and returns a trimmed catalog and signed scope token.
Both are stored together in `catalog_session` on the existing Couchbase
`session::<session_id>` document; the user JWT is not stored there. Concurrent
workers coordinate initialization with a CAS lease. A warm session reuses its
snapshot without a permission lookup. There is no shared default catalog fallback
in the live runtime; explicitly injected catalogs remain available for offline tests.

Every MCP and scratch request sends `X-Catalog-Scope` alongside the JWT and session
ID. The backend verifies the scope using its existing `MCP_SERVICE_KEY`; the agent
does not mint tokens. The scope is bound to the user identity and session, and
expires according to the backend TTL. Expired sessions refresh before use.
`POST /session/catalog/refresh` explicitly replaces the saved snapshot and token
together; both agent initialization endpoints require the user JWT and session ID
and return metadata only. Backend catalog changes reject old tokens until refresh.
Initialization failures fail closed. Column context SQL remains on demand.

Successful column-context lookups include a backend provenance receipt for the
owning column. The agent stores the result with its tool call, supplies the returned
rows and documented population/time limits to answer review, and preserves exact
returned business codes in prose. A narrower subsequent scope cannot replay that
context. Lookup results support their documented facts; configuration alone does
not establish historical usage or leave taken. Metadata questions can finish from
this evidence without an additional query or value-resolution call.

The agent prefers scoped blueprints, including for useful parts of a larger request,
and uses SQL for the remaining gaps. Employee analysis defaults to Active + On Leave
unless the user explicitly requests another population; whole-client configuration
lookups retain their documented population. Final answers emphasize important findings
and necessary caveats without repeating displayed tables. A bounded presentation
guard nudges verbose or repeated row summaries to shorten using existing evidence;
explicit requests for detailed explanations are exempt.

### Phoenix payload storage

Phoenix exports always compact payloads. Turn spans remain separate
and share `session.id`; child LLM spans inherit that ID. The current user question and new messages retain Phoenix’s native message fields,
including roles and tool-call details. Unchanged older history is omitted with an
omission count; model outputs remain readable. Tool definitions are retained once
with content-hash references in metadata. This bounded cache is local to each exporter process: restarts or eviction
safely export content again. Failed exports do not establish deduplication entries.

Compact exports omit SQL result cells (including JSON tool results and column
context), retaining result IDs where present, columns, and row counts. Large values
have explicit truncation markers. There is no separate payload-mode setting.

`OTLP_DISABLE_REDACTION=false` masks business numbers while retaining readable
questions, explanations, tool arguments and SQL structure. Structured SQL result
cells are omitted even when their values are strings. Operational metrics remain
visible. Credentials are always removed. `OTLP_DISABLE_REDACTION=true` preserves
business numbers; compaction still applies. The existing redaction default is unchanged.

These transformations affect telemetry copies only. Agent messages, returned
answers, SQL execution, and session/catalog storage are unchanged.
