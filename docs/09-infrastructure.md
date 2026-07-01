# 09 — Infrastructure

## Stores

| Store | Role |
|---|---|
| **ClickHouse** | HR data warehouse (flattened views: employee info, payroll, time & attendance; join on `employee_code`). Exposed read-only via the MCP. |
| **ClickHouse scratch schema** | Session-scoped, TTL'd tables for external uploads + large blueprint intermediates. Written via a privileged side-channel, read via normal MCP. |
| **Semantic Catalog** | **Git-backed YAML** (one file per table + a `rules.yaml`), deployed with the runtime (D53/D78). The curated semantic layer: entities, grain, per-measure `{agg, defined_over}`, `temporal`, rule definitions, ambiguities, synonyms, enum values, `sensitive`/`client_defined` flags. The **runtime** merges this with live MCP introspection (`introspection ⨝ catalog overlay`, D78) before handing schema to the model. Single source of truth blueprints inherit. See §Semantic Catalog. |
| **neo4j** | Blueprint DAG store: nodes for blueprints/steps, `USES` edges to tables/columns (transitive scope filtering), `composes` graph + blueprint-to-blueprint/rule traversal. **Also hosts the global-knowledge vector index** (D60) — both blueprint intent embeddings **and** knowledge embeddings live here; the standalone vector store is removed. |
| ~~Vector index + RAG~~ | **Folded into neo4j (D60).** Global knowledge (institutional knowledge + lessons), entity-agnostic, served via `searchKnowledge` — now a neo4j-native vector index. |
| **User knowledge store** | Per-user preferences and entity defaults (entity-bearing OK). |
| **Couchbase** | Session store keyed by `session_id`. |
| **Provenance/audit store** | Access-controlled, in-boundary store of candidate **evidence snapshots** (entity-bearing turn/tool-call quotes + `trace_id`). Keeps learned candidates auditable after `SESSION_TTL` without inlining entities into the entity-free global stores (D51). Own retention ≥ candidate lifetime. |

## Semantic Catalog (git-backed YAML)

The curated semantic layer that grounds correctness for **all** users. The MCP's `getTableSchema` returns live introspection only; the **runtime** applies the `introspection ⨝ catalog overlay` join (D78) before presenting schema to the model. Because it grounds every user's answers, it is the
single most correctness-critical artifact in the system, so it lives where versioning, diff,
rollback, audit, and human review come for free: **git**. The catalog deploys with the runtime (not the MCP service) — see §Deploy-coupled load below.

```
catalog/
  employee_info.yaml      # entities, grain, columns, synonyms, rules-cited, ambiguities, temporal
  payroll_fact.yaml
  time_attendance.yaml
  rules.yaml              # rule DEFINITIONS: active_employee, settled_pay_only, latest_period, …
```

- **Deploy-coupled load (D53; clarified by D78).** The catalog is **baked into the runtime deploy artifact** (not the MCP service); a merged PR goes live on the next runtime deploy/restart. So **catalog SHA ≡ runtime deploy SHA** — fully reproducible, and "the catalog version a blueprint validated against" is simply the runtime deploy version. The runtime reads the catalog at startup and performs the overlay join before serving schema to the model (D78). The warehouse is curated and rarely mutates, so gating catalog edits on deploy cadence is acceptable (edits are human-reviewed and batched with releases anyway). The learning loop / drift jobs read the catalog at the currently-deployed SHA.
- **Version = git commit SHA (= deploy SHA).** Stamped into blueprint `USES`-edge validation and
  golden replays, so a blueprint always validates against a known catalog version.
- **Write path = the schema-edit review inbox, via a bot-authored PR (D53).** Learning-loop
  `schema_edit` candidates ([05-memory-and-learning.md](05-memory-and-learning.md)) are opened by a
  **bot identity** as a branch + YAML patch + PR, with **CI** (schema lint + `explainQuery` dry-run
  against the patched catalog); a human merging the PR **is** the D18 "human review, never auto-commit"
  gate. One mechanism for the catalog write path and the review queue. (A blueprint paired with a
  `schema_edit(add_rule)` via `depends_on` stays blocked until that PR merges **and** ships in a
  deploy.)
