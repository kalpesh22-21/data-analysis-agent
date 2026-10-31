# Answer document v2: SQL sources and UI-owned rendering

Status: backend implementation; Ember must adopt this breaking response contract.
For the consolidated before/after checklist, exact URLs and migration examples, see
the [Ember v2 migration guide](verification/ember-ui-v2-migration-2026-10-31.md).
This replaces schema v1's handles, embedded values and chart points. There is no
parallel v1 payload on new turns. Existing saved documents require an explicit UI
migration or refresh; do not reinterpret v1 fields as v2.

## Terminal response

`/turn`, `/turn/resume`, and `/answer/refresh` retain their SSE lifecycle. Terminal
responses contain `status`, optional `pending_question`, `tool_calls_made`,
`answer_document`, and `review`. Paused turns have no composed document. Heartbeat
comments continue during long operations. Progress messages are user-facing, not
instructions for choosing widgets.

```json
{
  "schema_version": 2,
  "answer_id": "answer-0",
  "revision": 1,
  "generated_at": "2026-10-30T12:00:00Z",
  "takeaway": "Monthly gross earnings paid in 2026.",
  "sections": [{
    "id": "monthly-payroll",
    "title": "Monthly gross earnings paid, 2026",
    "evidence_refs": ["result-payroll"],
    "caveats": [],
    "actions": [],
    "presentation": {
      "kind": "line",
      "result_id": "result-payroll",
      "x_column": "Month",
      "y_column": "Gross Earnings",
      "label": "Gross earnings paid (USD)"
    }
  }],
  "sources": {
    "result-payroll": {
      "sql": "SELECT toStartOfMonth(pay_date) AS Month, sum(amount) AS `Gross Earnings` FROM dbpcm_warehouse.payroll WHERE register_type = 'EARN' AND pay_date >= '2026-01-01' AND pay_date < '2027-01-01' GROUP BY Month ORDER BY Month",
      "column_formats": {
        "Month": {"kind": "date", "grain": "month"},
        "Gross Earnings": {"kind": "currency", "currency": "USD", "precision": 2}
      },
      "retrieved_at": "2026-10-30T11:59:30Z",
      "row_count": 6,
      "completeness": "complete"
    }
  }
}
```

The SQL above illustrates the shape, not a universal payroll query. Runtime SQL
is the exact successful execution selected and approved for the answer, including
bound blueprint SQL. A result ID only joins sections to sources; it is not an
execution token. SQL and all column names belong in the data contract, not prose.

## Presentation kinds

| Kind | Fields besides `kind` | UI behavior |
|---|---|---|
| `text` | `markdown` | Render safe Markdown; no query. |
| `table` | `result_id` | Fetch and page the source. |
| `figure` | `result_id`, `column`, `label` | Fetch a one-row result and format that column. |
| `facts` | `result_id`, `columns` | Fetch a one-row result and show those columns in order. |
| `bars` | `result_id`, `x_column` **or** `x_columns`, `y_column`, `label`; optional `ranking` | Build comparisons from rows. Compound categories retain separate column bindings. |
| `line` | `result_id`, `x_column`, `y_column`, `label` | Parse periods using column metadata, order complete row pairs chronologically, preserve null gaps. |

`ranking`, when supplied, contains `direction: ascending|descending` and
`ties: dense|competition`. Preserve query order and requested top-N semantics;
do not invent grouping, totals, shares, aggregation, or omitted records.

There are **no rows, points, items, rendered values, or formatted-value strings**
in data presentations. Source formats apply to axes, tooltips, figures, facts,
and cells. Null is missing, not zero. Formatting affects display, not sorting,
chart arithmetic, or raw export data. See [value formatting](decisions/ui-value-formatting-contract.md).

The UI must offer **View table and Download CSV parts for every data result except
`figure`**. Tables expose their grid and CSV action; facts, bars and lines expose
table/CSV options without adding a duplicate visible section. Figures render their
single value without these options. Text has no query or data controls.

Control ownership and display fallback belong to the UI. There are no
`supporting_data`, `supporting_table`, `fallback`, or `fallback_table` fields.
Unknown or unsupported rendering falls back to the same source table without
asking the agent to query again.
Never silently drop rows or plot only the first fetched page as a complete chart.
Backend preview truncation is not a chart-size limit: fetch the saved result and
apply UI display guards. Backend validates source/column selection and known type
compatibility; the judge reviews measurement, scope, factual accuracy and result bindings. Presentation preferences alone do not block delivery.

## One entry point for execution and paging

First request to Lane5:

```http
POST /query/page
Authorization: Bearer <session JWT>
X-Session-Id: <session ID>
```

```json
{
  "sql": "<sources[result_id].sql>",
  "limit": 100,
  "offset": 0
}
```

