# Decision Record

Locked decisions from the design discussion. Newest at the bottom of each section. Date: 2026-06-29.

## Architecture
- **D1.** Two-plane split: **data plane** (ClickHouse MCP, read-only) vs. **knowledge plane**
  (blueprint/knowledge reads + offline writes). A bad knowledge write can't corrupt the data path.
- **D2.** One capable LLM per request; runtime orchestrates the model↔tools loop.
- **D3.** Skip OpenAI layers: table usage metadata, Codex enrichment, runtime/live-schema context.
  Human annotations live in `getTableSchema`; institutional knowledge via RAG; memory = global
  knowledge + blueprints + user knowledge.

## Tools
- **D4.** 11 agent-facing tools: 6 MCP + `searchBlueprints` + `getBlueprint` + `runBlueprint` +
  `searchKnowledge` + `askUser`. Writes are never tools.
- **D5.** `session_id`, JWT, column scope are **injected by code** into every tool call — not in tool
  schemas, not in model context. Security property: model can't forge/escalate scope.
- **D6.** `askUser` is a control-flow primitive that pauses/resumes the loop. Disciplined triggers:
  catalog `clarify_if`, missing required slot, low confidence, expensive/broad query. Otherwise
  default + stated assumption.

## Retrieval
- **D7.** Pipeline: embed → vector recall → **scope pre-filter** → **cross-encoder reranker** →
  pre-inject **top-3 thin cards** `{id, intent, slots-summary}`.
- **D8.** Progressive disclosure: thin cards → `getBlueprint` to expand → `runBlueprint` or fall back
  to raw agent loop.

## Blueprints
- **D9.** Blueprint = parameterized validated query **DAG** in neo4j (slots, resolves, uses_rules,
  composes, sql_template, status, hit_count).
- **D10.** Model slot-fills; **runtime executes the DAG deterministically** via `runBlueprint`.
  Safe parameterized binding (injection boundary). Topo-sort + parallel branches.
- **D11.** Intermediate passing: small → inline CTE/`VALUES`; large → session scratch schema. Reuses
  external-upload scratch infra.
- **D12.** Validated-blueprint fast path = **execute-then-verify silently**.
- **D13.** Lifecycle: land as `candidate`; validate by **ALL of** golden-input replay, hit-count
  threshold, and human review. Dedup at write → increment `hit_count` instead of duplicating.
- **D14.** Blueprints store transitive `(table,column)` `USES` edges; retrieval pre-filters by
  scope. Pre-filter is UX/defense-in-depth; injected `runQuery` scope is the hard boundary.

## Blueprint control flow
- **D32.** Blueprint `composes` is a **control-flow graph**, not pure data-flow. `node_kind`:
  `query` | `guard` | `approval`. Optional `when` (declarative predicate over typed outputs —
  count/empty/comparisons/and-or-not/set, **no raw SQL**) with `on_violation: abort|skip|ask`;
  optional `requires_approval` (pauses via `askUser`, `on_deny: abort|skip_remaining`). Guards must
  be **entity-agnostic**. Declared gates are **exempt from the silent fast path**. Any control-flow
  blueprint **requires human review** before `validated`.

- **D33.** **Branching is out of scope** for blueprints. They stay linear DAGs + `guard`/`approval`
  control flow + optional slots. Structurally different/conditional reports are handled by
  **multiple blueprints + agent selection**, not in-blueprint branching.

## Blueprint extraction (payload)
- **D34.** **Lift, don't generate.** The blueprint extractor parameterizes the **real SQL that ran
  and was accepted** in the session (from the `runQuery` trail) — never synthesizes fresh SQL. Learns
  the **final corrected** version; only proposes a blueprint when the session shows an **acceptance
  signal** (no later correction / thumbs-up / result used). Library grows only from real successful
  sessions.
- **D35.** **LLM classifies, deterministic stage rewrites.** Extractor emits a *parameterization
  plan* (per literal: `{table, column, value} → role`); a deterministic **SQL-AST** stage locates
  predicates and substitutes `{slot}` placeholders, validates via `explainQuery`, computes `USES`.
  LLM never re-emits SQL. **Trichotomy** (grounded on `getTableSchema`): **slot** (user-intent
  parameter) | **rule** (matches a catalog rule → `uses_rules`) | **inline** (structural constant).
  `resolves` inferred from (user NL term × column used). Missing rule → paired `schema_edit(add_rule)`
  with `depends_on`.

