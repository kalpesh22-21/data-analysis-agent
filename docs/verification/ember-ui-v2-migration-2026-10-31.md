# Ember migration guide: Lane5 answer-document v2

Date: 2026-10-31. Audience: Ember developer implementing the answer renderer, saved results, paging and export.

This guide consolidates the maintained [answer-document specification](../ui-answer-document-spec.md), [value-formatting contract](../decisions/ui-value-formatting-contract.md), [assumptions contract](../decisions/ui-assumptions-contract.md), and [developer handoff](remote-developer-handoff-2026-10-30.md). It describes the current implementation and replaces older v1/block-era wire examples. A repository push does not establish that the deployed service has this version.

## What changed

| Previous behavior / field | Current contract / required migration |
|---|---|
| `schema_version: 1` | `schema_version: 2`. Migrate or explicitly refresh saved v1 documents; do not reinterpret them as v2. |
| `assistant_text`, `answer_sql`, `answer_tables`, `layout`, `blocks` as parallel live rendering inputs | Render `answer_document.takeaway` and `sections`. Do not require legacy fields for a successful response. |
| `table_ref` and backend ID-to-SQL resolution | Each `sources[result_id]` contains exact validated SQL. `result_id` is only a local join key between a presentation and its source. It is not an API execution token. |
| Backend-rendered `value`, `formatted_value`, chart `points`, `items`, per-widget `format`, or embedded rows | Presentations contain kind and column selectors. Fetch rows, then render/format them in Ember. |
| Agent-controlled `supporting_data`, `supporting_table`, `fallback`, `fallback_table` | Removed. Ember owns View data, Download data, and display-size/type fallback. No flag from the agent enables these controls. |
| Duplicate chart and table sections for the same result | One chosen presentation plus UI-owned data controls using the same source/snapshot. |
| Separate UI snapshot-create call before fetching rows | `POST /query/page` with SQL returns the first page and saved `snapshot_id` together. |
| SQL reexecution or table-ref lookup for every page | Later calls to the same `/query/page` endpoint use `snapshot_id`, not SQL. |
| A proposed new full-file CSV export endpoint | Not added. Use existing ordered CSV part links. Do not concatenate parts. |
| Formatting arguments on page/refresh requests | Not accepted. Keep final `column_formats` with the saved answer and apply them locally. |

## Terminal object and source resolution

`POST /turn` and `/turn/resume` are SSE streams. Read through the terminal `result` event, not just the HTTP 200. Ignore SSE heartbeat comments; show progress messages as progress, not widget instructions. A `paused_*` result carries `pending_question` and no composed answer document. A text-only completed answer with a takeaway and zero sections is valid.

Illustrative terminal event (SQL is an example; use the exact SQL supplied by the real source):

```json
{
  "status": "done",
  "pending_question": null,
  "tool_calls_made": 5,
  "answer_document": {
    "schema_version": 2,
    "answer_id": "answer-0",
    "revision": 1,
    "generated_at": "2026-10-31T12:00:00Z",
    "takeaway": "Monthly gross earnings paid in 2026.",
    "sections": [{
      "id": "monthly-payroll",
      "title": "Monthly gross earnings paid, 2026",
      "evidence_refs": ["payroll-result"],
      "caveats": [],
      "actions": [],
      "presentation": {
        "kind": "line",
        "result_id": "payroll-result",
        "x_column": "Month",
        "y_column": "Gross Earnings",
        "label": "Gross earnings paid"
      }
    }],
    "sources": {
      "payroll-result": {
        "sql": "SELECT toStartOfMonth(pay_date) AS Month, sum(amount) AS `Gross Earnings` FROM dbpcm_warehouse.payroll WHERE register_type = 'EARN' AND pay_date >= toDate('2026-01-01') AND pay_date < toDate('2027-01-01') GROUP BY Month ORDER BY Month",
        "column_formats": {
          "Month": {"kind": "date", "grain": "month"},
          "Gross Earnings": {"kind": "currency", "currency": "USD", "precision": 2}
        },
        "retrieved_at": "2026-10-31T11:59:30Z",
        "row_count": 6,
        "completeness": "complete"
      }
    }
  },
  "review": {"status": "approved", "attempts": 1, "site": "exit_table"}
}
```

Resolve `section.presentation.result_id` within this document's `sources`. Send its `sql` to Lane5. Neither `section.id`, `answer_id`, nor `result_id` belongs in a paging request. Keep exact case/spaces in column selectors and format-map keys. Do not rewrite SQL to add paging LIMIT/OFFSET; a source may already have an intentional top-N limit.

## Presentation adapters and mandatory data controls