- **Catalog ↔ ClickHouse mismatch (D53; graceful degradation now runtime-side per D78).** The MCP always returns introspection; the runtime applies the overlay. So:
  - *Table in ClickHouse, no catalog entry* → runtime returns **structural-only** (no grain/rules/semantics);
    **usable in the raw loop** (flagged "uncatalogued") but **not blueprint-eligible** (no grain to
    gate). Graceful degradation handled by the runtime, not the MCP.
  - *Catalog entry, warehouse diverged* → caught by the **D43 catalog-vs-warehouse conformance probe**;
    resolution flows back as a `schema_edit` PR. No new mechanism — reuses the drift attestation.
- **Drift propagation.** A catalog SHA bump triggers the schema-drift maintenance job
  ([05-memory-and-learning.md](05-memory-and-learning.md) §Schema drift): re-run `USES`-edge
  validation + the static grain gate + golden replays for affected blueprints; demote anything
  broken. Per D38 (deferred), a `rules.yaml` change coarsely re-flags **all** blueprints citing the
  changed rule (no per-rule version targeting yet).

### Rule definitions live here, applied two ways

`uses_rules` cites a rule by name; `rules.yaml` holds the **definition** (`name → predicate fragment
+ applicable table/columns`). The same single definition is enforced along two paths — deterministic
injection inside blueprints vs. model-written SQL in the raw loop. That asymmetry is a governance
note: see [06-security-and-governance.md](06-security-and-governance.md) §Rule enforcement.

## ClickHouse-dialect SQL parser / AST (shared dependency — D52; parser = `sqlglot`, D62)

