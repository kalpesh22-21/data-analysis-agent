# Catalog YAML — Authoring Guide & Change Notes

> For the agent that generates the `getTableSchema` semantic YAML. Read this before
> (re)generating any table file. It explains **what we changed in the first pass and why**, and the
> **conventions to follow** so future files are correct by construction.

The catalog is the single most correctness-critical artifact in the system — it grounds every user's
answer and every blueprint. These files are the *curated* half of `getTableSchema` (the other half is
live ClickHouse introspection).

---

## What changed in review (and why)

### 1. `temporal` is now a **list of dimensions**, not a single `semantics` enum

**Old (don't use):**
```yaml
temporal: { semantics: per_period, period_col: pay_period }
temporal: { semantics: as_of_now }
```
**New:**
```yaml
temporal:
  dimensions:
    - { name: <id>, role: period, period_col: <col> }                      # discrete period key
    - { name: <id>, role: period, range: { start_col: <c>, end_col: <c> } } # period as a date range
    - { name: <id>, role: as_of,  effective_col: <col> }                   # effective-dated state
    - { name: <id>, role: event,  date_col: <col> }                        # transaction/event timestamp
  default_pin: <dimension name | null>
```
**Why:** the single enum couldn't express two real shapes we hit in payroll — a **ranged** period
(`PayPeriodStartDate`/`EndDate`, with *no* single key column) and a **dual-temporal** table (a
`PayDate` *event* **and** a coverage *period*). The dimensions model handles any number of temporal
dimensions.

**The rule (the whole point of this block):** a blueprint reading a table with ≥1 temporal dimension
must **constrain at least one** of them (a filter or `GROUP BY` on its column(s)). Pinning *any*
dimension bounds the time window; pinning **none** is the failure (e.g. averaging 12 years of payroll
into one number). `default_pin` just says which dimension is filled by default.

**Mapping the old shorthands:**
| Old | New |
|---|---|
| `semantics: as_of_now` | `dimensions: []`, `default_pin: null` |
| `semantics: per_period` + `period_col` | one `role: period` dimension; `default_pin` = it |
| `semantics: as_of_date` + `effective_col` | one `role: as_of` dimension |

### 2. `rules` must be **filter predicates**, not field-selection guidance

Each `rules` entry is a named **boolean SQL fragment** that can be ANDed into a query (it's injected
verbatim). Two kinds of mistakes were removed:
- **Not a predicate.** `predicate: "use MostRecentHireDate"` is guidance, not a filter — it was
  deleted. Field-defaulting belongs in **`ambiguities`** (which already carried it).
- **Incomplete boolean.** `predicate: "MostRecentHireDate >= TerminationDate"` was rewritten into a
  complete, composable, null-safe boolean with a note on how to compose it.

> If something is "which column/value to use," it's an **ambiguity/resolves**, not a rule.
> If something is "which rows to keep," it's a **rule** — and it must be valid SQL.

### 3. No contradictory defaults

Two entries claimed different defaults for the same term (`headcount`: one rule said "exclude only
not-hired", the ambiguity said "active-only"). Fixed the `applies_when` so they compose with clear
precedence. **Before adding a rule/ambiguity, check nothing else already defines a default for that
term.**

### 4. Removed `observed:` blocks

Row counts / min-max dates / distinct counts are **operational stats**, not semantics. They go stale,
aren't used by any gate, and don't belong in the curated catalog. Don't emit them.

---

## Conventions to follow

**Keep these blocks** (per table):
- `database`, `table`, `description`
- `grain: [...]` — the column set that makes a row unique **within the agent's query view**. Tenancy
  is enforced below the agent by ClickHouse row-level security (see Tenancy under General), so the view
  is always a single tenant — **do not add `ClientCode`** to grain; use the within-tenant key
  (e.g. `[EmployeeCode]`, not `[ClientCode, EmployeeCode]`).
  - **No exposed unique key** (log / line-item / field-response tables): do **not** invent a
    placeholder token (`grain: [ some_made_up_row_name ]` breaks the gates — they run
    `COUNT(DISTINCT <grain cols>)` as SQL). Declare `grain: []` + `grain_verifiable: false`, and put
    the logical analysis grain(s) in `grain_note`. The D43 catalog-vs-warehouse conformance probe
    (`COUNT(*) == COUNT(DISTINCT grain)`) is **skipped** for `grain_verifiable: false` tables;
    per-blueprint result-grain checks (D56) still apply to those blueprints' own declared result grain.
    If a reliable logical composite key *does* exist, prefer declaring it (e.g. PAF's
    `[ClientCode, PafTransactionId, FieldId]`) over `grain: []`.
- `temporal:` — the dimensions model above.
- `primary_key` (+ `primary_key_note` if none is reliable).
- `join_keys` — each `{ column, joins: <table>.<col>, cardinality }`. Cardinality must be consistent
  on both sides (employee `1:N` payroll ⇔ payroll `N:1` employee).
- `columns:` — per column `type` (real ClickHouse type incl. `Nullable(...)`), `description`, and
  where useful `synonyms`, `unit`. For enums:
  - **`values`** is a **map** `{ code: meaning }` — use it when codes are **opaque** (`A: active`,
    `EARN: earnings`). Do **not** put a bare list under `values`.
  - **`observed_values`** is a **list** of values actually seen — use it when values are
    self-describing (`[ Draft, Resolved, Archived ]`) or to record the observed subset of a `values`
    map. Mark sensitive PII columns with a structured **`sensitive: true`** flag (mirrors
    `client_defined: true`) — this is the catalog source of truth for the column-scope / governance
    layer, not just prose. Keep a human-readable note in `description` too.
- `measures:` — **only for fact-table measures** that get aggregated. Shape:
  `{ column, agg, defined_over }`. The measure name may be **conceptual** (e.g.
  `approved_time_off_hours`), so `column` names the underlying column (or `"*"`). `agg` is a
  **normalized token**: `sum | count | count_distinct | avg | min | max`. `defined_over` is free-text
  stating both the **scope filter** and the **aggregation grain** the measure is meaningful at — for
  filter-scoped measures (e.g. payroll `earnings = sum(Amount) where RegisterType='EARN'`) you **must**
  include the register/type filter; don't declare a clean `defined_over` that ignores it. (`defined_over`
  is read best-effort by the grain gate; keeping it precise is what makes the de-fan check meaningful.)
- `rules:` — filter predicates only (see above): `{ id, predicate, applies_when, description }`.
  Two kinds:
  - **Static rule** — `predicate` is a fixed SQL boolean (`EmployeeStatus = 'A'`).
  - **Resolved (dynamic) rule** — `predicate` references `resolveValues(<client_defined column>,
    '<concept>')`; the runtime resolves it to a concrete `IN (...)` set **per client** before the
    query runs. Use this instead of hardcoding client-defined codes (e.g. PAF `FieldId`, `EarnCode`,
    `TypeCode`). Never embed one client's literal codes in a rule.
- `ambiguities:` — `{ term, resolves_to, default, clarify_if }`. This is where "which field / which
  interpretation" lives. Add a `salary`-style cross-table ambiguity whenever a term maps to more than
  one column/table (e.g. planned/annual salary vs actual paid earnings).

**Client-defined code columns (`EarnCode`, `TypeCode`, departments, …):**
- These enums are **client-defined and drift over time**, so do **not** enumerate their values in a
  static `values` map. Instead mark the column `client_defined: true` (optionally `resolve_via: tool`).
- For concept terms over them (`PTO`, `sick_time`, `leave`, payroll register types…), the ambiguity
  should **point at the resolver tool**, not list codes:
  `resolves_to: "resolveValues(<column>, <concept>)"` — the runtime returns the matching codes for
  the asking client (D66). Keep `clarify_if` for genuinely ambiguous concepts (e.g. "PTO" maybe
  including Vacation / Floating Holiday).
- **Filter rules over a client-defined column** ("which rows to keep" → a rule, not an ambiguity) must
  **not** embed the tool call in the predicate — `FieldId IN resolveValues(...)` is not valid SQL and
  cannot be injected verbatim. Write the predicate as a **bound parameter** and declare the resolution
  separately: `predicate: "FieldId IN ({field_codes})"` + `resolve_via: "resolveValues(FieldId,
  <concept>)"`. The runtime resolves the concept to a code set and binds it as a parameter (D10: never
  string-interpolated). See the `*_change_fields` rules in `personnel_action_form_changes.yaml`.

**File organization:**
- **One file per table.** Do **not** bundle multiple tables (multiple `---` YAML documents) into one
  file — `getTableSchema(database, table)` loads per table.
- **Filename must equal the table name** (`performance_discussions.yaml`, not
  `performance_discusions.yaml`). Watch spelling.

**General:**
- **`clarify_if` / `applies_when` are model-interpreted prose, not engine triggers.** The agent reads
  them to decide whether to clarify or whether a rule applies; there is no deterministic
  keyword/condition engine behind them. Write them as clear natural-language conditions. The
  deterministic gates are grain, column scope, and parameter binding — not `clarify_if`.
- **Tenancy (multi-tenant warehouse, RLS-isolated below the agent).** The warehouse holds multiple
  `ClientCode`s, but isolation is enforced in **ClickHouse row-level security** via a custom
  `client_code` setting inferred from the JWT and applied by the MCP — **not** by the agent. The agent
  never sees, supplies, or filters `ClientCode`, and every query (blueprint SQL, raw loop, the D43
  conformance probe) runs against a **single-tenant view**. Catalog consequences: grain and joins use
  **within-tenant keys only** — do **not** add `ClientCode` to `grain`/`primary_key`, and do **not**
  add `ClientCode` equality to join predicates. `ClientCode` stays a real, selectable column
  (informational); it is simply never a filtering or keying concern for the agent.
- Entity-agnostic in spirit: describe the data layer and business rules, not specific people.
- Prefer declaring an ambiguity + `clarify_if` over silently guessing.
- Real ClickHouse column names and types, exactly as in the warehouse.

---

*Reference: the locked design lives in `docs/` — temporal model = D65 (revises D40); rules/grain =
D37; ambiguities drive `resolves` + the `askUser` clarify trigger.*
