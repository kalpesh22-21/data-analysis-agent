# Second-Order Analysis — `projectSeries` Tool + Time-Series Blueprints (D103/D104)

**Status:** **PROPOSED (2026-07-15) — design only, not built.** Companion to
[DECISIONS.md](DECISIONS.md), [02-tools-and-api.md](../02-tools-and-api.md),
[resolvevalues-design.md](resolvevalues-design.md) (the composite-tool template this follows), and
[04-blueprints.md](../04-blueprints.md). Scoped to the **MVP cut** agreed with the user: Phases **A**
(a first-order time-series blueprint), **B** (the `projectSeries` runtime tool), and the **prompt
rule** that routes forward-looking questions to the tool. Phase C (first-class derived-number
grounding), Phase D (the "need to hire" target anchor), and Phase E (real forecasting / the D73 skill
subsystem) are **deferred** and sketched in §7 only so the MVP does not paint them out.

**Motivating question (the acceptance example):**
> "How many people have we hired for the last 6 months, and if we keep going at this pace, how much
> will we need to hire?"

Today the agent answers the first clause well (it is a `COUNT` over `employee.MostRecentHireDate`) and
cannot honestly answer the second. This design closes that gap for the **pace-projection** reading of
the question. It deliberately does **not** attempt the **target-based "need"** reading (that requires a
headcount goal + attrition offset that do not live in the warehouse — see §7, Phase D).

---

## 0. Ground truth this design relies on (verified by reading code, not assumed)

- **The forward number must be produced by a tool, never by prose arithmetic.** The rule "Never
  fabricate numbers — every figure must come from a query you actually ran"
  (`runtime/prompts.py:73-74`) is **prompt-only**; a `grep` of `src/` finds no code that parses numbers
  out of `assistant_text` or cross-checks them against `result_preview`. A forecast typed into the
  answer by the model is exactly the ungrounded number the provenance/verify apparatus exists to
  prevent. Therefore the projection is a **traced tool computation**, and the model's job is to
  *surface* the tool's result, not to compute it.
- **A model-facing runtime tool has a known, live wiring path** (the same one `resolveValues` uses).
  The four seams: (1) schema constant appended to `_LOCAL_TOOL_SCHEMAS` in
  `runtime/mcp/tool_schema.py`; (2) a class with `async def run(model_args, credentials) -> ToolResult`
  (the `RuntimeTool` Protocol, `runtime/loop/agent_loop.py:123`); (3) instantiation + registration in
  the `runtime_tools` dict in `runtime/app.py::_build_agent_loop`; (4) optional arg-redaction key in
  `runtime/observability/redaction.py`. The loop's dispatch branch
  (`agent_loop.py::_run_loop`, the `handler = self._runtime_tools.get(...)` block) routes any
  registered name and handles the trail / preview / budget / provenance path generically — **no loop
  change is needed.**
- **A composite tool inherits enforcement for free.** `resolveValues`
  (`runtime/composite/resolve_values.py`) issues **one ordinary `runQuery` through the dispatcher**, so
  D5 credential injection, D57 column-scope, denial mapping, and provenance capture all apply to its
  inner query with zero new enforcement code. `projectSeries` follows this pattern exactly.
- **The historical time series is a plain blueprint.** A single-node `GROUP BY toStartOfMonth(...)`
  blueprint returning many rows is structurally identical to the shipped
  `bp-active-headcount-by-department` fixture (`tests/fixtures/corpus/blueprints.yaml`) and is
  D56-grain-verified today. Multi-row output is the norm, not an edge case.
- **There is no "last-N-months" range slot type.** `runtime/blueprint/slots.py` resolves a *single
  named period* and routes deictic tokens ("last", "this month") to `askUser`; a period *range* is a
  carried open question. The MVP encodes the window **in SQL**, not as a slot.
- **`employee` has the hire columns.** `MostRecentHireDate` is catalog-flagged as the "default field
  for generic hire-date questions"; rule `exclude_not_hired_default` drops pre-hire (`N`) records.

---

## 1. Scope

### In scope (MVP)
1. **Phase A** — author `bp-hires-per-month` (and the generic shape it generalizes) in the seed corpus;
   re-seed; prove the agent answers "hires per month, last N months" via the verified blueprint path.
