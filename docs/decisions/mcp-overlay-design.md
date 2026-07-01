# `getTableSchema` / `sampleRows` MCP-overlay design (D83/D84)

**Status:** Draft for review — accompanies the D83/D84 amendments in
[DECISIONS.md](DECISIONS.md#semantic-catalog). Supersedes the runtime-side overlay plan D78 had
implied for [phase0-runtime-design.md](phase0-runtime-design.md) Phase 1 (see that doc's line "Out of
scope … D78 `getTableSchema` catalog overlay").

**Scope:** design only. No code changes are made by this document; `clickhouse-api` is not edited.
This document is the brief for whoever implements D83/D84 in `clickhouse-api`.

**Repos referenced:**
- `clickhouse-api` (`/Users/kalpeshmulye/Development/clickhouse-api`) — where the changes land.
- `data-analysis-agent` (this repo) — holds the Semantic Catalog YAML (`databaseSchemaDocs/*.yaml`)
  and the existing runtime-facing loader (`src/data_agent/catalog/loader.py`).

---

## 0. What's reversed, in one paragraph

D78 moved the `introspection ⨝ catalog` join for `getTableSchema` out of the MCP and into the agent
runtime, on the theory that the MCP should stay a thin data plane. D83 reverses that: the join (and,
new this time, **scope-filtering** of the joined result) moves back into `clickhouse-api`. The
practical trigger is that the MCP already needs a catalog-shaped schema to do D57 column-scope
enforcement (`app/catalog.py`'s interim `system.columns`-derived `{col: type}` dict feeding
`extract_column_provenance`) — so "the MCP is catalog-free" was never quite true, and maintaining two
divergent catalog loaders (a rich one in the runtime, a thin one in the MCP) is more moving parts than
upgrading the one the MCP already has.

---

## 1. `getTableSchema` (MCP)

### 1.1 Pipeline (fixed ordering: merge, then filter)

```
1. Introspect:      SELECT name, type, comment FROM system.columns WHERE database=.. AND table=..
                     (unchanged — app/service.py:get_table_schema, current behavior)
2. Load catalog:    look up "{database}.{table}" in the richer semantic-catalog loader (§3)
3. MERGE:           introspection ⨝ catalog, keyed on column name (case-sensitive exact match,
                     consistent with D70's no-case-fallback rule)
4. SCOPE-FILTER:    drop any column whose "{database}.{table}.{column}" ∉ column_scope
                     (skip this step entirely if column_scope is None [stdio/local-trust] or
                     empty-frozenset [allow-all, D80b])
5. Return the filtered, merged shape (§1.2) to the caller.
```

**Why merge-then-filter, not filter-then-merge:** filtering first would require scope-checking against
*introspection* column names before their catalog semantics are attached — fine for columns, but the
table-level blocks (`grain`, `rules`, `ambiguities`, `join_keys`, `measures`) reference columns by name
in ways introspection alone can't validate membership against (e.g. a rule's predicate string). Merging
first gives one column-addressable structure to filter uniformly (§1.4). This also matches the order
D57 already uses for `runQuery` (extract full provenance, then compare to scope), so the same mental
model applies to both tools.

### 1.2 Merged (pre-filter) response shape

```jsonc
{
  "database": "dbpcm_warehouse",
  "table": "employee",
  "catalogued": true,                 // false → structural-only path, see §1.3
  "description": "Current-state employee record, one row per employee...",
  "grain": ["EmployeeCode"],
  "temporal": { "dimensions": [], "default_pin": null },
  "primary_key": ["EmployeeCode"],
  "join_keys": [
    { "column": "EmployeeCode", "joins": "payroll.EmployeeCode", "cardinality": "1:N" }
  ],
  "columns": [
    {
      "name": "EmployeeCode", "type": "String", "comment": "",
      "description": "Stable unique employee identifier; canonical employee join key.",
      "synonyms": [], "unit": null, "values": null, "observed_values": null,
      "client_defined": false, "sensitive": false
    },
    {
      "name": "AnnualSalary", "type": "Nullable(Decimal(18, 6))", "comment": "",
      "description": "Annual salary amount when applicable; not actual payroll paid.",
      "unit": "USD", "client_defined": false, "sensitive": true
    }
    // ... one entry per column that survives scope-filtering
  ],
  "measures": [
    { "name": "gross_earnings", "column": "Amount", "agg": "sum",
      "defined_over": "RegisterType = 'EARN'; summed over line items within (EmployeeCode, pay period)" }
  ],
  "rules": [
    { "id": "active_employee", "predicate": "EmployeeStatus = 'A'",
      "applies_when": "user asks for active/current active employees",
      "description": "Restrict to active employees." }
  ],
  "ambiguities": [
    { "term": "headcount", "resolves_to": ["COUNT(DISTINCT EmployeeCode) with EmployeeStatus = 'A'"],
      "default": "active_only", "clarify_if": "..." }
  ]
}
```

Notes:
- `columns[].name/type/comment` are the **introspection** fields (unchanged from today's
  `get_table_schema`); everything else on a column is the catalog overlay.
- `sensitive` is surfaced, not enforced by this response — it's informational for the model/UI (per
  D42(d), rules/flags are a correctness convention, not the access boundary; column-scope, not
  `sensitive`, is the boundary). It is still worth exposing since it can drive UI treatment
  (masking hints) downstream.
