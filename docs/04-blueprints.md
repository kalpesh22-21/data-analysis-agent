# 04 — Blueprints

A **blueprint** is a parameterized, validated query **DAG** stored in neo4j. It is a learned,
verified fast-path: instead of the model re-deriving SQL, it slot-fills and executes a known-good
plan. Blueprints range from a single slotted SQL to multi-branch reports.

## Model

Fields:

- `id`, `intent` (natural-language description, embedded for retrieval).
- `status`: `candidate` | `validated`. `hit_count`: successful, accepted uses.
- `resolves`: pins ambiguous catalog terms for this blueprint (e.g. `salary: gross_pay`).
- `slots`: typed parameters `{name, type, required}`. `required` missing → triggers `askUser`.
- `uses_rules`: catalog rules applied (e.g. `active_employee`, `settled_pay_only`). Rules come in two
  kinds (D67): **static** — the predicate is a fixed SQL boolean; and **resolved (dynamic)** — the
  predicate references `resolveValues(column, concept)` over a `client_defined` column, which the
  runtime **resolves to a concrete value set per client before injecting** (the codes are bound as
  params, not string-interpolated — same boundary as slots). Resolved rules are how client-defined
  code spaces (e.g. PAF `FieldId`, `EarnCode`) avoid hardcoding one tenant's codes.
- `composes`: ordered **control-flow** graph nodes — each with a `node_kind`, `feeds_from`,
  `consumes`, typed `output` (`table`/`scalar`), and optional `when` / `requires_approval`.
- `sql_template`: parameterized SQL (leaf nodes), bound safely — never string-interpolated.
- `canonical_key`: deterministic structural dedup key (D48) — `hash(resolves, uses_rules,
  result-grain, normalized sql_template AST)`; the single-writer-per-key serialization key.
- `drift_status`: `clean` | `suspect` + `last_drift_check_at` — the freshness-bounded silent-eligibility
  attestation (D43).
- `validated_against_catalog_sha`: the catalog/deploy SHA this blueprint was last validated against
  (D42/D53); a catalog bump re-validates affected blueprints.

### Example — simple
```yaml
id: bp_avg_salary_one_dept
intent: average salary for a single named department, active staff, for a given pay period
resolves: { salary: gross_pay }
slots:
  - { name: department, type: string, required: true }
  - { name: pay_period, type: period, required: true }   # forced: payroll_fact is per_period
uses_rules: [ active_employee ]
sql_template: |
  WITH emp_pay AS (                       -- de-fan: pay components → one total per employee
    SELECT p.employee_code, SUM(p.gross_pay) AS emp_gross
    FROM payroll_fact p
    WHERE p.pay_period = {pay_period}
    GROUP BY p.employee_code
  )
  SELECT e.department,
         AVG(ep.emp_gross)                AS avg_salary,
         COUNT(DISTINCT e.employee_code)  AS headcount
  FROM employee_info e
  JOIN emp_pay ep ON ep.employee_code = e.employee_code
  WHERE e.status = 'A' AND e.department = {department}
  GROUP BY e.department
```

This is **grain-correct**: it de-fans `payroll_fact` to one row per employee before averaging, and
pins a pay period (no silent cross-history averaging). The next section shows what makes it correct —
and the naive version a static gate must reject.

### Grain declaration (proposed — lives in `getTableSchema`)

Grain is owned **once, at the storage layer** (the semantic YAML returned by `getTableSchema`), and
blueprints inherit it — they never restate it. The two tables above declare:

```yaml
# getTableSchema → employee_info  (grain excerpt)
employee_info:
  grain: [ employee_code ]                 # one row per employee — current state
  temporal: { semantics: as_of_now }       # no period dimension; reflects "now"
  # …entities, columns, rules, ambiguities as today…

# getTableSchema → payroll_fact  (grain excerpt)
payroll_fact:
  grain: [ employee_code, pay_period, pay_component ]   # one row per component, per period, per emp
  temporal: { period_col: pay_period, semantics: per_period }
  measures:                                              # shape: { column, agg, defined_over }
    gross_pay: { column: amount, agg: sum, defined_over: "per (employee_code, pay_period); SUM over pay_component — the de-fan axis" }
```

#### `temporal` — the dimensions model (D65, revises D40)