| Kind | Bindings | Rendering | View data / Download data |
|---|---|---|---|
| `text` | `markdown` | Safe Markdown, no query | Neither; no data source |
| `figure` | `result_id`, `column`, `label` | Fetch one-row source; display selected scalar | **Exception: neither control required** |
| `facts` | `result_id`, `columns` | Fetch one-row source; display selected columns in order | **Both always offered** |
| `table` | `result_id` | Grid from ordered page columns/rows | Grid is View data; **Download data always offered** |
| `bars` | `result_id`, `x_column` or `x_columns`, `y_column`, `label`; optional `ranking` | Categories from x bindings, numeric measure from y | **Both always offered** |
| `line` | `result_id`, `x_column`, `y_column`, `label` | Temporal x, numeric y; sort complete row pairs chronologically | **Both always offered** |

“Always” means independent of agent flags, `actions`, preview size, or whether the agent included a table section. Keep controls visible during loading/errors with an appropriate disabled/retry state. Empty saved results have no downloadable parts; show that state instead of inventing an export. A chart's View data opens the same underlying source grid, and its Download data exports that same saved result. Share source loading/cache where sections reference one result.

Do not require the backend to choose whether a chart needs supporting data. Do not add a second visible table section merely to make data accessible. `actions` are separate prepared capabilities, not the authorization switch for these ordinary controls.

Use source formatting for cells, facts, figures, tooltips and axes. No backend display strings are supplied. `ranking`, if present, supplies `direction: ascending|descending` and `ties: dense|competition`; preserve query top-N semantics and do not invent rankings from partial pages.

Fetch enough saved rows for an honest chart. Never silently treat page one as the complete series. If UI limits or unsupported data prevent plotting, show the same source as a table with a short display explanation, retaining Download data. Do not ask the user how to render or trigger an agent rerun. Nulls are missing values/gaps, not zero; do not drop mismatched rows silently. Unknown new kinds with a resolvable data source can fall back to its table. Fail visibly if a referenced source is missing.

## URLs and request flow

All paths below target the configured **Lane5 base URL**, through Ember's authenticated backend proxy as appropriate. They are not new direct clickhouse-api routes. Forward the session JWT as `Authorization: Bearer <JWT>` and the same `X-Session-Id` for every authenticated request. Do not put either into URLs. Lane5 supplies backend catalog scope itself.

### First load

```http
POST /query/page
Content-Type: application/json
Authorization: Bearer <session JWT>
X-Session-Id: <session ID>
```

```json
{"sql":"<sources[result_id].sql>","limit":100,"offset":0}
```

Response example:

```json
{
  "snapshot_id": "11111111-1111-4111-8111-111111111111",
  "columns": ["Month", "Gross Earnings"],
  "rows": [["2026-01-01", "1100.00"]],
  "row_count": 6,
  "limit": 1,
  "offset": 0,
  "has_more": true,
  "created_at": "2026-10-31T12:00:01Z",
  "expires_at": "2026-10-31T16:00:01Z",
  "reset_table": false
}
```

The page example uses limit 1 to illustrate paging. Rows are arrays aligned with `columns`. Empty exports can return `columns: []`, `rows: []`, `row_count: 0` and `has_more: false`; show the empty state even if the saved source still has format metadata. Do not reject that response solely because its empty column list has no format keys. Persist `snapshot_id` against this answer revision and source ID. Lane5 internally uses the API's existing `POST /query/snapshots` and `GET /query/snapshots/{id}/page`; Ember need not perform those two calls. Those routes still exist; this is a consolidated UI flow, not removal of the API snapshot service.

### Later pages / saved reload

```http
POST /query/page
```

```json
{"snapshot_id":"11111111-1111-4111-8111-111111111111","limit":100,"offset":100}
```

Send exactly one of `sql` or `snapshot_id`. Limit is 1–1000, offset is nonnegative. Do not send `column_formats`, `table_ref`, or `refresh_of` when paging by ID. Saved paging reads frozen rows without SQL reexecution. Repeated SQL under the same owner/session/scope may reuse a snapshot, so SQL alone is not a refresh instruction.

Ember may persist the answer in its own store/S3; it does not need Lane5's complete history to render it. Save the whole v2 document, formats and source/snapshot associations. If no valid saved snapshot is available, use the source SQL with current authorization. A retained historical answer is not proof of current access.

### Explicit data refresh

```json
{"sql":"<same source SQL>","refresh_of":"11111111-1111-4111-8111-111111111111","limit":100,"offset":0}
```

Post to `/query/page`. Replace the snapshot association and rows when `reset_table: true`; never append replacement rows to the old result. Refresh data controls using that new snapshot. Retain source formats from the answer. This refresh does **not** regenerate the takeaway; make that distinction visible.

To refresh the analysis/narrative as well, use `POST /answer/refresh` with `{"answer_id":"answer-0","expected_revision":1}` and read its SSE terminal replacement document. Replace the document/revision and discard prior source/snapshot associations. Do not fabricate an increment locally.

### Download data: existing CSV parts

