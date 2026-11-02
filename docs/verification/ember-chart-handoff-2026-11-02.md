# Ember handoff: opt-in chart components

Date: 2026-11-02  
Lane5 implementation: `feature/data-as-of`, commit `b5c673b`  
Status: **Ember implementation requested. Lane5 and its reference renderer are implemented and tested; Ember end-to-end acceptance is outstanding.**

## 1. Requested outcome

Add five presentation kinds to Ember's answer-document v2 path:

| Kind | Purpose |
|---|---|
| `multi_series_line` | Compare 2–4 measures over time. |
| `grouped_bars` | Compare 2–4 measures within each category. |
| `stacked_bars` | Show disjoint additive components within each category. |
| `heatmap` | Show one measure across two categorical dimensions. |
| `donut` | Show 2–5 mutually exclusive parts of a complete whole. |

Keep existing `text`, `table`, `figure`, `facts`, `bars`, and `line` working. Pie, area, scatter, boxplot, gauge and automatic chart-kind switching are outside this change.

**Implement selector-based schema v2. Do not use the earlier handoff's proposed v1 materialized `points`, `items`, `values`, or per-series `format` payloads.** Lane5 now emits separate kind names instead of overloading `line` or `bars`; this allows each new component to be enabled independently.

The supplied Ember report referenced commit `91ccfb5fc` and a v1-only validator. That checkout was not available for re-verification here. First establish the target Ember branch's current version support. If it still accepts only v1, complete the [v2 migration](ember-ui-v2-migration-2026-10-31.md) before adding these kinds. Do not downgrade Lane5 or reinterpret v1 documents as v2.

## 2. Availability and rollout

Lane5 owns the allowed component list. Default:

```dotenv
ALLOWED_UI_COMPONENTS=["text","table","figure","facts","bars","line"]
```

All supported components, for integration testing only until Ember passes acceptance:

```dotenv
ALLOWED_UI_COMPONENTS=["text","table","figure","facts","bars","line","multi_series_line","grouped_bars","stacked_bars","heatmap","donut"]
```

This is runtime configuration, not a browser request parameter or a new capabilities endpoint. Restart Lane5 after changing it. Unknown/duplicate names fail startup; `text` and `table` are required for safe fallback. Agent, judge and salvage schemas expose only enabled components; server validation also rejects disabled kinds.

Ember should implement all five renderers, while the shared deployment keeps the six-kind default until verified. Disabling a component later prevents new answers from selecting it; it must not make already-saved documents unreadable. Renderer support and model availability are distinct.

### Selection policy: intent first, enabled components only

Lane5's agent chooses the presentation; Ember renders the selected kind or uses the same-result table fallback. The agent receives only the rules for enabled kinds:

| Question / result shape | Preferred enabled component |
|---|---|
| Explanation or limitation without a verified result | Text |
| One summary number | Figure |
| Several summary numbers in one row | Facts |
| One measure ranked or compared across categories | Bars |
| One measure over time | Line |
| 2–4 comparable measures over the same periods | Multi-series line |
| 2–4 measures compared side by side within categories | Grouped bars |
| Totals and their disjoint additive components by category | Stacked bars |
| Patterns in one measure across two dimensions | Heatmap |
| Complete composition of 2–5 mutually exclusive categories | Donut |
| Detailed records, incompatible units, or no suitable enabled chart | Table |

Honor explicit presentation requests when enabled and suitable. Otherwise choose an enabled alternative that preserves all requested information. Never drop a measure/category/detail, restrict the population, or change the metric to fit a chart. Never ask the user to choose a display format. Row count alone does not establish chart suitability.

Examples: a disabled donut can become enabled bars when all composition categories remain visible. A two-measure trend must not become a one-measure line when multi-series is disabled; a table preserves both measures. A small top-five ranking is still a ranking, not automatically a whole suitable for a donut. Grouped comparisons do not require additive measures; stacked components do.

## 3. Exact delivered selectors