A table declares **zero or more temporal dimensions**, each with a `role` and the column(s) that
realize it. This replaces the single-`semantics` enum (D40), which couldn't express **ranged**
periods (start/end, no key column) or **dual-temporal** tables — payroll has both (a `PayDate`
*event* and a `[PayPeriodStartDate, PayPeriodEndDate]` *coverage period*).

```yaml
temporal:
  dimensions:
    - { name: <id>, role: period,  period_col: <col> }                  # discrete period key
    - { name: <id>, role: period,  range: { start_col: <c>, end_col: <c> } }  # ranged period
    - { name: <id>, role: as_of,   effective_col: <col> }               # effective-dated state
    - { name: <id>, role: event,   date_col: <col> }                    # transaction/event timestamp
  default_pin: <dimension name | null>     # which dimension the resolver fills by default
```

Roles:
- **`period`** — a reporting period, as a discrete `period_col` **xor** a `range:{start_col,end_col}`.
- **`as_of`** — effective-dated state, point-in-time answerable (`effective_col`).
- **`event`** — a transaction timestamp (e.g. `PayDate`); bounds time but isn't the grain period.

The gate (extended): **a blueprint reading a table with ≥1 temporal dimension must constrain at
least one of them** (a filter or `GROUP BY` on a dimension's column(s)) — *or* declare a
`latest_*`-style rule. Pinning **any** dimension bounds the time window and clears the gate; the
catastrophe is pinning **none** (silent multi-year blend). `default_pin` only chooses which the
resolver fills by default / which a bare `period` slot targets; intent can redirect to another
dimension (e.g. payroll's `pay_date_default` vs `pay_period_default` rules).

Shorthands / mapping from D40:
- `as_of_now` → `dimensions: []` (no temporal dimension → no pin required).
- old `per_period` (`period_col`) → one `role: period` dimension, `default_pin` = it.
- old `as_of_date` (`effective_col`) → one `role: as_of` dimension.

Temporal **slot types** (`period`, `as_of_date`) bind a dimension of the matching role; for a
**ranged** period the `period` slot resolves to a specific `(start,end)` pair (or "the period
covering date X") and the SQL constrains via the range columns.

The **static fan-out / grain gate** (learning-loop static-validate stage) reads this and would
**reject / route-to-review** the naive form — `AVG(p.gross_pay) … JOIN payroll_fact p … GROUP BY
department` with no de-fan and no period — because:
1. `gross_pay` is aggregated with `AVG`, not its declared `agg: sum`, and **coarser than its
   `defined_over: pay_component`** with no de-fan (pre-aggregation) → averages component rows, not
   employees (fan-out double-count).
2. `payroll_fact` is `per_period` but the query has **no `pay_period` filter and no period in
   `GROUP BY`** → temporal grain unpinned (silent cross-history averaging).

The simple example above clears both checks: the `emp_pay` CTE applies `gross_pay`'s declared
`agg: sum` at its `defined_over` grain, and the required `period` slot pins `pay_period`.

### Example — multi-branch report (convergence DAG)
```
 (1) dept_actuals     (2) budget_targets     (3) company_avg
      │                     │                      │
      └──────────┬──────────┴──────────────────────┘
                 ▼
        (4) flag over-budget AND below-company-avg   feeds_from: [1,2,3]
```
Independent branches (1,2,3) run in parallel; the convergence node (4) waits and `consumes` all three.

### Control-flow nodes (guards & approval gates)

`composes` is **not pure data-flow** — a node carries a `node_kind` and optional control-flow.
**`node_kind ∈ {query, approval}`** (D59c — `guard` was cut; after D33's no-branching its "routes"
was vestigial, and its only real capability is the `when` clause that any node already supports):

- `node_kind: query` (default) — runs SQL.
- `node_kind: approval` — pauses via `askUser`, shows chosen **upstream** outputs, resumes on approval.

Any node — `query` or `approval` — may carry a `when` predicate (`on_violation: abort|skip|ask`),
which is where the old guard's abort/skip-on-predicate capability now lives.

```yaml
- order: 4
  node_kind: query | approval        # guard cut (D59c)
  feeds_from: [1,2,3]
  consumes: { ... }
  output: { flagged: table }
  when:                                   # precondition — evaluated BEFORE the node runs
    expr: "not empty($1.dept_actuals) and $3.company_avg > 0"
    on_violation: abort | skip | ask
    message: "No active staff in period — nothing to compare."
  requires_approval:                      # human checkpoint BEFORE the node runs
    prompt: "Detailing {n} flagged departments — proceed?"   # {n} from an UPSTREAM output
    show: [ "$1.dept_actuals" ]           # UPSTREAM/completed outputs only (D59b) —
                                          # never the gated node's own un-computed $4.flagged
    on_deny: abort | skip_remaining
```

- **`when.expr`** uses a small **declarative predicate language** over typed outputs only:
  `count()`, `empty()`, scalar comparisons, `and`/`or`/`not`, set membership. No raw SQL / arbitrary
  code — so the executor evaluates it safely and we can statically analyze it.
- **Approval gates reference upstream outputs only (D59b).** `requires_approval` fires **before the
  node runs**, so its `prompt` and `show` may reference **upstream / completed** outputs only — never
  the gated node's own un-computed output. ("Compute, then confirm before returning" would be a
  separate post-compute node kind; not added now.)
- **`when`/approval predicates must be entity-agnostic.** A threshold (`count > 50`,
  `company_avg > 0`) is fine; a predicate on an entity value (`department = 'Warehouse'`) is a
  **slot/filter, not a predicate** — the leakage gate still applies.

## Slot filling

Slot filling is a hand-off: the **model proposes** raw values from the conversation; the **runtime
validates, normalizes, and binds** them. The **resolvers are deterministic code** (normalize +
catalog/warehouse-domain lookup + validate) — **no LLM runs inside `runBlueprint`** (D49). The model
never emits SQL or final keys; the agent LLM's *only* role in slot-filling is proposing raw values
**before** execution. This is what keeps the fast path genuinely deterministic, silent, and secure.

**Where proposed values come from.** When the model picks a blueprint, it reads the `slots` (via
`getBlueprint`) and fills each from: the user's question + prior turns; **user knowledge**
(aliases/defaults, e.g. `"my team"` → `department=0420`); and `resolves` for the ambiguity side
(`salary`→`gross_pay`). It hands `runBlueprint` its best read, e.g. `{department:"Warehouse",
pay_period:"May 2026"}`.

**Runtime pipeline (per slot):** presence → type-resolve → bind.

1. **Presence.** `required` slot absent → `askUser`. Optional slot absent → its `optional_pattern`
   applies (e.g. `({department} IS NULL OR …)` binds NULL = "all").
2. **Type-resolve (deterministic per-type resolvers, against `binds_to`).** The model's raw value is
   coerced and validated by a resolver keyed on the slot `type`: `enum` → value ∈ allowed set; entity
   slot → optional existence check against `binds_to`; `period`/`as_of_date` → temporal resolution
   (below). These are **pure code, never an LLM** (D49). Genuinely ambiguous / multi-match / no match
   (incl. fuzzy NL like "the pay run after the holidays") → **`askUser`**, never an LLM guess.
   (Resolvers, not the model, produce final keys — so the model can't fabricate an invalid value that
   only fails at execution.)
3. **Bind safely.** Resolved values bind as **typed sqlglot-AST literals** (F1 — realized without a
   `runQuery` param surface per D89): the template's `{department}` becomes a placeholder that binds
   to one escaped literal inside one `SELECT`, carrying its type from `binds_to`. **Never
   string-interpolated** (the SQL-injection boundary; the same D10-safe mechanism `resolveValues`
   proved). *(Reframed from the original "ClickHouse server-side query parameters" design at build —
   no bound-param surface exists on `runQuery`; the AST-literal path is provably injection-safe.)*

Composite blueprints fill slots **once at the blueprint level**; values propagate to whichever step's
`sql_template` references them. (Step-to-step `consumes` is intermediate passing, not slot filling.)

### Temporal slot fill paths (`period` / `as_of_date`)

A period value is a **warehouse domain entity, not a calendar date** — it is resolved against the
**actual set of periods that exist** (`temporal.period_col` domain, ideally a period dimension table),
never parsed with `to_date`. The **period resolver** handles three fill paths:

| Fill path | Example | Resolution |
|---|---|---|
| **Explicit named** | "May", "2026-05", "Q2" | map NL → a valid period key in the domain. Maps to **multiple** (e.g. bi-weekly "May") → `askUser` (a single `period` slot pins one period). No match → `askUser`. |
| **Relative / deictic** | "latest", "current", "last period" | resolve **against the warehouse, not the wall clock** — `max(pay_period)` present, usually the latest **settled/closed** period (ties to `settled_pay_only`). Avoids calendar-month math returning empty. |
| **Absent + required** | (no period mentioned) | a **user-knowledge default**; **or** the blueprint declared `uses_rules: [latest_period]` → no slot to fill, runtime injects the latest settled period; **or** `askUser`. |

`as_of_date` is the analog for `as_of_date`-semantics tables (point-in-time snapshot), resolved to an
effective date rather than a period key.

> **Open:** single period only — ranges ("May vs June", "YTD") need a `period_range` slot shape or a
> list-typed period slot (not yet decided). And the resolver wants a **period dimension table** rather
> than `SELECT DISTINCT` on a large fact table.

## Execution — `runBlueprint(id, slot_bindings)`

> **Status: BUILT (D89, Session 13, Slices A/B/C).** The execution engine, the D56 verify gate, slot
> filling, D67 `resolve_via`, and mid-DAG approval pause/resume are all built and Layer-1-green (with a
> Layer-2 live leg green — single- and multi-node **scalar** DAGs execute end-to-end vs real neo4j +
> ClickHouse-via-MCP). **Honest boundary:** only **scalar-converging** DAGs execute — a
> table-intermediate DAG is **rejected pre-dispatch** (F2, deferred, gated on a `clickhouse-api`
> scratch-write surface). Step 4's table branch below is therefore the deferred path; step 4's scalar
> branch is what ships. See [DECISIONS.md](decisions/DECISIONS.md) D89.

The model supplies slot values; **the runtime executes the DAG deterministically** (model never
re-derives the SQL). Steps:

1. **Load** DAG from neo4j; **fill + validate** `slot_bindings` per **Slot filling** above (presence,
   type-resolve, ambiguity → `askUser`).
2. **Bind safely.** Slot values are untrusted (came from model/user) → bound as **parameters /
   strict per-slot types**, never naive string interpolation. This is the SQL-injection boundary.
3. **Topologically sort** `composes` by `feeds_from`; run independent branches in parallel.
4. **Pass intermediates (injection-safe — D59a).** `runQuery` is stateless/read-only, so a
   convergence node cannot reference an upstream result directly. The executor materializes each
   upstream `output` by **shape**, never by string-interpolating it into SQL text (upstream output is
   untrusted — it ran over warehouse + scratch + possibly uploaded PII, so a "department name" cell is
   attacker-controllable text):
   - **scalars** → bound as **typed sqlglot-AST literals** (F1, BUILT — a single-cell upstream output
     binds as one escaped literal, on the same D10-safe path as a slot; a scalar-consumed node
     returning `!=1` row / wrong column count / NULL **fails closed** before the consumer runs, since
     D56 only verifies the terminal). *(Reframed from "server-side parameters" at build per D89.)*
   - **tables (small *or* large)** → write to the **session scratch schema** and `JOIN`. **DEFERRED
     (F2, D89):** no scratch-write surface exists yet, so a table-intermediate DAG is **rejected
     pre-dispatch** (scalar-converging DAGs only) — this path is gated on the `clickhouse-api` Track-A
     surface.
   (Same scratch infra as external uploads — one mechanism.) This **refines D11** (was size-based
   "small → inline `CTE`/`VALUES`"); inlining untrusted rows as `CTE`/`VALUES` is dropped because it
   violated the D10 "never string-interpolated" boundary.
5. **Run each node** in dependency order. Before a node executes, the executor:
   a. evaluates `when` (if present) → on violation apply `abort` / `skip` / `ask`;
   b. handles `requires_approval` (if present) → pause via `askUser`, render `show` outputs, then
      resume or apply `on_deny`;
   c. for `query` nodes, executes via MCP `runQuery` (jwt/scope/session_id injected by code), captures
      typed outputs, wires `consumes` (`$1.dept_actuals` → downstream input).
6. **Verify (mandatory gate — D56; BUILT D89).** Every result passes an **agent-side verification
   gate before the user sees it; the user is never asked to verify**. *(This requires the blueprint to
   **declare its `result_grain`** at the §Execution level — the deterministic check has nothing to
   check against otherwise; a `grain_verifiable:false` blueprint skips the grain check **visibly**
   (`grain_checked:false`), never silently. Verify-FAIL / unmappable grain / grain-probe error all
   **withhold** the rows.)* The gate is two parts: (a) **code-computed
   assertions** — the D43 probe-#1 **grain-integrity** check (`result row count == COUNT(DISTINCT
   declared result-grain)`; each measure aggregated at its declared `{agg, defined_over}`) plus the
   `result_signature` invariants, evaluated **deterministically in code**; (b) **LLM review** of the
   result + those assertions against user intent. The deterministic check is the teeth — a wrong-grain
   number is shape-correct, so a model eyeballing it cannot catch the fan-out double-count. **Any
   failed assertion or access-scope error → fall back to a fresh agent loop or `askUser`, never return
   the suspect answer.** There is **no silent path** (D56 supersedes D12): the verification always
   runs; it simply does not *pause* or *nag* — declared `approval` gates remain the only deliberate
   pauses. SQL stays visible for transparency ([08-ui.md](08-ui.md)) but is never a chore the user
   must perform.
7. **Record.** On user acceptance → increment `hit_count`; on correction → log negative signal.

### Pause / resume durability (D45)

A DAG can **pause** mid-execution (`requires_approval`, or `when … on_violation: ask`) waiting on a
human. The runtime must survive a restart (deploy, crash, autoscale) across that pause:

- **At a pause**, the executor writes a **pause checkpoint** to the session doc *before* yielding:
  `{blueprint_id, slot_bindings, completed_nodes:[{order, output_ref|scratch_ref}], awaiting_node,
  partial trail, consumed:false}`. The runtime is then **stateless across the pause** — any instance
  resumes (see [09-infrastructure.md](09-infrastructure.md)).
- **Resume** loads the checkpoint, CAS-flips `consumed` (exactly-once; double-clicks can't re-enter),
  and continues **at `awaiting_node`** — completed nodes are never re-run. Scratch intermediates are
  reconnected via stored refs on a **best-effort** basis (N8): a pause is user-bounded, so if scratch
  has TTL'd away during a long pause, resume **falls back to a fresh agent loop** rather than failing
  — never a wrong answer. (No hard "scratch TTL ≥ pause window" requirement; the fallback covers it.)
- **Crash during active (non-paused) compute needs no checkpoint:** the path is read-only and
  idempotent, so the runtime simply **re-runs the whole turn**; orphaned scratch TTLs away.
  **Re-run is the compensation** — there is no write to undo. This is the atomicity story for a DAG
  that dies mid-flight: read-only + TTL'd scratch ⇒ nothing to compensate, only to GC.
- **Abandoned pause** (user never answers) expires with the session (`SESSION_TTL`) + scratch TTL —
  no separate GC. If resume can't continue (scratch expired, node errors) → fall back to a fresh
  agent loop / `askUser`, never a wrong answer.

## Lifecycle

```
session learning ──▶ candidate ──▶ (validation) ──▶ validated ──▶ retired (if stale/superseded)
```

- New blueprints land as **`candidate`** (never auto-trusted from one session).
- **Validation methods (ALL apply, different failure modes):**
  1. **Golden replay (regression)** — re-run with **sampled valid inputs** and check the stored
     **entity-free output signature** (shape + invariants). Catches breakage `explainQuery` can't
     (empty results, join fan-out, grain/type shift). **Not** for data drift — that's hit-count + human.
  2. **Hit-count threshold** — repeated accepted uses with no corrections (semantic trust).
  3. **Human review.**
- **Dedup at write (two layers — D48):**
  - **Hard dedup = deterministic canonical key + single-writer-per-key.** Compute
    `canonical_key = hash(resolves, uses_rules, result-grain, normalized sql_template AST)` — the
    *structural* facts, **not** NL `intent`. Serialize writes per `canonical_key` (partitioned
    write-stage queue / per-key lock over the existing Redis) so exactly one worker does the
    read-match-(create | increment) for a given key at a time. Concurrent equivalent candidates
    therefore resolve to **one create + one `hit_count` increment**, never a duplicate. The key is
    **sound, not complete** (same key ⇒ truly equivalent, so auto-merge is safe; equivalents written
    differently may miss — caught by the soft layer). **Fail-soft (D52):** if the SQL parser can't
    normalize the template, **skip the hard key** and fall through to the soft layer — worst case a
    human-reconciled duplicate, never a wrong merge.
    - **Dependency on D37 degrades gracefully (N4):** `result-grain` comes from grain declaration
      (D37, still *proposed*). But it is **subsumed by `normalized SQL AST`** for the equality test —
      identical ASTs ⇒ identical result grain — so it is a **redundant defensive cross-check**, not a
      soundness requirement. The key is sound on the AST alone if D37 never lands; D37 only adds
      robustness against shallow AST normalization. So D48 does **not** block on D37.
  - **Soft dedup = embedding similarity → review inbox.** Embed `intent`, semantic-match for
    *near* matches the hard key misses (variant / partial overlap / contradiction) → route to the
    review inbox for human adjudication. High-recall; need not be race-perfect.
- Only **silent-eligible** blueprints take the silent fast path; candidates may be suggested
  cautiously. **Silent-eligible** is a freshness-bounded *predicate*, not a status — see §Drift
  attestation.
- **Control-flow blueprints** (any `guard`/`approval` node) **require human review** before
  `validated` — never auto-promoted on hit-count alone.

## Drift attestation — silent-eligibility (D43)

Validation (above) proves a blueprint correct **at authoring time**. But the warehouse mutates and
the catalog evolves under a validated blueprint, so "validated → silent forever" is unsafe: the
silent path has the least oversight yet the most exposure to drift. The fix is a **freshness-bounded
attestation** — a blueprint stays silent only while a recent *incorrectness* check still holds.

### Semantic drift = three concrete checks (not one fuzzy canary)

We detect **incorrectness, not change.** A correct blueprint whose numbers legitimately moved (retro
pay, a real raise) is **not** flagged — that is data-value drift, deliberately out of scope (honors
D36). The "semantic-drift canary" decomposes into three deterministic probes:

| Probe | Scope | Check |
|---|---|---|
| **Result grain-integrity** | per blueprint (on replay w/ sampled inputs) | result row count `== COUNT(DISTINCT declared result-grain)`; each measure aggregated at its declared `{agg, defined_over}` — catches the canonical `AVG(gross_pay)` fan-out double-count |
| **Catalog-vs-warehouse conformance** | per table (shared across blueprints) | declared `grain` still holds on live data (`COUNT(*) == COUNT(DISTINCT grain cols)`); enum coverage; `temporal.period_col`/`effective_col` still present. A failing table flags **every** blueprint over it |
| **Rule-semantics currency** | per blueprint | cited `rules.yaml` definitions unchanged (git SHA) since last attestation — ties to D38's coarse re-flag |

The **promotion scheduler** runs these periodically and stamps each blueprint with
`drift_status ∈ {clean, suspect}` + `last_drift_check_at`.

### The silent-eligibility predicate (target design)

```
silent_eligible  ⇔  status = validated
                AND  drift_status = clean
                AND  last_drift_check_at within DRIFT_TTL
```

**Not silent-eligible → split by reason:**
- `drift_status = suspect` (a probe actually **failed**) → **demote to `candidate`** (suggested
  cautiously, never auto-run) + review-inbox flag, until re-validated + re-attested.
- merely **stale** (TTL lapsed, no failure) → **non-silent degraded run** (execute, but show SQL
  prominently + inline grain-integrity assert + a "verify this" nudge) **and** trigger an on-demand
  re-attestation; a clean result restores silent.

### Rollout (phased — D43, as amended by D56)

**Amended by D56: there is no silent path at any phase.** The drift *attestation* is still phased,
but it no longer gates a silent vs. non-silent choice — every response is verified before the user
sees it (step 6). What's phased is *where* grain errors are caught:

- **Phase 1 (launch):** grain errors are caught at **runtime** — the D56 verification gate runs the
  probe-#1 grain-integrity check inline on **every** blueprint response and falls back to the raw loop
  on failure. This requires **grain *declaration*** (D37a / D40) at launch (the check needs a grain to
  check against). The full three-probe **scheduled** attestation is **not** yet built; the
  confident-wrong-answer risk that D43 Phase 1 originally accepted is **closed by the runtime gate**.
- **Phase 2 (committed fast-follow):** build the three probes as a **scheduled** background
  attestation + the **static authoring gate** (D37b, reject wrong-grain blueprints before they are
  stored) + the freshness-bounded `drift_status` stamping + split-by-reason demotion. Phase 2 adds
  *authoring-time* and *periodic* defense on top of the *runtime* gate — defense in depth, not a
  silent-path toggle.

## Scope edges (neo4j)

Each blueprint stores its `(tables, columns)` dependency set as `USES` edges, computed across the
**whole DAG** (transitive over `composes`). Used to pre-filter retrieval by user scope. Edges are
written at blueprint-creation time and re-validated if the schema changes.

## Storage (neo4j) — Slice-2 retrieval projection (D87)

Specified in [decisions/neo4j-corpus-design.md](decisions/neo4j-corpus-design.md) and built
(Session 11). The Slice-2 projection: `:Blueprint` nodes (id-unique, native **768-dim cosine**
vector index over the intent embedding, `embedding_model` parity stamp, Track-B lifecycle/provenance
properties reserved) with the transitive USES set stored **denormalized as byte-exact
`database.table.column` strings** for zero-traversal scope pre-filtering (D86), plus reserved
`:Column`/`:Table` nodes and `:USES`/`:OF_TABLE` edges (written same-transaction,
deleted-and-rewritten on re-seed, unread until the D38-era graph consumers). `:KnowledgeChunk`
mirrors the vector/parity shape. **Full-DAG storage (`composes`, `sql_template`, `slots`, `resolves`,
`uses_rules`, `result_grain` steps) is BUILT (D89, Slice A)** — the six JSON props are stored with
write-time validation and expanded additively by `getBlueprint`, and are now **executed** by
`runBlueprint`, not merely stored. The **scope-honesty gate** (a template can only read within its
declared `uses`, table-aware) is enforced at load.

---

**Status:** BUILT — single-node + scalar-converging DAGs executable (model + execution engine + D56
verify + slot filling + D67 `resolve_via` + mid-DAG approval pause/resume + lifecycle Locked; Slice-2
retrieval projection **Locked/built** (D87); full-DAG storage **built + executed** (D89)). **Deferred:**
table-intermediate DAGs (F2, rejected pre-dispatch — needs a `clickhouse-api` scratch-write surface)
and the full **authoring-time** static grain gate (D37/D37b, Phase-2 — the runtime D56 gate is the
launch teeth).
**Open questions:**
- **Grain ownership (correctness — proposed D37):** no grain contract today, so a wrong-grain
  blueprint (e.g. `AVG(gross_pay)` over a 1:N `payroll_fact` join, no period filter) passes every
  **authoring-time** gate — though the runtime **D56 verify gate catches it at execution** (catching it
  statically at authoring is the proposed-D37 deliverable). Declare grain in `getTableSchema` YAML + static fan-out gate at authoring +
  a temporal slot type. See [decisions](decisions/DECISIONS.md) D37 and [open questions](decisions/OPEN-QUESTIONS.md).
- **Rule-semantics drift (proposed D38):** `uses_rules` pins rules by name only; a definition change
  mutates citing blueprints silently. Version rules; re-flag dependents.
- **Dedup precision (proposed D39):** key dedup on `intent + resolves + uses_rules + result-grain`,
  not `intent` embedding alone, to avoid merging blueprints with divergent SQL semantics.
- ~~Intermediate inlining vs. injection boundary~~ — **RESOLVED by D59a:** scalars → typed CH params,
  tables → scratch `JOIN`; no untrusted rows ever interpolated into SQL.
- ~~Approval gate timing~~ — **RESOLVED by D59b:** `requires_approval` references **upstream/completed**
  outputs only, never the gated node's own un-computed output.
- ~~`guard` node_kind vs. D33~~ — **RESOLVED by D59c:** `guard` cut; capability folded into `when`.
  `node_kind ∈ {query, approval}`.
- **`skip`/`skip_remaining` semantics** in a parallel-converge graph (skip-node vs skip-dependents;
  "remaining" by what order), and **mid-DAG runtime failure** handling — still open.
- Concrete `slot_bindings` payload shape and per-`type` binding rules (incl. list/`IN` and enum slots).
- ~~Inline-vs-scratch size threshold for intermediates.~~ **RESOLVED by D59a:** shape-based — scalars → typed params; all tables → session scratch. No threshold.
- Output-signature definition: which invariants to capture/check, and the input-sampling strategy.
- ~~**Golden replay on a mutable warehouse**~~ — **RESOLVED by D43:** the semantic-drift canary is
  specified as three probes (grain-integrity, catalog-conformance, rule-currency), scheduled in Phase 2;
  D36 scopes replay to regression. Remaining: `DRIFT_TTL` + probe cadence.