First ensure the source has a saved snapshot using `/query/page`. Then:

```http
GET /query/snapshots/11111111-1111-4111-8111-111111111111/download-urls
```

Example for authenticated storage:

```json
{
  "files": [{
    "part": 1,
    "url": "query/snapshots/11111111-1111-4111-8111-111111111111/download?part=1",
    "rows": 6,
    "filename": "query-export-part-0001.csv"
  }],
  "expires_in": null
}
```

Offer the ordered `files` as separate downloads with the supplied filenames. Each is a standalone CSV with its own header. Relative `query/...` paths resolve against the Lane5 service base (not the current Ember page path) and require the same authentication; the bytes stream through Lane5 from clickhouse-api. With S3, `url` is instead an absolute presigned URL and bytes download directly from S3: **do not attach the session JWT to that URL**. Obtain fresh links when presigned links expire. `expires_in` describes link lifetime, not data freshness.

Do not concatenate parts or expect a `download_url` in the page response. There is no new whole-file endpoint, and no model call or SQL rerun is required for each download. CSV retains raw export values, not UI currency symbols or formatted numbers; do not regenerate it from the visible page. Empty results return `files: []`.

## Formatting, assumptions and timestamps

The authoritative display map is `sources[result_id].column_formats`. Blueprint formats are authored; ad-hoc results can be enriched by the existing non-reasoning formatter before judge review. Code accepts only eligible formatting entries and preserves protected metadata. Ember does not call that model or resend formats to query APIs.

- `text` / `identifier`: preserve strings, leading zeros, phone punctuation and long IDs. Never infer numeric formatting from their appearance.
- `integer` / `number`: group numbers; respect precision without silently rounding nonintegral integer values. Use decimal-safe handling for large integers and decimal strings.
- `currency`: declared currency code, default two fractional places; never assume USD.
- `percent`: `scale: ratio` means multiply by 100; `scale: percent` is already percent units.
- `date` / `datetime`: calendar dates must not shift timezone; timestamps preserve instants. Optional `grain` describes year/quarter/month/day/timestamp.
- `boolean`: Yes/No. Null is Not available, not zero. Missing metadata uses raw text. Optional unit/precision affect display only.

Examples: identifier `"02108"` → `02108`; count `75039` → `75,039`; USD `"1157.14"` → `$1,157.14`; ratio `"0.25"` → `25.00%`. Retain raw values for sorting, geometry and exports.

Recorded assumptions are incorporated into approved section caveats. Render those once; do not recreate a legacy parallel assumptions panel or infer additional caveats from SQL. Preserve distinct limitations and optional section explanations. Titles should appear once per card. Render Markdown safely.

Use source `data_as_of`/`freshness` only when supplied by code. `retrieved_at` and snapshot `created_at` mean retrieval/refresh times, not data-as-of. Never infer freshness from an unknown/null date or insert it into the takeaway. Judge-corrected prose and bindings are already persisted as delivered; follow-up context uses that corrected answer. Clarification question/options are likewise saved for resume.

## Error handling and acceptance

Preserve the JSON body and `Retry-After` through Ember's proxy. In particular:

| Response | UI handling |
|---|---|
| 400/422 | Show request error; no blind retry loop. |
| 403 | Clear inaccessible rows and stop use of old snapshot/download links. |
| 404/410 | Invalidate active snapshot; retain the historical answer and offer explicit recreation/refresh from SQL under current access. |
| 409 `SNAPSHOT_IN_PROGRESS` | Respect `Retry-After`; retry same operation, not parallel snapshots. |
| 409 `SNAPSHOT_REPLACED` | Read returned `snapshot`, clear old rows (`reset_table: true`), replace snapshot ID, fetch offset zero. Never replay old offsets or old export bytes. |
| 429 | Respect `Retry-After`. |
| Unconfigured storage / upstream error | Show recoverable failure; do not claim an empty result or silently execute SQL per page. |

Check structured errors nested under `detail` as well as the replacement envelope; do not collapse them into a message string and lose recovery fields. Lane5 performs one bounded scope-refresh/rebuild attempt; Ember still handles the replacement response.

Acceptance: live SSE and saved reload for every presentation; figures alone omit data controls; tables/facts/bars/lines expose them; exact source-column binding; ZIP/phone/large-ID preservation; page two without reexecution; ordered multipart CSV; refresh replaces rows; revised answers reset associations; 403/expiry/scope replacement; partial and salvage answers using the same v2 contract; text-only answers; pause → choice → completion or cancellation. Verify with actual Ember adapters. Local reference-UI tests do not certify Ember or production S3/Trino behavior.

Local release evidence: [focused battery report](focused-release-battery-2026-10-31.md), including 13 passing focused cases, 16 terminal responses and 6,940 passing runtime/UI tests. Follow the remote acceptance checklist above after adopting the changes.