These examples are **wire presentations**, not model tool arguments. The model uses `category_column` and `measure_column`; Ember receives `x_column`, `y_column` and `value_column` as shown below. Column selectors are exact, case-sensitive output aliases, including spaces.

### Multi-series line

```json
{
  "kind": "multi_series_line",
  "result_id": "r_monthly",
  "x_column": "Month",
  "label": "Monthly earnings (USD)",
  "series": [
    {"y_column": "Regular Earnings", "label": "Regular"},
    {"y_column": "Overtime Earnings", "label": "Overtime"}
  ]
}
```

### Grouped bars

```json
{
  "kind": "grouped_bars",
  "result_id": "r_departments",
  "x_column": "Department",
  "label": "Employees by status",
  "series": [
    {"y_column": "Active Employees", "label": "Active"},
    {"y_column": "Terminated Employees", "label": "Terminated"}
  ]
}
```

### Stacked bars

```json
{
  "kind": "stacked_bars",
  "result_id": "r_departments",
  "x_column": "Department",
  "label": "Employees by status",
  "additive": true,
  "series": [
    {"y_column": "Active Employees", "label": "Active"},
    {"y_column": "Terminated Employees", "label": "Terminated"}
  ]
}
```

`additive: true` declares disjoint additive components. Do not stack rates, overlapping populations, or a total together with its parts. No ranking fields are emitted for these multi-series kinds. Preserve series order and labels.

### Heatmap

```json
{
  "kind": "heatmap",
  "result_id": "r_heatmap",
  "x_column": "Month",
  "y_column": "Department",
  "value_column": "Overtime Hours",
  "label": "Overtime hours"
}
```

Here `y_column` is a dimension, not a numeric measure. The result has at most one row per dimension pair.

### Donut

```json
{
  "kind": "donut",
  "result_id": "r_headcount",
  "x_column": "Department",
  "y_column": "Headcount",
  "label": "Active employees by department",
  "composition": true
}
```

`composition: true` declares exhaustive, mutually exclusive parts of the stated whole. A top-N result alone is not a whole; an Other category must already be computed in SQL. Do not invent Other, drop categories, or infer composition from a small row count. Invalid/oversized donuts fall back to the same-result table in this implementation; automatic conversion to another chart is not specified.

## 4. Envelope, source formats and result rows

The presentation sits in a normal v2 section:

```json
{
  "schema_version": 2,
  "answer_id": "answer-example",
  "revision": 1,
  "generated_at": "2026-11-02T20:00:00Z",
  "takeaway": "Regular earnings increased between January and February.",
  "sections": [{
    "id": "earnings",
    "title": "Monthly earnings",
    "evidence_refs": ["r_monthly"],
    "caveats": [],
    "actions": [],
    "presentation": {
      "kind": "multi_series_line",
      "result_id": "r_monthly",
      "x_column": "Month",
      "label": "Earnings (USD)",
      "series": [
        {"y_column": "Regular Earnings", "label": "Regular"},
        {"y_column": "Overtime Earnings", "label": "Overtime"}
      ]
    }
  }],
  "sources": {
    "r_monthly": {
      "sql": "<exact successful SQL supplied by Lane5>",
      "retrieved_at": "2026-11-02T19:59:58Z",
      "row_count": 2,
      "completeness": "complete",
      "column_formats": {
        "Month": {"kind": "date", "grain": "month"},
        "Regular Earnings": {"kind": "currency", "currency": "USD", "precision": 2},
        "Overtime Earnings": {"kind": "currency", "currency": "USD", "precision": 2}
      }
    }
  }
}
```

The SQL placeholder is explanatory, not executable. A matching page's data portion could be:

```json
{
  "columns": ["Month", "Regular Earnings", "Overtime Earnings"],
  "rows": [
    ["2026-01-01", "10000.00", "1200.00"],
    ["2026-02-01", "11000.00", null]
  ],
  "row_count": 2,
  "has_more": false
}
```