Keep `column_formats` in the saved answer document and apply it in the UI; do not
send it in paging or refresh requests. Lane5 calls the existing clickhouse-api snapshot-create endpoint and then its
snapshot-page endpoint, returning the first page in one UI response. Later pages
call only the existing snapshot-page endpoint. Both requests carry a server-minted
catalog scope; clickhouse-api has no new `/query/page` route. The API enforces read-only SQL,
row/column authorization, ownership and resource limits; receiving SQL from an
answer does not grant permission. SQL is not wrapped with page LIMIT/OFFSET: the
full allowed result is materialized once. Identical SQL in the same caller/session
and scope reuses its saved result under the existing deduplication policy.

The **same response** returns the first page:

```json
{
  "snapshot_id": "11111111-1111-4111-8111-111111111111",
  "columns": ["Month", "Gross Earnings"],
  "rows": [["2026-01-01", "1100.00"]],
  "row_count": 6,
  "limit": 1,
  "offset": 0,
  "has_more": true,
  "created_at": "2026-10-30T12:00:01Z",
  "expires_at": "2026-10-30T16:00:01Z",
  "reset_table": false
}
```

Subsequent pages and reload use the same endpoint:

```json
{"snapshot_id":"11111111-1111-4111-8111-111111111111","limit":100,"offset":100}
```

Send exactly one of `sql` or `snapshot_id`. Saved paging must not send formats or
`refresh_of`; it reads frozen rows without SQL execution. `limit` is 1–1000;
`offset` is non-negative. `has_more` and `row_count` are exact. Persist the returned
snapshot ID alongside Ember's saved answer/source association. There is no
`table_ref`, handle-to-SQL lookup, or separate snapshot-creation request in this flow.

Explicit **data refresh** posts SQL, `refresh_of: <old snapshot_id>`,
and `offset: 0` to `/query/page`. It returns the replacement's first page and
`reset_table: true`. Replace rows and the saved snapshot association;
never append new-result rows to old-result rows. Repeated refresh requests reuse
that replacement. Do not claim an old takeaway describes newly refreshed rows:
refresh the whole answer when a new analytical interpretation is required.

## CSV

Use the existing authenticated `GET /query/snapshots/{snapshot_id}/download-urls`
to obtain the ordered CSV parts. S3 storage returns presigned part URLs; Couchbase
storage returns authenticated relative `/query/snapshots/{id}/download?part=N`
URLs. Download each part separately with its supplied filename. Each part is a
standalone CSV with its own header. No concatenation, SQL rerun, or agent call is
needed. Empty results have no parts. Rows and column headers use the existing export behavior; answer display formats
are not forwarded to export storage. There is no new full-file download endpoint or `download_url` page field.

## Errors, scope and refresh

- 400/422: invalid SQL or request shape; show the error without retry loops.
- 403: authorization denied; clear unavailable data.
- 404/410: saved result missing/expired; retain the historical answer and offer
  explicit refresh using its SQL under current access.
- 409 `SNAPSHOT_IN_PROGRESS`: respect `Retry-After` and retry the same request.
- 409 `SNAPSHOT_REPLACED`: replace the returned snapshot association and clear old
  rows (`reset_table: true`), then request offset zero with its new snapshot ID.
  Lane5 preserves one bounded scope-refresh/rebuild attempt before this response.
- 429: respect `Retry-After`; do not create parallel snapshots for the same result.

Snapshot storage must be enabled/configured in clickhouse-api. Lane5's REST base
must target that API. There is no silent fallback to reexecuting SQL for each page.

`POST /answer/refresh {answer_id, expected_revision}` still reruns and reviews the
answer, streams a complete replacement document, and atomically increments its
revision. Reset source/snapshot associations with that replacement. Data refresh
and answer refresh are distinct: the latter also updates the narrative.

Freshness and `data_as_of` remain code-owned source metadata. A snapshot's
`created_at` is execution/storage time, not source freshness. Do not substitute it
for an unavailable freshness date. Assumptions/caveats remain approved prose;
render them once, and let paging controls disclose how many saved rows are shown.
Prepared actions retain their separate authenticated capability contract.

## Acceptance checks

Verify every data presentation using its SQL source; table/CSV options appear for
tables, facts, bars and lines, never figures;
one initial `/query/page` call returns rows; later pages do not reexecute SQL;
answer-source formats apply across paging and refresh; CSV part links preserve the existing download contract;
refresh resets rows; scope changes cannot expose old rows; expired results offer
refresh; a chart over the UI limit falls back honestly; no v1 values or handles
are required. Test terminal SSE and Ember-saved reload, not only backend fixtures.

### Judge corrections and history

Judge-approved wording, section and binding corrections use the same v2 response
contract and existing executed results. The persisted answer document is the one
sent to the UI; follow-up model context includes that document's sections and
sources, not an earlier finalization draft. Necessary clarifications may likewise
be rewritten before delivery; the delivered question/options are persisted for
resume. Cosmetic differences alone must not delay an otherwise correct answer.
