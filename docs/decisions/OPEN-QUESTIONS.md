# Open Questions

Forks and details still to resolve. Grouped by area; link to the owning chapter.

## Learning loop ([05](../05-memory-and-learning.md)) — NEXT DELIVERABLE
- Extractor prompt + per-target output contract.
- Leakage-gate quarantine handling for near-misses.
- Conflict-resolution UX for contradictory global knowledge.
- Hit-count thresholds for candidate → validated.
- **Provenance/audit store** (D51): concrete store choice + retention duration (≥ candidate lifetime).

## Blueprints ([04](../04-blueprints.md))

### Grain ownership — HEADLINE (correctness, not authoring)
The blueprint model has no **grain contract**, so nothing distinguishes a correct-grain blueprint
from a wrong-grain one (e.g. `AVG(gross_pay)` over a 1:N `payroll_fact` join with no period filter),
and none of the validation gates catch it: golden replay reproduces a wrong number perfectly,
`hit_count` measures popularity, human review is the only (eyeball) defense — then it runs **silently**.
Fix is *declare-once-at-storage, enforce-at-authoring*, not per-blueprint vigilance:
- **Declare grain in the `getTableSchema` semantic YAML** (machine-usable, not prose): per-table
  `grain: [...]`, per-measure `{agg, defined_over}`, `temporal: {period_col, semantics}`. Single
  source of truth; blueprints inherit it, never restate it.
- **Static fan-out / grain gate in the learning-loop static-validate stage** (beside the
  `explainQuery` dry-run): parse joins → compute result grain from declared grains → reject/route to
  review when a measure is aggregated coarser than its `defined_over` without a de-fan, or a
  `per_period` table is joined with no period filter/`GROUP BY`. Deterministic, no warehouse round-trip.
- **Add a temporal slot type** (`period` / `as_of_date`): any blueprint touching a `per_period` table
  must bind a period slot or declare `uses_rules: [latest_period]`-style intent — no silent
  cross-history averaging.

### Rule-semantics drift (same spine as grain)
`uses_rules` pins catalog rules **by name only**, so changing a rule's *definition* (e.g.
`active_employee` starts including a new status) silently changes every blueprint citing it — SQL
unchanged, USES edges unchanged, golden replay on frozen data still passes. **Version catalog rules;
blueprints pin a rule version; a rule change re-flags dependents** (extend USES-edge revalidation
from columns to rule semantics).

### Dedup precision — **RESOLVED by D48**
(was: fuzzy `intent`-embedding merges could collapse blueprints whose SQL materially differs.) Now a
**two-layer** scheme: **hard** dedup on a deterministic canonical key `hash(resolves, uses_rules,
result-grain, normalized SQL AST)` (sound, not complete) with **single-writer-per-key** serialization
(also closes the concurrent-duplicate race — review item E); **soft** embedding match on `intent`
routes near-misses to the review inbox. Refines the proposed D39 (intent → soft layer). Remaining
sub-detail: the SQL-AST canonicalization rules (alias/ordering normalization depth).

### Execution semantics (track separately — not grain)
- ~~Intermediate inlining vs. the injection boundary~~ — **RESOLVED by D59a:** scalars → typed
  ClickHouse params; tables (small or large) → session scratch + `JOIN`. No untrusted upstream rows
  are ever string-interpolated into SQL. Refines D11.
- ~~Approval gate `show` inconsistency~~ — **RESOLVED by D59b:** `requires_approval` fires before the
  node runs and references **upstream/completed** outputs only, never the gated node's own un-computed
  output. A post-compute confirmation would be a separate node kind (not added now).
- **Mid-DAG runtime failure / pause durability** — **RESOLVED by D45.** Atomicity: the data path is
  **read-only + idempotent** and scratch is **TTL'd**, so a mid-flight death needs no compensation —
  **re-run is the compensation**, orphaned scratch TTLs away. Pauses survive restarts via a
  **pause checkpoint** on the session doc (stateless runtime, resume at `awaiting_node`). Remaining
  sub-detail: interaction with `skip_remaining` below when resuming a partially-skipped DAG.