**Chosen parser: [`sqlglot`](https://github.com/tobymao/sqlglot) (Python, native ClickHouse dialect)**
— D62. Pure-Python, in-process (matches the Python runtime), and the one library that serves all
three consumers: qualified `(table, column)` extraction via `find_all(exp.Column)` + optimizer
`qualify_columns` (fed the catalog schema); normalized-AST canonicalization for the D48 key; and
AST→template rewrite for D35. **Limitation:** it re-implements the ClickHouse grammar (not
ClickHouse's own), so dialect gaps on exotic syntax are the expected failure source — contained by the
per-consumer fail behaviors below. **Parser-accuracy oracle:** ClickHouse `system.query_log.columns`
reports the exact columns a finished query read, so the parser's extracted set can be **graded against
engine ground truth** on real queries (the D57 false-reject/coverage test harness); non-executing
`EXPLAIN` is the pre-flight parseability check.

A **ClickHouse-dialect** SQL parser is now load-bearing across multiple locked decisions, spanning
both the runtime and the learning loop. It is a named component, not an implementation detail, because
its consumers guard hard boundaries:

| Consumer | Uses it for | Boundary | Fail behavior when parse fails |
|---|---|---|---|
| **D57** | extract referenced columns to enforce **live** `runQuery` column-scope | **security (live)** | **fail-closed + alert (D63)** — reject the query, never run it unchecked; false-reject rate measured via the `system.query_log.columns` oracle and driven down by parser coverage (never relax to fail-open) |
| **D44** | extract referenced columns for trail provenance | **security (replay)** | **fail-closed** — no parse ⇒ no provenance ⇒ **drop the entry** from replay (never assume in-scope) |
| **D48** | normalize SQL AST for the canonical dedup key | **correctness** | **fail-soft** — skip the hard key, fall through to the **soft embedding layer** (review inbox); worst case a human-reconciled duplicate, never a wrong merge |
| **D35** | rewrite parameterization plan → `sql_template` | authoring | **fail-to-review** — candidate can't be safely templatized ⇒ route to **human review**, never auto-promote |

Must be a **ClickHouse-dialect** parser (not generic SQL) — dialect gaps are the likely failure
source, and the fail behaviors above are what keep a gap from becoming a leak or a bad merge.

The **D57 live consumer also enumerates referenced *tables*** (not only columns) from the same parse:
any `scratch.*` table must belong to the injected `session_id` (`s_<session_id>_*`) or the query is
rejected — the **scratch session-isolation boundary** (D64,
[06-security-and-governance.md](06-security-and-governance.md) §Scratch isolation). Same parse, same
**fail-closed** posture as the column check.

## Session management (Couchbase)

- Every UI request carries `session_id` (UI-generated and tracked), plus JWT + column scope.
- Persisted per turn: **messages + tool-call/result trail**. Each tool result also carries its
  **column-provenance set** (columns referenced, `USES` semantics) — used to re-filter the replayed
  trail against current scope every turn (D44, [06-security-and-governance.md](06-security-and-governance.md)).
- **Thinking is discarded** to save tokens — the durable state is the tool I/O trail, which is also
  exactly what the end-of-session learning loop replays.
- **Full results are persisted** (the learning loop needs complete results to mine blueprints/goldens)
  but **only a preview is injected into model context** per turn, and history is token-budgeted — what
  is *persisted* ≠ what is *replayed* (D46, [03-context-and-retrieval.md](03-context-and-retrieval.md)).
- Follow-ups load full server-side context (scope-filtered) so users refine intent without restating.
- **Retention (D44):** the session doc has a single Couchbase **TTL** (`SESSION_TTL`); the whole doc
  auto-expires. `SESSION_TTL` **must exceed the learning-loop completion window** (the loop reads the
  trail post-close). PII cell-values dwell the full TTL — accepted trade-off; `SESSION_TTL` value TBD.
- **Pause checkpoint (D45):** on an `askUser` / approval pause, the runtime persists a resumable
  **pause checkpoint** on the session doc (pending question, partial turn trail, and — mid-DAG —
  `blueprint_id` + `slot_bindings` + completed-node output/scratch refs + `awaiting_node`, with a
  CAS `consumed` flag). This makes the runtime **stateless across pauses**: any instance can resume,
  so deploys/restarts during a pause don't lose the turn. **Only pauses are checkpointed** — a crash
  during active compute re-runs the (idempotent, read-only) turn. See
  [04-blueprints.md](04-blueprints.md) §Pause / resume durability.

## Retrieval infra

- Shared embedding model across question / blueprint intent / knowledge, plus the cross-encoder reranker below: both are a **custom API (not OpenAI)** — D71; endpoint + specific model ids TBD. Each is called via a **simple HTTP `POST`** (not any SDK), so each call produces a **manual `EMBEDDING` / `RERANKER` span** (D24).
- **Both vector corpora (blueprint intent + global knowledge) live in neo4j** (D60) — one engine for
  graph traversal + vector recall; no separate vector store.
- Vector recall → **cross-encoder reranker** → top-3 thin cards (blueprints) and reranked knowledge.
- Scope **pre-filter** runs before rerank (drop out-of-scope blueprints).

## Queue & job orchestration

The learning loop is fed by a durable work queue. **Default: Redis Streams** (consumer groups +
a dead-letter stream), self-hosted in-boundary.

- **Why Redis Streams:** human-paced volume (one job per *closed session*, not per turn);
  durable at-least-once delivery that pairs with `content_hash` idempotency; DLQ via pending-entries
  + a `learning:dead` stream; stays inside the trust boundary.
- **Swap-ins:** SQS (delay queues + native DLQ) if on AWS; RabbitMQ (DLX + delayed-message plugin).
  Not Kafka — replay/multi-consumer isn't the need.
- **Escalation:** if we want the multi-stage, retry-heavy, human-in-the-loop pipeline to be durable
  end-to-end (survive restarts, wait days for review approval), wrap the pipeline in **Temporal** and
  keep Redis/SQS only as the ingestion buffer. v1 stays simple: queue + idempotent stateless workers,
  pipeline state on the candidate envelope.

### Message contract — keep it a reference, not the transcript

The message is small; the loader pulls the actual transcript at an exact version:

```yaml
LearningJob:
  session_id:        <id>
  couchbase_doc_id:  <id>          # where the transcript lives
  couchbase_cas:     <rev/CAS>     # exact version to load (no mid-write races)
  user_id:           <id>
  scope_ref:         <scope id/hash>   # NOT the JWT
  trace_id:          <phoenix trace>   # correlate job → request-path traces
  session_closed_at: <ts>
  content_hash:      <hash of transcript>   # idempotency key
```

`content_hash` dedups re-enqueues of an unchanged session. The JWT / raw scope is **never** in the
message (consistent with [06-security-and-governance.md](06-security-and-governance.md)).

### Enqueue / session-close detection (debounce)

Not per turn. A **periodic sweeper** + a `learning_status` flag on the session doc:
`active → pending → queued → processing → done`.

- Each turn bumps `last_activity`.
- Sweeper finds sessions idle > N min with `status=pending`, flips to `queued`, enqueues.
- Flag transitions are the idempotency guard; the sweeper is self-healing (re-runnable after a crash).

### What is NOT in the queue

The **promotion scheduler is not queue-driven.** Candidate → `validated` waits on hit-count
accumulating over weeks and human approvals over days — that is **state** on candidate envelopes
(neo4j / stores), scanned by a **cron-like periodic scheduler**, not messages parked in a queue.

## Offline learning pipeline

- Runs async after a session ends; **not** in the request path and **never** a model tool.
- Reads the Couchbase transcript; writes candidates to neo4j / vector index / user store / review inbox.
- **Kill-switch (D58c):** `LEARNING_ENABLED=false` disables the **whole** loop — write router **and**
  promotion scheduler — **instantly, without a deploy**. Operational safety valve for a discovered
  leak / bad-blueprint pattern. **Reads are unaffected** (`searchKnowledge` / `searchBlueprints` over
  already-published artifacts keep serving); only write-back + promotion stop. The sweeper still marks
  sessions but enqueues nothing while disabled (or the workers no-op) — backlog drains when re-enabled.
- See [05-memory-and-learning.md](05-memory-and-learning.md).

## ClickHouse MCP — service identity (D75)

The ClickHouse MCP data plane is the **existing `clickhouse-api` service** (FastAPI + MCP,
JWT/OIDC auth at `app/auth_jwt.py` + `app/principal.py`) — **adopted and extended**, not built from
scratch (D75). It already provides the 6 core data tools and read-only enforcement + tenant/row-level
isolation. The remaining extension pieces — D57/D62/D63 column-scope enforcement, D64 scratch isolation, D5
scope/session_id injection, and the Phase-0 provenance extractor — are added to `clickhouse-api` by extension. **D66 `resolveValues` (moved to runtime by D77) and D42 catalog overlay (moved to runtime by D78) are no longer `clickhouse-api` extension items.** After D77 + D78, the clickhouse-api extension scope is enforcement-only.

## Boundaries recap

- **Data plane** (ClickHouse MCP = `clickhouse-api`, adopted + extended per D75): read-only; jwt/scope/session_id injected by code.
- **Scratch writes**: privileged side-channel, not an agent tool.
- **Knowledge writes**: offline only.

---

**Status:** Partial (topology Locked; concrete tech for embeddings/reranker TBD)
**Open questions:**
- ~~Vector index choice (native neo4j vector vs. external)~~ — **RESOLVED by D60: neo4j-native**,
  hosting both blueprint intent + global-knowledge vectors. Embedding + reranker are a **custom API (not OpenAI)** (D71) via simple HTTP `POST`; endpoint + model ids TBD.
- Reranker hosting (custom API — D71).
- Couchbase document model for the session trail.