- A column not present in the catalog's `columns:` block but present in introspection is still
  returned with introspection fields only + nulls for catalog fields (this is the **column-level**
  analog of the table-level "no catalog entry → structural-only" rule; the table has *a* catalog
  entry, this one column just isn't documented in it).

### 1.3 Uncatalogued-table path (restates D53(c), now MCP-side)

If `"{database}.{table}"` has no entry in the semantic catalog at all:
- `catalogued: false`
- All catalog-derived fields are omitted/null: no `description`, `grain`, `temporal`, `primary_key`,
  `join_keys`, `measures`, `rules`, `ambiguities`; `columns[]` carries only introspection fields.
- This is **not an error** — the MCP still returns whatever introspection it has (unchanged from
  today), same as D53/D78 originally specified, just executed in the MCP now instead of the runtime.
- Scope-filtering (§1.4) still applies to the (structural-only) `columns[]` list — an uncatalogued
  table is not exempt from column scope.

### 1.4 Scope-filtering rule (uniform, block-level, no partial redaction)

**Column list:** drop any column whose qualified name (`database.table.column`) is not in
`column_scope` (empty scope = allow-all per D80b; `None` = enforcement disabled, stdio/local-trust).
Dropping a column removes its introspection fields *and* its catalog overlay fields together — there
is no "hide description but keep type" partial state.

**Table-level metadata blocks that reference columns** (`grain`, `primary_key`, `join_keys`,
`measures`, `rules`, `temporal.dimensions[].*_col`, each `ambiguities[].resolves_to` entry that names a
column) are filtered by the same rule, applied per-entry:

> If a block-entry references **any** column not in scope, drop that entire entry from the response.
> Never emit a partially-redacted predicate string (e.g. never turn
> `"EmployeeStatus = 'A'"` into `"<redacted> = 'A'"` — either the whole rule ships or it doesn't).

Concretely:
- `grain: [EmployeeCode]` — if `EmployeeCode` is out of scope, drop the whole `grain` array (emit
  `grain: null` or omit the key) rather than an empty/partial array. This is the answer to the
  Deliverable-2 question about whether table-level semantics are withheld when measure columns are out
  of scope: **yes, but derived from one general column-reference rule, not a special "all measures
  out" case.** If `grain` names a column that's out of scope, the model loses the grain-integrity gate
  it needs for that table anyway — a partial grain vector would be actively misleading (it would look
  valid but not represent uniqueness), so withholding it fails safe.
- `rules[]` — drop any rule whose `predicate` references an out-of-scope column. Requires a name
  extraction over the predicate string (reuse the D62 `sqlglot` parser configured for expression-only
  parsing, or a lighter identifier-extraction regex scoped to the catalog's own column vocabulary for
  that table — sqlglot is already a dependency post-D75, so prefer it for consistency with D57/D69).
- `ambiguities[]` — filter `resolves_to` entries individually (a `salary` ambiguity might resolve to 4
  different columns/tables across different scope levels); drop entries referencing out-of-scope
  columns/tables, keep the rest. If **all** `resolves_to` entries for a term are dropped, drop the
  whole ambiguity entry (nothing left to disambiguate to).
- `measures[]` — drop any measure whose `column` is out of scope.
- `join_keys[]` — drop any join-key entry whose `column` is out of scope (this also hides a join path
  to another table the caller can't use anyway).
- `temporal` — if the dimension's `period_col`/`effective_col`/`date_col` is out of scope, drop that
  dimension (and `default_pin` if it becomes meaningless without it); if `dimensions: []` already
  (no temporal columns), nothing to filter.

This single rule (column-reference ⊆ scope, else drop the whole entry) is deliberately the *only*
filtering primitive — it composes cleanly across every catalog block without bespoke per-field logic,
and it's the same "referenced columns ⊄ scope ⇒ reject/drop" shape D57 already uses for `runQuery`, so
the mental model transfers.

#### 1.4.1 Implementation amendment (post-implementation security review)

The initial `clickhouse-api` implementation of this section (`app/semantic_catalog/overlay.py`) shipped
with three fail-open gaps in the free-text predicate/resolution scan, found by a security review + QA
pass and since fixed in the same file. This subsection supersedes the "errs toward keeping" framing
above and in the original `rules[]`/`ambiguities[]` bullets with the corrected, fail-closed posture:

- **(a) Scan vocabulary is the introspected column universe, not the catalog's `columns:` block.** A
  column can be a real, live database column (e.g. `SSN`) that is present in `system.columns`
  introspection but undocumented in the catalog YAML's `columns:` block (§1.2's normal "undocumented
  column" case). The free-text scan's per-table vocabulary — used to decide whether a `rules[].predicate`
  or `ambiguities[].resolves_to` string references that column — must be the table's *introspected*
  columns, and the cross-table index used to resolve another table's column name must be built from
  *every* table's introspected columns, not from the catalog's documented subset. Scoping the vocabulary
  to catalog-documented columns only makes an undocumented column's rule/ambiguity references invisible
  to the scan, and the reference (with the out-of-scope column's name and predicate/resolution text)
  ships regardless of scope.
- **(b) `rules[].predicate` is parsed with sqlglot (the design's original recommendation), fail-closed.**
  The predicate is parsed as a ClickHouse-dialect SQL expression and its `Column` AST nodes are extracted
  and resolved against (this table's introspected columns) ∪ (every other table's introspected columns,
  via the same cross-table index `ambiguities[]` already used). Resolution is fail-closed in both
  directions that previously erred toward keeping the entry: an **unparseable predicate** is dropped
  (never kept on the assumption it's "probably fine"), and an extracted identifier that **cannot be
  resolved to any known column anywhere** in the introspected universe is also dropped, rather than
  treated as "not a reference." This also removes an own-table-only restriction the shipped
  implementation initially had versus `ambiguities[]`: a rule's predicate can name another table's
  column by bare identifier (as `ambiguities[].resolves_to` prose already legitimately does), and that
  reference must be resolved and scope-checked the same way. **Accepted tradeoff:** a predicate that is
  not valid standalone SQL is dropped rather than kept — a usability cost, judged acceptable because
  rule predicates are authored as SQL expression fragments (unlike `ambiguities[].resolves_to`, which is
  free prose and stays on the regex scan below), so this should be rare in practice, and the alternative
  (keeping an unverifiable predicate) is the fail-open behavior this amendment exists to close.
- **(c) `ambiguities[].resolves_to` fails closed on a cross-table name collision.** The free-text regex
  scan (kept, per the original design rationale, since `resolves_to` entries can be natural-language
  prose mixed with SQL fragments that a parser can't reliably consume) previously resolved a cross-table
  identifier only when it named a column in *exactly one* other catalogued table; a name colliding across
  ≥ 2 tables (a real, live condition — e.g. `EmployeeCode` collides across 6 production tables,
  `ClientCode` across 9) was treated as unresolvable-by-name and silently ignored, erring toward keeping
  the entry. It is now resolved to *all* candidate tables at once, and the entry is treated as
  out-of-scope if the referenced column is out of scope on *any* candidate — over-drop is the safe
  direction for a genuinely ambiguous identifier. A token matching *no* known column anywhere (an
  ordinary prose word) is still, correctly, not treated as a reference at all — that half of the original
  design's tolerance for free text is unchanged and is not itself a fail-open gap, since it can only
  cause a legitimate in-scope entry to be over-included, never an out-of-scope one to leak (it never
  resolves to a *column*, so it can never mark something out of scope by mistake either — see the
  fail-closed identifier-collision behavior above for the case that actually does resolve to a column).

None of this changes the column list / `grain` / `primary_key` / `join_keys` / `measures` / `temporal`
filtering rules above, which were verified correct as originally specified.

### 1.5 Open sub-decision folded into Open Question 5

Whether *dropping* a rule/ambiguity/join-key entry (as opposed to including it) itself leaks
information (e.g. "the model can infer `AnnualSalary` exists and matters to `salary` resolution even
after that ambiguity option disappears") is discussed in Open Question 5 below — flagged, not resolved
here, since it's a genuine security-nuance fork rather than an implementation detail.

---

## 2. `sampleRows` (MCP)

**Current behavior (`app/service.py:sample_rows`):** `SELECT * FROM db.table LIMIT n` — no column-scope
check at all today (only the database allowlist). This is the gap WORKLOG.md already flags:
*"clickhouse-api MCP does NOT column-scope `sampleRows`/`getTableSchema` (only `runQuery`, via the D57
SQL parse)."*

**Recommended fix — reject, matching `runQuery` (per the user's stated framing "same as runQuery"):**

```
1. Build the equivalent SELECT * expansion: enumerate the table's full column list from the catalog
   schema (same "SELECT * expansion via catalog" logic as D69/OQ-2 — sampleRows IS effectively
   SELECT * LIMIT n, so the OQ-2 rule applies verbatim: if the table isn't in the catalog schema,
   columns can't be enumerated → fail-closed / reject).
2. Compute forbidden = { table's columns } - column_scope (skip if scope is None or empty/allow-all,
   same three-state model as run_query).
3. If forbidden is non-empty → reject with COLUMN_SCOPE_VIOLATION (same error shape/code
   run_query already raises), never execute the query.
4. If nothing forbidden → execute SELECT * LIMIT n unchanged.
```

**Alternative considered — project to in-scope columns** (rewrite to
`SELECT <in-scope-cols> FROM db.table LIMIT n` instead of rejecting):
- **Pro:** more useful in the common case (a partially-scoped user still gets to sample the columns
  they *can* see, which is arguably the more natural sampleRows UX — "show me what you can show me").
- **Con:** behavioral asymmetry with `runQuery` (`SELECT *` there is rejected outright per D69/OQ-2, not
  silently narrowed) — a model that assumes "sampleRows and `SELECT * LIMIT 5` behave the same" would
  be surprised. Also a projection changes the *columns* key of the response, which is a bigger
  contract change for callers than a pure reject/pass gate.
- **Recommendation: reject**, per the user's explicit framing and for consistency with the `runQuery`
  `SELECT *` treatment (D69/OQ-2) — one enforcement mental model for both tools, not two. If the
  scoped-sampling UX gap turns out to matter in practice, projecting is a compatible one-way-door
  change to revisit later (nothing about "reject now" forecloses "project later").

---

## 3. The richer catalog loader the MCP needs

### 3.1 What exists today, and its gap

- **`clickhouse-api/app/catalog.py`** (interim): builds `{database.table: {column: type}}` from
  `system.columns` — pure introspection, no semantic content. This is what feeds `extract_column_
  provenance` for D57 scope enforcement today. WORKLOG.md already flags this as "interim" pending the
  (formerly runtime-side, now MCP-side per D83) real catalog.
- **`data-analysis-agent/src/data_agent/catalog/loader.py`** (`build_sqlglot_schema()`): reads
  `databaseSchemaDocs/*.yaml` but **also only extracts `{column: type}`** — built for `qualify_columns`
  (D62), not for the overlay. It does not read `grain`, `temporal`, `primary_key`, `join_keys`,
  `measures`, `rules`, `ambiguities`, or per-column `description`/`synonyms`/`unit`/`values`/
  `observed_values`/`client_defined`/`sensitive`.

**Neither existing loader is the overlay loader.** A new function is needed that reads the *same* YAML
files but returns the full structure.

### 3.2 Proposed new loader function

Add (in whichever repo ends up hosting the canonical catalog source — see Open Question 1) a function
alongside `build_sqlglot_schema()`, e.g. `load_semantic_catalog(schema_dir) -> dict[str, TableEntry]`
returning, per `"database.table"` key, the full parsed YAML dict (or a lightly-typed wrapper) rather than
the reduced `{col: type}` view. Concretely: parse **once** per file, and derive `build_sqlglot_schema()`'s
`{col: type}` view **from** that same rich parse (a projection), rather than parsing the YAML twice with
two independent field-extraction functions that must be kept in sync by hand. This is a refactor of
`data_agent/catalog/loader.py` worth doing regardless of where the canonical copy lives, since today
`_extract_columns()` already silently drops every field except `type` — the overlay loader needs the
rest of that same dict it's currently discarding.

Rough shape:
```python
def load_semantic_catalog(schema_dir: Path | str) -> dict[str, dict[str, Any]]:
    """Return {"database.table": <full parsed YAML dict>, ...} — one entry per table file.
    Superset of build_sqlglot_schema(); build_sqlglot_schema() should become a thin
    projection over this (col -> type) once both consumers exist, to avoid parsing
    the YAML twice with divergent field lists.
    """
```

Scope-filtering (§1.4) then operates on this rich dict, using `qualified_key + "." + column` membership
checks against `column_scope`, exactly mirroring the shape `extract_column_provenance`'s USES-set
comparison already uses (D69/OQ-3: `(database.table, column)` granularity) — no new granularity concept
is introduced.

### 3.3 Relationship to `build_sqlglot_schema()` / `extract_column_provenance`

Unchanged: `build_sqlglot_schema()` keeps being the `{col: type}` shape `qualify_columns` and
`extract_column_provenance` need for D57/D62/D69 column-provenance extraction on `runQuery`/`sampleRows`.
The new richer loader is additive, for `getTableSchema`'s overlay only. Whether they become two views
over one parse (recommended, §3.2) or stay two independent parses is an implementation detail for
whoever builds this, not a design fork — recommend the single-parse refactor to avoid the two functions
silently drifting (e.g. one adding a new YAML field the other doesn't know exists).

---

## Open Questions for the user (numbered, recommendations included)

### OQ-1 — Catalog delivery mechanism to the MCP
Three options:
- **(a) Copy-in**, mirroring the D79a provenance-extractor precedent: `clickhouse-api` gets its own copy
  of `databaseSchemaDocs/*.yaml` (e.g. under `clickhouse-api/app/catalog/data/`), with a CI file-diff /
  content-hash parity check against `data-analysis-agent`'s copy, same-sprint mirroring discipline.
- **(b) Shared installable package** — a small internal Python package (e.g. `semantic-catalog`) that
  both repos depend on, versioned and published to an internal index; each repo pins a version.
- **(c) Canonical home moves to `clickhouse-api`** — the YAML files live in `clickhouse-api`, and
  `data-analysis-agent`'s runtime reads them via… a copy-in the other direction, or a package, or (if the
  runtime and MCP are co-deployed) a shared volume/mount. This inverts today's "spec repo authoritative"
  posture (D79a) and would need its own authority story (who reviews/merges `schema_edit` PRs, and
  against which repo?).

**Recommendation: (a) copy-in**, same shape as D79a. Rationale: D79a already established this pattern
for the provenance extractor for the same reason it applies here — the catalog changes relatively
rarely (curated, human-reviewed, D53(b)) and copy-in avoids release-ceremony while a mirroring convention
is proven out; promoting to (b) a shared package "once the contract holds for one release" is the same
graduation path D79a already committed to, so reusing it here is one convention instead of two. **(c) is
not recommended** — it inverts the D18/D53 "human-reviewed PR to the spec repo is the write-path gate"
model without a clear replacement, and the runtime's own OQ-B (deferred version-skew risk, referenced in
[phase0-runtime-design.md](phase0-runtime-design.md)) already flags this exact sync risk as a known,
accepted-for-now cost — copy-in inherits that same accepted risk rather than introducing a new one.
**Residual risk (either a or b):** the runtime and MCP can still momentarily disagree on catalog content
between one service's redeploy and the other's — mitigated, not eliminated, by the D84 `catalog_sha`
exposure making the skew observable (see D84) plus the same-sprint mirroring discipline (D79a).

### OQ-2 — `catalog_sha` computation/stamping mechanics under copy-in
D84 locks *what* `catalog_sha` means (the catalog subtree's own git SHA, not either service's deploy
SHA) but not *how* it's computed once the catalog is copy-in'd into a second repo (a copy-in loses the
original repo's git history at that path).

**Recommendation:** the copy-in step (whether manual or CI-automated) writes a small sidecar file (e.g.
`CATALOG_SHA` — a single line, the source-repo commit SHA of the last commit touching
`databaseSchemaDocs/**` at copy time) alongside the copied YAMLs in `clickhouse-api`. Both services read
this sidecar as `catalog_sha` — the runtime from its own `databaseSchemaDocs/` (git log directly, since
it's the source of truth there), the MCP from the sidecar file written at copy-in time. A CI parity
check (OQ-1) additionally verifies the sidecar's SHA matches the source repo's actual current
`databaseSchemaDocs/` HEAD when both repos are mirrored same-sprint, catching a stale copy-in before it
ships.

### OQ-3 — `sampleRows` reject vs. project
**Recommendation: reject**, matching `runQuery`'s `SELECT *` treatment (D69/OQ-2) for mental-model
consistency. See §2 for the full trade-off; projecting to in-scope columns remains a compatible later
change if the UX gap proves material.

### OQ-4 — Does the runtime's D44 defense-in-depth change?
**Recommendation: no change — keep it.** Today (`phase0-runtime-design.md`, per the D44 2026-07-01
turn-scoped clarification in DECISIONS.md) the runtime's replayed-trail provenance filter already
treats `getTableSchema`/`sampleRows` results as data-bearing and checks them against `column_scope`
*because* "D80(b) only column-scopes `runQuery`" at the MCP. Once D83 makes the MCP scope-filter
`getTableSchema` and `sampleRows` too, this runtime-side check becomes a **redundant-but-harmless**
belt-and-suspenders pass: an already-scope-filtered result trivially satisfies the runtime's own
provenance check (its provenance is, by construction, ⊆ scope), so no additional entries get dropped —
the check just always passes for these two tools going forward. Recommend **keeping** it rather than
special-casing it away, because (a) it's the same mid-session-scope-narrowing protection D44 exists
for generally (a session's replayed trail must re-check against the *current* scope every turn,
regardless of whether the *original* MCP call already checked against the scope *at call time* — scope
can narrow between then and a later turn), and (b) removing it would special-case two tools out of an
otherwise-uniform D44 mechanism for a marginal efficiency gain, adding complexity for no real benefit —
exactly the kind of unjustified abstraction-shedding to avoid. Net effect: defense-in-depth, cheaply,
not double work in any meaningful sense (the check is a set-membership comparison, not a query).

### OQ-5 — Does returning `rules`/`ambiguities`/enum `values` expose anything sensitive under scope-filtering?
Two distinct sub-risks, not fully resolved here:
1. **Existence leakage via a dropped entry.** Per §1.4/§1.5, a rule/ambiguity referencing an
   out-of-scope column is dropped wholesale rather than redacted — but the *absence* of an expected
   entry (e.g. a `salary` ambiguity that normally lists 4 resolution options now lists 2) can itself
   signal "there exist ≥2 more salary-related columns you can't see." This is a strictly smaller leak
   than including the predicate text (no column *names* or *semantics* are shown), and is arguably
   unavoidable in any column-scoped system — a scoped-down user who is told "the ambiguities/rules for
   this table" learns *something* about a table's overall richness) even from a shorter list. Flagged as
   an accepted, hard-to-avoid residual signal, not a blocking gap — the alternative (never showing table-
   level context length changes with scope) isn't practical.
2. **`sensitive`/`client_defined` flag interaction with scope-filtering.** These flags are per-column
   metadata (§1.2 notes them as informational, not the enforcement boundary — D42(d)). Because
   scope-filtering (§1.4) drops out-of-scope columns entirely, a column's `sensitive`/`client_defined`
   flag is only ever visible when the column itself is already in scope — so there's no separate leak
   path here; the flags ride along with (and disappear with) the column they describe. No additional
   handling needed beyond the uniform column-drop rule.

**Recommendation:** accept residual risk (1) as unavoidable and document it (this section is that
documentation); no special handling needed for (2) since it falls out of the general rule. If risk (1)
becomes a real concern later (e.g. an adversarial user probing ambiguity-list length to enumerate
scoped-out columns), a future hardening could pad dropped-entry counts or return a generic
"N additional catalog entries hidden by your access scope" marker instead of silent omission — not
recommended now (adds complexity + a new thing to keep in sync with the drop logic) but noted as the
available lever.

---

## Summary of recommendations (for quick reference)

| # | Fork | Recommendation |
|---|---|---|
| OQ-1 | Catalog delivery to MCP | Copy-in (mirrors D79a), CI parity check, graduate to shared package later if the contract stabilizes |
| OQ-2 | `catalog_sha` under copy-in | Sidecar `CATALOG_SHA` file written at copy-in time; CI checks it against source repo HEAD |
| OQ-3 | `sampleRows` reject vs. project | Reject (matches `runQuery`/D69-OQ-2); projection is a compatible later change |
| OQ-4 | Runtime D44 defense-in-depth | Keep unchanged — redundant-but-harmless, protects against mid-session scope narrowing regardless of MCP-side filtering |
| OQ-5 | Rules/ambiguities/flags leakage | Accept residual existence-leakage from dropped entries (documented); flag values ride with their column and need no separate handling |

---

## Cross-reference reconciliation checklist (prose that needs updating elsewhere)

The following files assert or imply the D78 (runtime-side overlay) arrangement and should be updated to
reflect D83/D84 before this is considered fully landed. Enumerated here rather than edited in this pass
(scope of this task was ADR + design, not a full-repo prose sweep):

1. **`docs/02-tools-and-api.md`** — the `getTableSchema` row (currently: *"Returns live ClickHouse
   introspection only... The runtime then applies the Semantic Catalog overlay..."*) needs to say the
   **MCP** returns the merged + scope-filtered schema; the closing paragraph ("The MCP returns
   introspection only; the semantic-catalog overlay (D42) is applied by the agent runtime (D78)...")
   needs the D83 correction. Also add the new scope-filter behavior for `sampleRows`.
2. **`docs/09-infrastructure.md`** §Semantic Catalog — three sentences say "the runtime applies the
   overlay" / "the catalog deploys with the runtime (not the MCP service)" / "graceful degradation now
   runtime-side per D78" — all need the D83/D84 correction (catalog now deploys to both; MCP applies the
   overlay + degradation again).
3. **`docs/11-testing.md`** line ~53-54 — *"`getTableSchema` returns live introspection only (D78)"* and
   *"Runtime catalog overlay (vs. catalog + MCP): the runtime performs `introspection ⨝ catalog
   overlay`..."* — this becomes an **MCP component test** (MCP ↔ ClickHouse ↔ catalog), not a runtime
   component test; update the Layer-2 test categorization.
4. **`docs/WORKLOG.md`** — the "Next brick" line naming "D78 `getTableSchema` catalog overlay" as
   upcoming runtime work should note the D83 relocation; the standing follow-up *"Replace the interim
   `system.columns` catalog in `clickhouse-api` with the D78 runtime catalog source"* is **directly
   reversed** by this change — restate as "replace the interim catalog with the full semantic-catalog
   loader (§3), now living in/consumed by `clickhouse-api` itself, not sourced from the runtime."
5. **`docs/decisions/TRACEABILITY.md`** row for **D53** (`D53-uncatalogued-table-structural-only`,
   currently annotated *"Graceful degradation is runtime-side per D78... test at the runtime component
   layer, not the MCP"*) — flip to an MCP component-layer test.
6. **`docs/decisions/OPEN-QUESTIONS.md`** §Semantic Catalog — opening sentence *"The curated semantic
   layer applied by the agent runtime (not the MCP). Per D78..."* needs the D83 correction throughout
   that section's framing (the code-side dependencies listed there, e.g. `resolveValues`'s
   `resolve_via` binding, are unaffected — only the "who applies the overlay" framing sentence changes).
7. **`docs/decisions/phase0-runtime-design.md`** — its own "Out of scope" line naming "D78
   `getTableSchema` catalog overlay" as deferred runtime work should get a note that this work is now
   never coming to the runtime (D83 relocates it permanently to the MCP, not just defers it) — the
   runtime's Phase-0 `getTableSchema` passthrough (`app/service.py:get_table_schema` call site) needs no
   further runtime-side overlay work in Phase 1; the runtime should simply receive an already-overlaid,
   already-scope-filtered response from the MCP once D83 ships, and continue passing it through as-is.

None of these are blocking for the ADR/design decision itself; they are follow-up documentation debt
this reversal creates, listed so a follow-up pass (or the next `planner`/doc-sweep session) can close
them without re-deriving the list.