- **D36.** **Golden = entity-free output signature + replay-with-sampled-inputs** (option A). Store
  `result_signature` (shape + invariants), **not** the session's entity values; replay **samples
  valid slot values at run time**. Golden replay is scoped to **regression** (schema-drift / edit
  breakage that `explainQuery` can't catch), **not** data-drift. No separate entity-bearing store.
  Blueprint payload is now **Locked** (D34–D36).

## Blueprint correctness — PROPOSED (pending review, not yet locked)
- **D37 (proposed).** **Grain is a declared invariant, owned at storage and enforced at authoring —
  not per-blueprint vigilance.** Three parts: (a) declare grain in the `getTableSchema` semantic YAML
  (per-table `grain`, per-measure `{agg, defined_over}`, `temporal: {period_col, semantics}`) as the
  single source of truth blueprints inherit; (b) add a **static fan-out/grain gate** to the
  learning-loop static-validate stage — compute result grain from declared grains and reject/route to
  review on coarse-aggregate-without-de-fan or `per_period`-join-without-period-filter (deterministic,
  no warehouse round-trip); (c) add a **temporal slot type** (`period`/`as_of_date`) so any
  `per_period` table forces a pinned period. Rationale: D36 scopes golden replay to regression, not
  data/semantic drift, so grain errors otherwise survive every automated gate and then run silently.
- **D38 (proposed — DEFERRED: "no versioning for now").** **Version catalog rules; pin rule versions
  in `uses_rules`.** A rule-definition change (e.g. `active_employee` admitting a new status) currently
  mutates every citing blueprint silently. **Deferred** per the no-versioning decision: until revisited,
  a rule change instead **re-flags *all* blueprints citing that rule** for re-validation (coarse, not
  version-targeted). The D39 dedup key drops `@version` accordingly.
- **D39 (proposed).** **Dedup key = `intent + resolves + uses_rules + result-grain`**, not `intent`
  embedding alone. Prevents collapsing blueprints whose NL intent matches but whose SQL semantics
  differ (gross vs net, period filter, contractor inclusion). A dedup false-positive is a correctness
  bug, weighted accordingly vs. a retrieval false-positive.
- **D40 (locked).** **`temporal.semantics` is a closed enum**: `per_period` (needs `period_col`;
  blueprint must bind a `period` slot or `uses_rules: [latest_period]`) | `as_of_date` (needs
  `effective_col`; blueprint must bind an `as_of_date` slot) | `as_of_now` (no temporal slot). Every
  table's `temporal` block carries exactly one. The two temporal slot types pair 1:1 with the first
  two. Locks the temporal half of D37(c); the rest of D37/D39 remain proposed.
  **→ REVISED by D65 (single enum couldn't model ranged / dual-temporal tables).**

- **D41 (locked).** **Slot filling = model proposes, runtime resolves/binds.** Model fills raw values
  from conversation + user knowledge; runtime runs **per-type resolvers** keyed on slot `type` and
  `binds_to` (presence → type-resolve → bind), `askUser` on missing/ambiguous, binds via **ClickHouse
  server-side params** (never interpolated). **Period fill paths:** explicit-named (resolve to a valid
  period key in the domain; multi-match → `askUser`), relative/deictic (resolve to `max` settled
  period, not wall-clock), absent+required (user default / `latest_period` rule / `askUser`).
  Composite blueprints fill slots once at blueprint level. Period values resolve against the warehouse
  period domain, not `to_date`.

- **D65 (proposed, 2026-06-30 — revises D40).** **`temporal` is a list of dimensions, not one enum.**
  Each dimension has a `role` ∈ {`period` (via `period_col` xor `range:{start_col,end_col}`), `as_of`
  (`effective_col`), `event` (`date_col`)} plus a table-level `default_pin`. **Gate:** a blueprint
  reading a table with ≥1 temporal dimension must **constrain at least one** (filter/`GROUP BY` on its
  column(s)) or use a `latest_*` rule — pinning *any* dimension bounds the window; pinning none is the
  failure. Motivated by **payroll**: ranged period (`PayPeriodStartDate`/`EndDate`, no key column) +
  dual-temporal (a `PayDate` *event* and a coverage *period*) — neither expressible in D40. D40
  shorthands map: `as_of_now`→`dimensions: []`; `per_period`→one `period` dim; `as_of_date`→one
  `as_of` dim. A ranged-`period` slot resolves to a `(start,end)` pair / "period covering date X".

## Client-defined value resolution
- **D66 (locked, 2026-06-30).** **New data-plane tool `resolveValues(table, column, concept, period?)`
  for client-defined / time-varying code spaces** (`EarnCode`, `TypeCode`, departments…). Static
  catalog maps can't model per-tenant, drifting enums. Returns **client-scoped** values ranked by
  semantic match to `concept` (`ClientCode`/scope **injected**, D5). **Live** each call (`DISTINCT
  (code,description)` in scope → semantic rank); **no tenant-knowledge cache for now** — ephemeral,
  index later. 12th agent-facing tool; justified as **non-overlapping**. Integrations: (a) catalog
  marks `client_defined` columns and points their ambiguities at the tool; (b) it's the **D41 slot
  resolver** for client-coded slots, so blueprints stay entity-agnostic — they store the *concept*
  (`EarnCode IN {pto_codes}`), resolved per-client at run time, never the codes; (c) low-confidence /
  multi-match results drive an `askUser` clarify. **Explicitly deferred:** tenant-knowledge scope
  (global/tenant/user) — not added now.

- **D67 (locked, 2026-06-30).** **Two rule kinds: static and resolved (dynamic).** A catalog `rule`'s
  `predicate` is either a fixed SQL boolean (**static**) or references `resolveValues(column, concept)`
  over a `client_defined` column (**resolved**) — the runtime expands it to a concrete per-client value
  set before injection, bound as params (not string-interpolated). Resolved rules replace hardcoded
  client-specific codes (e.g. PAF `FieldId` lists, `EarnCode`/`TypeCode` concepts). Extends D66; same
  injection boundary as slots (D41/D5).

## Context budget & loop guardrails
- **D46 (locked, 2026-06-30).** **What is persisted ≠ what is injected.** The trail has two consumers:
  the **learning loop** needs the **full** tool I/O trail (complete results) end-of-session; **model
  context** needs only enough to continue. So: (a) **results are preview-only in context** —
  `{SQL, column shape, row_count, ≤N-row preview}`; full results persisted to Couchbase + shown in UI
  (removes the dominant growth term; 10k-row results never enter context); (b) **history is
  token-budgeted with summarized overflow** — newest turns verbatim, older turns compacted to a
  running summary that **preserves SQL verbatim** and summarizes prose. Preview size + history token
  budget are tunable. Resolves review item F.
- **D47 (locked, 2026-06-30).** **The raw agent loop runs under per-turn budget caps** (max tool-call
  iterations / tokens / wall-clock). On hitting a cap the runtime **pauses via `askUser`** ("continue,
  refine, or stop?", returning the best partial result) and emits a Phoenix event — never a silent
  infinite loop. The blueprint fast path is already bounded by DAG size, so this guards the raw loop.
  Cap values are tunable. Resolves review item G.

## Blueprint dedup & resolvers
- **D48 (locked, 2026-06-30).** **Dedup is two layers; the hard layer is race-safe by construction.**
  **Hard dedup:** a **deterministic canonical key** `hash(resolves, uses_rules, result-grain,
  normalized sql_template AST)` — the structural facts, **not** NL `intent` — with writes serialized
  **single-writer-per-`canonical_key`** (partitioned write-stage queue / per-key Redis lock over the
  D30 Redis; the key is derived mid-pipeline at the dedup stage, so serialization is at the **write
  stage**, not ingestion). Concurrent equivalents → one create + one `hit_count` increment, never a
  duplicate. The key is **sound, not complete** (same key ⇒ truly equivalent → safe auto-merge;
  equivalents written differently may miss). **Soft dedup:** embedding similarity on `intent` →
  variant/conflict → review inbox (human-adjudicated, race-tolerant). **Refines D39:** `intent` lives
  in the *soft* layer; the *hard* key is the deterministic structural subset. Fixes the prior race
  where `content_hash` (identical transcripts only) + fuzzy embedding missed concurrent semantic
  duplicates. Resolves review item E. **(N4)** `result-grain` derives from D37 (still proposed) but is
  **subsumed by `normalized SQL AST`** for the equality test (identical ASTs ⇒ identical grain), so it
  is a redundant defensive cross-check — the key stays **sound without D37**; D37 only adds robustness
  against shallow AST normalization. D48 does **not** block on D37.
- **D49 (locked, 2026-06-30).** **Slot resolvers are deterministic code — no LLM inside
  `runBlueprint`.** Type-resolve = normalize + catalog/warehouse-domain lookup + validate, keyed on
  slot `type` and `binds_to`. The agent LLM's *only* slot-filling role is **proposing raw values**
  (D41), before execution. Anything not deterministically resolvable (multi-match, no-match, fuzzy NL
  like "the pay run after the holidays") → **`askUser`**, never an LLM guess. Keeps the fast path
  genuinely deterministic + silent + secure. Fixes the misleading "resolver model" wording in
  [04-blueprints.md](../04-blueprints.md) §Slot filling. Resolves review item H.

- **D50 (locked, 2026-06-30).** **Context assembly filters before it compacts; the history summary is
  a derived view, not a persisted artifact.** Fixed order: load raw trail → **scope-filter (D44)** →
  **budget/compact (D46)** → inject. Because the provenance-filter precedes compaction, D46's prose
  summary only ever contains in-scope entries and needs no surviving per-column provenance — resolving
  the direct D44×D46 conflict (provenance filtering vs. provenance-destroying compaction). Summary is
  recomputed per turn from the scope-filtered raw trail (already persisted for learning) and **cached
  by `(scope, content-hash of compacted set)`** so it re-runs only when the set ages or scope changes.
  Resolves new-unknown N1.

- **D51 (locked, 2026-06-30).** **Candidate evidence is snapshotted at extraction into an
  access-controlled provenance/audit store; the candidate carries only an `evidence_ref`.** A
  candidate can dwell in the review inbox for days while its source session expires at `SESSION_TTL`
  (D44) — bare refs would dangle. Snapshotting makes the candidate self-contained. **Because evidence
  quotes are entity-bearing, the snapshot is NEVER inlined into the entity-free global stores**
  (neo4j blueprints, knowledge vector index) — that would breach D17; it lives in a dedicated
  in-boundary audit store (session-store PII posture, own retention ≥ candidate lifetime). Resolves
  new-unknown N2.

- **D52 (locked, 2026-06-30).** **A ClickHouse-dialect SQL parser/AST is a named shared component**
  (runtime + learning loop), load-bearing for **security** (D44 column provenance), **correctness**
  (D48 canonical dedup key), and authoring (D35 template rewrite). **Per-consumer fail behavior** when
  a parse fails: D44 → **fail-closed** (drop the entry from replay — never assume in-scope); D48 →
  **fail-soft** (skip the hard key, fall to the soft embedding/review layer — never a wrong merge);
  D35 → **fail-to-review** (don't auto-promote). Must be ClickHouse-dialect (not generic SQL); dialect
  gaps are the likely failure source and the fail behaviors keep a gap from becoming a leak or a bad
  merge. Resolves new-unknown N3.

- **D53 (locked, 2026-06-30).** **Semantic Catalog operations.** (a) **Deploy-coupled load:** catalog
  baked into the deploy artifact, merged PR live on next deploy ⇒ **catalog SHA ≡ deploy SHA** (fully
  reproducible; catalog-edit latency = deploy cadence, acceptable for a rarely-mutating curated
  store). (b) **Bot-authored PRs:** `schema_edit` candidates open a branch + YAML patch + PR with CI
  (schema lint + `explainQuery` dry-run); human merge = the D18 gate. (c) **Mismatch handling:**
  table-without-catalog-entry → structural-only `getTableSchema`, raw-loop-usable but not
  blueprint-eligible; catalog-diverged-from-warehouse → caught by the **D43 conformance probe** →
  `schema_edit` PR (no new mechanism). Resolves new-unknown N5.

- **D54 (locked, 2026-06-30).** **Preview-only results (D46) come with a push-computation-into-SQL
  norm + explicit truncation signal.** The preview carries `row_count` + a `truncated` flag so the
  model never mistakes it for the whole answer; the model answers aggregate/superlative/ranking
  questions by **querying** (`ORDER BY…LIMIT`, `argMax`, `GROUP BY`), not by eyeballing rows
  (preview-only *enforces* this; blueprints already encode it). The **user still sees all rows** (full
  result in the UI). Re-queries to compute over the full set count against the **D47** loop budget, so
  the norm is what stops preview-only from inflating iterations; **no pagination tool** is added (tool
  surface stays minimal). Resolves new-unknown N6.

- **D55 (locked, 2026-06-30).** **Two clarifications (N8).** (1) D45 pause-resume reconnects scratch
  on a **best-effort** basis — a pause is user-bounded, so if scratch has TTL'd away, resume **falls
  back to a fresh agent loop** (no hard "scratch TTL ≥ pause window" requirement). (2) D47 — when the
  user chooses **"continue"** past a budget cap, the runtime **grants a fresh budget window**, re-
  prompting only if that window is also exhausted. Resolves new-unknown N8.

## Memory / learning
- **D15.** End-of-session write router: extractor → leakage gate → dedup/conflict → promotion/routing.
- **D16.** Targets: blueprint (neo4j), global knowledge (RAG), user knowledge (per-user), schema edit.
- **D17.** **Global = entity-agnostic** (blueprints, global knowledge); **user knowledge may carry
  entities**. Leakage gate enforces; entity-bearing facts reroute to user knowledge.
- **D18.** **Schema edits → human review queue, never auto-commit.** Blueprints/global knowledge
  auto-land as candidates.
- **D26.** Learning loop = **two processes**: a per-session **write router** (async job) and a
  store-wide **promotion scheduler**. Neither is a model tool; both run in-boundary and are traced.
- **D27.** Write-router stages: loader/normalizer → **triage** (skip empty sessions) → **grounded
  extractor** (RAG over existing stores so it won't re-propose) → generalize + static-validate
  (explainQuery dry-run, capture session run as golden) → leakage gate → dedup/conflict → writers.
- **D28.** Every candidate carries a **provenance envelope** (source session + Phoenix trace,
  evidence, rationale, entity-scan, dedup, `content_hash`) — auditable and idempotent.
- **D29.** Promotion is a **separate scheduler**: candidate → validated via golden replay / hit-count
  / human; negative signals (corrections, failed replay, schema drift) demote or retire.
- **D30.** Ingestion queue = **Redis Streams** (self-hosted, consumer groups + dead-letter stream).
  Message is a **reference, not the transcript** (`session_id`, `couchbase_doc_id`+`cas`, `user_id`,
  `scope_ref`, `trace_id`, `session_closed_at`, `content_hash`); JWT/raw scope never included.
  Session-close detection via a **periodic sweeper** + `learning_status` flag
  (`active→pending→queued→processing→done`). Promotion scheduler is **cron-scanned state, not queued**.
  Escalate to Temporal only if the pipeline needs durable end-to-end orchestration.
- **D31.** Extractor output is **structured-schema-forced** (retry on mismatch); every candidate has
  a shared header (`type`, `confidence`, `evidence`, `rationale`, `proposed_action`,
  `entity_self_check`); **no `evidence` ⇒ rejected**. Accepted payloads: **`global_knowledge`,
  `user_knowledge`, `schema_edit`** (Locked). **`blueprint` payload Deferred** to a dedicated
  discussion (incl. the golden-fixture entity-governance fork). Cross-target candidates link via
  `depends_on`.

## Observability
- **D23.** **Arize Phoenix + OpenTelemetry** (OpenInference conventions) for tracing across the
  request path and the offline learning loop. Span kinds map ~1:1 to components (AGENT/LLM/TOOL/
  RETRIEVER/EMBEDDING/RERANKER/CHAIN/GUARDRAIL).
- **D24.** **Self-hosted** Phoenix inside the trust boundary — HR PII must not reach a SaaS endpoint.
  Auto-instrument the Anthropic SDK; manual spans for custom runtime + learning loop.
- **D25.** Traces grouped by `session.id`. **Never log JWT/scope token** (log a scope id/hash only);
  **mask result rows + bound slot values** via OpenInference hide flags + a custom redactor. Log
  shape (counts/columns/latency), not content. Leakage gate emits a `GUARDRAIL` span.

## External data
- **D19.** External CSV/xlsx joins via a **session-scoped ClickHouse scratch schema** (not DuckDB).
  Loaded via a privileged side-channel, not an agent tool. Read-only MCP unchanged.
- **D20.** Scratch tables: session/access-scoped, TTL'd. Upload sessions are weak blueprint
  candidates; if learned, generalize the upload to a BYO-fact-table slot.

## Security / infra
- **D21.** Read-only agent path; scratch writes via privileged side-channel only.
- **D22.** Couchbase session store keyed by `session_id`; persist messages + tool I/O trail; discard
  thinking.
- **D44 (locked, 2026-06-30).** **The replayed session trail is a scope layer + has a retention
  policy** (the trail is raw HR PII). **(C) Mid-session scope change — provenance-filter:** every
  stored tool result carries a **column-provenance set** (columns *referenced* = `USES` semantics, so
  derived aggregates over forbidden columns are gated too); **every turn**, the runtime drops trail
  entries whose provenance ⊄ current scope before context assembly. The `runQuery` hard gate (new
  queries) + provenance-filter (replayed history) close both leak paths; in-scope context is
  preserved. Added as the "replayed session trail" row in the [06](../06-security-and-governance.md)
  scope table. **(D) Retention — single TTL:** the session doc has one Couchbase `SESSION_TTL`; the
  whole doc auto-expires, no partial value-purge. **Constraint:** `SESSION_TTL` must exceed the
  learning-loop completion window. Accepted trade-off: PII cell-values dwell the full TTL.
  `SESSION_TTL` value is an open parameter. Resolves review items C and D.
- **D45 (locked, 2026-06-30).** **In-flight turns are durable across restarts via a pause checkpoint;
  active compute relies on idempotent re-run.** On an `askUser`/approval pause the runtime persists a
  **pause checkpoint** to the session doc (pending prompt, partial trail, and mid-DAG `blueprint_id`
  + `slot_bindings` + completed-node output/scratch refs + `awaiting_node` + CAS `consumed` flag) —
  making the runtime **stateless across pauses** (any instance resumes; deploy/crash-safe). Resume
  CAS-consumes the checkpoint (exactly-once) and continues at `awaiting_node`; completed nodes never
  re-run. **Only pauses are checkpointed** — a crash during active, non-paused compute simply
  **re-runs the whole turn**, which is safe because the data path is **read-only + idempotent** and
  scratch is **TTL'd**. This also settles the prior "no atomicity/compensation" gap: **re-run is the
  compensation** (nothing to undo, only scratch to GC). Abandoned pauses expire via `SESSION_TTL` +
  scratch TTL. Rejected: sticky+in-memory (lossy on deploy) and Temporal-wrapped turns (too heavy for
  a short interactive pause; 09 reserves Temporal for the learning loop). Resolves review item B.
- **D64 (locked, 2026-06-30).** **Scratch-table access is bound to the owning `session_id`, enforced
  at the MCP by the same SQL parse that enforces column scope (D57).** Gap closed: scratch tables are
  named `scratch.s_<sessionId>_<file>` and called "access-scoped," but the only described enforcement
  was the D57 **column**-scope parser, which gates warehouse *columns* against the user's scope — it
  said nothing about which *session* may read a given scratch table, and uploaded scratch tables aren't
  in the column-scope model at all. A naming convention is **not** an access boundary: nothing stopped
  session A's `runQuery` from `JOIN scratch.s_<sessionB>_…`, and uploaded files are often the most
  sensitive PII. Fix, consistent with the chosen MCP-receives-scope-as-input model (D5/D57, not
  ClickHouse roles): on every `runQuery`, the MCP uses the **D52/D62 `sqlglot` parse** (already run for
  column extraction) to also enumerate **referenced tables**; any table in the `scratch` database
  **must match `s_<injected session_id>_*`**, else the query is **rejected**. The model never sees or
  supplies `session_id` (D5), so it cannot forge cross-session access. **Fail-closed (consistent with
  D63):** a parse failure already rejects the query, so an unparseable scratch reference never runs
  unchecked; a parsed reference to a foreign or malformed scratch name is rejected via the
  §Graceful-denial path. **Defense-in-depth (optional, not the boundary):** because the privileged
  ingestion side-channel (D19) creates scratch tables at a known time under a known session, it *may*
  additionally grant ClickHouse access only to that session's identity — but per D57 the **MCP parse is
  the live boundary**, not the engine grant. Scratch isolation is therefore a **session boundary**
  (own-session-only), orthogonal to and stacked with the D57 **column boundary** (in-scope-columns-only)
  and the D44 replayed-trail boundary. Resolves architectural-review finding #2.

## Semantic catalog
- **D42 (locked, 2026-06-30).** The **Semantic Catalog** is a first-class component, stored as
  **git-backed YAML** (one file per table + `rules.yaml`). It is the curated half of `getTableSchema`
  (entities, grain, per-measure `{agg, defined_over}`, `temporal`, rule definitions, ambiguities,
  synonyms, enums); the structural half stays live ClickHouse introspection, so
  `getTableSchema = introspection ⨝ catalog overlay`. Consequences: (a) **catalog version = git
  commit SHA**, stamped into `USES`-edge validation + golden replays; (b) the learning-loop
  `schema_edit` review inbox emits a **PR**, and human merge **is** the D18 never-auto-commit gate
  (one mechanism for write path + review queue); (c) a catalog SHA bump triggers the schema-drift
  maintenance job (re-validate `USES` edges + grain gate + golden replay, demote breakage), and per
  D38 a `rules.yaml` change coarsely re-flags all citing blueprints; (d) **rule definitions live in
  `rules.yaml`, enforced two ways** — deterministic AST injection in blueprints vs. model-written SQL
  in the raw loop (governance note in [06-security-and-governance.md](../06-security-and-governance.md)),
  and rules are a correctness convention, never an access boundary. Resolves review item A
  (catalog had no storage home).

## Blueprint silent-path safety
- **D43 (locked, 2026-06-30).** **Silent-eligibility is a freshness-bounded attestation, not the
  permanent `validated` status.** "Semantic drift" decomposes into **three concrete probes** run by
  the promotion scheduler: (1) **result grain-integrity** (per blueprint: row count `== COUNT(DISTINCT
  result-grain)`, measures at declared `{agg, defined_over}`); (2) **catalog-vs-warehouse
  conformance** (per table: declared `grain`/enums/temporal cols still hold on live data; a failing
  table flags every blueprint over it); (3) **rule-semantics currency** (cited `rules.yaml` SHA
  unchanged). Scope is **incorrectness, not change** — pure data-value drift is **not** flagged
  (honors D36). Predicate: `silent_eligible ⇔ status=validated AND drift_status=clean AND
  last_drift_check_at within DRIFT_TTL`. **Not eligible → split by reason:** `suspect` (a probe
  failed) → demote to `candidate` + review flag; merely **stale** → non-silent degraded run (show SQL
  + inline grain assert + verify nudge) + on-demand re-attest. **Phased rollout:** Phase 1 (launch)
  ships `validated→silent` unconditionally — an **explicitly accepted, time-boxed risk**; Phase 2
  (committed fast-follow, not open backlog) builds the probes + predicate and **supersedes D12's
  unconditional silent path**. Resolves review item I; the OPEN-QUESTIONS "semantic-drift canary" is
  now a specified Phase-2 deliverable.

- **D56 (locked, 2026-06-30).** **No silent path — the agent verifies every blueprint response
  before the user sees it; the user is never asked to verify.** Supersedes D12's "execute-then-verify
  **silently**" and reframes D43 Phase 1. Every `runBlueprint` result passes a **mandatory agent-side
  verification gate** before it is returned: (a) **code-computed assertions** — the D43 probe-#1
  **grain-integrity** check (`result row count == COUNT(DISTINCT declared result-grain)`; each measure
  aggregated at its declared `{agg, defined_over}`) plus the blueprint's `result_signature`
  invariants, evaluated **deterministically in code**, not by model judgment; (b) **LLM review** of
  the result + the code-computed assertions against user intent. **Any failed assertion → fall back to
  the raw agent loop** (or `askUser`), **never return the suspect answer** (extends 04 §Execution
  step 6, which already mandated fallback-on-verify-failure). Rationale: the pre-existing "model
  sanity-checks shape and narrates" step is a **rubber-stamp** — a wrong-grain number is shape-correct
  (one row per dept, positive value), so an LLM eyeballing it cannot catch the fan-out double-count;
  the teeth come from the **deterministic** grain check, not the LLM. **The user is never nudged to
  "verify this"** (rejected as its own trust failure); SQL stays visible for transparency
  ([08-ui.md](../08-ui.md)) but verification is the agent's job. **Launch dependency:** because the
  deterministic check needs a grain to check **against**, this makes **grain *declaration*** (D37a /
  D40 `grain` + `{agg, defined_over}` + `temporal` in the catalog YAML) a **launch requirement**; the
  **static authoring gate** (D37b) stays a Phase-2 deliverable — so grain errors are caught at
  **runtime** at launch (every response gated) and **additionally** at **authoring time** once D37b
  lands. **Cost accepted:** every fast-path response pays an extra deterministic check (often a
  lightweight distinct-grain count query) + a real LLM verify gate — a small latency/token cost on the
  fast path, taken deliberately because trust is the product. Resolves review risk #1.

- **D57 (locked, 2026-06-30).** **Column-level scope is enforced at the MCP layer by parsing the SQL
  (option 2a).** The runtime injects `scope` into every `runQuery` (D5); the MCP **parses the live
  query with the D52 ClickHouse-dialect parser**, extracts its **referenced columns** (`USES`
  semantics — same derivation as D44 trail-provenance, so an `AVG(gross_pay)` is gated even though it
  returns no raw payroll column), and **rejects the query if referenced columns ⊄ scope**. Scope is
  *not* pushed down to ClickHouse roles (option 2b rejected — see below); the **parser is the live
  security boundary.** Consequence: the D52 parser gains a **fourth consumer, and the only one that
  gates *live* access** — [09-infrastructure.md](../09-infrastructure.md) §SQL parser. **Rejected 2b
  (scope → ClickHouse role / `SET ROLE`, engine enforces):** would keep the parser off the security
  hot path, but does not fit the chosen MCP-receives-scope-as-input model. **Accepted exposure:**
  unlike D44 (replay, fail-closed = drop entry), D48 (fail-soft), and D35 (fail-to-review), the
  live-query consumer faces the hardest fail fork — a parse failure means either **reject a legitimate
  query** (agent looks broken) or **run it unchecked** (PII leak), and the model emits arbitrarily
  exotic ClickHouse SQL. **The parse-failure behavior on a live query is deliberately left OPEN here**
  and resolved in the parser/failure discussion (review risk #8 / D52). Until then the working
  assumption is **fail-closed + alert** (never fail-open), with an adversarial-SQL test suite to
  measure the false-reject rate. Resolves the "how is live scope actually enforced" half of review
  risk #2; the parse-failure half is tracked open.

- **D58 (locked, 2026-06-30).** **Global-store entity-leak posture: human pre-gate for knowledge,
  sampled detection for blueprints, plus a global learning kill-switch.** The leakage gate (stage 4)
  is probabilistic, validation never re-scans for entities, and `searchKnowledge` returns candidates
  immediately (no buffer) — so the gate's false-negative rate would equal the cross-user PII-leak rate
  with no checkpoint. Fix, per target:
  - **(a) `global_knowledge` — human review *before* retrievable.** Amends D18 and **reverses the
    "searchKnowledge returns candidate docs" assumption for knowledge specifically**: knowledge
    candidates land in the **review inbox**, and `searchKnowledge` returns **only human-approved**
    statements. Rationale: free-text prose is the hardest to guarantee entity-free and the easiest
    place for a stray name to hide; with immediate retrievability and entity-blind validation, a human
    is the **only** reliable backstop. Cost accepted: slower knowledge flywheel.
  - **(b) `blueprint` — keep auto-land as `candidate` (D13), add sampled leak detection.** A sampled
    fraction of auto-landed blueprint candidates **plus every leakage-gate near-miss** routes to the
    review inbox; humans **measure** the residual leak rate and **retract** leaked blueprints (pull
    from index; use the D25 `GUARDRAIL` trace to find who was exposed). Proportionate because
    blueprints leak far less than prose — literals are lifted to slots, SQL is templated. Blueprint
    candidate retrievability (suggested cautiously, never auto-run — D13/D43) is unchanged.
  - **(c) Global learning kill-switch.** An environment variable (e.g. `LEARNING_ENABLED=false`)
    **disables the entire learning loop** — write router **and** promotion scheduler — instantly,
    **without a deploy**. Operational safety valve: on discovering a leak pattern or a bad-blueprint
    pattern, ops can halt **all** write-back immediately. **Reads are unaffected** — `searchKnowledge`
    / `searchBlueprints` over already-published artifacts keep working; only the write-back/promotion
    side stops. Resolves review risk #3.

- **D59 (locked, 2026-06-30).** **Composite-blueprint execution semantics — three holes closed;
  composite is in the first cut.** Resolves review risk #5.
  - **(a) Intermediate passing is injection-safe (refines D11).** Replaces D11's size-based "small →
    inline `CTE`/`VALUES`; large → scratch" with a **shape-based** split that never interpolates
    untrusted upstream output into SQL text: **scalars → bound as typed ClickHouse server-side
    parameters** (carry their type, never rendered as strings); **tables (small *or* large) →
    materialized to the session scratch schema and `JOIN`ed**. This honors the D10 "never
    string-interpolated" boundary for inter-node data too — upstream outputs run over warehouse +
    scratch + possibly externally-uploaded PII, so a "department name" cell is untrusted text and must
    never become SQL. Cost: a scratch round-trip even for a small table (accepted; scalars stay cheap
    as params).
  - **(b) Approval gates reference upstream outputs only.** `requires_approval` fires **before the
    node runs**, so its `prompt` interpolation and `show` list may reference **upstream / completed**
    outputs only (e.g. an upstream `{n}` count, `$1.dept_actuals`) — **never the gated node's own
    un-computed output** (the old `show: [$4.flagged]` example was impossible). A genuine
    "compute-then-confirm-before-returning" need would be a **separate post-compute node kind**, not
    conflated with the pre-run gate; not added now.
  - **(c) `guard` node_kind cut (amends D32).** After D33 (no branching) a guard's "routes" was
    vestigial; its only real capability — abort/skip on a predicate — is the **`when` clause already
    attachable to any node** (`when` + `on_violation: abort|skip`). So the `node_kind` enum is now
    **`query | approval`** (was `query | guard | approval`). One fewer node kind.
  - **(d) Scope: composite blueprints ship in the first cut**, refined later if needed (not deferred
    to a later phase).

- **D60 (locked, 2026-06-30).** **Commit to neo4j deliberately; it hosts both blueprint *and*
  knowledge vectors.** Reviewed whether neo4j earns its operational weight (it could be a
  document+vector store wearing a graph costume — `USES` sets are precomputed at creation time, so
  scope pre-filtering is a set-subset check, not live traversal). Decision: **the blueprint model is
  treated as a genuine graph** (blueprint-to-blueprint composition via `composes`, dependency
  networks, "what's downstream of table/rule X" traversal as a real capability — e.g. D38's re-flag
  scan), so a graph engine is the right substrate. To make that weight pay off, neo4j **absorbs the
  global-knowledge vector index**: it now hosts **both** blueprint intent embeddings **and**
  knowledge embeddings, **resolving the "native neo4j vector vs. external" open question in neo4j's
  favor** and removing the standalone "Vector index + RAG" as a separate store. The cross-encoder
  reranker (D7) still sits in front, unchanged. **Not consolidated:** the provenance/audit store
  (D51) stays separate (distinct PII posture + retention). Net store list drops the standalone vector
  index; neo4j is now justified by serving graph traversal + both vector corpora. Resolves review
  risk #6 and the [09](../09-infrastructure.md) "vector index choice" open question (embedding-model
  choice + reranker hosting remain open).

- **D61 (locked, 2026-06-30).** **Task/progress streaming to the UI masks turn latency.** A turn —
  even the fast path — costs an embed + vector recall + cross-encoder rerank + **two LLM round-trips**
  (pick blueprint, then the D56 verify gate) + ≥2 ClickHouse queries, plausibly 8–15s before the user
  sees an answer. Rather than (only) chase a lower *total* latency, the UI streams **step-level
  progress events** throughout the turn — "searching for a matching blueprint…", "running query…",
  "step 2 of 4: department actuals…", "verifying results…" — so **perceived latency ≪ total
  latency**. **One instrumentation, two consumers:** the same stage boundaries that emit OTel/Phoenix
  spans ([10](../10-observability.md)) double as user-facing progress events — context assembly,
  blueprint pick, each `runBlueprint` DAG node, the D56 verify gate, and a D47 "this is taking a
  while" pause all surface as progress. **PII-safe (consistent with D25):** progress labels show
  **step/shape, not cell values or bound slot values**; SQL may still be surfaced under the
  transparency principle ([08](../08-ui.md)) but progress text stays high-level. **"Time to first
  progress event"** becomes the felt-latency metric. **Scope note:** this addresses *perceived*
  latency; it does **not** remove the need for a hard **wall-clock ceiling** (the D47 cap value, still
  TBD) — but it lowers the urgency of fabricating p50/p95 targets before Phase-0 traffic exists.
  Resolves the perceived-latency half of review risk #7; latency *budgets* (D47 values, TTFT target)
  remain open, to be set against real measurements.

- **D62 (locked, 2026-06-30).** **The D52 ClickHouse-dialect parser is `sqlglot` (Python).** Runtime
  is Python, so an in-process pure-Python library beats a Go/Rust parser behind a service/FFI
  boundary. `sqlglot` is the only option that serves **all three** D52 consumers with one library:
  - **D57/D44 column extraction** — `parse_one(sql, dialect="clickhouse").find_all(exp.Column)` plus
    the **optimizer `qualify_columns`** (fed the catalog schema) resolves unqualified columns to their
    owning table, yielding the **qualified `(table, column)` USES set** the scope/provenance check
    needs (this is what makes a derived `AVG(gross_pay)` gate-able).
  - **D48 canonical dedup key** — `sqlglot`'s optimizer/normalize (identifier normalization, alias
    qualification, canonicalize) produces the **normalized AST** the canonical key hashes.
  - **D35 template rewrite** — AST transform locates predicate nodes → substitutes `{slot}`
    placeholders → regenerates via the ClickHouse generator.
  - **Known limitation (the reason D52 has fail-behaviors):** `sqlglot` **re-implements** the
    ClickHouse grammar — it is **not** ClickHouse's own parser — so dialect gaps will exist on exotic
    syntax (some table functions, higher-order/lambda forms, newest syntax). The per-consumer fail
    paths (D57 live → **fail-closed**, D48 → **fail-soft**, D35 → **fail-to-review**) contain it.
  - **Parser-accuracy oracle:** ClickHouse `system.query_log.columns` records the exact columns each
    finished query accessed — a **ground-truth oracle** to grade the parser's extracted `(table,
    column)` set on real queries, making the D57 "measure the false-reject / parse-gap rate" test
    suite concrete + automatable. (Post-execution, so a *validation* oracle, not the live gate;
    non-executing `EXPLAIN` is the pre-flight parseability check.) Resolves the "which parser" half of
    the [09](../09-infrastructure.md) §SQL-parser open question; the **live parse-failure policy**
    (D57, working assumption **fail-closed + alert**) and measured coverage-gap rates remain open,
    now backed by the query_log harness. Resolves the parser-choice half of review risk #8.

- **D63 (locked, 2026-06-30).** **Live `runQuery` parse failure → fail-closed + alert (never
  fail-open).** Closes the policy left open by D57. When `sqlglot` (D62) cannot parse a live query
  well enough to extract its referenced columns, the MCP **rejects the query and alerts** — it never
  runs an unverifiable query against the warehouse. Consistent with the rest of the security posture
  (no silent path D56, knowledge human-gate D58a, D44 fail-closed trail-drop): a **rejected legit
  query is a recoverable annoyance** (surfaced cleanly, user can rephrase / it falls back to the raw
  loop), whereas a **fail-open leak is unrecoverable**. De-risked by D62's **`system.query_log.columns`
  oracle**: the false-reject rate is **measurable before launch**, so "agent looks flaky" is a number
  to drive down by improving parser coverage — not a blind risk. **If the measured trip-rate is
  material, invest in parser coverage** (contribute dialect fixes upstream / pre-normalize known-gappy
  constructs); do **not** relax to fail-open. The user-facing rejection reuses the §Graceful-denial
  path in [06](../06-security-and-governance.md). Resolves the parse-failure half of review risk #8;
  the eight review risks are now all dispositioned.

## Testing
- **D64 (locked, 2026-06-30).** **Four-layer test strategy with an automated spec-conformance suite.**
  Because nearly every locked decision is a **hard invariant** (D56 no-silent, D57 scope enforcement,
  D58 knowledge human-gate, D44 provenance filter, D59 injection-safe intermediates, D61 progress
  streaming, D63 fail-closed parser), tests must **prove** them, not assume them. Layers: **(1) Unit**
  — the deterministic security/correctness primitives (provenance extraction, scope check, grain-
  integrity assertion, dedup key, resolvers, template rewrite, `when` evaluator) with adversarial
  negatives; **(2) Component/integration** — one component against its **real** containerized store
  (MCP↔ClickHouse, executor↔ClickHouse+neo4j, learning loop↔neo4j/Redis, retrieval↔neo4j,
  session↔Couchbase); **(3) E2E spec-conformance** — a **full `docker-compose` stack** seeded with a
  deterministic fixture (incl. a **deliberately wrong-grain blueprint** as the D56 negative), driven
  through the **real UI by Playwright (MCP)**, asserting the observable flows **and** the trust/
  security invariants (one scenario per flow/decision — see [11-testing.md](../11-testing.md)); **(4)
  Eval/canary** — the existing golden Q&A + result-set/LLM-judge Phoenix experiments (D36, 05/10),
  the data-correctness-over-time layer. **LLM determinism in e2e:** CI uses record/replay cassettes +
  asserts **structural/behavioral invariants** (never exact prose); a **nightly live-model** run
  catches drift, graded by Layer 4. **CI:** Layers 1–2 per-PR; Layers 3–4 nightly/release; the D62
  `system.query_log.columns` oracle runs as a parser-accuracy job tracking the D63 false-reject rate.
  **Sequencing:** Layer-1/2 units can be built per-module as code lands; Layers 3–4 need the runtime +
  UI to exist (Phase-0 build). Adds [11-testing.md](../11-testing.md) as a chapter.