2. **Phase B** — the `projectSeries` runtime composite tool: fetch a basis series via its own
   `runQuery`, compute a **deterministic** extrapolation in-runtime, return a structured result.
3. **The prompt rule** — route "at this pace / going forward / project / forecast / on track to"
   questions to `projectSeries`; forbid free-hand forward arithmetic in prose.
4. **Assumption discipline for "need"** — when the question implies a target ("need to hire") but none
   is available, the model states the interpretation via `recordAssumptions` ("projected current
   hiring pace forward; not measured against a headcount target") or asks — it never invents a target.

### Explicitly out of scope (deferred — see §7)
- **Phase C** — a derived-provenance kind, a projection sanity/verify gate, and a measured-vs-projected
  UI badge (making the derived number first-class in lineage/verify/contract).
- **Phase D** — the "need to hire" target anchor: terminations-per-month, attrition-adjusted net need,
  and target-via-`askUser`/`searchKnowledge`.
- **Phase E** — seasonality/trend models (Holt-Winters, YoY), confidence intervals, and the D73
  executable-skill subsystem (unbuilt; `skills/*/SKILL.md` are dormant prompt fragments).
- **A "last-N-months" range slot type** — nice-to-have; the MVP puts the window in SQL.

---

## 2. Phase A — the first-order time-series blueprint

**Deliverable:** `bp-hires-per-month` authored in `tests/fixtures/corpus/blueprints.yaml`, loaded via
`scripts/seed_neo4j_corpus.py` (the loader validates: every `{slot}` declared, template parses under
the ClickHouse dialect, column footprint ⊆ `uses`).

Sketch (final column names/rules to confirm against the catalog at authoring time):

```yaml
- id: bp-hires-per-month
  intent: "Number of employees hired per calendar month over a recent window"
  uses_rules: [exclude_not_hired_default]
  result_grain: [month]           # D56 grain-verify: row_count == COUNT(DISTINCT month)
  sql_template: |
    SELECT toStartOfMonth(MostRecentHireDate) AS month,
           COUNT(DISTINCT EmployeeCode)       AS hires
    FROM   dbpcm_warehouse.employee
    WHERE  MostRecentHireDate >= toStartOfMonth(now()) - INTERVAL 6 MONTH
      AND  EmployeeStatus != 'N'
    GROUP  BY month
    ORDER  BY month
```

**Notes.**
- **[As-built, 2026-07-15]** The parameterized-window gap is now **closed and committed (`1d82420`)**:
  the `relative_window` and `period_range` slot types shipped (see [04-blueprints.md](../04-blueprints.md)
  §Windowed-period slots), and `bp-hires-per-month` (relative_window) + `bp-hires-in-range`
  (period_range) exist as validated fixtures. So Phase A's window is a real slot, not a SQL literal;
  Phase A remaining = end-to-end verification against the seeded warehouse.
- This blueprint is independently useful (answers "hires per month" on its own) and is the **basis
  series** `projectSeries` consumes — but the tool re-issues its own query rather than depending on
  blueprint plumbing (§3.3), so A and B are decoupled.
- **Verify:** a live-layer test posing "how many did we hire each month for the last 6 months";
  confirm the `verified ✓` (D56) path fires.

---

## 3. Phase B — the projection **blueprint** (pivoted from a tool)

> **[Pivot, 2026-07-15 — supersedes the tool design below.]** Phase B ships as a **verified SQL
> blueprint `bp-hires-projection`**, not a runtime tool. Rationale: the run-rate projection is fully
> expressible in ClickHouse, so a blueprint is the better home — it rides the existing **D56 verify +
> provenance + trail** path for free and adds **no new SQL-construction/attack surface** (the tool would
> have re-implemented GROUP-BY-month windowing and, in review, got the partial-month bound and gap-fill
> wrong — both of which a single-row aggregate `sum(hires)/N` sidesteps by construction). "Not prose"
> never required "a new tool"; it required a **grounded, traced computation**, which a blueprint is.
>
> **As-built:** `bp-hires-projection` (`tests/fixtures/corpus/blueprints.yaml`) — a single-row aggregate
> over `dbpcm_warehouse.employee`: `hires_in_window`, `avg_monthly_hires = COUNT(DISTINCT EmployeeCode)/N`,
> `projected_next_n_hires = rate × horizon`. Two `relative_window` slots (`window_months`,
> `horizon_months`). The trailing window is **data-anchored and shifted forward one month** so it spans
> exactly N buckets (not N+1), and it applies `exclude_not_hired_default`
> (`EmployeeStatus != 'N' OR IS NULL`) in both the outer filter and the `max()` anchor subquery. A prompt
> nudge (`prompts.py`) routes forward-looking questions to `searchBlueprints`/`runBlueprint` and requires
> the model to state the constant-pace assumption via `recordAssumptions`. Reviewed (CRITICAL denominator
> + HIGH not-hired fixes applied) and QA'd (21 tests, live-run against the seeded warehouse).
>
> The bespoke tool is **reshelved to Phase E**, where it would carry real statistical forecasting
> (intervals, seasonality) that SQL cannot express — the only case that actually justifies a tool over a
> blueprint. The original tool design is retained below for that future reference.

### 3.1 Model interface (original tool design — deferred to Phase E)

```
projectSeries(
  table:          str,             # e.g. "dbpcm_warehouse.employee"  (catalog-validated)
  date_column:    str,             # e.g. "MostRecentHireDate"        (catalog-validated)
  count_column:   str | null,      # e.g. "EmployeeCode" -> COUNT(DISTINCT EmployeeCode); null -> COUNT(*)
  method:         "run_rate" | "cagr" | "moving_average" = "run_rate",
  basis_months:   int,             # how many months of history to fit on (e.g. 6)
  horizon_months: int              # how many months forward to project (e.g. 6)
) -> {
  degraded:    bool,
  method:      str,
  basis_window:{ start, end, points },
  historical:  [{ month, value }],
  projected:   [{ month, value }],
  caveats:     [str]
}
```

`jwt` / `session_id` / `column scope` are **injected** at dispatch, never model parameters (D5) — the
tool never sees them; its inner `runQuery` gets them from the dispatcher.

**No free-text SQL fragments (injection posture, revised after reading `composite/sql_builder.py`).**
The original sketch took a `count_expr` / `filter` string; that is a new SQL-construction surface and
is dropped. Every model-supplied identifier (`table`, `date_column`, `count_column`) is
**catalog-allowlisted** the way `sql_builder.resolve_target` does it (`TargetValidationError`,
fail-closed, before any SQL is built), and the series SQL is assembled from **sqlglot AST nodes +
typed literals** (`toStartOfMonth(col)`, `COUNT(DISTINCT col)`, `INTERVAL <int> MONTH` where `<int>`
is a bound integer literal) — never string-interpolated model text. A row-level predicate (e.g.
`EmployeeStatus != 'N'`) is **out of MVP scope**: prefer running the tool over a table/column pair
that already means "hires", or add a *structured* filter later (§7). This keeps the tool's new SQL
surface to a fixed, identifier-parameterized template.

### 3.2 The three methods (deterministic only — no LLM, no stochastic models)
- **`run_rate`** — mean monthly value over the basis window × horizon (the literal reading of "at this
  pace"). The MVP default.
- **`cagr`** — compound monthly growth rate fitted across the basis window, projected forward. Rejects
  a zero/negative basis (→ caveat + degrade to `run_rate`).
- **`moving_average`** — trailing k-month average carried forward (smooths a noisy series).

Anything beyond these (seasonality, ARIMA, Holt-Winters, confidence intervals) is Phase E.

### 3.3 Implementation — mirror `ResolveValuesComposite`

New module `runtime/composite/project_series.py`. Structure copied from
`composite/resolve_values.py`:

1. `run(model_args, credentials) -> ToolResult` — the model-call adapter: opens a TOOL span
   (`tracing.tool_span`), emits `tool_dispatch_start/ok/error` observer events, wraps everything in a
   B4-parity broad-except that degrades to a clean `PROJECT_SERIES_INTERNAL_ERROR` `ToolResult` rather
   than crashing the turn.
2. Validate `table`/`date_column`/`count_column` against the catalog allowlist (fail-closed) — reuse
   `sql_builder.resolve_target` / `_resolve_db_table` + a `catalog.schema[db_table]` membership check;
   an unknown/ambiguous identifier raises `TargetValidationError` and never reaches the MCP. No
   free-text fragment parsing — the only model input to SQL is validated identifiers + bound integers.
3. Build the basis SQL from sqlglot AST nodes and issue **one**
   `self._tool_dispatcher.dispatch("runQuery", {...}, credentials)` —
   `SELECT toStartOfMonth(<date_column>) AS month, COUNT(DISTINCT <count_column>|*) AS value
   FROM <db>.<table> WHERE <date_column> >= toStartOfMonth(now()) - INTERVAL <basis_months> MONTH
   GROUP BY month ORDER BY month`. Inner denials (`COLUMN_SCOPE_VIOLATION`, etc.) pass through verbatim.
   (Anchor on `now()` for a true trailing window in production; the projection math is unit-tested on
   synthetic series, so the anchor choice doesn't affect those tests.)
4. Compute the projection in Python from the returned rows (pure function, unit-testable in isolation).
5. Wrap into a `ToolResult` via the shared `_build_preview`. **Provenance** = the inner `runQuery`'s
   provenance (the `(table, column)` USES-set) — so the tool's basis is lineage-tracked today, even
   before Phase C adds a derived-provenance kind for the *projected* values.

### 3.4 Seams to touch (and only these)
1. `runtime/mcp/tool_schema.py` — add `PROJECT_SERIES_TOOL_SCHEMA`; append to `_LOCAL_TOOL_SCHEMAS`
   (auto-exposes it to the model and registers the name in the collision guard). No injected params.
2. `runtime/composite/project_series.py` — the new tool (above).
3. `runtime/app.py::_build_agent_loop` — instantiate `ProjectSeriesComposite(dispatcher, catalog,
   observer=…, tracer=…)` and add it to the `runtime_tools` dict. Wire **unconditionally** (like
   `recordAssumptions`) — it has no external backing stack, only the already-present dispatcher.
4. `runtime/observability/redaction.py` — args are structural (table/column/method/ints), so likely
   **no** change; add a key only if a free-text arg is introduced.
5. `runtime/dispatch/denial_mapping.py` — add `PROJECT_SERIES_*` codes to `_DENIAL_TABLE` so replayed
   trail entries render specific messages (the L5 lesson from `resolveValues`).

**Not touched:** `agent_loop.py::_run_loop` (generic dispatch/trail/budget path handles it),
`ToolResult`/`_build_preview` (reused as-is), the MCP data plane (still exactly 6 tools — this is a
runtime composite, consistent with D4/D77).

### 3.5 Error family
`PROJECT_SERIES_UNKNOWN_TARGET` (unknown/ambiguous table/column, retryable),
`PROJECT_SERIES_INSUFFICIENT_DATA` (basis window has < 2 points — cannot fit a rate),
`PROJECT_SERIES_INTERNAL_ERROR` (malformed inner result — degraded safely, never a raw crash).
Inner-`runQuery` denials pass through unchanged.

---

## 4. The prompt rule (the actual fabrication-gap fix)

> **[As-built]** The shipped nudge (`prompts.py` "## Answering") routes forward-looking questions to
> `searchBlueprints`/`runBlueprint` (the projection **blueprint**, not a tool), keeps the "don't compute
> forward numbers in prose" guard, and adds the constant-pace + no-invented-target assumption discipline
> via `recordAssumptions`. The original tool-oriented wording is retained below for the Phase-E reference.

Original (tool-oriented) — add to `AGENT_SYSTEM_PROMPT` (`runtime/prompts.py`), in "## Answering":

> When a question asks what will happen going forward — "at this pace", "if we keep going", "project",
> "forecast", "on track to" — do **not** compute the forward number yourself. Call `projectSeries` to
> produce it, and report the value it returns. Say which method was used and surface the tool's
> caveats. If the question asks what you will *need* (a target, a goal, a plan) and no target is
> available from the data or the user, do not invent one: either ask, or record the assumption that you
> projected the current pace forward rather than measuring against a target.

This is what converts "the model *can* multiply" into "the model *routes* to a traced computation."

---

## 5. Testing strategy

- **Unit (pure math):** fixed basis series → known projected values, per method; boundary cases
  (single point → `INSUFFICIENT_DATA`; flat series; zero/negative basis → `cagr` degrades).
- **Component (tool over a fake dispatcher):** `projectSeries` issues exactly one `runQuery`, passes
  the injected credentials, returns the right `ToolResult` shape, degrades on a malformed inner result
  (B4 parity), propagates `COLUMN_SCOPE_VIOLATION`.
- **Live layer:** against the seeded `dbpcm_warehouse`, pose the acceptance question end-to-end;
  assert the answer's forward figure equals the tool's `projected` sum and that no forward number
  appears without a `projectSeries` call in the trail.
- **Blueprint (Phase A):** `bp-hires-per-month` loads, validates, executes, and D56-verifies.

---

## 6. Risks & mitigations
- **Model still does mental math despite the tool.** Mitigate with the explicit prompt rule (§4) and a
  live-layer assertion (§5) that a forward figure without a `projectSeries` trail entry fails the test.
- **A naive `run_rate` misleads on a seasonal series.** Mitigate with mandatory caveats in the result
  ("assumes constant pace; not seasonally adjusted") that the prompt requires the model to surface.
  Real seasonality is Phase E.
- **"Need" answered as "pace".** Mitigate with §4's assumption/ask discipline; fully resolved only by
  Phase D.
- **Derived number not yet first-class in lineage/verify.** Accepted for the MVP: the *basis* is
  provenance-tracked (§3.3.5) and the projection is clearly labeled a projection; Phase C makes the
  projected values themselves first-class.

---

## 7. Deferred phases (sketch only — not part of this MVP)

- **Phase C — grounding the derived number.** (a) a derived-provenance kind in
  `runtime/provenance/capture.py` + `_compute_turn_provenance_union` so lineage reads "projected from
  employee.MostRecentHireDate, run-rate over 6mo" instead of dropping to `None`; (b) a value-level
  projection sanity gate (basis ≥ N points, non-negative outputs, method applicability) analogous to
  D56's structural gate in `runtime/blueprint/verify.py` — the "type/positivity invariants" already
  flagged open there; (c) a first-class `projection` block on `TurnOutcome` /
  `app.py::_outcome_to_dict` + a **measured-vs-projected** UI badge (ui-slice1 contract) so a forecast
  never renders as a fact.
- **Phase D — the "need to hire" anchor.** `bp-terminations-per-month` (net = gross − attrition), and a
  target from one of: `askUser`, a hiring-plan doc via `searchKnowledge`, or an explicit parameter;
  `net_need = target − (current_headcount − projected_attrition)`.
- **Phase E — real forecasting + skills.** Seasonality/trend + confidence intervals; architecturally
  the D73 `specialized_analysis` **skill** (`docs/12-extensibility.md`) — but that hook/skill subsystem
  is PROPOSED-only (no loader/registry/`HookContext`; the `skills/*/SKILL.md` files are dormant),
  so it is a project of its own and explicitly not the near-term path. Tool-first now, skill later.

---

## 8. Proposed decisions
- **D103 — `projectSeries` runtime composite tool.** A model-facing runtime tool (not an MCP data-plane
  tool; MCP stays at 6) that fetches a monthly basis series via one injected `runQuery` and computes a
  deterministic extrapolation (`run_rate`/`cagr`/`moving_average`) in-runtime. Forward numbers come
  from this tool, never from prose arithmetic. Follows the D77 composite template.
- **D104 — time-series blueprints are ordinary single-node blueprints.** A `GROUP BY toStartOfMonth(...)`
  series is authored and D56-verified like any multi-row blueprint; the recent-window filter is
  encoded in SQL until a `period_range` slot type exists.

**Open questions:**
- Should `projectSeries` accept a caller-supplied basis series (from a prior `runBlueprint`) as an
  alternative to self-querying, to avoid a second scan? (MVP: self-query only, for provenance
  simplicity.)
- Default method — `run_rate` proposed; confirm against the most common phrasing ("at this pace").
- Minimum basis points before a projection is allowed (proposed: 2; possibly 3 for `cagr`).