- **`skip` / `skip_remaining` semantics** in a parallel-converge graph: skip-node vs skip-dependents;
  "remaining" by what order. (Still open.)
- ~~`guard` node_kind vs. D33 (no branching)~~ — **RESOLVED by D59c:** `guard` cut; its abort/skip
  capability folded into the `when` clause every node already supports. `node_kind ∈ {query, approval}`.

### Carried over
- Concrete `slot_bindings` payload shape and per-`type` binding rules (incl. list/`IN` and enum slots).
- **Period range** slots ("May vs June", "YTD") — `period_range` shape or list-typed period slot.
- **Period dimension table** for the period resolver (vs `SELECT DISTINCT` on a large fact table).
- Output-signature definition (which invariants to capture/check) + input-sampling strategy for replay.
- **Golden replay on a mutable warehouse** — **RESOLVED by D43** (was: frozen goldens prove SQL
  *stability* not *correctness vs. current data*). The "semantic-drift canary" is now specified as
  **three concrete probes** (result grain-integrity, catalog-vs-warehouse conformance, rule-semantics
  currency) gating a **freshness-bounded silent-eligibility** predicate; scope is incorrectness-not-
  change (honors D36). **Phasing (per D56):** Phase 1's D56 verify gate runs probe #1 (grain-integrity)
  on **every** result at runtime — so there is no unverified return; probes #2 (catalog-conformance)
  and #3 (rule-currency) are the **Phase-2 scheduled** additions (accepted time-boxed drift risk until
  then). Remaining sub-detail: `DRIFT_TTL` value + probe sampling/scheduling cadence.
- ~~Inline-vs-scratch size threshold for intermediates.~~ **RESOLVED by D59a:** shape-based, not size-based — scalars → typed params; all tables (small or large) → session scratch. No threshold.
- Golden-input capture: where golden results are stored and refreshed.
- neo4j graph schema (node labels, edge types, indexes) — to be specified.

## Semantic Catalog (`databaseSchemaDocs/`) — from the catalog gap review

The curated semantic layer applied by the **MCP**, not the runtime. Per D83 (reverses D78), the MCP's `getTableSchema` performs the `introspection ⨝ catalog YAML overlay` join itself and scope-filters the result before returning it. A review of the first-pass YAMLs fixed a set of gaps in-file; the items below are the **code-side dependencies** those fixes now assume, plus **data-layer gaps** the catalog can only document. (The code-side dependencies below — e.g. `resolveValues`'s `resolve_via` binding — are unaffected by the D83 relocation; only the "who applies the overlay" framing sentence above changes.)

### New catalog fields that need consuming code (introduced by the review)
- **`grain_verifiable: false`** — set on tables with no exposed unique key (`payroll`, `accrual_events`,
  `performance_discussions`, which now use `grain: []`). The **D43 catalog-vs-warehouse conformance
  probe must SKIP** these tables (not run `COUNT(*) == COUNT(DISTINCT grain)` against an empty/absent
  key and crash). Per-blueprint result-grain checks (D56) still apply to those blueprints' own grain.
- **`resolve_via` on a rule** — a filter rule over a client-defined code column declares
  `predicate: "FieldId IN ({field_codes})"` + `resolve_via: "resolveValues(FieldId, <concept>)"`
  (see PAF `*_change_fields`). The **runtime resolves the concept → code set via its runtime
  `resolveValues` (D77) and binds the result as params before executing** the predicate (D10: never
  string-interpolated). No mechanism exists for this yet. Note: because `resolveValues` is now a
  runtime composite (D77), the resolved concept→code expansion also happens runtime-side, consistent
  with the D5 injection boundary.
- **`sensitive: true` on a column** — added to known PII columns. Intended as the **catalog source of
  truth for the column-scope / governance layer**; needs that layer to actually consume it (today
  scope/masking are maintained out-of-band).

### `measures` schema (decision: minimal reconciliation)
Authored shape is `{ column, agg, defined_over }` (measure name may be conceptual). `agg` normalized to
`sum | count | count_distinct | avg | min | max`. **Decision: `defined_over` stays best-effort prose**
carrying both scope filter and aggregation grain — the de-fan check reads it best-effort, not as a
structured grain. (Full structured split of scope-vs-grain was considered and deferred.) AUTHORING_NOTES
updated; the [04](../04-blueprints.md) example still shows the old `{agg, defined_over}` form — align it.

