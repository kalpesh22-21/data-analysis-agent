# `runBlueprint` — Fast-Path Execution Engine + D56 Verify Gate + D67 `resolve_via` Design

**Status:** Proposed (design only; nothing built this pass). Sub-slices A/B/C proposed below.
**Brick:** the largest remaining Phase-1 brick — the deterministic blueprint fast path.
**Precedent it builds on:** `resolveValues` (D77, `resolvevalues-design.md`), read tools (D88,
`read-tools-design.md`), the neo4j corpus (D87, `neo4j-corpus-design.md`), the Phase-0 runtime
(`phase0-runtime-design.md`).
**Owning spec:** `04-blueprints.md` (execution semantics), DECISIONS D9/D10/D11/D33/D41/D45/D48/D49/
D56/D57/D59/D64/D65/D67, OPEN-QUESTIONS §Blueprints (OQ-T1 + the four carried sub-items).

---

## 0. Ground truth this design relies on (verified by reading code + docs)

The blueprint model has the most prior spec of any brick; this design **resolves and scopes**, it does
not re-invent. Three findings from the *code* materially constrain the engine and are the spine of
this document — the docs describe an end-state the current 6-tool MCP cannot yet fully realize:

- **F1 — `runQuery` has no bound-parameter surface.** `MCPClient.call_tool("runQuery", {"sql","limit"})`
  (`mcp/client.py:50`) accepts a SQL *string* and a row limit — nothing else. Every place `04-blueprints.md`
  and D41/D59a say slot values and scalar intermediates "bind as **ClickHouse server-side query
  parameters**, never string-interpolated" is describing a surface that **does not exist**.
  `resolveValues` already hit this wall (`resolvevalues-design.md`, D77: *"no bound-param surface on
  `runQuery`"*) and resolved it by rendering values as **sqlglot-AST typed literals** (`sql_builder.py`
  — `exp.Literal.string(...)`, ClickHouse-escaped), which preserves the D10 "never naive
  string-interpolation" boundary *without* protocol params. **Decision (§3.3): `runBlueprint` binds
  slots and scalar intermediates the same way — via sqlglot-AST typed-literal substitution into the
  template AST, not f-string interpolation, not server-side params.** The injection boundary (D10) is
  honored; "server-side parameter" is re-read as "typed-AST-literal binding" for Phase 1. A true
  bound-param surface is a `clickhouse-api` (Track A) extension, out of scope for this brick.

- **F2 — there is no scratch *write* surface.** `runQuery` is read-only (INSERT/DDL/table-functions
  blocked, `02-tools-and-api.md`); the only scratch-*creating* path is the privileged ingestion
  side-channel (D19), which is **unbuilt**. D59a mandates table intermediates be **materialized to
  session scratch and `JOIN`ed** — that requires writing a table, which no Phase-1 tool can do.
  **Decision (§2.4): table-intermediate passing between DAG nodes is OUT OF SCOPE for this brick** and
  gated on a future `clickhouse-api` scratch-write surface. The first cut executes: **(a) single-node
  (leaf) blueprints** and **(b) multi-node DAGs that pass only *scalar* intermediates** (which fold
  into downstream SQL as typed AST literals, no scratch needed). This is an honest, large, useful
  subset — the canonical `avg_salary_by_dept` and most report blueprints are single-node or
  scalar-converging.

- **F3 — the runtime `CatalogHandle` carries only `{column: type}`; grain/measures/temporal are not
  wired into the runtime.** `provenance/catalog_handle.py` wraps `build_sqlglot_schema()` (the thin
  `{db.table: {col: type}}` projection). The D56 verify gate needs **grain**, and the D65 temporal
  gate needs **temporal dimensions** — both live only in `load_semantic_catalog()` (`catalog/loader.py`,
  the full YAML overlay, currently unused by the runtime). **Decision (§4.2): add a
  `SemanticCatalogHandle` that surfaces per-table `grain`, `grain_verifiable`, `temporal`, and
  per-measure `{agg, defined_over}` to the runtime** — a read-only, deploy-coupled handle mirroring
  `CatalogHandle`, built once at startup from `load_semantic_catalog()`.