The actual page response also includes snapshot/paging metadata. Values can be decimal strings. Use `sources[result_id].column_formats` for cells, labels, tooltips and axes. Do not require widget-level formats or formatted strings. Use Ember's decimal-safe formatter; numeric conversion is for chart geometry only. Decline unsafe geometry rather than silently rounding a large value. Never parse a formatted display string to obtain a number.

For multi-series, format signatures `(kind, currency, scale, unit)` must match; precision may differ. Conflicting currencies, units or percent scales fall back to a table, not dual axes. Unknown formats never authorize invented units.

## 5. Data loading and snapshot lifecycle

Use the existing v2 [execution/paging contract](../ui-answer-document-spec.md), including authentication and error handling:

1. Resolve `presentation.result_id` in document-level `sources` and section `evidence_refs`.
2. Reuse the saved snapshot association if present. Otherwise first load posts `{sql, limit, offset: 0}` to Lane5 `/query/page` using the exact source SQL.
3. Persist the returned snapshot ID against the answer/source association. Later pages post `{snapshot_id, limit, offset}`; do not send SQL again to obtain each page.
4. Share the result binding across chart, View data and CSV. Deduplicate concurrent loads of the same result. Never run a chart-specific rewritten SQL query.
5. Render a loading state while obtaining complete chart data. A bounded request of up to 120 rows is sufficient for these charts: `has_more: true` means table fallback. Do not display the first page as the complete chart. Keep the table pageable.
6. Refresh posts `{sql, refresh_of: oldSnapshotId, offset: 0}`. Clear/replace chart and table rows together and preserve reset/replacement/error semantics from the v2 migration. A table/chart refresh does not rewrite the reviewed takeaway.
7. Reuse the persisted binding on history load. Show an explicit unavailable/expired state if authorized recovery fails; no infinite recreation loop. Do not treat current component configuration as a reason to discard historical documents.

`completeness` on a document source describes the execution preview, not full answer completeness or whether the UI has fetched every row. The legacy 25-row preview gate does not apply to these v2 selectors.

**Known limitation A25 remains open:** first-page snapshot creation may re-execute SQL after answer review. This work does not bind an answer-time frozen snapshot or eliminate takeaway/data drift. Share one fetched snapshot across the UI surfaces, but do not claim it necessarily contains the original judged rows. A separate backend snapshot fix is required.

`retrieved_at`, snapshot `created_at`, and genuine `data_as_of` are distinct. Never synthesize Data as of from retrieval or snapshot creation.

## 6. Rendering guards

All chart selections use exact unambiguous columns from a single result. No client-side joins, pivots, deduplication or measurement aggregation. Reject missing selectors, invalid numeric values and non-finite geometry. Fall back to the same-result table, preserving title, explanation, caveats and controls.

| Kind | Required behavior / bounds |
|---|---|
| All five | At most 120 complete rows; no silent truncation. |
| Multi-series kinds | 2–4 uniquely named series/measure columns, compatible formats, unique category keys; at most 480 values. An all-null series falls back. |
| `multi_series_line` | At least two unique valid periods. Order complete row tuples chronologically; never sort an axis independently of values. Null is a gap (`connectNulls: false`). Recognize year, quarter, month and date periods using source grain. |
| `grouped_bars` | Preserve category and series order. Support negative measures with a zero baseline. Null is missing, not zero. |
| `stacked_bars` | Require `additive: true`; reject negative/null components and percent measures. Preserve zero-valued components and labels. |
| `heatmap` | Distinct x/y/value columns; unique x/y pairs; max 24 x-categories, 20 y-categories, 120 rows. Missing/null cells remain blank. No duplicate-cell summation. |
| `donut` | Require `composition: true`; 2–5 unique categories, nonnegative non-null additive values, positive total; reject percent measures. Zero slices retain labels. |

Reference renderer precision guard rejects values above `Number.MAX_SAFE_INTEGER` or with more than 15 decimal digits before numeric geometry conversion. Preserve exact values in the table regardless of chart eligibility. Bounds are display guards, never user population restrictions or SQL LIMIT instructions.