### Tenancy (decision: RLS, recorded)
The warehouse is multi-tenant but isolation is **ClickHouse row-level security** (a custom `client_code`
setting from the JWT, applied by the MCP) — **not** the agent. The agent never sees/filters `ClientCode`
and its view is single-tenant. Catalog consequence (now enforced): grain/joins use **within-tenant keys
only**; `ClientCode` is never in `grain`/`primary_key` nor a join predicate.

### Data-layer gaps the catalog can only document (need warehouse / business action)
- **No supervisor foreign key** — org hierarchy is exposed only as `…SupervisorEmployeeName` strings;
  "who reports to X" / span-of-control is best-effort name-matching until a `SupervisorEmployeeCode`
  exists. (`supervisor` ambiguity documents the limitation.)
- **Requisition island** — no `RequisitionId` on `applicant_tracking_application`; application↔requisition
  funnel analysis (apps/req, fill rate, time-to-fill) is impossible without an external mapping.
- **String-typed dates/numerics** — candidate dates, `RequisitionOpenPositions`, `MonitoringPeriodDays`,
  `ExceptionPointTotal` are `String`. Safe-cast recipes (`…OrNull`) added, but proper typing is the fix.
- **Unconfirmed `terminated` rule** — the employee `rehired_not_terminated` guard rests on an explicit
  "ASSUMPTION to confirm" (rehire signalled by `MostRecentHireDate` advancing past `TerminationDate`);
  needs business sign-off.
- **Missing table doc** — `weekly_booked_sales_reps_only` is a declared join target with no catalog YAML;
  `getTableSchema` on it fails until authored.
- **Soft department joins** — `DistributedDepartmentCode`/`AccrualEventDepartmentCode → employee.DepartmentCode`
  are label-based, client-defined, low/medium confidence; not reliable FKs.

### `resolveValues` accept-vs-clarify threshold (extends D66, runtime-side per D77)
Beyond the deferred per-(client,column) index, there is **no defined confidence threshold** for when a
ranked `resolveValues` match is auto-used vs. routed to `askUser` — risk of silently filtering on a
plausible-but-wrong code. As a runtime composite (D77), this threshold is a runtime implementation
decision, not an MCP concern.

**Resolved for Phase 1 (Session 9, D77 build):** the runtime applies **no hard threshold** — it returns
numeric `score`s plus a computed `top_margin` (gap between the top two), and the tool description guides
the model to call `askUser` when scores are low/clustered or when `ranking == "freq_only"` (D85 degrade).
The clarify decision stays model-side by design (`resolveValues` resolves; `askUser` clarifies). A soft
runtime hint/hard threshold remains a compatible later change if traffic shows the model over-accepts.

## Retrieval ([03](../03-context-and-retrieval.md))
- Reranker model + threshold; recall `k` vs. final top-3. The reranker is a **custom API (not OpenAI)** (D71) called via a **simple HTTP `POST`** with a manual `RERANK` span (D24).
  **Contract resolved (Session 9b):** the user-provided mock at `~/Development/SQL/mocks/reranker_api`
  is authoritative — `POST /rerank {"query","documents"}` → `{"scores":[...]}` (same order, higher =
  more relevant, caller re-sorts; mock model `cross-encoder/ms-marco-MiniLM-L-6-v2`). `HttpRerankerClient`
  ships aligned + Layer-2-validated live. **Update (Session 10, D86):** pipeline shape settled and
  BUILT (Slice 1) — `recall_k=30`/corpus, top-3 cards + top-3 knowledge, degrade paths, all provisional
  `RuntimeSettings` tunables (`retrieval_*`); the pipeline is Layer-2-green against the live mocks.
  **Still open:** production endpoint/auth, threshold/cutoff tuning under real traffic, the **neo4j
  vector index + corpus schema (Slice-2 first task**, retrieval-pipeline-design.md), rendered-block
  token-budget accounting (OQ-R8), and the Slice-2 trust-boundary reconfirmation (§8.1).