Further verified ground truth (unblocking, not constraining):
- **Runtime-tool registry exists and is `runBlueprint`-ready.** `AgentLoop.runtime_tools: Mapping[str,
  RuntimeTool]` (`loop/agent_loop.py:312`), `RuntimeTool` protocol (`run(args, creds) -> ToolResult`),
  `_run_runtime_tool` crash-guard + provenance sanitizer (§2 hardening, *"the containment seam the
  future `runBlueprint` brick relies on"*). `runBlueprint` drops in as one more registry entry.
- **`askUser` is the sole terminal/pause branch** — it writes a `PauseCheckpoint` and returns
  `paused_ask_user`. Approval gates and `when…on_violation: ask` reuse this exact machinery (§2.5).
- **`PauseCheckpoint`** (`session/models.py:172`) today carries `{reason, pending_question, awaiting,
  consumed, budget_window_count}` — **no DAG state**. D45 requires `{blueprint_id, slot_bindings,
  completed_nodes, awaiting_node}`; this brick **additively extends `PauseCheckpoint`** (§2.5).
- **`resolve()` is the D67 hook, already typed.** `ResolveValuesComposite.resolve(*, table, column,
  concept, period, credentials) -> ResolveOutcome` (`composite/resolve_values.py:242`) — a
  programmatic entry point with no model round-trip, exactly as D77 promised for `resolve_via`.
  `ResolveOutcome.values: list[ResolvedValue]` (each `{value, description, score, freq}`).
- **`ToolDispatcher.dispatch("runQuery", …)`** is the single choke point that gives D57 column-scope,
  D64 scratch-isolation, D5 credential injection, provenance capture, and denial mapping **for free** —
  `resolveValues` reuses it and so must every per-node query in `runBlueprint` (§2.3).
- **`getBlueprint` stored projection** (`retrieval/vector_index.py`, `_GET_BLUEPRINT_QUERY`) returns
  the D87 thin fields only; `BlueprintDetail` (`retrieval/models.py:55`) is documented as *"a STRICT
  subset that grows additively when the full DAG … is stored with the `runBlueprint` brick"*. The
  additive extension is §1.
- **Catalog reality:** `payroll.yaml` has `grain: []` + `grain_verifiable: false` + dual-temporal
  (`pay_period` ranged + `pay_event`) + `measures:{gross_pay:{column:Amount, agg:sum, defined_over:
  <prose>}}`. `employee.yaml` has `grain:[EmployeeCode]`, `temporal.default_pin: null`. PAF rules carry
  `resolve_via: "resolveValues(FieldId, '<concept>')"` + `predicate: "FieldId IN ({field_codes})"`.
  **`defined_over` is best-effort PROSE** (OPEN-QUESTIONS §measures decision) — this bounds what D56
  can check (§4.3).

---

## Q1 — Full-DAG storage (OQ-T1): additive extension of the D87 corpus

**One-line:** Extend the D87 `:Blueprint` node with **five additive JSON-typed properties**
(`resolves`, `slots`, `uses_rules`, `sql_template`, `composes`) written by the corpus loader from
fixtures and surfaced by an extended `getBlueprint`; the retrieval/recall path and every D87 invariant
are **unchanged** because the new properties are unread by recall.

### 1.1 Storage shape (neo4j, additive per D87's "reserved additive extension")

The D87 design explicitly reserved full-DAG storage. Blueprints are a small, curated, **trusted** seed
(same posture as D87/D88 — Track B authoring is out of scope). The DAG is a nested structure; neo4j is
not a document DB, so rather than explode it into `:Node`/`:Slot` sub-graphs (premature — recall never
traverses it, and the graph-consumer use is D38-era), we store the DAG as **JSON-serialized string
properties** on the existing `:Blueprint` node:

```
(:Blueprint {
   id, intent, slots_summary, intent_embedding, embedding_model, uses,   # D87 (unchanged)
   status, drift_status, canonical_key, hit_count, catalog_sha,          # D87 (unchanged)
   # --- additive, this brick (OQ-T1) ---
   resolves_json,       # JSON: {"salary":"gross_pay", ...}
   slots_json,          # JSON: [{name,type,required,binds_to?,optional_pattern?,enum_values?}, ...]
   uses_rules_json,     # JSON: ["active_employee", {id:"...", resolve_via:"resolveValues(FieldId,'…')"}]
   sql_template,        # string (leaf single-node blueprints) — parameterized SQL with {slot} tokens
   composes_json,       # JSON: [{order,node_kind,feeds_from,consumes,output,sql_template,when?,requires_approval?}, ...]
   result_grain_json    # JSON: ["EmployeeCode","pay_period"] — the blueprint's DECLARED result grain (D56 needs this; §4.3)
})
```

Rationale for JSON-string properties over sub-graph nodes:
- **Recall never reads them** — the D87 vector recall + scope pre-filter consume only `intent_embedding`
  + `uses`; adding opaque string properties is zero-risk to those invariants (verified: the recall
  Cypher `_BLUEPRINT_RECALL_QUERY` selects an explicit field list — new properties are simply not
  selected, so nothing changes).
- **Reversible / low-ceremony** — one loader change + one `getBlueprint` query-field addition. A
  sub-graph model is the D38 graph-consumer's job, not the fast path's.
- **`result_grain_json` is new and load-bearing (F3/§4.3):** for `grain_verifiable: false` base tables
  (payroll), the D56 check runs against the **blueprint's own declared result grain**, not the base
  table's (which is `[]`). OPEN-QUESTIONS §grain_verifiable confirms: *"Per-blueprint result-grain
  checks (D56) still apply to those blueprints' own grain."* So every blueprint must declare it.

### 1.2 Fixtures + loader

- `BlueprintSeed` (`corpus_loader.py:44`) gains the five additive fields (all optional-defaulted so
  existing D87/D88 fixtures still load — additive, no migration). `_UPSERT_BLUEPRINT` SETs the new
  properties.
- **Write-time validation (fail-loud, mirrors `_validate_blueprint_uses`):** the loader validates that
  (a) every `{slot}` token in `sql_template` / each node's `sql_template` has a matching entry in
  `slots_json`; (b) `sql_template` parses under sqlglot ClickHouse dialect (D62) **and is a single
  read-only `SELECT`** — no DDL/DML and no multi-statement block (`SELECT 1; DROP TABLE payroll` is
  rejected, so a trailing statement cannot ride through the column check); (c) the parsed template's
  footprint ⊆ the declared `uses` (the stored scope key set), enforced **at two levels**: every
  **source table** in the FROM/JOIN must resolve to a `(db, table)` in `uses` (closing the
  JOIN-to-an-unlisted-table hole where an alias-qualified `p.SSN` reads an undeclared table), and every
  **column** must qualify against a schema built only from `uses` with `expand_alias_refs=False` (so an
  output-alias name cannot mask a real same-named column read). `*` stars and dictionary-family
  functions (`dictGet…`) are rejected outright — both defeat column-level analysis. So a blueprint
  cannot reference a table or column outside its own advertised footprint. (d) `composes_json` is a DAG
  (no cycles, `feeds_from` references exist, ≤ a hard node-count cap so a huge/adversarial DAG fails
  loud, never a stack overflow). A violation raises `CorpusLoadError` — an authoring mistake fails the
  seed load, never ships a silently-broken blueprint.
  - **Authoring constraint (fail-closed ergonomics, not security):** because `expand_alias_refs=False`
    is what closes the alias-mask scope hole, a `GROUP BY` (or `WHERE`) that references a **SELECT
    output alias** rather than the underlying real column fail-closes at load. Authors must name the
    **real column** in `GROUP BY` (`GROUP BY Department`, not `GROUP BY dept` where `dept` is an output
    alias). `ORDER BY`/`HAVING` alias references are tolerated by the qualifier; `GROUP BY` is not — a
    known, deliberate trade-off (the scope check is not weakened to smooth this ergonomics edge).
- Fixture format: a `blueprints.yaml` entry gains `resolves`, `slots`, `uses_rules`, `sql_template`,
  `composes`, `result_grain` keys (the `04-blueprints.md` example YAML is the authoring shape).

### 1.3 `getBlueprint` expansion (additive, D88(b) posture preserved)

`_GET_BLUEPRINT_QUERY` selects the five new properties; `BlueprintDetail` gains the parsed fields;
`GetBlueprintTool` renders them in `result_full`. **The non-oracle scope posture (D88(b)) is
unchanged** — out-of-scope still returns byte-identical `{found:false}`, and the FOUND path still
carries the scoped-`uses` footprint provenance (§S1 in `read-tools-design.md`). The model now sees the
typed `slots` (to fill), `resolves`, `uses_rules`, and SQL — the progressive-disclosure "expand" step
(D8) is finally complete.

---

## Q2 — Blueprint execution engine

**One-line:** A deterministic DAG walker that topologically orders `composes`, runs each `query` node
through `ToolDispatcher.dispatch("runQuery", …)` (D57/D64/D5 free), evaluates `when` predicates and
`approval` gates via the existing `PauseCheckpoint` machinery, passes **scalar** intermediates as typed
AST literals (table passing deferred, F2), and hands every result to the D56 verify gate before return.

### 2.1 Where it lives

A new `runtime/blueprint/` package, mirroring `composite/`:
- `blueprint/models.py` — typed `Blueprint`, `SlotSpec`, `Node`, `WhenClause`, `ApprovalGate`,
  `ResultGrain` (parsed from the §1 JSON on load).
- `blueprint/executor.py` — `BlueprintExecutor` (the DAG walk).
- `blueprint/slots.py` — the per-type slot resolvers (Q3).
- `blueprint/verify.py` — the D56 gate (Q4).
- `blueprint/tool.py` — `RunBlueprintTool` implementing `RuntimeTool` (Q5).
- `blueprint/template.py` — sqlglot-AST slot/scalar binding (F1/§3.3).

### 2.2 DAG walk (topological, no branching per D33)

1. **Load** the `Blueprint` (via the extended `getBlueprint` store read — the DAG is already fetched
   for the model, but the executor re-reads authoritatively by id, scope-checked identically).
2. **Fill + validate slots** (Q3) → a `bound: dict[slot_name, BoundValue]`. Any resolver returning an
   `askUser` signal → pause (§2.5) before any node runs.
3. **Topologically sort** `composes` by `feeds_from`. Single-node blueprints (the common case) are a
   trivial one-element order. **Independent branches run sequentially in Phase 1**, not in parallel —
   `04-blueprints.md` says "in parallel" but the agent loop dispatches tool calls sequentially
   (`agent_loop.py` deviation note: parallel dispatch is a deferred perf question, §11 OQ); DAG-node
   parallelism inherits that decision. Correctness is identical; only latency differs. Named
   trade-off, reversible.
4. **Per node, in order:**
   a. Evaluate `when` (Q2 predicate evaluator) → `abort` | `skip` | `ask`.
   b. Handle `requires_approval` → pause via `askUser`-style checkpoint, render `show` (upstream
      outputs only, D59b), resume or apply `on_deny`.
   c. `query` node → substitute bound slots + upstream **scalar** outputs into the node's
      `sql_template` AST (F1 typed-literal binding) → `ToolDispatcher.dispatch("runQuery", {sql},
      creds)` → capture typed `output`.
5. **Verify** the final result through the D56 gate (Q4). Pass → return; fail → fall back to the raw
   loop (§4.4).

### 2.3 Per-node execution reuses the `runQuery` choke point (non-negotiable)

Every node query goes through `ToolDispatcher.dispatch("runQuery", …, credentials)` — **never a direct
MCP call.** This is how the executor gets D57 column-scope enforcement, D64 scratch-isolation, D5
credential injection, D63 fail-closed parse, provenance capture, and denial mapping **for free and
identically to `resolveValues`** (D77 as-built: *"issues the backing `runQuery` via the single
`ToolDispatcher` choke point so D57/D5 enforcement, provenance capture, and denial mapping are reused,
not re-implemented"*). A node whose query is denied (e.g. a column outside the user's scope) surfaces as
a `runBlueprint` denial (§5.4), never bypassed.

### 2.4 Intermediate passing — scalar only in Phase 1 (F2)

- **Scalar output** (a node whose `output` is `{name: scalar}`, e.g. `company_avg`): the value is read
  from the node's single-cell result and **bound into the downstream node's SQL as a typed AST
  literal** (F1) — exactly the `resolveValues` value-binding boundary. Injection-safe (D10): the cell
  is untrusted text but becomes a typed literal via sqlglot, never concatenated.
- **Table output → OUT OF SCOPE (F2).** D59a mandates scratch materialization + `JOIN`; no Phase-1 MCP
  tool can write a scratch table. A blueprint whose `composes` requires a **table** intermediate is
  **rejected at load** (`CorpusLoadError`) and, defensively, at execution (`RUN_BLUEPRINT_UNSUPPORTED`
  → fall back to raw loop). This is the honest boundary: the convergence-DAG example in
  `04-blueprints.md` (dept_actuals + budget_targets + company_avg → flag) needs table passing and is a
  **fast-follow gated on the `clickhouse-api` scratch-write surface**, tracked as a new OQ (§8).

### 2.5 Approval gates + pause/resume mid-DAG (D45)

`PauseCheckpoint` is **additively extended** (existing fields unchanged; new fields default so
`askUser`/`budget_cap` checkpoints are untouched):

```python
@dataclass(frozen=True)
class PauseCheckpoint:
    reason: str  # "askUser" | "budget_cap" | "blueprint_approval" | "blueprint_when_ask" | "blueprint_slot"
    pending_question: dict | None
    awaiting: str
    consumed: bool
    budget_window_count: int = 0
    # --- additive, this brick (D45 mid-DAG durability) ---
    blueprint_id: str | None = None
    slot_bindings_json: str | None = None            # the resolved BoundValues (re-fill is deterministic; store the raw too)
    completed_nodes_json: str | None = None          # [{order, output_scalar}] — SCALAR outputs only in Phase 1
    awaiting_node: int | None = None                 # the node order to resume at
```

- **At a pause** (approval gate, `when…on_violation:ask`, or a slot that resolved to `askUser`), the
  executor writes the checkpoint **before yielding**, then returns `paused_ask_user` up through the
  loop (the loop's existing `askUser` interception path is reused — the executor does not itself pause;
  it returns a *pause signal* the loop honors, keeping the terminal-pause branch singular, §Q2 seam).
- **Resume** (`AgentLoop.resume`, CAS-consume, D45 exactly-once): if the consumed checkpoint carries a
  `blueprint_id`, control re-enters `BlueprintExecutor` at `awaiting_node` with `completed_nodes`
  rehydrated — **completed nodes never re-run**. Scalar outputs are stored inline in the checkpoint (no
  scratch ref needed in Phase 1, sidestepping D55's best-effort scratch-reconnect entirely — a Phase-1
  simplification F2 buys us).
- **Crash during active (non-paused) compute** needs no checkpoint (D45): the path is read-only +
  idempotent → **re-run the whole turn**, orphaned nothing (no scratch writes in Phase 1). Clean.
- **The loop seam:** the executor returns a small `ExecOutcome` union — `Completed(result)` |
  `Paused(checkpoint, question)` | `Failed(reason)`. `RunBlueprintTool.run` maps `Completed`→ok
  `ToolResult`, `Failed`→error `ToolResult`, and `Paused` needs the **one new loop affordance**: a
  runtime tool that pauses. Today only `askUser` pauses. **Decision (§Q6 seam):** generalize the loop's
  terminal-pause check — after a runtime tool returns, if it signals a pause (a sentinel on `ToolResult`
  or a dedicated `PausedToolResult`), the loop writes the checkpoint and returns `paused_ask_user`
  exactly as for `askUser`. This is the single non-trivial `AgentLoop` change this brick makes.

### 2.6 `when` predicate evaluator (D32/D59c)

A small **declarative** evaluator over typed node outputs: `count()`, `empty()`, scalar comparisons,
`and`/`or`/`not`, set membership — **no raw SQL, no eval()** (`04-blueprints.md`). It is a pure
function `evaluate(expr_ast, outputs) -> bool`. **Entity-agnostic gate (D59/leakage):** the loader
rejects a `when` expr that references an entity *value* (`department = 'Warehouse'`) — that is a
slot/filter, not a predicate (`04-blueprints.md`); only thresholds/shape (`count > 50`, `company_avg >
0`, `empty($1)`) are allowed. Layer-1-authoritative (D45-style — hard to assert end-to-end).

### 2.7 Budget interaction (a DAG = how many `tool_calls_made`?)

**Decision: one `runBlueprint` tool call counts as exactly one `tool_calls_made`** (the resolveValues
precedent, `agent_loop.py`: a runtime tool *"counts as exactly one `tool_calls_made`"*). The per-node
`runQuery` calls it issues internally do **not** each increment the model-facing budget — they are the
tool's implementation, invisible to the loop's iteration accounting (identical to `resolveValues`'
single inner `runQuery`). Rationale: the model made one decision (run this blueprint); the DAG size is
the blueprint's business, not the model's budget. The D56 verify gate's LLM review is a **separate**
model round-trip (§4.4) and is accounted separately (it re-enters the loop, not nested in the tool).

---

## Q3 — Slot binding

**One-line:** Deterministic per-type resolvers (D49, **no LLM in `runBlueprint`**) that take the
model's raw proposed values and produce typed sqlglot-AST-literal bindings; multi-match / no-match /
fuzzy → an `askUser` pause signal, never a guess; D67 `resolve_via` rules expand through the existing
`resolveValues.resolve()` hook.

### 3.1 `slot_bindings` payload shape (resolves the carried OQ)

The model calls `runBlueprint(id, slot_bindings)`. **Decision: `slot_bindings` is a flat JSON object of
`{slot_name: raw_value}`** where `raw_value` is a string, number, boolean, or a list (for `IN`/enum
list slots):

```json
{ "id": "bp_avg_salary_one_dept",
  "slot_bindings": { "department": "Warehouse", "pay_period": "May 2026" } }
```

Flat + raw is correct because (D41) **the model proposes raw values; the runtime resolves/binds.** The
model never sends typed keys or final codes — it sends its best NL read. Per-slot *type* comes from the
stored `slots_json` (`binds_to`, `type`), not from the payload. A list value (`["PTO","SICK"]`) targets
a list/`IN` slot. This is the minimal shape that carries model intent without leaking binding
responsibility to the model.

### 3.2 Per-type resolvers (D41/D49, pure code)

`blueprint/slots.py` — a resolver keyed on `SlotSpec.type`, each `resolve(raw, spec, catalog, creds) ->
Bound | AskUser`:
- **`string` / `entity`** — normalize; optional existence check against `binds_to` (a `runQuery`
  `SELECT … WHERE col = <literal> LIMIT 1` through the dispatcher, scope-enforced). No match / multi-
  match on a supposedly-unique entity → `askUser`.
- **`enum`** — value ∈ `enum_values` (from `slots_json`, sourced from catalog); not in set → `askUser`.
- **`period` / `as_of_date`** (D41/D65 temporal) — resolve against the **warehouse period domain**, not
  `to_date`. **Phase-1 honest scope:** explicit-named (map NL → a valid period key via a `runQuery`
  `SELECT DISTINCT period_col` probe, scope-enforced) and absent+required (`latest_period` rule → `max`
  settled period via a probe, or `askUser`). **Deictic/relative** ("the pay run after the holidays") →
  `askUser` (D49: fuzzy NL is never guessed). This mirrors `resolveValues`' deferral of deictic period
  resolution to "the D67 era" — we are now in that era but scope it to explicit + latest, deferring the
  period-*dimension-table* and NL-relative resolution (carried OQ). **Ranged periods (payroll `pay_period`
  = start/end range, F0 catalog):** a `period` slot resolves to a `(start,end)` pair and the template
  constrains via the range columns (`04-blueprints.md` ranged-period path).
- **`list` / `IN`** — each element resolved by the element type; binds as a sqlglot `IN (<lit>,…)` list.

**Every resolver is pure code + `runQuery` domain probes — never an LLM** (D49, the hard invariant). A
resolver's probe `runQuery` is a normal dispatched call (scope-enforced); it does **not** count against
the model budget (it is inside the one `runBlueprint` call, §2.7).

### 3.3 Safe binding (F1 — the injection boundary, realized without server-side params)

`blueprint/template.py`: parse the node `sql_template` once under sqlglot (ClickHouse dialect, D62),
locate each `{slot}` placeholder token, and **replace it with a typed sqlglot AST literal** built from
the resolved value (`exp.Literal.string`, `exp.Literal.number`, an `exp.Tuple` for `IN`) carrying the
slot's declared type. Regenerate SQL from the AST. This is byte-for-byte the `resolveValues`
`sql_builder` discipline (D77, `D77-concept-never-in-sql` — ClickHouse-escaping of adversarial values
verified empirically). **The slot value never touches SQL as a string** (D10). The `04-blueprints.md`
"`{department:String}` server-side param" language is realized as a typed AST literal; the security
property (no interpolation) is identical, the mechanism differs (F1). This divergence is called out in
§8 as a documentation-alignment item and a candidate for a future true-param MCP surface.

### 3.4 D67 `resolve_via` wiring (the dynamic-rule expander)

A `uses_rules` entry may be **static** (`predicate: "RegisterType = 'EARN'"` — a fixed SQL boolean,
injected as an AST predicate) or **resolved/dynamic** (`predicate: "FieldId IN ({field_codes})"` +
`resolve_via: "resolveValues(FieldId, 'employee status')"`, from PAF `*_change_fields`). For a resolved
rule, the executor:
1. Parses `resolve_via` → `(column, concept)`.
2. Calls **`ResolveValuesComposite.resolve(table=<rule table>, column=<column>, concept=<concept>,
   period=None, credentials=creds)`** — the typed D67 hook that already exists (`resolve_values.py:242`),
   **no model round-trip** (D77 built it *"as a typed programmatic entry point for the (still-unwired)
   D67 rule expansion"* — this brick wires it).
3. Takes `ResolveOutcome.values` (the client-scoped code set), and binds them as a typed AST `IN`-list
   literal into the `{field_codes}` placeholder — **bound as params, never string-interpolated** (D67,
   same F1 boundary as slots).
4. **Degrade/ambiguity handling (D66c/D85):** if `resolve()` returns `degraded=True` (embedding down →
   freq-only) or a low `top_margin`, the executor **pauses to `askUser`** to confirm the code set before
   filtering — the fast path does not silently filter on a guessed client code. This honors D66(c) and
   D85 exactly as the model-facing tool would. If `resolve()` returns empty (no codes for the concept) →
   the rule matches nothing; per the rule's intent this typically means "abort with a clean 'no matching
   codes' answer" rather than silently returning all rows — **decision: empty resolved-rule set → fall
   back to raw loop** (never silently drop the filter, which would over-return).

`resolveValues.resolve()`'s inner `runQuery` is scope-enforced (D57), so the resolved codes are
provably in-scope; its provenance flows into the blueprint's provenance union (§5.3).

---

## Q4 — The D56 verify gate (the "no silent path")

**One-line:** Every `runBlueprint` result passes a **mandatory two-part gate before the user sees it**:
(a) a **deterministic** grain-integrity + `result_signature` check computed in code (the teeth), and
(b) an **LLM review** against user intent; any failed deterministic assertion → **fall back to the raw
loop, never return the suspect answer** — and the gate always runs, it just never *pauses* or *nags*.

### 4.1 Where it runs

**Post-execution, pre-return**, inside `RunBlueprintTool` — after the DAG's final node, before the
`ToolResult` is handed back to the loop. It is not a separate model-facing tool; it is the tool's own
exit gate (D56: *"verification is the agent's job"*, *"the user is never asked to verify"*).

### 4.2 The deterministic check — what is actually computable (F3)

The gate needs grain metadata not currently in the runtime → **add `SemanticCatalogHandle`** (F3),
built once at startup from `load_semantic_catalog()`, exposing per `db.table`: `grain: list[str]`,
`grain_verifiable: bool`, `temporal` (dimensions + `default_pin`), `measures: {name: {column, agg,
defined_over}}`. Deploy-coupled + immutable, mirroring `CatalogHandle`.

The **grain-integrity assertion** (D43 probe #1, the fan-out canary):
- **Result-grain check:** `result row count == COUNT(DISTINCT <declared result-grain columns>)`.
  The declared result grain is `result_grain_json` (§1.1) — **the blueprint's own declared grain**, not
  a base table's. This is why `result_grain_json` is stored: for `grain_verifiable: false` base tables
  (payroll `grain: []`), there is nothing to check *against* at the base-table level, so the gate
  checks the **result** against the **blueprint's declared result grain** (OPEN-QUESTIONS §grain_verifiable:
  *"Per-blueprint result-grain checks (D56) still apply"*). Computed by re-issuing a
  `SELECT count(), count(DISTINCT <grain cols>) FROM (<the blueprint's final SQL>)` probe (scope-enforced,
  one extra dispatched query — the D56 *"cost accepted: an extra lightweight distinct-grain count
  query"*). Rowcount ≠ distinct-grain-count → **FAIL** (fan-out double-count caught).

### 4.3 Honest limit: `defined_over` is prose, so the measure-agg check is partial

D56 also says *"each measure aggregated at its declared `{agg, defined_over}`"*. But `defined_over` is
**best-effort prose** (OPEN-QUESTIONS §measures decision: *"`defined_over` stays best-effort prose …
the de-fan check reads it best-effort, not as a structured grain"*). So the runtime **cannot
structurally verify** "gross_pay was summed over pay_component within (employee, period)". **Decision:**
the deterministic gate in Phase 1 is the **result-grain row-count check** (§4.2) — which *does* catch
the canonical `AVG(gross_pay)` fan-out (the result would have more rows / a wrong distinct-grain count),
plus the `result_signature` invariants (below). The measure-`agg` structural check is **deferred to the
Phase-2 static authoring gate (D37b)** where the measure's `agg` is checked against the SQL AST at
authoring time — honest, and consistent with D56's split (runtime gate now, authoring gate Phase 2).
The row-count check is the load-bearing teeth D56 relies on; it is fully computable today.

**`result_signature` invariants:** the entity-free output signature (D36) — column shape, types,
non-null/positivity invariants, expected row-count bounds — stored on the blueprint and checked in code.
Definition of the invariant set is a carried OQ (OPEN-QUESTIONS: *"Output-signature definition"*);
Phase-1 interim = **column-shape + type match + declared-grain row-count**, the minimum that makes the
gate meaningful.

### 4.4 Fail behavior + the LLM review

- **Deterministic FAIL → fall back to the raw agent loop** (D56, `04-blueprints.md` step 6: *"Any
  failed assertion … → fall back to a fresh agent loop or `askUser`, never return the suspect
  answer"*). Concretely, `RunBlueprintTool` returns a `ToolResult(status="error",
  error_code="RUN_BLUEPRINT_VERIFY_FAILED", retryable=True)` with a model-facing message that says the
  fast path did not verify and to answer from the raw loop — the model then proceeds with
  `getTableSchema`/`runQuery`. **No suspect number ever reaches the user.**
- **LLM review (D56 part b):** a separate model round-trip reviews `{result preview, the code-computed
  assertions, user intent}`. This is **not** inside the tool (which is deterministic, D49) — it is the
  loop's normal next round-trip: the tool returns the verified result + the assertion outcomes as part
  of `result_preview`, and the loop's next model turn *is* the review (the model narrates/accepts, or
  requests a raw-loop re-derivation). Rationale: keeping the LLM review as an ordinary loop round-trip
  (not a nested call) preserves the "no LLM inside runBlueprint" invariant (D49) while still delivering
  D56's two-part gate. The deterministic teeth are what block a wrong answer; the LLM review is the
  intent check on top.

### 4.5 Does the gate apply to raw `runQuery` results too? (read D56's "every result" carefully)

D56's letter is *"every **blueprint** response"* / *"every `runBlueprint` result"* — the grain-integrity
gate is defined against a **declared** grain, which only blueprints have. A raw-loop `runQuery` has no
declared result grain to check against (the model wrote ad-hoc SQL). **Decision: the D56 deterministic
gate applies to `runBlueprint` results only in this brick.** Raw-loop correctness is the model's job +
the D57 scope gate + transparency (SQL shown, D56 *"SQL stays visible"*). Extending a grain gate to
raw-loop results would require inferring a grain from arbitrary SQL — that is the Phase-2 static-gate
machinery (D37b), not this brick. This is faithful to D56 ("every *blueprint* result") and honestly
scoped.

---

## Q5 — `runBlueprint` the tool

**One-line:** A `RuntimeTool` with args `{id, slot_bindings}`, returning the verified final result (with
per-node progress but a single final answer), carrying the **union of every inner `runQuery`'s
provenance**, with a fail-closed error family and raw-loop fallback on any non-clean outcome.

### 5.1 Schema / params

```
runBlueprint(id: string, slot_bindings: object)   # slot_bindings = {slot_name: raw_value} (§3.1)
```
Locally-authored schema appended in `tool_schema.py` (like `resolveValues`/read tools); advertised
always; intercepted in the loop via the registry. The tool description instructs the model: fill slots
from conversation + user knowledge + `resolves`; the runtime validates/binds; a missing required slot or
ambiguous value will pause to ask the user.

### 5.2 Result shape

`result_full` = `{ "blueprint_id", "status": "verified", "columns", "row_count", "truncated",
"preview_rows", "sql": [<per-node SQL, for transparency>], "verify": {"grain_ok": bool,
"signature_ok": bool} }`. The model sees `result_preview` (preview-only, D46); the **full** result
persists to Couchbase + renders in the UI (D46). Per-node previews are surfaced only as **progress
events** (§5.5), not as separate results — the model gets **one final answer** (the blueprint's terminal
node output), matching "the runtime executes the DAG; the model never re-derives" (D10).

### 5.3 Provenance (decide: union of per-node runQuery provenance)

**Decision: the `runBlueprint` `ToolResult.provenance` = the union of every inner dispatched
`runQuery`'s captured provenance** (each node query + each slot-probe + each `resolve_via` inner query).
Rationale: this is the *actual* column footprint the execution touched, captured by the same
`capture_provenance` the dispatcher already runs per inner call — it is correct-by-construction and
identical to how `resolveValues` carries its inner query's provenance (D77 `D77-provenance-carried-from-
inner`). It is **more precise** than the D88(c) stored-`uses` footprint (which is the *whole DAG's*
declared USES); using the live union means the D44 replay filter drops the blueprint answer under scope
narrowing exactly to the columns it really used. Fail-closed: if any inner call has `None` (undetermined)
provenance, the union is `None` (the assistant message drops from replay — the `_compute_turn_provenance_
union` posture, `agent_loop.py:461`).

### 5.4 Error family + degrade discipline (fail-closed, raw-loop fallback)

- `RUN_BLUEPRINT_NOT_FOUND` — id absent OR out-of-scope (non-oracle, D88(b) — byte-identical, no scope
  probe). Retryable (re-search).
- `RUN_BLUEPRINT_SLOT_INVALID` — a required slot missing / unresolvable and **not** ask-able (should be
  rare; normally → pause). Retryable.
- `RUN_BLUEPRINT_UNSUPPORTED` — the DAG needs table-intermediate passing (F2) or a true bound-param
  surface (F1) the Phase-1 runtime can't provide → **fall back to raw loop.** Non-retryable-as-blueprint.
- `RUN_BLUEPRINT_VERIFY_FAILED` — D56 deterministic assertion failed → **fall back to raw loop** (§4.4).
  Retryable (via raw loop).
- Inner `runQuery` denials (`COLUMN_SCOPE_VIOLATION`, D64 scratch, D63 parse-fail) **pass through**
  verbatim with `tool_name="runBlueprint"` (the `resolveValues` §7 precedent) — never bypassed.
- `RUN_BLUEPRINT_INTERNAL_ERROR` — B4-parity last-resort crash guard (never `str(exc)`, logged
  server-side), the `_ReadTool._guarded` / `resolveValues._safe_run_inner` precedent.

**Fail-closed / degrade posture:** every non-clean outcome either **pauses** (needs user input) or
**falls back to the raw loop** (D56/D63/D80 discipline) — a `runBlueprint` failure is a *recoverable
annoyance* (the raw loop still answers), never a wrong answer and never a crash. This is the whole
point: the fast path is an optimization; its failure degrades to the always-available raw loop.

### 5.5 Observability (D25/D61)

One `TOOL` span for `runBlueprint`; nested per-node `runQuery` spans (via the dispatcher, ambient OTel
context — the `resolveValues` nesting precedent). Progress events (D61): "running blueprint step 2 of 4:
department actuals…", "verifying results…" — **step/shape only, no cell values, no bound slot values**
(D25/D61 PII-safe). The model-authored `slot_bindings` values are **redacted** from spans/progress
(the `concept`/`query` redaction precedent, D25 — `redact_tool_args` extended for `runBlueprint`); the
bound node/probe SQL literals are masked by the existing `mask_sql` on the inner `runQuery` spans.

**Scope of the "no slot values in progress" claim (Slice-B clarification, reviewer redaction gray-area,
position (a)):** the claim covers **telemetry-only surfaces** — span attributes and the `blueprint_step`
progress payloads the executor emits — which carry step/shape only and never a slot value. It does **not**
cover a `no_match`/`multi_match` **clarification question**: that question is *inherently user-facing* (it
asks the user to confirm THEIR OWN proposed value, e.g. "I couldn't find 'Marketing' — which did you
mean?"), and echoing the user's own input back to them is not a leak. Such a question is delivered through
the same `loop_paused_ask_user` path as `askUser`, whose `pending_question` is by design the user-facing
prompt (never a warehouse *resolved/bound* value — those are what §5.5 redacts). So the precise invariant
is: **no resolved/bound domain value and no result cell ever reaches a span or a progress payload**; a
clarify question may echo the user's *own* proposed slot value, exactly as `askUser` does. (QA5 pins this
as the conscious record: `test_no_match_pause_question_echoes_value_into_progress_event_flag`.)

---

## Q6 — The two owed items (D88-deferred, QA-pinned)

### 6.1 Runtime/MCP tool-name collision guard (at registration)

Today `fetch_function_schemas` (`tool_schema.py:194`) **blindly appends** local schemas
(`askUser`, `resolveValues`, the read tools, and now `runBlueprint`) after the MCP-fetched tools. If the
MCP ever advertises a tool named `runBlueprint` (or any local name), the model would see **two schemas
with the same name** — and worse, the loop's `runtime_tools` interception (`agent_loop.py:619`) would
silently shadow the MCP tool, or vice-versa, depending on dispatch order. **Decision: add an explicit
assertion in `fetch_function_schemas` that the set of locally-authored tool names is disjoint from the
MCP-advertised tool names; a collision raises at startup (fail-closed, loud)** — a config/deploy error
surfaced at schema-fetch, never a silent runtime ambiguity. The runtime-tool registry is the source of
truth for "these names are ours." This is cheap, reversible, and closes the QA-pinned gap.

### 6.2 Duplicate-tool-call-id replay semantics

The trail stores `tool_call_id` per entry; replay (`_tool_trail_entry_to_canonical`, `agent_loop.py:236`)
rebuilds an `[assistant-with-tool_calls, tool]` pair keyed by that id. **The OpenAI API requires every
`tool_call_id` in a turn to be unique and each to have exactly one matching `tool` response.** A model
that emits two tool calls with the same `id` in one response (or a resume that re-appends an entry with a
colliding id) would produce **two `tool` messages with the same `tool_call_id`** on replay → an API 400
that aborts the turn. This matters more for `runBlueprint` because a paused-and-resumed DAG re-enters and
appends new trail entries. **Decision:**
- **At write:** `append_trail_entry` deduplicates by `tool_call_id` within a turn — a colliding id is a
  contract violation; the runtime **rejects the second** (logs a warning, keeps the first) rather than
  persisting an un-replayable trail. The loop's per-iteration dispatch already assigns ids from the
  model's tool-call ids; a well-behaved model never collides, but a misbehaving one must not poison the
  trail.
- **At replay:** `_assembled_to_canonical` asserts `tool_call_id` uniqueness defensively and drops a
  duplicate pair (keeping the first) so a legacy/corrupt trail can never emit an API-invalid message
  list. Fail-closed toward a *valid* (if lossy) replay, never a turn-aborting one.

Both are Layer-1-testable pure-ish logic; both are small.

---

## Q7 — Slicing (fakes-first, Layer-1-green per slice; mirrors the retrieval bricks)

Each slice ships Layer-1-green with fakes before its Layer-2/3 legs; the D68 release gate is Layer-3.

### Slice A — Storage + pure-function foundations (no execution yet)
**Scope:** §1 full-DAG storage (corpus schema + loader validation + fixtures + `getBlueprint`
expansion); the `SemanticCatalogHandle` (F3); the `blueprint/models.py` parse layer; the pure
resolvers `blueprint/slots.py` (against fakes/probes stubbed); the D56 **deterministic** assertion
`blueprint/verify.py` (pure grain-integrity + signature check over a supplied result); the sqlglot
slot-binding `blueprint/template.py` (F1); the `when` evaluator (§2.6); the two owed items (§6, both
pure). **Tests (Layer-1):**
- `D56-wrong-grain-falls-back` (unit half): a wrong-grain result ⇒ assertion FAIL (the fan-out canary).
- `D49-resolver-no-llm-multi-match-asks-user`: resolvers are pure code; multi-match/fuzzy ⇒ askUser
  signal, **never an LLM call**.
- template binding: adversarial slot value ⇒ ClickHouse-escaped AST literal, `{slot}` never string-
  interpolated (the `D77-concept-never-in-sql` sibling).
- `when` evaluator: declarative semantics; entity-valued predicate ⇒ rejected (non-agnostic).
- corpus loader: full-DAG round-trip; malformed template / undeclared slot / column ⊄ uses / cycle ⇒
  `CorpusLoadError`; `getBlueprint` returns the additive fields (extends `D88-getblueprint-non-oracle`
  — still byte-identical `{found:false}` out-of-scope).
- §6: name-collision guard raises; duplicate-tool-call-id dedup at write + replay.
**Layer-2:** corpus loader vs live neo4j (full-DAG properties persist + read back; recall unchanged).

### Slice B — Single-node execution + the tool + verify gate wiring
**Scope:** `BlueprintExecutor` for **single-node** blueprints (trivial topo order); per-node dispatch
through `ToolDispatcher` (D57/D64/D5 free); slot resolvers wired to real probe queries; the D56 gate
wired (deterministic FAIL ⇒ raw-loop fallback); `RunBlueprintTool` + registry entry + error family +
provenance union + redaction; the **one loop affordance** for a pausing runtime tool (§2.5 seam) — but
approval nodes come in Slice C, so Slice B exercises the pause seam only via a required-slot `askUser`.
**Tests (Layer-1):**
- `D56-wrong-grain-falls-back` (full): executor runs the wrong-grain fixture ⇒ gate FAILs ⇒
  `RUN_BLUEPRINT_VERIFY_FAILED` ⇒ never returns the number.
- `D5-scope`/`D57`: an out-of-scope column in a node query ⇒ inner denial passes through as a
  `runBlueprint` denial, never bypassed.
- provenance = union of inner runQuery provenance; `None` if any inner undetermined (fail-closed).
- one `runBlueprint` = one `tool_calls_made`; inner probes/queries not double-counted.
- `RUN_BLUEPRINT_NOT_FOUND` non-oracle (out-of-scope == absent).
- redaction: `slot_bindings` values absent from every span/progress payload (real-OTel e2e).
**Layer-2:** `runBlueprint` executor vs real `clickhouse-api` MCP↔ClickHouse + neo4j — single-node
`avg_salary_by_dept` end-to-end, scope-enforced; verify gate runs live.

### Slice C — Multi-node (scalar-passing) DAG + approval pause/resume + D67 + Layer-3
**Scope:** topo walk over ≥2 nodes with **scalar** intermediates (F2 — table passing explicitly
rejected); `when` clauses wired (`abort`/`skip`/`ask`); `approval` gates (D59b upstream-only `show`) →
pause via the extended `PauseCheckpoint` (§2.5); D45 mid-DAG resume at `awaiting_node` (completed nodes
never re-run); **D67 `resolve_via`** wired through `resolveValues.resolve()` (§3.4) incl. degrade/askUser
(D66c/D85). **Tests:**
- Layer-1: scalar-intermediate binding as typed AST literal; `resolve_via` expands via `resolve()`, no
  model round-trip, binds code set as `IN`-list literal; degraded resolve ⇒ askUser; empty resolve ⇒
  raw-loop fallback; `when on_violation` branches.
- Layer-2: `D45-pause-resume-survives-restart` (mid-DAG approval ⇒ checkpoint ⇒ resume at
  `awaiting_node`, exactly-once CAS); `D59a-scalar-intermediate-as-typed-param` (scalar passing);
  `D59b-approval-upstream-only-reference`.
- **Layer-3 conformance (D68 red burndown — the headline unlock):**
  `No-silent verification` (D56 — wrong-grain fixture caught/falls back, never returned silently);
  `Ask → fast path` (D61 — "Using blueprint…" chip + progress stream + SQL shown);
  `Pause/resume durability` (D45 — approval pause ⇒ restart container ⇒ resumes at `awaiting_node`);
  `Scope denial` (out-of-scope blueprint not surfaced / not runnable);
  `Ask → clarify` (D49 — a slot multi-match renders a clarification chip).

### Out of scope for this brick (explicit)
- **Table-intermediate DAG passing (F2/D59a)** — needs a `clickhouse-api` scratch-write surface
  (Track A). New OQ (§8). The convergence-DAG example in `04-blueprints.md` waits on it.
- **True server-side bound params (F1/D41 literal)** — a `clickhouse-api` `runQuery` param surface
  (Track A). Phase-1 uses typed-AST-literal binding; security-equivalent.
- **Static authoring gate D37b** (reject wrong-grain blueprints *before* storage) — **Phase 2** (D56
  makes runtime the launch defense; authoring gate is fast-follow).
- **Scheduled drift probes #2 (catalog-conformance) + #3 (rule-currency)** — **Phase 2** (D43); only
  probe #1 (grain-integrity) runs, at runtime, this brick.
- **Blueprint *authoring / learning loop*** — extraction (D34/D35), dedup/`canonical_key` (D48),
  promotion/lifecycle, golden-replay validation. Track B; corpus stays hand-seeded/trusted (D87/D88).
- **Measure-`agg` structural verification** — `defined_over` is prose (F3/§4.3); deferred to D37b.
- **Period-range slots / period-dimension table / NL-relative period resolution** — carried OQs;
  Phase-1 does explicit-named + latest-settled + `askUser` on fuzzy.
- **Parallel branch execution** — sequential in Phase 1 (perf-only, §2.2); correctness identical.

---

## Q8 — Open questions with interim defaults

| OQ | Interim default (this brick) | Revisit when |
|---|---|---|
| **`skip`/`skip_remaining` in a converge graph** (skip-node vs skip-dependents; "remaining" by what order) | **`skip`** = skip this node + mark its `output` empty; downstream `when`/`consumes` must tolerate empty (they already can via `empty()`). **`skip_remaining`** (approval `on_deny`) = stop the DAG, return best-partial upstream result to the verify gate. "Remaining" = all nodes after `awaiting_node` in topo order. | table-passing DAGs land (F2) — richer skip needed |
| **`slot_bindings` payload shape** | flat `{name: raw_value}` (§3.1); list for `IN`/enum-list slots | list/period-range slots (carried OQ) |
| **Result-signature invariant set** | column-shape + type + declared-grain row-count (§4.3) | Phase-2 golden/replay defines the full invariant set |
| **`result_grain` authoring** | a required stored `result_grain_json` per blueprint (§1.1) | D37b static gate can *derive* it from the AST |
| **Table-intermediate materialization (F2)** | rejected at load + execution (`RUN_BLUEPRINT_UNSUPPORTED` → raw loop) | `clickhouse-api` scratch-write surface exists |
| **True bound params (F1)** | typed-AST-literal binding (security-equivalent) | `clickhouse-api` `runQuery` param surface exists |
| **Deictic/relative period** | `askUser` (D49 — never guessed) | period-dimension-table lands |
| **DAG node parallelism** | sequential (§2.2) | perf pass (shared with the §11 tool-dispatch-parallelism OQ) |
| **`resolve_via` empty result** | fall back to raw loop (never silently drop the filter, §3.4) | rule-authoring convention firms up |

---

## 9. Risks & trade-offs (named explicitly)

- **The engine's docs describe a surface the MCP lacks (F1/F2).** Biggest risk: someone reads
  `04-blueprints.md` "server-side params" / "scratch JOIN" and builds against a non-existent MCP
  surface. Mitigation: F1/F2 are §0 headlines; the doc-update list (§10) aligns `04-blueprints.md`.
  Trade-off accepted: Phase-1 blueprints are single-node or scalar-converging — a real, large subset —
  and the security boundary (D10) is fully preserved via AST-literal binding.
- **`defined_over` prose limits the D56 measure check (F3/§4.3).** The row-count grain check catches the
  canonical fan-out, but a subtler measure-agg error (wrong `agg`, right rowcount) is not caught until
  D37b (Phase 2). Trade-off: D56's *teeth* (row-count) ship now; the *belt* (measure-agg) is Phase 2.
  This is faithful to D56's own phasing (runtime gate now, authoring gate Phase 2).
- **The one new `AgentLoop` affordance (a pausing runtime tool, §2.5).** Until now only `askUser`
  pauses. Generalizing the pause seam is the single non-trivial loop change; it must not regress the
  `askUser`/`budget_cap` paths. Mitigation: additive `PauseCheckpoint` fields + a distinct pause
  `reason`; existing paths untouched; Layer-1 regression on `test_agent_loop.py`.
- **Provenance union vs stored `uses` (§5.3).** Choosing the *live* union (more precise) over the
  D88(c) stored footprint diverges slightly from the read-tools posture. Trade-off: precision (drops
  from replay exactly to columns used) at the cost of one more thing to reason about; justified because
  the executor *has* the real inner-provenance for free.
- **Verify-gate cost (D56 accepted).** Every fast-path answer pays an extra distinct-grain probe query +
  the LLM review round-trip. D56 explicitly accepts this ("trust is the product"). Latency masked by
  D61 progress streaming.

---

## 10. Doc/code updates on completion (NOT done this pass — enumerated per the brief)

**Docs:**
- `04-blueprints.md` — (a) reframe "server-side query parameters" → "typed sqlglot-AST-literal binding
  (F1; no `runQuery` param surface in Phase 1)"; (b) mark table-intermediate scratch passing (D59a) as
  **deferred** (F2, gated on `clickhouse-api` scratch-write); (c) note the §Execution `result_grain`
  declaration requirement; (d) update the §measures example to `{column, agg, defined_over}` (prose)
  matching the catalog reality (OPEN-QUESTIONS already flags the stale example); (e) flip §Storage
  "full-DAG storage reserved" → **built (this brick)**; (f) status Partial → note single-node +
  scalar-converge executable, table-passing deferred.
- `02-tools-and-api.md` — `runBlueprint` row: finalize the `slot_bindings` shape (§3.1); resolve the
  standing "exact `slot_bindings` shape" OQ.
- `03-context-and-retrieval.md` — progressive-disclosure step 4 now fully live (getBlueprint expands
  the DAG; runBlueprint executes it).
- `DECISIONS.md` — a new locked decision (next Dnn) recording: F1 (AST-literal binding not server-side
  params), F2 (table passing deferred), the D56 runtime gate scoping (grain row-count now, measure-agg
  Phase-2/D37b), the `slot_bindings` shape, the DAG=one-tool-call budget rule, provenance=live-union,
  §6 owed items resolved, the pausing-runtime-tool loop affordance. Amends/extends D59a (table passing
  deferred), D45 (PauseCheckpoint additive fields), D41 (binding mechanism clarified).
- `OPEN-QUESTIONS.md` — close OQ-T1 (full-DAG storage, name-collision guard, dup-tool-call-id) as built;
  open the two new Track-A OQs (scratch-write surface F2, runQuery param surface F1); update §Blueprints
  skip/skip_remaining + slot_bindings + result-signature to "interim-defaulted (this brick)".
- `TRACEABILITY.md` — move `D56-wrong-grain-falls-back`, `D56-verify-gate-always-runs`,
  `D45-pause-resume-survives-restart`, `D59a-*`, `D59b-approval-upstream-only-reference`,
  `D49-resolver-no-llm-multi-match-asks-user` from ⛔ not-built toward 🟡/✅ as each slice lands; add
  rows for the §6 owed items and the new decision.
- `11-testing.md` — the `runBlueprint` executor Layer-2 row + the four unlocked Layer-3 scenarios move
  from Partial toward green; the wrong-grain + composite/approval fixtures get authored.

**Code (net-new, additive):** `runtime/blueprint/` (models, executor, slots, verify, tool, template);
`SemanticCatalogHandle` (F3, in `provenance/catalog_handle.py` or a sibling); `PauseCheckpoint` +
`SessionStore` additive fields (§2.5); the `AgentLoop` pausing-runtime-tool seam (§2.5) + dup-id dedup
(§6.2); `tool_schema.py` `runBlueprint` schema + name-collision guard (§6.1); `corpus_loader`/
`vector_index`/`BlueprintDetail` full-DAG additive fields (§1); `redact_tool_args` extended for
`runBlueprint`.

---

## Appendix — what in the docs materially constrains the engine (for the orchestrator)

1. **D49 is absolute: no LLM inside `runBlueprint`.** Every resolver is pure code + scope-enforced
   probe queries. The D56 LLM *review* is an ordinary loop round-trip *after* the tool returns, not a
   nested call — this is the only way to satisfy both D49 (deterministic tool) and D56 (two-part gate).
2. **D57/D64/D5 come free only if every node query goes through `ToolDispatcher.dispatch("runQuery")`.**
   A direct MCP call would bypass column scope, scratch isolation, and provenance. Non-negotiable.
3. **D56 has no silent path and the deterministic check is the teeth** — a wrong-grain number is
   shape-correct, so the LLM can't catch it; the row-count grain check must run and block. It needs
   `result_grain` declared (§1.1) and `SemanticCatalogHandle` wired (F3).
4. **D10 injection boundary holds via AST-literal binding, not server-side params (F1)** — the same
   mechanism `resolveValues` already proved (`D77-concept-never-in-sql`).
5. **D45 exactly-once resume** already exists for `askUser`/`budget_cap`; mid-DAG resume is an additive
   `PauseCheckpoint` extension + the executor re-entry at `awaiting_node`. Scalar-only Phase-1 passing
   means no scratch-reconnect (D55) complexity — a real simplification F2 buys.
6. **Table passing (D59a) and true params (D41) are blocked on `clickhouse-api` (Track A) surfaces that
   do not exist** — the honest boundary of what this brick can execute (single-node + scalar-converge).
