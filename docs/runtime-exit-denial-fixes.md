# Resilient exit and column-denial fixes

Date: 2026-09-28

## Resilient exit

A stopped or failed turn first reuses an unchanged, independently approved partial
answer. If no such answer exists and a judge review has already failed or returned
unreviewed in this request, exit recovery skips another judge call. Otherwise it
starts one attempt only if at least 30 seconds of aggregate review budget remain.
Its timeout is the configured judge timeout capped by the remaining budget,
including evidence loading and enrichment. With less than 30 seconds, it skips
new review and uses the safe fallback. Cached approved partials can still be reused.
Timeout cancels the attempt; final delivery does not review the fallback again.

The existing safe ship disposition and refusal explanation remain intact. A
rejected answer stays rejected; review unavailability never grants approval.
A fallback cannot claim tables/cards are included after they have been removed.
Primary proposal review budgets and judge input construction are unchanged.

## Column-denial event

Post-call tool spans emit `tool.column_denial` for supported API column denials.
Attributes are `tool_name`, `tool_call_id` when available, `error_code`, and
`columns` (up to 16 qualified column identifiers). Extraction recognizes bounded
API error formats and corroborates identifiers against the requested columns.
No full diagnostic, SQL literal, or result row is added to this event. Telemetry
failures cannot fail the tool call. This is distinct from summary-only SSE progress.

## Semantic retry bound

For `runQuery`, `explainQuery`, and `sampleRows`, two failures identifying the same
column and error category prevent a third attempt against that target, even if
SQL aliases, projections or limits change. The count is isolated by tool, turn,
and access-scope hash. A successful call using the target resets its count.
Queries removing the denied dependency remain allowed.

Permission failures retain `COLUMN_SCOPE_VIOLATION`; missing-column exhaustion uses
`SQL_REPAIR_EXHAUSTED`. Neither proves that data is absent. Uncertain targets and
unrecognized error formats retain the existing exact-SQL and no-progress guards;
this guard never guesses a semantic target or performs provenance extraction.

## Validation

Regression tests exercise cancelled exit review, unavailable-review skipping,
approved-subset reuse, preservation of rejection and history, real span events,
SQL rewording, corrected queries, permission categories, scope isolation, and
successful-target resets. No ClickHouse API changes are required for these fixes.

## Scalar data answers

Counts and other single-value warehouse answers include both concise prose and a
query-backed table. The finalizer attaches cited successful scalar runQuery or
verified runBlueprint results by their existing result IDs when the model omits
the table. It does not rerun SQL, include unrelated reads, or restore explicitly
excluded components. Existing SQL resolution, scope checks, judge review, and
history persistence apply. Greetings and product-help responses need no table.