- Shared embedding model choice — a **custom API (not OpenAI)** (D71), called via a **simple HTTP
  `POST`** with a manual `EMBEDDING` span (D24); shared across question + blueprint intent + knowledge.
  **Contract resolved (Session 9b):** the mock at `~/Development/SQL/mocks/embedding_api` is
  authoritative — `POST /embed {"input_text":[...]}` → bare `[[float,...],...]` (768-dim,
  `all-mpnet-base-v2`, no auth). `HttpEmbeddingClient` rewritten to it + Layer-2-validated live.
  **Still open:** production endpoint/auth + final model id.
- **`resolveValues` (D66, now runtime-side per D77):** ranking signals (semantic + synonyms +
  frequency) weighting; the deferred **per-(client,column) index** (currently live `DISTINCT` each
  call); temporal label-drift handling via the `period?` arg. These questions now live **agent-
  runtime-side** — the backing `runQuery` handles enforcement; ranking and caching decisions belong
  to the runtime implementation of D77.
  **Update (Session 9, D77 build):** ranking weights are shipped as **provisional `RuntimeSettings`
  tunables** (`resolve_values_similarity_weight=0.7`, so `0.7·cosine + 0.3·log-freq`; `query_limit=200`,
  `top_k=10`) pending real traffic. The `period?` arg is honored only as a **concrete structured**
  `{column, start, end}` filter; deictic/relative period-domain resolution (label-drift over time,
  D41/D65) is **still open**, deferred to the D67 era. The **per-(client,column) index** remains
  deferred (live `DISTINCT` each call). ~~**Still open:** the custom **embedding API contract**~~
  **resolved Session 9b** — the user-provided mock (`~/Development/SQL/mocks/embedding_api`) is the
  authoritative contract and `HttpEmbeddingClient` is aligned + live-validated; an unconfigured API
  still degrades to freq-only (D85). **D67 `resolve_via` wiring** is still open,
  but the composite now exposes a typed `resolve()` entry point it can call (no model round-trip).
- **Context-budget params (D46):** result preview row cap `N` + history token budget + when
  compaction triggers. (Mechanism resolved; values TBD.)
- **Loop-budget caps (D47):** max iterations / tokens / wall-clock per turn. (Mechanism resolved;
  values TBD.)

## Security / infra ([06](../06-security-and-governance.md), [09](../09-infrastructure.md))

### D75 — clickhouse-api extension: two sub-questions — **RESOLVED by D79**

1. ~~**Provenance extractor packaging.**~~ **RESOLVED by D79(a): copy-in** (spec repo authoritative,
   mirror same-sprint; promote to shared library once the D69/D70 contract stabilizes).

2. ~~**Column-scope model mismatch / transport.**~~ **RESOLVED by D79(b) + D81:** `column_scope` is a
   signed **JWT claim** (D79b); `session_id` travels as an unsigned **`X-Session-Id` header** (D81,
   amends D79b) — both threaded through the existing `Principal`/ContextVar into `runQuery` (the
   tenant-level → column-level shift). Remaining sub-detail: the large-scope-vs-header-size mitigation
   (inline `column_scope` list vs. a signed scope *reference*) — decided only if it bites.

### Other security / infra items

- **Per-user column-entitlement source (D82, interim).** Currently every JWT is minted with `column_scope: []` (allow-all, D80/D82) — column enforcement is a no-op until entitlements are wired. Open: where does the mint step look up a user's permitted columns — IdP claims stamped at login (e.g. Entra group → scope claim) vs. a backend entitlement service / token-exchange that populates `column_scope` at mint time? Resolving this also unlocks the D82 interim: once a non-empty scope is being minted, column enforcement activates automatically (D57 is already in place).