Use `@paycom/ui` Highchart and theme colors where available. The old ChartBlock may provide rendering patterns, but its materialized wire payload is not this contract. Enable a visible, accessible series legend; no separate legacy legend block is supplied. Use exact source-formatted tooltips and clear axis labels. Respect reduced motion. Keep View data/CSV available for every new kind and preserve drawer keyboard/focus behavior.

## 7. Suggested Ember implementation points

Paths below come from the supplied Ember handoff; verify them on the target branch.

| Area | Work |
|---|---|
| `DataAgentStorage.ts` | Extend the v2 presentation union and schemas for the five exact selectors; retain source formats and source/result mapping validation. Keep v1 history handling explicit if supported. |
| `State.ts` / storage handlers | Hydrate chart result bindings on render, share/persist snapshot associations, handle cancellation, reload, refresh and failures. |
| `answer/presentationAdapters.ts` | Resolve columns/formats, validate complete data and bounds, produce chart-ready geometry without creating new measurements; centralize table fallback reasons. |
| `answer/PresentationDispatcher.tsx` | Dispatch the five new kinds. |
| `answer/presentations/*` | Implement kit-backed charts with legends, null handling, units and accessibility. |
| `answer/AnswerSection.tsx` | Loading/error/fallback surfaces plus shared View data/CSV binding. |
| Browser and component tests | Cover the matrix below, including saved answers and configuration changes. |

A malformed/unsupported chart must not erase unrelated valid sections. If the selected source itself is unusable, show an explicit limitation rather than an invented chart or an unauthorized fetch.

## 8. Acceptance checklist

- [ ] Capture the deployed Lane5/Ember versions and prove schema v2 renders before enabling new kinds.
- [ ] Each of the five selectors renders from fetched rows using document source formats.
- [ ] Multi-series line keeps a February null as a gap; shuffled periods reorder entire rows correctly; duplicate periods fall back.
- [ ] Grouped bars render positive, zero, negative and null measures honestly.
- [ ] Stacked bars render two non-zero components; negative/null/percent cases fall back.
- [ ] Heatmap renders two dimensions; duplicate pairs fall back; missing cells remain blank.
- [ ] Donut renders 2–5 parts, including a zero part; 6 parts, all-zero totals, negative/null values and missing composition fall back.
- [ ] Conflicting currency/unit/percent scale, missing/ambiguous columns, unsafe numeric values, >4 series, >120 rows and partial pages fall back without dropping data.
- [ ] View data and CSV use the chart's snapshot. Loading both does not mint independent results.
- [ ] Initial live render, history reload, pagination, explicit refresh, snapshot replacement/expiry, interrupted requests and failed recreation behave correctly.
- [ ] Exact decimal/currency/percent/unit formatting works in tooltips and table; CSV remains raw. No fabricated Data as of.
- [ ] Desktop/mobile layout, readable legend/axes, keyboard drawer behavior, accessible chart descriptions and reduced-motion pass.
- [ ] Default Lane5 config still advertises/emits only six original kinds. Enable new kinds in a test deployment, then verify live DeepSeek + MCP through Ember for all five.
- [ ] Disabling a kind again does not break previously saved answers containing it.

## 9. Evidence and rollout boundary

Lane5 commit `b5c673b` passed a 1,302-test regression selection, subsequent focused policy/wiring/browser checks, and five real DeepSeek + local MCP/ClickHouse fixture runs. Each opt-in case emitted its requested kind and passed judge review. A default-config donut request emitted ordinary bars. Actual approved documents and result rows rendered in the reference browser UI. See the [Lane5 chart decision and verification record](../decisions/2026-11-02-chart-components.md) for trace IDs and limits.

Those results are **not Ember acceptance** and are not production-data verification. Keep the new components disabled in shared deployment until the checklist above passes. Deploy Ember support first, enable Lane5 components deliberately, then run the integrated battery. No new backend chart endpoints are required.
