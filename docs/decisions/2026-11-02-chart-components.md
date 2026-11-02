# Opt-in chart components for answer-document v2

Status: Lane5 backend and reference UI implementation. Ember must adopt these selectors before enabling new components in a shared deployment. Existing v2 selectors are unchanged; this does not restore v1 embedded chart values or solve the deferred frozen-answer-results issue.

## Configuration and rollout

`RuntimeSettings.allowed_ui_components` / environment `ALLOWED_UI_COMPONENTS` is a JSON array. The default remains:

```dotenv
ALLOWED_UI_COMPONENTS=["text","table","figure","facts","bars","line"]
```

Available opt-ins are `multi_series_line`, `grouped_bars`, `stacked_bars`, `heatmap`, and `donut`. For example, enable donuts while retaining existing components:

```dotenv
ALLOWED_UI_COMPONENTS=["text","table","figure","facts","bars","line","donut"]
```

Unknown or duplicate names fail startup. `text` and `table` are mandatory for limitations and safe fallback. Restart the runtime after changing configuration. There is no user-controlled request override. The setting controls creation of new answers; previously saved documents remain readable by a capable UI.

The configured policy is scoped to each run/resume and reset on completion or failure. Agent tool schemas, judge corrections, partial answers and salvage schemas use the same policy; validation and materialization reject disabled presentations. Shared schema objects are not mutated. A disabled kind must not be smuggled in as an enabled single-series kind with extra series fields.

New kinds are separately named so enabling existing `line` or `bars` cannot accidentally enable multi-series or stacking. This refines the earlier proposal to overload line.series and bars.mode. Model input uses category_column / measure_column; the sealed document uses x_column / y_column as before.

## Model selection and wire examples

All data presentations select one exact successful result_id. Series are wide result columns, not joins/pivots calculated by the UI. Formats exist only on sources[result_id].column_formats. The model never supplies points, values or per-widget formats.

Model proposal:

```json
{"kind":"multi_series_line","result_id":"r","category_column":"Month","label":"Monthly earnings (USD)","series":[{"measure_column":"Regular Earnings","label":"Regular"},{"measure_column":"Overtime Earnings","label":"Overtime"}]}
```

Delivered presentation:

```json
{"kind":"multi_series_line","result_id":"r","x_column":"Month","label":"Monthly earnings (USD)","series":[{"y_column":"Regular Earnings","label":"Regular"},{"y_column":"Overtime Earnings","label":"Overtime"}]}
```

`grouped_bars` uses the same selectors. `stacked_bars` additionally requires `additive: true` in both proposal and wire: measures must be disjoint additive components, not overlapping counts, rates, or a total plus its parts. Multiple series do not carry ranking fields.

Heatmap proposal selects category_column, series_column, measure_column; wire selects x_column, y_column, value_column:

```json
{"kind":"heatmap","result_id":"r","x_column":"Month","y_column":"Department","value_column":"Overtime Hours","label":"Overtime hours"}
```

Donut proposal selects category_column and measure_column, plus composition:true. Wire:

```json
{"kind":"donut","result_id":"r","x_column":"Department","y_column":"Headcount","label":"Active employees by department","composition":true}
```

Composition is a semantic commitment, not inferred from a unit or row count: exhaustive mutually exclusive parts of the stated whole. A top-N needs explicit SQL-computed Other to represent a whole. The judge reviews that commitment against SQL and the requested scope. Do not sum percentages or non-additive rates into donut shares. Stacks and donuts reject known percent measures.

## UI obligations and reference guards

- Render from the same selected result used for View data/CSV, without client joins, pivots or aggregation. Do not silently chart a partial page or drop rows. Use a table if the result cannot be rendered completely within limits.
- Series: 2–4, unique columns and labels, common axis format signature (kind/currency/percent scale/unit). Precision may differ for individual tooltips. Unknown lineage does not grant units. Conflicting formats fall back to the table.
- Multi-series lines: a common temporal axis, at least two unique periods, chronological ordering applied to complete rows, nulls remain gaps. An all-null series falls back rather than disappearing.
- Grouped bars allow negative values with a zero-based scale and preserve null gaps. Stacked bars require nonnegative, non-null components; unsupported signed stacks fall back.
- Donuts: 2–5 unique categories, nonnegative non-null values and a positive total. Zero-valued categories retain their labels. Larger compositions fall back to the table, not silent top-five slicing.
- Heatmaps: unique category pairs, missing values remain blank, never zero. No duplicate-cell summation. Reference limits: 24 x-categories, 20 y-categories, 120 returned rows.
- Reference chart limit: 120 rows; max multi-series points 480. These are rendering guards, not query LIMIT clauses or population restrictions. Preview truncation alone does not limit backend selectors.
- Reference UI declines unsafe numeric geometry, preserves formatted values in exact accessible labels/tooltips, includes View data/CSV and uses no animation. Ember should implement the same behavior with its kit wrapper, theme palette, accessibility, and reduced-motion support.

Reference rendering is intentionally not an Ember implementation. No Ember branch was available in this workspace. New components remain disabled by default until the consuming UI can render them.

## Verification — 2026-11-02

- Broad runtime/composer/recovery/UI regression selection: 1,302 passed. Subsequent focused checks: 20 chart-policy/runtime tests, 28 browser renderer tests, and 73 application/config/tool-schema tests passed. These selections overlap; they are not a summed suite count.
- Real DeepSeek (`accounts/fireworks/models/deepseek-v4p1-flash`) through the local HTTP/SSE runtime and MCP/ClickHouse: all five opt-in kinds completed, were judge-approved, and emitted their requested kind. These runs used local fixture data, not a production tenant.
- A separate process using the default six-kind configuration advertised exactly those six kinds to the model. An explicit donut request completed using ordinary bars, without emitting a disabled presentation.
- Persisted-document, v2 selector contract, terminal delivery, and connected tracing checks passed. The five actual approved documents were rendered with their recorded result rows in Playwright; all five produced charts without fallback. Screenshot inspected.
- Browser regressions cover partial results, duplicate categories/periods, incompatible formats, null gaps, negative stacks, invalid composition and rendering bounds; unsupported cases use the same-result table fallback. Runtime tests cover concurrent policy isolation and rejected disabled-kind calls.

Local evidence: `/tmp/charts-live/results.json`, `/tmp/charts-live/verification.json`, `/tmp/charts-live/reference-charts.png`, and `/tmp/charts-default-live/results.json`. These temporary artifacts are not committed.

| Live case | Trace | Seconds |
|---|---|---:|
| Donut | `9c46c744159e6dcbc60cfcce8a98e333` | 39.58 |
| Grouped bars | `7cc044116cddc137b67bcb6a5201e172` | 70.77 |
| Stacked bars | `2a027204c605abe708c0bbc43f3f7e7a` | 74.24 |
| Multi-series line | `7a595f38502bbf990cac1127ce3bcdaf` | 76.62 |
| Heatmap | `19fcd899dd0e70500d1a9a98b033584c` | 64.85 |
| Default-disabled donut → bars | `bf3ad56bc9867618627c0300f1a55572` | 51.25 |

No production deployment or Ember browser acceptance is claimed. Frozen answer-time snapshots remain the separate A25 task.