- **Planned move to a managed IdP (Microsoft Entra / Keycloak / Okta), cross-reference D79/D82.** `token_service` is an interim issuer. The production direction is to replace it with a managed identity provider (Microsoft Entra or equivalent). Because the ClickHouse MCP already validates standard OIDC JWTs via JWKS (D79) — checking `iss`/`aud` and fetching signing keys from `OIDC_JWKS_URL` — this swap is an **issuer/config change** (point `OIDC_JWKS_URL` / `OIDC_ISSUER` / `OIDC_AUDIENCE` at Entra; the IdP mints the JWT with the `column_scope` + `user_name`/tenant claims), **not an MCP code change**. Prerequisite: the chosen IdP must be able to stamp the `column_scope` claim (ties into the per-user-entitlement item above).

- **`X-Session-Id` forgeability (hardening, from the D81 security review).** `session_id` reaches the
  MCP as an **unsigned** `X-Session-Id` header (D81), so scratch namespace selection rests on an
  application-layer prefix check with no signature and no ClickHouse row policy. A JWT-authenticated
  caller bypassing the trusted backend could target another session's scratch. Accepted for now
  (trusted backend; scratch ephemeral; feature unbuilt). If scratch grows cross-user-sensitive data,
  **bind `session_id` to the JWT `sub`** (HMAC prefix) or add a scratch row policy. Decide before the
  scratch-upload feature (D19/D64) ships.
- ~~Scope representation passed by UI (JWT claim vs. separate object).~~ **RESOLVED by D79b:** `column_scope` is a signed JWT claim.
- Scratch TTL duration + cleanup ownership.
- **`SESSION_TTL` value** (D44) — must exceed the learning-loop completion window.
- **SQL parser choice** — **RESOLVED by D62: `sqlglot` (Python).** Remaining: **dialect coverage** —
  how often each fail-path (closed/soft/review) actually trips, measured via the ClickHouse
  `system.query_log.columns` ground-truth oracle on real queries.
- ~~Live `runQuery` parse-failure behavior~~ — **RESOLVED by D63: fail-closed + alert** (reject, never
  run unchecked). Remaining: build the adversarial-ClickHouse-SQL test suite + the
  `system.query_log.columns` oracle harness to **measure** the false-reject rate and drive parser
  coverage.
- ~~Vector index choice (native neo4j vs. external)~~ — **RESOLVED by D60: neo4j-native**, hosting
  both blueprint intent + global-knowledge vectors. Reranker hosting + embedding-model choice still open.
- Couchbase session document model — now must include the **per-result column-provenance set** (D44).
- ~~Mid-session scope-change leak via replayed trail~~ — **RESOLVED by D44** (provenance-filter).

## UI ([08](../08-ui.md))
- Charting library / visual design.
- ~~Review-inbox scope (schema-only vs. all candidate types).~~ — **RESOLVED by D58:** all
  global_knowledge candidates + schema edits + knowledge conflicts + blueprint variants + sampled
  blueprint candidates / leakage near-misses.
- Per-query generated apps (deferred).

## Observability ([10](../10-observability.md))
- Phoenix hosting namespace + trace retention policy.
- Redactor implementation + attribute allow-list.
- Online eval judges (live vs. batch) and the canary eval set.
- Trace sampling rate (100% vs. sampled) given PII + volume.

## Extensibility ([12](../12-extensibility.md))
- **Sandboxing model for skills.** Launch is first-party in-process (no sandbox). If third-party
  skills are introduced: what boundary (subprocess, WASM, gVisor)? What review process?
- **Skill versioning and compatibility.** When `HookContext` payload evolves, how are older skills
  gated? Is there a `min_schema_version` startup check, or is the contract additive-only?
- **Permission model for `capability` enum.** Who approves new capability tags, and how is the
  capability ↔ hook-point whitelist extended?
- **Skill testing conventions.** Should the Layer 3 `docker-compose` stack include a fixture skill
  to exercise the dispatch path end-to-end?
- **Skill ordering at the same hook point.** Sequential-in-registration-order is simple but
  file-system-order-dependent. Should the manifest have an explicit `priority` field?
- **Skill failure and D47 budget interaction.** When a skill is terminated for exceeding
  `max_latency_ms`, is the remaining turn budget reduced by the consumed wall-clock, or reset?
  (Working answer: reduced, consistent with D47.)

## Whole-system
- Full component diagram polish.
- Evaluation harness implementation (golden Q&A canaries).
