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
  `searchKnowledge` + `askUser`. Writes are never tools. *(Count amended by D66 to 12 — `resolveValues` added as a model-facing tool; see also D77 which moves its implementation to the runtime. See [D66](#client-defined-value-resolution) and [D77](#client-defined-value-resolution).)*
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
- **D66 (locked, 2026-06-30; amended by D77).** **`resolveValues(table, column, concept, period?)`
  for client-defined / time-varying code spaces** (`EarnCode`, `TypeCode`, departments…). Static
  catalog maps can't model per-tenant, drifting enums. Returns **client-scoped** values ranked by
  semantic match to `concept` (`ClientCode`/scope **injected**, D5). **Live** each call (`DISTINCT
  (code,description)` in scope → semantic rank); **no tenant-knowledge cache for now** — ephemeral,
  index later. Model-facing tool; justified as **non-overlapping**. Integrations: (a) catalog
  marks `client_defined` columns and points their ambiguities at the tool; (b) it's the **D41 slot
  resolver** for client-coded slots, so blueprints stay entity-agnostic — they store the *concept*
  (`EarnCode IN {pto_codes}`), resolved per-client at run time, never the codes; (c) low-confidence /
  multi-match results drive an `askUser` clarify. **Explicitly deferred:** tenant-knowledge scope
  (global/tenant/user) — not added now. **→ D77 moves the implementation into the agent runtime
  (over `runQuery`); the model interface and concept/ranking behaviour are unchanged.**

- **D77 (locked, 2026-06-30; BUILT 2026-07-01, Session 9 — see
  [resolvevalues-design.md](resolvevalues-design.md)).** **`resolveValues` moves from a ClickHouse-MCP
  data-plane tool to an agent-runtime composite tool implemented over `runQuery`.** The model interface
  is unchanged
  (`resolveValues(table, column, concept, period?)` → `[{value, description, score, freq}]`); only
  the implementation location moves. Under the hood the runtime:
  1. Issues an ordinary `runQuery` (D57 column-scope enforcement and D5 `ClientCode`/tenant RLS
     are automatic — no separate enforcement path needed in the MCP) — roughly
     `SELECT <column>, <descriptionCol>, count() AS freq FROM <table> [WHERE <period>] GROUP BY … ORDER BY freq DESC LIMIT N`.
  2. Ranks the returned rows in the runtime by **semantic similarity of `concept`** (the embedding
     client in the runtime, D71) combined with `freq`, returning `[{value, description, score, freq}]`.

  **Rationale:**
  - Enforcement free and correct — the backing `runQuery` is already scope-checked (D57) and
    tenant-isolated; no separate enforcement path is needed in the MCP.
  - Injection-safe by construction — `concept` never touches SQL; ranking happens in the runtime
    after the query (consistent with D10).
  - The embedding client (D71) lives in the runtime, not duplicated into the MCP service.
  - Keeps the ClickHouse MCP data plane pure — exactly the **6 read tools**
    (`listDatabases`, `listTables`, `getTableSchema`, `sampleRows`, `runQuery`, `explainQuery`).

  **Reconciles D4:** D4 states "6 MCP" tools; `docs/02-tools-and-api.md` had previously listed 7
  (including `resolveValues`). With D77, `resolveValues` is a runtime composite, so the MCP data
  plane is literally 6 tools and D4 is now internally consistent.

  **Amends D66:** D66 defined `resolveValues` as a live MCP resolution tool. The concept/ranking
  behaviour (D66 integrations a/b/c) and model interface are unchanged; only the implementation site
  moves from the MCP service to the agent runtime.

  Cross-references: [D4](#tools), [D5](#tools), [D10](#blueprints), [D57](#blueprint-silent-path-safety),
  [D66](#client-defined-value-resolution), [D71](#model-provider), [D75](#clickhouse-mcp--adoption-decision).

  **As-built (Session 9):** the runtime issues the backing `runQuery` via the single `ToolDispatcher`
  choke point (so D57/D5 enforcement, provenance capture, and denial mapping are reused, not
  re-implemented), intercepting `resolveValues` in the agent loop exactly like `askUser` but returning
  an inline result. `table`/`column`/`period.column` are fail-closed validated against the catalog
  allowlist (D70 exact-case) and rendered as sqlglot-AST identifiers/literals — `concept` provably never
  reaches SQL (D10), verified empirically in review. Description-column discovery is
  convention-based **and scope-aware** (out-of-scope sibling desc column → value-only fallback). `period`
  is an optional structured `{column, start, end}` (no bound-param surface on `runQuery`); deictic/
  relative period resolution (D41/D65) is deferred to the D67 era. Ranking is
  `0.7·cosine_norm + 0.3·normalized-log-freq`, top-10 of a LIMIT-200 candidate pool (provisional
  tunables in `RuntimeSettings`). Embedding-failure behaviour is **D85**. `resolve()` is exposed as a
  typed programmatic entry point for the (still-unwired) D67 rule expansion.

- **D85 (locked, 2026-07-01, Session 9 — extends D77).** **On an embedding-client failure,
  `resolveValues` degrades to frequency-only ranking rather than failing the tool call, and surfaces the
  degradation to the model.** D63 fail-closed governs *enforcement* (column scope), not *ranking
  quality* — the backing `runQuery` has already scope-checked every returned value, so every candidate is
  provably in-scope regardless of how it is ordered. Failing the whole call on an embedding outage would
  convert a quality degradation (worse ordering of already-safe values) into an availability outage of a
  core Phase-1 tool. So: an embedding transport/validation failure (unreachable endpoint, non-2xx,
  malformed/empty/non-finite vectors, or a length mismatch vs. inputs) falls back to ranking by
  normalized log-frequency alone. **The degradation is not silent:** the tool's `result_full` is a
  wrapper `{degraded: bool, ranking: "semantic+freq" | "freq_only", top_margin, values}` — so a
  freq-only top score can never masquerade as a perfect semantic match — and the tool description
  instructs the model that `freq_only` results warrant an `askUser` confirmation before filtering on a
  guessed value (D66(c)). An *unconfigured* embedding API (no endpoint set) is the same path: no client
  is wired and every call runs `freq_only` (a stub embedder is **test-only**, never a production
  substitute). Cross-references: [D63](#security--infra), [D66](#client-defined-value-resolution),
  [D71](#model-provider), [D77](#client-defined-value-resolution).

- **D86 (locked, 2026-07-01, Session 10 — retrieval pipeline Slice 1).** **The retrieval pipeline
  (D7/D8) is built behind a `VectorIndex` seam, degrades-not-fails at every external stage, and
  structurally sanitizes retrieved text before it enters the model context.** Three parts:
  **(a) `VectorIndex` seam** — the pipeline depends on a `VectorIndex` protocol
  (`recall(query_vector, kind, k)`), not on neo4j; Slice 1 ships an in-memory index (test/dev), the
  neo4j-native index (D60) arrives as a drop-in in Slice 2. This let the embed→recall→scope-filter→
  rerank→inject path ship and be Layer-2-proven against the real D71 clients before any graph-store
  work. **(b) Degrade-not-fail, observably** (extends the D85 posture from `resolveValues` to
  retrieval): `retrieve()` never raises — no/failed embedder or index → empty `RetrievedContext`
  (pre-injection simply absent; the turn proceeds Phase-0-style); no/failed reranker or a rerank
  score-count mismatch → recall order with `reranked=false`; a failing user-memory provider or
  observer → skipped. **No degrade is silent:** each path emits a shape-only `retrieval.degraded`
  span (reason attribute) + the retrieval progress event with zero counts, and logs server-side with
  traceback. The contract is enforced defensively (broad excepts at each stage), not by client
  conformance. **(c) Render trust boundary** — retrieved text (blueprint intent/slots, knowledge
  chunks, memory items) is *content-trusted* (write-time leakage gate, 03 §Scope) but
  *structure-sanitized* at render: newlines/control chars collapsed or stripped and per-item length
  caps, so corpus text can never forge sections or inject at system privilege into the single
  pre-injection message; blueprint candidates are additionally scope-pre-filtered (transitive USES ⊄
  `column_scope` → dropped; `uses=None` → dropped fail-closed; empty scope = allow-all, D80
  semantics). Slice 2 must reconfirm the trust boundary before Track-B-written content lands
  (retrieval-pipeline-design.md §8.1). Deferred there too: rendered-block token-budget accounting
  (OQ-R8). See [retrieval-pipeline-design.md](retrieval-pipeline-design.md). Cross-references:
  D7/D8 (pipeline + progressive disclosure), [D60](#blueprints--learning) (neo4j-native vectors),
  [D71](#model-provider), [D80](#security--infra) (empty-scope semantics), D85 (degrade precedent),
  D44/D46 (context assembly), D24/D25 (spans, shape-only).

- **D87 (locked, 2026-07-01, Session 11 — retrieval Slice 2).** **The neo4j corpus schema: USES as a
  denormalized property (+ reserved graph edges), per-corpus native vector indexes, and a strict-write /
  degrade-read embedding-model parity stamp.** Specifics: `:Blueprint` and `:KnowledgeChunk` nodes each
  carry a native **768-dim cosine** vector index and a uniqueness constraint on `id`. A blueprint's
  transitive USES set is stored **denormalized as a `uses: list<string>` property of byte-exact
  `"database.table.column"` scope keys** — recall reads it with zero traversal (per D60's
  "precomputed set-subset, not live traversal"), and the runtime scope pre-filter (D86) consumes it as
  a plain set-subset check, so **the stored key format must byte-match the JWT `column_scope` format**
  (the slice's highest-risk contract; loader-validated fail-closed at write — every entry must be a
  ≥3-part dotted string — and coerced fail-closed at read: a missing/empty/malformed `uses` maps to
  *undetermined* → the blueprint is dropped, never allow-all). The same write transaction also
  maintains reserved `:Column`/`:Table` nodes with `:USES`/`:OF_TABLE` edges (unread in Slice 2;
  deleted-and-rewritten per blueprint on re-seed so property and edges cannot drift) for the D60/D38
  graph capability, plus lifecycle/provenance properties (`status`, `drift_status`, `canonical_key`,
  `hit_count`, `created_by`, `catalog_sha` per D84) seeded-but-unread so Track B needs no migration.
  **Model parity**: every node is stamped `embedding_model`; the loader is **strict at write** (a
  mixed-model seed is refused inside the write transaction, atomically; an empty-string stamp counts
  as a conflicting model) and recall **degrades at read** (`WHERE node.embedding_model =
  $expected_model`; a full-corpus mismatch sets a `retrieval.model_mismatch` span attribute). The
  runtime's `embedding_model` setting is therefore the **load-bearing read-path parity key** (defaults
  to the corpus stamp `all-mpnet-base-v2`); a model swap requires a full reindex. Corpus writes are
  MERGE-by-id idempotent (duplicate fixture ids refused). See
  [neo4j-corpus-design.md](neo4j-corpus-design.md). Cross-references: [D60](#blueprints--learning),
  D84 (`catalog_sha` semantics), D86 (the `VectorIndex` seam this drops into), D38 (future graph
  consumers), D41 (blueprint model).

- **D88 (locked, 2026-07-01, Session 12 — read tools).** **The model-facing read tools
  (`searchBlueprints`, `getBlueprint`, `searchKnowledge`) ship as runtime tools behind a generalized
  registry, with a non-oracle scope posture and footprint-split provenance.** Specifics:
  **(a) Runtime-tool registry** — a `RuntimeTool` protocol + an `AgentLoop.runtime_tools` dict
  replaces per-tool interception branches (`resolveValues` migrated in, behaviour-identical;
  `askUser` remains the sole hardcoded terminal/pause branch; unknown names still fall through to
  MCP dispatch). The loop hardens the seam: a registry handler that raises is contained
  (`RUNTIME_TOOL_INTERNAL_ERROR`, no `str(exc)`), and a returned provenance of the wrong type is
  coerced to `None` fail-closed before the trail write. **(b) Non-oracle `getBlueprint`** — an
  out-of-scope blueprint is **indistinguishable from a real miss** (`ok` + `{found: false}`,
  byte-identical fields), so restricted blueprints' existence isn't leaked (06 §scope table); the
  scope check reuses the canonical transitive-USES filter, and `getBlueprint` returns only the D87
  stored projection until full-DAG storage lands with `runBlueprint`. **(c) Provenance split by
  footprint (amends the brick's original all-`frozenset()` design at review)** — `searchBlueprints`
  (thin cards) and `searchKnowledge` (entity-agnostic, write-gated) carry safe-empty `frozenset()`
  provenance (kept in D44 replay); a **FOUND `getBlueprint`** result *is* a column footprint, so its
  trail entry carries the scope-checked `uses` as provenance and **drops from replay under
  mid-session scope narrowing** — the `getTableSchema` posture. **(d)** `searchKnowledge` bypasses
  the scope filter by design (chunks are entity-agnostic and human-gated at write, D58(a)).
  **(e)** Shared error family `RETRIEVAL_TOOL_{INVALID_ARGS,UNAVAILABLE,INTERNAL_ERROR}`; unwired
  tools return a clean local `RETRIEVAL_TOOL_UNAVAILABLE` (never dispatched to the MCP); `k` over-max
  clamps, `k < 1` rejects; the model-authored `query` arg is fully redacted from spans/progress
  (D25, the `concept` precedent). Deferred to the `runBlueprint` brick: the runtime/MCP tool-name
  collision guard and duplicate-tool-call-id replay semantics (both QA-pinned). See
  [read-tools-design.md](read-tools-design.md). Cross-references: D8 (progressive disclosure),
  D41, D44 (replay filter), D58, D77 (runtime-tool precedent), D86/D87.

- **D89 (locked, 2026-07-02, Session 13 — the runBlueprint brick, Slices A/B/C).**
  **`runBlueprint(id, slot_bindings)` ships as a `RuntimeTool` that executes stored full-DAG
  blueprints fail-closed: scope-honest at load, D56-verified on return, injection-safe at every
  bind, and durable across a mid-DAG approval pause.** The honest boundary is **scalar-converging
  DAGs** — table-passing intermediates are rejected pre-dispatch (F2). Specifics:
  **(a) The scope-honesty gate (the load-bearing check, Slice A).** A blueprint's `sql_template` can
  only read **within its declared `uses`** — enforced **table-aware** at load via `qualify_columns`
  against a `uses`-derived schema PLUS `_assert_source_tables_in_uses` (any JOINed / subquery /
  table-function source absent from `uses` is rejected). Closes `SELECT *`, alias-mask, table-blind
  bare-name, `dictGet`, cross-db, and qualified-JOIN-to-unlisted-table bypasses (all reviewer/QA
  exploits verified closed; legit in-`uses` multi-table JOINs accepted). This makes the D88(c)
  stored `uses` footprint **honest** — a blueprint cannot read outside the set its scope filter is
  checked against. **(b) D56 wired live — "no unverified return" (Slice B).** Every result passes the
  D56 grain-integrity row-count probe on the **terminal** node; verify-FAIL, unmappable grain,
  grain-probe error, or a denied probe all **WITHHOLD** the rows (`ExecFailed` carries none);
  `grain_verifiable:false` **skips visibly** (`grain_checked:false`, reported honestly, never a
  silent pass). The deterministic grain check is the teeth; the LLM review is an ordinary loop
  round-trip *after* the tool returns (satisfies both D49 no-LLM-inside and D56 two-part gate).
  **(c) Injection-safe binding, no `runQuery` param surface (F1, D59a/D10).** Slot values AND
  scalar intermediates are bound as **typed sqlglot-AST literals** — a `{slot}`/query-result cell
  becomes one placeholder → one escaped literal inside one `SELECT`, provably never SQL syntax; a
  query-result cell is treated as **hostile data** on the identical D10-safe path as a slot. No
  bound-param surface is opened on `runQuery`. **(d) Scalar-only intermediates, F2 boundary.** A
  node's single-cell scalar output feeds a downstream node's slot; a scalar-consumed node returning
  `!=1` row / wrong column count / NULL fails closed (`SLOT_INVALID`) **before** the consumer runs
  (D56 only verifies the terminal, so a fanned-out intermediate must fail closed on its own).
  **Table intermediates are rejected pre-dispatch** — no scratch-write surface exists yet (F2,
  gated on `clickhouse-api` Track A). **(e) D45 mid-DAG approval pause/resume (Slice C).** An
  approval node (D59b) checkpoints completed nodes' scalar outputs + per-node provenance + SQL;
  resume re-enters at `awaiting_node`, **CAS-consumes exactly-once** (double-resume rejected),
  survives a runtime restart (state only from the checkpoint), and the **resumed provenance union +
  SQL span the whole DAG** (D44 replay stays **fail-closed across the pause**). The approval
  decision is **affirmative-only** — ambiguous/negative → deny/re-pause, **never fail-open consent**.
  Budget: **1 `tool_calls_made` per `runBlueprint`** across pause+resume, despite N inner queries.
  **(f) D67 `resolve_via` wired (Slice C).** A `concept → value-set` expansion runs via the typed
  `resolveValues.resolve()` hook (no model round-trip), bound as an **IN-list of literals** (never
  interpolated); wired for single- AND multi-node; empty → fail-closed, degraded → raw-loop
  fallback, denial passed through verbatim. **(g) D49 resolvers + askUser-clarify** — per-type slot
  resolvers stay deterministic code; a multi-match/near-miss pauses via `askUser`. **(h) Error
  family** `RUN_BLUEPRINT_*` + the shared `RUNTIME_TOOL_INTERNAL_ERROR` containment (D88a).
  **Honest deferrals (NOT built this brick):** table-intermediate DAGs (needs the F2 scratch-write
  surface); Layer-3 Playwright demo-wiring (the runBlueprint conformance scenarios are
  Layer-1/2-proven, not yet demo-green); no live `resolve_via` seed (D67 Layer-1 only); drift probes
  #2/#3 (Phase-2 per D43); the authoring-time static grain gate (D37b, Phase-2 — runtime D56 gate is
  the launch teeth). Cross-references: D33 (linear DAG, no branching), D41 (slot bind mechanism),
  D45 (pause checkpoint additive fields), D48/D49 (dedup + deterministic resolvers), D56 (verify
  gate — now BUILT), D59a (scalar-literal passing / table passing deferred), D59b (approval nodes),
  D67 (resolve_via — now BUILT), D87 (stored DAG schema), D88 (read-tools registry + non-oracle
  posture + the two owed guards, now discharged), D10 (injection boundary), D5 (scope injection).

- **D67 (locked, 2026-06-30; BUILT — Session 13, wired into `runBlueprint` per [D89](#blueprints)).**
  **Two rule kinds: static and resolved (dynamic).** A catalog `rule`'s
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

- **D53 (locked, 2026-06-30; deploy location clarified by D78, further amended by D84 — 2026-07-01).**
  **Semantic Catalog operations.** (a) **Deploy-coupled load:** catalog
  baked into the **runtime** deploy artifact ~~(not the MCP service — see D78)~~ **and, per D84, now
  also into the MCP's deploy artifact** — merged PR live on next deploy of either/both services.
  ~~⇒ **catalog SHA ≡ runtime deploy SHA**~~ **→ D84 redefines `catalog_sha` as the catalog subtree's
  own git SHA, independent of either service's deploy SHA**, once two services carry the catalog
  (fully reproducible per-artifact still holds; catalog-edit latency = deploy cadence, acceptable for
  a rarely-mutating curated store). (b) **Bot-authored PRs:** `schema_edit` candidates open a branch +
  YAML patch + PR with CI (schema lint + `explainQuery` dry-run); human merge = the D18 gate. (c)
  **Mismatch handling:** table-without-catalog-entry → structural-only schema — **per D83 this is now
  the MCP's responsibility again** (the MCP overlays nothing and returns normal introspection;
  see D78 for the interim runtime-side arrangement this reverses), raw-loop-usable but not
  blueprint-eligible; catalog-diverged-from-warehouse → caught by the **D43 conformance probe** →
  `schema_edit` PR (no new mechanism). Resolves new-unknown N5. See [D84](#semantic-catalog) for the
  full two-artifact amendment.

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
  Auto-instrument the OpenAI SDK (agent LLM only — D71) via the OpenInference OpenAI instrumentor;
  manual spans for custom runtime + learning loop. The **embedding and reranker are a custom API
  called via a plain HTTP `POST`** (not any SDK — D71), so those calls require **manual `EMBEDDING` /
  `RERANKER` spans** — auto-instrumentation does not cover them.
- **D25.** Traces grouped by `session.id`. **Never log JWT/scope token** (log a scope id/hash only);
  **mask result rows + bound slot values** via OpenInference hide flags + a custom redactor. Log
  shape (counts/columns/latency), not content. Leakage gate emits a `GUARDRAIL` span.

## Model provider
- **D71 (locked, 2026-06-30).** **Agent LLM is OpenAI; Responses API (`/v1/responses`) is primary,
  Chat Completions (`/v1/chat/completions`) is fallback.** This supersedes any prior Anthropic/Claude
  assumption. Rationale for two endpoints: the Responses API is the intended surface for agentic
  tool-use loops (native tool-call semantics, future streaming improvements); Chat Completions is the
  fallback for availability gaps or features not yet on the Responses API. **This is NOT a pluggable
  provider layer** — there is one model provider (OpenAI) and two endpoint modes; the runtime switches
  between them, not between vendors. **The OpenAI switch applies to the agent LLM only.** The
  **embedding and reranker models are NOT OpenAI** — they are a **separate custom API called via a
  simple HTTP `POST` request** (not the OpenAI SDK, not an auto-instrumented client). Because these
  calls are plain HTTP, they produce **manual `EMBEDDING` / `RERANKER` spans** — the OpenInference
  OpenAI auto-instrumentor covers only the agent LLM's Responses/Chat Completions SDK calls. The
  custom embedding/reranker endpoint + specific model ids are TBD (see [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md)
  §Retrieval). Consistent with D2 (one capable LLM per request) and D23/D24 (observability).
  **Update (Session 9b, 2026-07-01):** the request/response **contracts are now fixed** by user-provided
  mock services (`~/Development/SQL/mocks`): `POST /embed {"input_text":[…]}` → bare `[[float,…],…]`
  (mock: all-mpnet-base-v2, 768-dim); `POST /rerank {"query","documents"}` → `{"scores":[…]}` (caller
  re-sorts). `HttpEmbeddingClient`/`HttpRerankerClient` are aligned and Layer-2-validated live against
  them; production endpoint/auth + final model ids remain open. See
  [retrieval-pipeline-design.md](retrieval-pipeline-design.md) §0.

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
  **2026-07-01 Phase-0 clarification:** the replayed-trail scope filter (D44) extends to
  conversational assistant messages, tagged with their turn's tool-result provenance-union; user
  messages carry no warehouse data and are always retained; undetermined ⇒ dropped fail-closed. See
  [phase0-runtime-design.md §5.1](phase0-runtime-design.md#51-message-provenance-filter-2026-07-01-d44-clarification).
  **2026-07-01 turn-scoped continuity fix (STATUS-GATED):** D44's strict provenance drop applies to
  REPLAY of PRIOR turns, PLUS any current-turn entry that actually SUCCEEDED (`status == "ok"`) — only
  a current-turn **denied/errored** entry is exempt, because (and only because) it carries no result
  rows (`ToolResult.result_preview`/`result_full` are `None` for any non-`"ok"` status), so the model
  can see its own current-turn tool errors and self-correct (§3.4) without any leak. A successful
  current-turn `sampleRows`/`getTableSchema`/`runQuery` result is never exempt — its provenance is
  checked against `column_scope` exactly as any prior-turn entry's is, because those tools' result rows
  ARE data-bearing and (for `sampleRows`/`getTableSchema`) are not themselves column-scoped by the MCP
  (D80(b) only column-scopes `runQuery`). Cross-turn D44 is unchanged. See
  [phase0-runtime-design.md §5.2](phase0-runtime-design.md#52-turn-scoped-continuity-refinement-2026-07-01-correctness-fix).
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
- **D42 (locked, 2026-06-30; overlay location amended by D78, D78 reversed by D83).** The **Semantic Catalog** is a first-class component, stored as
  **git-backed YAML** (one file per table + `rules.yaml`). It is the curated semantic layer
  (entities, grain, per-measure `{agg, defined_over}`, `temporal`, rule definitions, ambiguities,
  synonyms, enums). The structural half of `getTableSchema` is live ClickHouse introspection;
  **D78 clarified (2026-06-30) that the `introspection ⨝ catalog overlay` join happens in the agent
  runtime, not the MCP** — **D83 (2026-07-01) reverses this**: the join is back in the ClickHouse MCP.
  Consequences (as amended by D83/D84): (a) **catalog version = git
  commit SHA**, stamped into `USES`-edge validation + golden replays (the catalog now ships with
  **both** the runtime and MCP deploys — see D84 for the two-artifact SHA reconciliation); (b) the learning-loop
  `schema_edit` review inbox emits a **PR**, and human merge **is** the D18 never-auto-commit gate
  (one mechanism for write path + review queue); (c) a catalog SHA bump triggers the schema-drift
  maintenance job (re-validate `USES` edges + grain gate + golden replay, demote breakage), and per
  D38 a `rules.yaml` change coarsely re-flags all citing blueprints; (d) **rule definitions live in
  `rules.yaml`, enforced two ways** — deterministic AST injection in blueprints vs. model-written SQL
  in the raw loop (governance note in [06-security-and-governance.md](../06-security-and-governance.md)),
  and rules are a correctness convention, never an access boundary. Resolves review item A
  (catalog had no storage home). See [D83](#semantic-catalog) for the current overlay-location decision
  (supersedes the D78 pointer previously here).

- **D78 (locked, 2026-06-30 — REVERSED by D83, 2026-07-01; kept verbatim below for history).** ~~The `getTableSchema` semantic-catalog overlay moves to the agent runtime.~~ The ClickHouse MCP's `getTableSchema` returns **live introspection only** (columns/types/engine/comments). The **runtime** performs the `introspection ⨝ catalog YAML overlay` join — merging in grain, per-measure `{agg, defined_over}`, `temporal`, rule definitions, ambiguities, synonyms, enum values, and `sensitive`/`client_defined` flags — before handing the enriched schema to the model. **Amends D42** (overlay location moves; catalog content/format unchanged). **Clarifies D53**: the catalog deploys with the **runtime**, not the MCP service; D53's "catalog SHA ≡ deploy SHA" idea holds but SHA ≡ runtime deploy SHA. Graceful-degradation path (table without catalog entry → structural-only) now happens runtime-side: the MCP always returns whatever introspection it can; the runtime overlays nothing if no catalog entry exists.

  **Rationale (terse, as originally argued — see D83 for why this is now reversed):**
  - Keeps the ClickHouse MCP a pure, thin data plane — introspection is a warehouse fact; the curated semantic layer is a runtime/knowledge concern.
  - The catalog is git-backed YAML that already versions/deploys with the runtime; the runtime is the natural owner of the overlay + `catalog_sha` stamping.
  - Consistent with D77 (enrichment/composition happens runtime-side) and the two-plane split (D1).
  - Removes the last non-enforcement piece from the clickhouse-api extension scope — after D77 + D78, clickhouse-api's remaining delta is enforcement-only: D57 column-scope + D63 fail-closed + D64 scratch + D5 scope injection.

  Cross-references: [D1](#architecture), [D42](#semantic-catalog), [D53](#blueprint-dedup--resolvers),
  [D57](#blueprint-silent-path-safety), [D75](#clickhouse-mcp--adoption-decision), [D77](#client-defined-value-resolution),
  [D83](#semantic-catalog).

- **D83 (locked, 2026-07-01 — REVERSES D78; amends D42).** **The `getTableSchema` semantic-catalog
  overlay moves back into the ClickHouse MCP (`clickhouse-api`), restoring the D42/D75 arrangement
  D78 had undone.** The MCP's `getTableSchema` now performs **introspection ⨝ catalog YAML overlay**
  itself, merging in grain, per-measure `{agg, defined_over}`, `temporal`, rule definitions,
  ambiguities, synonyms, enum values, and `sensitive`/`client_defined` flags — **then scope-filters**
  the merged result against the caller's `column_scope` (D5/D80) before returning it. The agent
  runtime no longer performs this join; it receives an already-overlaid, already-scope-filtered
  schema. See [mcp-overlay-design.md](mcp-overlay-design.md) for the full design (merge/filter
  ordering, response shape, uncatalogued-table path, richer catalog loader).

  **What changes vs. D78:**
  - `getTableSchema` (MCP) response shape reverts to `introspection ⨝ catalog`, now **additionally
    scope-filtered** — a capability neither the original D42 arrangement nor D78 had, since D78's
    scope-filtering gap (`getTableSchema`/`sampleRows` were never column-scope-checked — only
    `runQuery` was, per D57) is closed as part of this reversal.
  - `sampleRows` (MCP) gains column-scope enforcement for the first time (reject-if-`table's
    columns ⊄ scope`, consistent with `runQuery`'s D57 treatment) — see [mcp-overlay-design.md](mcp-overlay-design.md)
    §2. This was an open follow-up flagged in [WORKLOG.md](../WORKLOG.md) ("clickhouse-api MCP does
    NOT column-scope `sampleRows`/`getTableSchema`") independent of the overlay question; this
    reversal is the natural place to close it since both tools are touched anyway.
  - The MCP needs a **richer catalog loader** than its current interim `system.columns`-derived
    `{col: type}` catalog (`app/catalog.py` in `clickhouse-api`) — it must load the **full** YAML
    semantic fields, not just types. See [mcp-overlay-design.md](mcp-overlay-design.md) §3.
  - The runtime's D44 replayed-trail provenance filter and D50 context-assembly ordering are
    **unaffected** — they operate on `runQuery`/`sampleRows` result provenance, not on the
    `getTableSchema` response shape, and stay in place as defense-in-depth (see Open Question 4 in
    [mcp-overlay-design.md](mcp-overlay-design.md)).

  **Why reversed (vs. D78's stated rationale):**
  - D78's rationale rested on "MCP stays a thin data plane; overlay is a runtime/knowledge concern."
    In practice the MCP **already** must load a catalog-shaped schema to do D57 column-scope
    enforcement (`get_catalog_schema()` in `clickhouse-api/app/catalog.py`, feeding
    `extract_column_provenance`) — so the MCP is not, in fact, catalog-free today. Extending that
    existing catalog dependency to carry the full semantic YAML (not just `{col: type}`) is a smaller
    delta than maintaining **two** divergent catalog loaders (the runtime's richer one for the
    overlay, the MCP's thin one for scope) that must stay in lockstep.
  - Scope-filtering `getTableSchema`/`sampleRows` **requires** column-level knowledge at the MCP
    regardless of where the overlay lives (D57's parser + catalog schema already live there) — since
    the MCP must scope-filter *some* form of the schema response anyway, doing the overlay there too
    avoids a "merge in the runtime, then filter again in the runtime using MCP-supplied scope" split
    that duplicates the scope-check logic in two places.
  - Consolidates the **security-relevant** part of `getTableSchema` (which columns/semantics a given
    caller may see) at the single enforcement boundary (D57's MCP boundary), rather than trusting the
    runtime to apply scope-filtering correctly on a runtime-side overlay — matches the existing
    posture that the MCP, not the runtime, is the hard boundary for column-level access (D57).
  - Accepted trade-off (see [mcp-overlay-design.md](mcp-overlay-design.md) Open Question 1): the
    catalog must now be deployed to/readable by **two** services (MCP and runtime — the runtime still
    needs it for D44/D57 provenance extraction and D14 blueprint scope pre-filtering), reintroducing a
    two-artifact sync/version-skew question that D78 had collapsed to one. D84 addresses this.

  Cross-references: [D1](#architecture), [D42](#semantic-catalog), [D53](#blueprint-dedup--resolvers),
  [D57](#blueprint-silent-path-safety), [D75](#clickhouse-mcp--adoption-decision),
  [D77](#client-defined-value-resolution), [D78](#semantic-catalog), [D80](#clickhouse-mcp--adoption-decision),
  [D84](#semantic-catalog).

- **D84 (locked, 2026-07-01 — amends D53).** **The Semantic Catalog now deploys to (and is readable
  by) *two* services — the ClickHouse MCP and the agent runtime — not the runtime alone; `catalog_sha`
  is redefined as the catalog's own git-subtree version, independent of either service's deploy SHA.**

  - **Why two services need it now:** D83 moves the `getTableSchema` overlay to the MCP, so the MCP
    needs the full semantic YAML. The **runtime still needs it too** — for D44 replayed-trail
    provenance filtering, D14 blueprint `USES`-scope pre-filtering, D37/D40/D65 grain declarations
    consumed by the (future) blueprint grain gate, and D67 rule definitions. Neither service can drop
    it.
  - **`catalog_sha` redefinition.** D53 defined `catalog_sha ≡ runtime deploy SHA` (single artifact,
    single deploy). With two artifacts, "the runtime deploy SHA" is no longer a well-defined stand-in
    for "the catalog version a blueprint validated against." **`catalog_sha` is now the git commit SHA
    of the last commit touching `databaseSchemaDocs/**`** (a subtree SHA, computable independent of
    which service(s) have redeployed) — **not** either service's overall application deploy SHA. Both
    the MCP and the runtime **stamp and expose their currently-loaded `catalog_sha`** (e.g. a
    `/health` or `/version` field) so a **mismatch between the two is directly observable** — this is
    the version-skew detector D78's single-artifact model didn't need and D83 reintroduces.
  - **Delivery mechanism to the MCP** is an **open question, not locked here** — see
    [mcp-overlay-design.md](mcp-overlay-design.md) Open Question 1 for the copy-in vs. shared-package
    vs. canonical-relocation fork and the recommendation.
  - **Drift/skew handling:** a `catalog_sha` mismatch between the live MCP and the runtime's recorded
    blueprint `catalog_sha` is folded into the **D43 catalog-vs-warehouse conformance probe** as an
    additional signal (not a new mechanism) — it indicates the schema a blueprint was authored/
    validated against may differ from what `getTableSchema` currently returns to the model, which is a
    drift risk of the same shape D43 already probes for.
  - **Mismatch/uncatalogued-table handling (restates D53(c), now MCP-side again):** table-in-ClickHouse
    with no catalog entry → the MCP returns **structural-only** introspection (no
    grain/rules/ambiguities/semantics) — raw-loop-usable, not blueprint-eligible; catalog entry
    diverged from live warehouse → still caught by the D43 conformance probe → `schema_edit` PR. No new
    mechanism beyond relocating the "who returns structural-only" responsibility from the runtime
    (D78) back to the MCP.

  Cross-references: [D42](#semantic-catalog), [D43](#blueprint-silent-path-safety),
  [D44](#security--infra), [D53](#blueprint-dedup--resolvers), [D57](#blueprint-silent-path-safety),
  [D75](#clickhouse-mcp--adoption-decision), [D78](#semantic-catalog), [D83](#semantic-catalog).

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

- **D56 (locked, 2026-06-30; BUILT — Session 13, the runtime grain-integrity gate wired live in
  `runBlueprint` per [D89](#blueprints); authoring-time D37b gate stays Phase-2).** **No silent path
  — the agent verifies every blueprint response before the user sees it; the user is never asked to
  verify.** Supersedes D12's "execute-then-verify
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

## Extensibility
- **D72 (locked, 2026-06-30).** **Lifecycle hook points are named, typed, and have fixed contracts;
  they are NOT arbitrary intercept points.** Seventeen named hook points span the request lifecycle
  (H1–H14) and the offline learning loop (H15–H17). Each hook declares: what it reads (a redacted
  shape/count view — never raw cell values, never the JWT/scope/session_id per D5), what it may
  mutate (only: context fragment injection at H3, thin-card reordering at H4, veto at H1/H7/H9,
  fallback at H9, structured non-PII annotation at H13), and whether it can veto/abort. Hooks at
  H15–H17 (learning loop) are **read-only** — no mutation — to prevent a hook from corrupting the
  D1 two-plane split. Hook failures (unhandled exceptions) are logged as `GUARDRAIL` spans (D23)
  and treated as `continue`; a buggy hook never drops a live turn. H1 failures abort the turn
  (safe: the turn has not started). See [12-extensibility.md](../12-extensibility.md) §1 for the
  full hook-point table and contracts.

- **D73 (locked, 2026-06-30).** **Skills are first-party, manifest-registered capabilities invoked
  by the runtime — the model never participates in skill selection.** A skill is a distinct extension
  axis from tools (D4, fixed 11) and blueprints (D9–D13, learned query DAGs): tools are the model's
  action vocabulary; blueprints are learned fast-path query patterns; a skill is a pre-approved
  runtime capability (formatting, cross-system lookup, specialized analysis, document generation)
  that may orchestrate tool calls while staying within the D5 injection boundary. Skills are
  registered at startup via a YAML manifest (`name`, `version`, `description`, `when_to_use`,
  `capability` ∈ a closed enum, `triggers.intent_patterns`, `triggers.blueprint_ids`, `hook_points`,
  `entry_point`, `max_latency_ms`). The runtime selects skills by matching turn intent against
  `intent_patterns` (or `blueprint_ids` on fast-path completion) at the registered hook point;
  selection is deterministic, runtime-only. **Security boundary:** a skill receives `session_id_hash`
  and `scope_id` (hashes, not the raw token — D5), never the JWT; any tool call a skill dispatches
  has credentials injected by the runtime at dispatch (same as model-initiated calls). **Budget
  boundary:** skill wall-clock and tool-call iterations count against the turn's D47 budget; a skill
  exceeding `max_latency_ms` is terminated and logged as a `GUARDRAIL` span. Launch trust model:
  first-party only, in-process, no sandboxing (see OPEN-QUESTIONS). See
  [12-extensibility.md](../12-extensibility.md) §2–3.

- **D74 (locked, 2026-06-30).** **Hooks and skills reuse existing span kinds; no new span kind is
  introduced.** Hook execution → `CHAIN`; veto/reject/fallback → `GUARDRAIL`; unhandled exception →
  `GUARDRAIL`. A skill-triggered tool call → `TOOL` (child of the skill's `CHAIN` span). PII posture
  is identical to D25: no cell values, no bound slot values, no JWT, no raw scope token in any
  span attribute; `slot_names` (not values) are permitted. See [12-extensibility.md](../12-extensibility.md)
  §4.

## ClickHouse MCP — adoption decision
- **D75 (locked, 2026-06-30).** **Adopt the existing `clickhouse-api` service as the ClickHouse MCP
  data plane; do not build a new MCP from scratch. Extend it with the missing enforcement pieces.**

  The **`clickhouse-api` repo** is a FastAPI + MCP service with JWT/OIDC auth (tenant/row-level
  isolation via ClickHouse row policies driven by a JWT claim).

  **Already provided by `clickhouse-api` (leaves our build scope — verified by code inspection):**
  - 6 of the 7 data-plane tools: `listDatabases`, `listTables`, `getTableSchema`, `sampleRows`,
    `runQuery`, `explainQuery`.
  - Read-only enforcement: SQL allowlist (SELECT/WITH/EXPLAIN/SHOW/DESCRIBE) + denylist (blocks
    INSERT/UPDATE/DELETE/DDL/SET + external table functions url/s3/file/remote), comment-stripping,
    string-literal masking, auto-LIMIT injection, and ClickHouse session settings `readonly=1` +
    `max_execution_time` + `max_result_rows` + `max_rows_to_read`.
  - JWT/OIDC auth (`app/auth_jwt.py`, `app/principal.py`) with per-tenant ClickHouse-settings injection.
    Note: this is **tenant/row-level** isolation, distinct from the spec's **column-level** scope.

  **Still missing — must be ADDED to `clickhouse-api` by extension (verified absent):**
  - **D57 column-scope enforcement** — no per-request column scope, no SQL column extraction, no
    reject-if-columns-⊄-scope. Any authenticated user can currently read any column.
  - **D62 `sqlglot` parser** — not a dependency; not used anywhere in the service.
  - **D63 fail-closed on parse failure.**
  - **D64 scratch `scratch.s_<session_id>_*` isolation** — no `session_id` concept at the MCP.
  - **D5 column-scope injection** — the JWT Principal carries claims but no computed `column_scope`;
    `runQuery` has no `scope` or `session_id` parameter today.
  - ~~**D66 `resolveValues`** tool — completely absent (the 7th data-plane tool).~~ **Removed from
    this list by D77:** `resolveValues` is now a runtime composite over `runQuery`, not an MCP
    addition. The MCP data plane stays at 6 tools.
  - ~~**D42 `getTableSchema` semantic-catalog overlay** — Removed from this list by D78 (the join moved to the agent runtime).~~
    **Restored to this list by D83 (2026-07-01):** the `introspection ⨝ catalog YAML overlay` join
    (D42) **plus scope-filtering the merged result** (D5/D80) moves back into `clickhouse-api`'s
    `getTableSchema`. This is once again an MCP extension item — see [D83](#semantic-catalog) and
    [mcp-overlay-design.md](mcp-overlay-design.md). Requires the MCP to load the **full** semantic
    YAML (not just `{col: type}` — see [D84](#semantic-catalog) and the richer-loader design), and to
    add column-scope enforcement to `sampleRows` as well (D83), which had never been scope-checked.
  - The **Phase-0 column-provenance extractor** (built in this repo under D52/D62/D68/D69/D70) is
    the component that powers column extraction (D57) and scratch-table name extraction (D64). Under D75 it must be **delivered into
    `clickhouse-api`** (the enforcement site). ~~How it is packaged — copied in, published as a shared
    library, or imported — is an open question~~ — **Resolved by D79(a): copy-in** (spec repo
    authoritative; mirror same-sprint; promote to a shared library once the D69/D70 contract stabilizes).
    D83/D84 raise the **same delivery-mechanism question again for the catalog itself** — see
    [mcp-overlay-design.md](mcp-overlay-design.md) Open Question 1.

  **Enforcement boundary:** remains **at the MCP** (consistent with D57 — "enforced at the MCP by
  parsing the SQL"), not in the agent runtime.

  Cross-references: [D1](#architecture), [D4](#tools), [D5](#tools), [D42](#semantic-catalog),
  [D52](#blueprint-dedup--resolvers), [D57](#blueprint-silent-path-safety),
  [D62](#blueprint-silent-path-safety), [D63](#blueprint-silent-path-safety),
  [D64](#security--infra), [D66](#client-defined-value-resolution), [D68](#delivery--sequencing),
  [D69](#testing), [D70](#testing), [D77](#client-defined-value-resolution), [D83](#semantic-catalog),
  [D84](#semantic-catalog),
  [D78](#semantic-catalog).

- **D79 (locked, 2026-06-30).** **clickhouse-api extension mechanics: extractor delivery + scope transport.**
  Resolves the two D75 sub-questions (previously in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) §Security/infra).
  - **(a) Extractor delivery = copy-in.** The Phase-0 provenance extractor is copied into
    `clickhouse-api` (`app/sqlparse/`) rather than shared via package or submodule. The **spec repo
    (`data-analysis-agent`) is authoritative**; any change to the extractor or its D69/D70 contract is
    mirrored into `clickhouse-api` in the **same sprint** (optionally enforced by a CI file-diff check).
    Rationale: the D69/D70 contract is new and will churn; copy-in avoids release ceremony while it
    stabilizes. **Promote to a shared internal library once the contract holds for one release** — a
    cheap one-way door deferred until it pays for itself.
  - **(b) Scope transport = signed JWT claim.** `column_scope` (a set of `database.table.column`
    triples, D69/OQ-3) travels as a **claim in the runtime-signed JWT** that
    `clickhouse-api` already validates — read alongside the existing tenant claim; threaded through the
    existing `Principal`/ContextVar into `runQuery`. Signed by the issuer ⇒ unforgeable; the model never
    holds the token (D5). This is the **tenant-level → column-level** shift flagged in D75.
    **Caveat (not a blocker):** a very large scope can exceed HTTP header size — if it bites, send a
    signed **scope *reference*** (a short id resolved server-side), not the inline list. **Fallback**
    (only if a runtime cannot mint custom claims — not our case): a short-lived HMAC-signed `X-Scope`
    header. Enforcement stays at the MCP boundary (D57); stdio transport skips enforcement (local-trust).
    **→ Amended by D81:** `session_id` is NOT a JWT claim — it moved to an unsigned `X-Session-Id`
    request header; only `column_scope` rides the JWT.
  Cross-references: [D5](#tools), [D52](#blueprint-dedup--resolvers), [D57](#blueprint-silent-path-safety),
  [D62](#blueprint-silent-path-safety), [D63](#blueprint-silent-path-safety), [D64](#security--infra),
  [D69](#testing), [D70](#testing), [D75](#clickhouse-mcp--adoption-decision), [D81](#clickhouse-mcp--adoption-decision).

- **D80 (locked, 2026-06-30).** **Enforcement is MCP-transport-only; empty `column_scope` = allow-all.**
  Clarifies D57/D79 after the first implementation + security review.
  - **(a) MCP-only enforcement.** Column-scope + scratch checks fire on the **MCP HTTP transport**
    (the model-facing path). The **REST API is a trusted admin/service surface and is intentionally
    unscoped** — `require_principal` sets the principal but not the scope ContextVar, so REST
    `runQuery` runs unenforced. This is consistent with D57 ("enforced at the MCP") — REST was never
    a model-facing path. Guard against accidental model exposure of REST separately (do not route the
    agent through REST).
  - **(b) `column_scope: []` = allow-all (unrestricted).** Three states: `current_scope is None`
    (stdio/local-trust) → enforcement fully skipped; **empty** frozenset (`[]` claim) → **all warehouse
    columns allowed**; **non-empty** → allowlist enforced (reject any referenced column ⊄ scope). Scope
    thus **narrows** from a default of full access; a token restricts only by listing columns. **The
    parser still runs whenever scope is not None**, so **D63 fail-closed and D64 scratch isolation
    remain in force even for an allow-all (`[]`) token** — cross-session scratch access and unparseable
    SQL are always rejected regardless of column scope.
  Rationale: matches the token issuer's original intent (`[]` = no restriction); keeps the hard
  cross-session + parse boundaries independent of column scope; the agent's real tokens still carry
  explicit column scopes. Trade-off accepted: default/unscoped tokens see all columns — acceptable
  because issuing a scoped token is the runtime's job and the MCP path is the enforced one.
  Cross-references: [D5](#tools), [D57](#blueprint-silent-path-safety), [D63](#blueprint-silent-path-safety),
  [D64](#security--infra), [D69](#testing), [D75](#clickhouse-mcp--adoption-decision), [D79](#clickhouse-mcp--adoption-decision).

- **D81 (locked, 2026-06-30).** **`session_id` is injected per-request, NOT carried in the JWT (amends D79b).**
  `session_id` is **generated by the UI** ([01-architecture.md](../01-architecture.md) — the UI owns
  `session_id`) and flows **UI → agent backend → each MCP call**, injected by trusted server code as a
  **request header** (`X-Session-Id`). It is **not** a JWT claim and the agent LLM never sees it (D5).
  **`column_scope` stays a JWT claim** (D79b): scope is an **authorization** property, so end-to-end
  signing makes it unforgeable; `session_id` is a **per-conversation routing/namespace selector** that
  changes each session, so injecting it per-request is correct and avoids re-minting a JWT per session
  (one identity token can serve many sessions). The MCP reads `X-Session-Id` into `current_session_id`
  for scratch isolation (D64). Not model-forgeable — the model never controls the MCP transport;
  the backend sets the header. **Accepted risk / hardening follow-up:** unlike the JWT-signed
  `column_scope`, `X-Session-Id` is **unsigned**, so a JWT-authenticated caller who bypasses the agent
  backend (or a compromised backend) could set an arbitrary `session_id` and reach another session's
  scratch namespace — scratch isolation is **application-layer-only** (the `s_<session_id>_*` prefix
  check), with no ClickHouse row policy behind it. Acceptable for now (trusted backend; scratch is
  ephemeral per-session upload; the feature is not yet built), but if scratch ever holds
  cross-user-sensitive data, **bind `session_id` to the JWT subject** (e.g. an HMAC of
  `(session_id, sub)` in the required prefix) so a valid prefix can't be forged for another user's
  session. Tracked in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) §Security/infra. Cross-references:
  [D5](#tools), [D64](#security--infra), [D79](#clickhouse-mcp--adoption-decision),
  [D80](#clickhouse-mcp--adoption-decision).

- **D82 (locked, 2026-06-30).** **On login, the UI acquires its JWT via a server-side mint; the token defaults to all-column access (interim).**
  - **Login → mint:** on successful login, the **UI's backend** calls `token_service`'s guarded `POST /token` (or an external IdP in production) to obtain a **signed JWT** on behalf of the session. The browser **receives** the JWT; it does **not** hold a signing/private key and cannot mint tokens itself.
  - **`session_id` generated by the UI (D81):** the UI generates a fresh `session_id` at session start and sends it as `X-Session-Id` per D81. The JWT and `session_id` are separate credentials; one identity token can serve many sessions.
  - **Interim default — `column_scope: []` = all-column access (D80):** for now, every token is minted with an **empty `column_scope`**, which D80 defines as allow-all (no column restriction). Per-user column entitlements are **not yet wired**, so every logged-in user currently gets unrestricted column access. Row-level tenant isolation (`SQL_tenant`/`user_name`) is enforced separately at the warehouse and is unaffected by this default.
  - **Per-request forwarding:** on every turn, the backend forwards the JWT as a **Bearer token** + `X-Session-Id` header to the MCP (D81). The MCP validates the JWT and extracts `column_scope` (D79b); scope enforcement fires per D57/D80 (the parser still runs; D63/D64 remain in force even for an allow-all token). The model/agent LLM never sees the JWT, scope, or `session_id` (D5).
  - **Deferred — per-user entitlements:** when per-user column entitlements are wired, the mint step will populate a non-empty `column_scope` at mint time; where those entitlements are looked up (IdP claims vs. a backend entitlement service / token-exchange) is the open question tracked in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) §Security/infra.
  - **Future direction — managed IdP:** `token_service` is an interim issuer. The production direction is to replace it with a managed identity provider (Microsoft Entra / Keycloak / Okta). Because the MCP already validates standard OIDC JWTs via JWKS (D79 — checks `iss`/`aud`, fetches keys from `OIDC_JWKS_URL`), this swap is an **issuer/config change** (point `OIDC_JWKS_URL` / `OIDC_ISSUER` / `OIDC_AUDIENCE` at Entra; the IdP mints the JWT with `column_scope` + `user_name`/tenant claims), **not an MCP code change**. Prerequisite: the chosen IdP must stamp the `column_scope` claim — which ties into the per-user-entitlement open question above.

  Cross-references: [D5](#tools), [D57](#blueprint-silent-path-safety), [D63](#blueprint-silent-path-safety), [D64](#security--infra), [D79](#clickhouse-mcp--adoption-decision), [D80](#clickhouse-mcp--adoption-decision), [D81](#clickhouse-mcp--adoption-decision).

## Delivery / sequencing
- **D68 (locked, 2026-06-30).** **Three-phase delivery with a red-burndown conformance harness from day one.**

  **Amended 2026-07-01 (post D71/D75/D77/D78/D79–D82):** phase *structure* unchanged; the Phase-0/Phase-1
  scope lines below now name the OpenAI model provider (D71), the delivered `clickhouse-api` MCP (D75),
  the auth/token path (D79/D81/D82), and the custom embedding/reranker API (D71). `resolveValues` (D77)
  and the `getTableSchema` catalog overlay (D78) stay **Phase 1**. The **skills/lifecycle-hooks mechanism
  (D72–D74) is deliberately out of all phases** — it ships dormant and is wired only on explicit go-ahead.

  **Further amended 2026-07-01 (post D83/D84):** the `getTableSchema` catalog overlay stays a **Phase
  1** deliverable, but its delivery site flips from the agent runtime to `clickhouse-api` (the MCP) —
  see D83. This does not change phase *sequencing*: Phase 0 still ships `getTableSchema` as
  introspection-only (the MCP simply hasn't been extended with the overlay+scope-filter yet), and the
  overlay work lands in Phase 1 as a `clickhouse-api` extension task rather than a runtime task. The
  richer catalog loader (D84) is now needed by **both** the runtime and the MCP in Phase 1, not the
  runtime alone.

  ### Phase definitions and exit criteria

  **Phase 0 — Walking skeleton (raw-loop alpha).**
  Scope: `sqlglot` parser (D52/D62) → the **delivered `clickhouse-api` MCP data plane** (adopt+extend, D75; D1/D57/D63/D64) → Couchbase session store (D22/D44/D45) → raw agent loop driven by the **OpenAI model** (Responses API primary / Chat Completions fallback, D71) with **minimal, non-retrieval context assembly** (schema + conversation; the embed→recall→rerank pipeline is Phase 1). **Auth path:** the UI mints its JWT server-side on login (D82), the backend forwards it as a Bearer token + the `X-Session-Id` header (D81/D79), the MCP validates and injects scope (D5). `getTableSchema` is **introspection-only** in Phase 0 (the D83 catalog overlay + scope-filter is Phase 1, now a `clickhouse-api` task — see amendment above). Plus minimal UI with progress streaming (D61) + Phoenix tracing (D23/D24/D25). Scope-enforced, transparent, **raw-loop-only** SQL agent. No blueprints, no knowledge plane reads, no learning loop.
  - **First brick:** the `sqlglot` parser is built and TDD'd first — it is the shared dependency of five consumers (D57 live scope, D44 provenance, D48 dedup, D35 template rewrite, D64 scratch isolation) and is pure logic with no infrastructure prerequisite. Nothing else starts until its unit test suite (Layer 1) is green.
  - **Exit criteria:** all Phase-0 conformance scenarios green (scope denial, mid-session scope narrowing, parser fail-closed, observability + PII, pause/resume durability, budget-cap pause); Layer 1 + Layer 2 MCP + session-store tests passing in CI; Phoenix traces reachable and PII-clean on a real turn.

  **Phase 1 — Blueprints + D56 verify gate (the launchable product).**
  Scope: knowledge plane reads (`searchBlueprints`, `getBlueprint`, `runBlueprint`, `searchKnowledge`), the full D56 verification gate, composite blueprints (D59), **retrieval pipeline (D7/D8) on the custom (non-OpenAI) embedding + reranker API (D71)**, grain declaration (D37a/D40/D65), `resolveValues` (D66/D67 — runtime composite, D77), D53/D84 catalog CI + the **`clickhouse-api`-side** `getTableSchema` catalog overlay + scope-filter (D83; supersedes the runtime-side D78 plan), `sampleRows` column-scope enforcement (D83), review inbox UI, and all remaining Layer 3 conformance scenarios. Runs alongside **Track B** (see below).
  - **Track B (parallel) — learning-loop write router:** D26–D31 write router stages, D58 leakage gate (human pre-gate for `global_knowledge`; sampled detection for blueprints), D48 hard-dedup with single-writer-per-key, D51 provenance/audit store, `LEARNING_ENABLED=false` kill-switch (D58c), review-inbox ingestion, D53 `schema_edit` PR bot. Track B is fully offline/decoupled from the request path and has no Phase-0 prerequisites beyond the session store and Redis.
  - **Exit criteria (gates first release):** ALL Layer 3 spec-conformance scenarios green (the full ~13-scenario suite from [11-testing.md](../11-testing.md) §Layer 3) + Layers 1–2 green for every shipped module. This is the full-conformance gate — there is no "ship request path, learn later" escape hatch (see tension note below).

  **Phase 2 — Drift defense fast-follow (committed, not open backlog).**
  Scope: D43 three-probe drift attestation (result grain-integrity scheduler, catalog-vs-warehouse conformance probe, rule-semantics currency check) + `silent_eligible` freshness predicate + D37b static authoring grain gate. Supersedes D12's unconditional silent path. Adds authoring-time and periodic defense on top of the Phase 1 runtime gate (D56) — defense in depth, not a silent-path toggle.
  - **Exit criteria:** drift probe tests green in CI; `drift_status` correctly stamps and demotes blueprints; static authoring gate rejects wrong-grain blueprints before storage.

  ### Full-conformance gate commitment
  The full Layer 3 spec-conformance suite (all ~13 scenarios in [11-testing.md](../11-testing.md) §Layer 3) **must be green to gate the first release** — not just a security subset. Rationale: three of the 13 scenarios directly exercise the learning loop (`correction→learning`, `knowledge human-gate`, `LEARNING_ENABLED=false`), so "ship request path, learn later" would leave those scenarios permanently red and the release blocked. The learning loop is not Phase-2 cleanup — it is a Phase-1 parallel track with its own exit criterion.

  ### Parallel learning-loop commitment
  Track B (the offline learning-loop write router) runs concurrently with Phase 1's request-path work. It is fully decoupled from the live request path (D1: data plane vs. knowledge plane; D26: two processes) and can be built, tested with real stores (Layer 2: learning loop vs. neo4j/Redis), and wired without blocking or blocking the Phase-1 request-path work. The shared enabling components (Couchbase session store, Redis Streams D30, neo4j) are all Phase-0 or early Phase-1 deliverables that Track B can consume as they land.

  ### Accepted tension + red-burndown mitigation
  Tension: "full conformance gates release" + "learning loop in parallel" means Track B must be substantially complete before the first release, even though it is logically a background/offline concern. There is no ship-first-learn-later escape.
  Mitigation: stand up the full Layer 3 conformance harness (Docker stack + Playwright MCP + all ~13 scenarios) in **Phase 0 as a red burndown** — every scenario is written as a failing test from day one; each feature delivery flips its scenario green; the burndown chart is the shared definition of launch-ready and the alignment artifact for the whole team. This makes the tension visible and tracked rather than discovered late.

  ### sqlglot as Phase-0 first brick
  `sqlglot` (D62) is chosen over any other parser start point because it is: (a) pure Python logic with zero infrastructure dependency; (b) the shared substrate for five hard-invariant consumers (D57 scope enforcement, D44 provenance/replay, D48 canonical dedup key, D35 template rewrite, D64 scratch isolation); (c) TDD-able immediately with adversarial inputs; and (d) a parse failure in any of those consumers produces a security or correctness consequence (fail-closed / fail-soft / fail-to-review per D52), so getting the fail-behavior tests green first means every later consumer inherits a tested safety net.

  Cross-references: D1, D5, D7, D8, D13, D22, D23, D24, D25, D26–D31, D35, D37, D40, D42, D43, D44, D45, D46, D47, D48, D51, D52, D53, D56, D57, D58, D59, D61, D62, D63, D64, D65, D66, D67, D75, D76, D77, D78.

## Testing
- **D76 (locked, 2026-06-30).** **Four-layer test strategy with an automated spec-conformance suite.**
  <!-- Renumbered from a duplicate D64 (collided with the scratch-isolation D64); D64 now refers only to scratch isolation. -->

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

- **D69 (locked, 2026-06-30).** **Column-provenance extraction contract — five edge-case rules
  (resolves OQ-1..OQ-5 from test-plans/column-provenance-extraction.md).**

  **(OQ-1) Lambda / higher-order bodies.** Columns referenced inside lambda bodies (e.g.
  `arrayMap(x -> x * Amount, ...)`) ARE part of the USES set. If `sqlglot` (D62) cannot prove it
  walked the lambda body — e.g. `Amount` is absent from the extracted set even though the lambda
  body references it — the extractor MUST raise `ProvenanceExtractionError`. No silent skip is
  permitted. Rationale: identical to D63's general fail-closed rule — a silently skipped lambda
  body is an under-extraction, and under-extraction is a security gap. The implementer must verify
  sqlglot's lambda-walk behavior empirically; if it silently drops body columns, pre-normalize or
  treat any lambda-bearing query as a parse failure until coverage is confirmed.

  **(OQ-2) `SELECT *` expansion.** `SELECT *` is expanded to ALL columns of each referenced table
  via the catalog schema. The query passes only if every expanded column ∈ scope; if any expanded
  column is out of scope the query is rejected. If a referenced table is not in the catalog schema
  (columns cannot be enumerated) → fail-closed / reject. Rationale: scope is column-level (D57);
  the engine does NOT strip columns (role-pushdown option 2b was rejected), so any weaker check
  (e.g. representative or ID-column-only) would under-gate and could leak columns such as
  `gross_pay`. Expanding via the catalog preserves "referenced columns ⊄ scope ⇒ reject" (D57)
  intact for the star case.

  **(OQ-3) Canonical USES pair shape.** The canonical USES pair is FULLY-QUALIFIED:
  `(database.table, column)` — a three-part granularity. Scope expressions and comparisons are
  at the same three-part granularity. Rationale: disambiguates warehouse vs. scratch (D64) vs.
  cross-database references; prevents bare-name namespace collisions where two databases share a
  table name. The catalog schema dict fed to `qualify_columns` must therefore key tables at
  `database.table` granularity, and the scope vector injected by D5 must express allowed columns
  as `database.table.column` triples.

  **(OQ-4) Scratch table column qualification.** Scratch tables (`scratch.s_<sessionId>_<file>`,
  D64) are NOT column-scope-checked — they are session-gated only via the D64 `s_<sessionId>_*`
  name-match. Column references against scratch tables are accepted without catalog qualification
  (scratch tables are not in the Semantic Catalog; their schema is user-supplied at upload time).
  The session-ID boundary is the gate for scratch; column scope enforcement applies only to
  warehouse tables. This is consistent with D64's statement that scratch isolation is a session
  boundary, stacked with but orthogonal to the column boundary.

  **(OQ-5) EXPLAIN queries.** `extract_column_provenance` is NEVER called on EXPLAIN queries.
  This is a **caller precondition**: the MCP (and any other D52 consumer) must strip or gate
  EXPLAIN statements before invoking the extractor. The precondition is documented and enforced
  at the call site; the extractor does not need to handle EXPLAIN internally. Rationale: EXPLAIN
  is a distinct code path (`explainQuery` tool) and is not a `runQuery` target; adding an
  internal EXPLAIN handler would obscure the caller contract and create dead code.

  Cross-references: D44, D52, D57, D62, D63, D64.

- **D70 (locked, 2026-06-30).** **Identifier matching is case-sensitive and exact; any unresolvable
  reference fails closed (extends D69; surfaced by the provenance-extractor security review).**
  Identifier resolution against the Semantic Catalog is **case-sensitive and exact for both tables
  and columns** — matching ClickHouse's own identifier semantics (`amount` and `Amount` are genuinely
  different identifiers). A table or column reference that cannot be **exactly** resolved against the
  catalog → **fail-closed** (`ProvenanceExtractionError`); there is **no case-insensitive fallback**
  and **no silent skip**. Rationale: the review found that (a) a case-insensitive table fallback plus
  (b) `qualify_columns` leaving case-mismatched columns unattributed combined to make
  `SELECT amount FROM payroll` (lowercase, catalog `Amount`) return an **empty USES set** that passes
  any scope check — a silent under-extraction, i.e. exactly the leak the extractor exists to prevent.
  The scope enforcer must **not** rely on ClickHouse rejecting the bad-case query downstream — that
  inverts the D57/D63 defense-in-depth. Enforcement: the extractor runs sqlglot's **validating**
  qualify so an unresolvable column **raises** rather than getting empty attribution, and the
  case-insensitive table fallback is removed (exact match only). Scratch tables remain the documented
  exception (D69/OQ-4): their columns are session-gated, not catalog-resolved, so validating-qualify
  is **not** applied to scratch column references. Also tightens the D69/OQ-1 lambda rule: the
  coverage check is **structural** (assert sqlglot produced a `Lambda` node with a non-None body and
  a non-empty parameter list) rather than relying on the same `find_all` traversal it is trying to
  verify (which was tautological in the failure case). Cross-references: D57, D62, D63, D64, D69.
  **→ D90 closes a second instance of this same fail-open class (uncatalogued unqualified columns +
  uncatalogued lambda-body columns), surfaced by the D62 oracle.**

- **D90 (locked, 2026-07-02, Session 14 — surfaced by the D62 oracle).** **An unqualified column that
  `qualify_columns` leaves unattributed (`col.table == ''`) and that is neither a catalog column, a
  provable SELECT-list output-alias reference, nor a bare column of a scratch-only SELECT, is a
  genuinely-unresolvable reference and fails closed — closing the D70 fail-open where such columns
  (including uncatalogued lambda-body columns) were silently dropped.** D70 fixed the *case-mismatch*
  under-extraction; the D62 false-reject oracle surfaced a **second, distinct instance of the same
  class**: `SELECT NonExistentCol FROM employee` (a bare column against a single valid table, where
  the column is simply **absent from the catalog** — not a case variant) was returned as
  `EXTRACTED_OK` with an **empty/understated USES set** rather than fail-closed, because sqlglot's
  `qualify_columns` leaves an unknown bare column unqualified and the old step-8 "case (C)" swallowed
  every such name as a presumed computed alias. An understated USES set is a fail-open: an empty set
  is ⊆ every scope, so the **runtime D44 replay filter** keeps a trail entry that should drop under
  mid-session scope narrowing (replaying out-of-scope values into context). The **MCP live path is
  backstopped** — it builds its enforcement schema from ClickHouse **introspection** (`system.columns`),
  so a real column is always present and scope-checked; the fail-open bites only the runtime replay
  path, which re-parses with the **curated YAML catalog** that can drift behind the warehouse.
  **Fix:** the unqualified-unknown case fails closed (`ProvenanceExtractionError`) **unless** the name
  is provably (a) a **declared projection alias referenced outside the projection list** (GROUP BY /
  ORDER BY / HAVING — distinguished structurally by clause location *and* an explicit alias
  declaration, so an identity-alias-wrapped broken projection column still raises), or (b) a bare
  column of a SELECT whose **every base source is in the `scratch` DB** (scratch is uncatalogued by
  design, D69/OQ-4; any catalogued source in the mix → fail closed, so a warehouse column can't be
  smuggled as scratch). The lambda coverage check likewise fails closed on any lambda-body column that
  is neither a bound parameter nor already in the USES set. Applied **byte-identical to both extractor
  copies** (D79a copy-in: `data-agent` + `clickhouse-api`). Adversarially verified (reviewer +
  QA: no fail-open reachable across alias-shadowing, scratch/warehouse smuggling, and lambda
  param/body smuggling; no legit-query false-reject). Cross-references: D57, D62, D63, D64, D69, D70,
  D79a, D44.
