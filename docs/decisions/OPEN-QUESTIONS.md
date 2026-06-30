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
  change (honors D36). **Phase-2 deliverable** — Phase 1 ships silent-on-validation as an accepted
  time-boxed risk. Remaining sub-detail: `DRIFT_TTL` value + probe sampling/scheduling cadence.
- Inline-vs-scratch size threshold for intermediates.
- Golden-input capture: where golden results are stored and refreshed.
- neo4j graph schema (node labels, edge types, indexes) — to be specified.

## Retrieval ([03](../03-context-and-retrieval.md))
- Reranker model + threshold; recall `k` vs. final top-3.
- Shared embedding model choice.
- **`resolveValues` (D66):** ranking signals (semantic + synonyms + frequency) weighting; the
  deferred **per-(client,column) index** (currently live `DISTINCT` each call); temporal label-drift
  handling via the `period?` arg.
- **Context-budget params (D46):** result preview row cap `N` + history token budget + when
  compaction triggers. (Mechanism resolved; values TBD.)
- **Loop-budget caps (D47):** max iterations / tokens / wall-clock per turn. (Mechanism resolved;
  values TBD.)

## Security / infra ([06](../06-security-and-governance.md), [09](../09-infrastructure.md))
- Scope representation passed by UI (JWT claim vs. separate object).
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

## Whole-system
- Full component diagram polish.
- Evaluation harness implementation (golden Q&A canaries).
