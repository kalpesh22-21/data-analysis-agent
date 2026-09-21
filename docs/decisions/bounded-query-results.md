# Bounded query results — 2026-09-20

The user approved implementing the first two result-size safeguards before
finalizing judge improvement #3 (evidence for each numerical claim). This change
adds source limits and receive limits; it does not implement claim verification
or relocate semantic review.

## Implemented

| Layer | Setting | Default | Behavior |
|---|---|---:|---|
| clickhouse-api | MAX_RESULT_ROWS | 10,000 | Existing row limit; now must be positive |
| clickhouse-api | MAX_RESULT_BYTES | 4,194,304 (4 MiB) | New native result-byte limit |
| Agent MCP client | MCP_MAX_RESPONSE_BYTES | 16,777,216 (16 MiB) | New limit per HTTP response, before JSON/SSE parsing |

ClickHouse queries receive both native result limits through readonly_settings,
with result_overflow_mode=throw. The byte setting is reserved from tenant-setting
overrides. Environment examples and Helm defaults expose the new limits. Native
limit errors retain the existing CLICKHOUSE_QUERY_ERROR path, with guidance to
narrow or aggregate the request without silently dropping required records.

The MCP client checks Content-Length before reading and counts actual streamed
bytes even when that header is absent, invalid, or too small. A chunk crossing
the limit is never yielded to the JSON/SSE consumer. The response is closed and
the operation fails with RESULT_TOO_LARGE (retryable GATE), with no successful
result passed to storage. All MCP HTTP responses are covered, including tool
catalog/initialization responses and error bodies, not only runQuery results.
The 16 MiB transport allowance accounts for protocol and JSON expansion; it is
not a conversion of the native 4 MiB limit.

The client requests identity encoding. A server returning compressed content
anyway is rejected before HTTPX decompression with
MCP_RESPONSE_UNSUPPORTED_ENCODING (non-retryable infrastructure failure). This
avoids unbounded decompression ahead of the byte counter.

A per-operation watcher terminates the request promptly when the guard fires:
the installed SDK can otherwise log a response-reader failure while leaving its
pending request waiting. Both normal completion and guard failure close the SDK
session and cancel the watcher. Existing dispatcher denial telemetry records
the error code without adding response payloads to the denial.

## Validation

- Agent runtime: 3,900 passed, 5 skipped.
- Source settings/client/error tests: 55 passed.
- Transport tests cover JSON and SSE, Content-Length rejection before reading,
  absent/false lengths, exact-limit success, compression rejection, SDK error
  propagation, normal SDK completion and resetting the counter per response.
- Live MCP: a normal constant query completed; a deliberately tiny receive cap
  rejected initialization promptly with RESULT_TOO_LARGE.
- Live ClickHouse: row overflow and a materialized byte-overflow result both
  threw TOO_MANY_ROWS_OR_BYTES with no usable stdout; a small result succeeded.

## Boundaries

These are result limits, not an absolute pod RSS or query-working-memory budget.
Native ClickHouse byte accounting differs from serialized HTTP size and checks
blocks. See the [ClickHouse result-limit documentation](https://clickhouse.com/docs/reference/settings/session-settings/max-result).
The local server also accepted an oversized constant-only SELECT under a tiny
native byte limit; the independent receive cap remains necessary. Native limits
alone must not be described as a strict byte bound on every serialized response.

Allowed JSON still expands into Python objects. Concurrent requests, database
query working memory, storage/document limits, and upstream ClickHouse API
materialization need their own budgets. Existing API row truncation flags remain
unchanged. This change does not introduce result truncation on limit failure.
Non-MCP side-channel HTTP clients and existing stored results are outside the new
receive guard. Configuration changes require deployment/restart; no running
service was redeployed by this task.

## Judge discussion status

- Improvement #1 agreed: deterministic checks before execution; semantic review
  after execution, before answering. Implementation pending.
- Improvement #2 agreed: all table-level rules for referenced tables, explicit
  blueprint rules and relevant column documentation. Implementation pending.
- Improvement #3: direct-value claim verification is still being discussed. The
  source/receive safeguards above are the implemented prerequisite; storing
  arbitrarily large results was not approved.
- Improvements #4–#7 remain to be discussed/finalized in sequence.
