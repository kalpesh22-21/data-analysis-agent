# API-owned query provenance (version 1)

Date: 2026-09-28

The ClickHouse API is the authority for access dependencies of executed SQL.
The agent must not reparse a successful query against a second catalog to decide
its provenance. The agent still handles errors, model recovery, result delivery,
and scope checks when replaying stored results. Its pre-execution measurement and
aggregation checks remain independent of this access-provenance contract.

## Response

MCP `runQuery`, `sampleRows`, and `explainQuery` add this sibling to successful
result objects, including successful empty results:

```json
{
  "columns": ["Total Pay"],
  "rows": [[123]],
  "row_count": 1,
  "provenance": {
    "version": 1,
    "columns": [["hr.payroll", "GrossPay"], ["hr.payroll", "PayDate"]]
  }
}
```

`columns` inside provenance is a sorted, unique list of `[database.table, column]`
access dependencies, including filters, joins, grouping and ordering, not output
aliases. An empty list is valid for a constant query. Hidden policy columns are
excluded consistently with the API's scope enforcement. Scratch dependencies are
retained; their ownership is enforced by the API's bound session.

The API reuses the dependency set from its existing security guardrails. It does
not extract provenance a second time after execution. The receipt belongs to the
same authenticated response as the data. REST response shapes remain unchanged;
the service's `include_provenance` flag is enabled by the MCP wrappers.

## Agent handling

The dispatcher validates version/shape and checks the receipt against the current
scope before exposing rows. It persists the dependencies in existing provenance
fields and removes the wire metadata from model-facing result data. Unknown
additive receipt fields are ignored; unsupported versions are rejected explicitly.

For final answer tables, an exact SQL and credential match reuses a bounded cached
receipt. Otherwise the agent calls API `explainQuery` to validate the designated
SQL without fetching its rows. Validation failures return the API error to the
model; they are not silently dropped.

API/SQL errors keep their original recognized code and bounded message in the
agent's persisted diagnostic path, with credentials removed. SQL repair hints
supplement the actual error. Errors are not evidence of missing data, and failed
attempts never become successful empty results. Progress remains summary/status
only; raw diagnostics do not become progress labels.

Missing, malformed or out-of-scope receipts produce `API_PROVENANCE_INVALID`,
without exposing rows. This is a non-retryable API contract failure, not a SQL
repair request. Old stored results with unknown provenance remain blocked from
replay and receive an explicit `API_PROVENANCE_MISSING` explanation rather than a
vague “result withheld” message. Other supported results remain usable.

## Rollout and verification

Deploy the ClickHouse API first, then the agent. An older API without receipts
will trigger an explicit contract error in the new agent; there is intentionally
no fallback to local SQL provenance extraction.

Coverage includes aggregate/filter dependencies, empty results, hidden policy
columns, absent/malformed receipts, unavailable agent catalogs, scope mismatch,
table validation, unchanged REST shapes, and preservation of actual SQL errors.
