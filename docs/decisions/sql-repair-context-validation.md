# SQL repair context and harness recovery

Implements the harness-focused follow-up to [the difficult SQL probes](difficult-sql-probes.md). The changes apply to every model provider. The SQL procedure targets the actual ClickHouse execution backend.

## Changes

- Preserve structured, bounded SQL diagnostics through dispatch, trail persistence, and model-context reconstruction. Unknown-function errors can include a verified built-in spelling when that function appears in the submitted SQL. Raw engine messages, row values, and arbitrary suggestions are not forwarded. User-facing error text stays separate. Existing trail storage is reused; no persistence migration is required.
- Stop a third identical failed SQL attempt after two matching failures in the same turn and scope. SQL normalization preserves literals and removes comments. Different queries, engine errors, scopes, and turns are not conflated. The refusal is a gate, not another warehouse execution. Cardinality-probe failures now distinguish execution errors from evidence of duplicate rows and retain safe repair details.
- Failed tool receipts expose their real result ID and evidence-use instructions. A runtime `SQL_REPAIR_EXHAUSTED` receipt supports `EXECUTION_FAILED` for a blocked intent. It never supports completion, denied permissions, or a claim that data is absent. Ordinary syntax errors do not establish exhaustion. The existing explicit-binding path handles the new reason; no separate intent-state machine was added.
- Add a compact ClickHouse procedure covering exact function casing, bounded calendar generation, zero-activity groups, half-open dates, percentage denominators, conventional medians, and salary rankings with ties. The calendar recipe was executed through the real scoped MCP guard and returned all 36 months.
- Extend grain checks to joins against window-derived relations, including dependent CTEs. Undeduplicated joins on rank-level keys are rejected structurally, even if current data happens to have no ties. Supported joins still receive scoped uniqueness probes. Unrelated non-aggregate joins retain their existing behavior. Ranking functions are not treated as aggregate measurements, and a ranked employee relation may join its department directory without requiring one employee per department. Preflight parser failures report a safe line/column and syntax guidance instead of claiming aggregation risk.
- Clarify measurement and final-review responsibilities for zero-activity groups and source grain. Recorded hire dates can support a scoped hire-date comparison; reviewers must not invent hire-event/rehire semantics or demand historical events unless needed by the request or catalog.
- Fix provenance extraction for outer ORDER BY aliases whose CTE definitions are visited later. The defining columns are still independently validated; unresolved source expressions remain rejected.
- Preserve runtime fallback text in history with data-free provenance. Data-free denied calls no longer taint the provenance of successful answers. Unknown provenance on successful results or errors containing partial data still fails closed. This also repairs live/history discrepancies after a successful SQL correction.

## Validation

The new regression module covers diagnostic persistence and redaction, canonical failure references, repeated execution containment, scope/error/turn isolation, explicit blocked-intent binding, rank-level duplication, unrelated joins, probe diagnostics, fallback projection, and provenance after data-free versus partial-data errors.

Intermediate artifacts are retained; a judge-approved result alone is not counted as a pass. The first rerun exposed a snapshot/event-history overreach in measurement review. Subsequent runs exposed data-free denial taint and CTE alias visit-order failures in history, then a ranking follow-up paused after malformed SQL and overbroad grain checks. Each led to the targeted fixes above. A startup connection race before backend readiness is excluded from model-probe results; the retry began only after HTTP readiness.

## Execution boundaries

Live requests use fresh sessions against port 18104, GPT-4.1, the answer judge at 60 seconds with 32,000 evidence tokens, and unredacted Phoenix tracing. Backend output is inspected through SSE and persisted history. SQL results are independently checked with tenant-scoped read-only ClickHouse queries. Browser rendering and UI pagination execution are not part of this run. Warehouse fixtures were not changed.

## Final results

- Full repository suite, including local Couchbase: **8,213 passed, 193 skipped, zero failures/errors** in 76.74 seconds. The 193 skipped tests are opt-in/environment-dependent coverage, not hidden failures. Log: `/tmp/sql-repair-validated-suite.log`; JUnit: `/tmp/sql-repair-validated-suite.xml`.
- Six scenarios validated across the full reviewed battery plus the final targeted ranking follow-up. All six latest outcomes completed, matched SSE/history text and tables, had one approved/reviewed answer judge nested as `agent.turn → answer_judge → Response`, and had no judge timeout or ship guard. This is not a claim that every intermediate attempt passed.
- Q1: six fixture rows; actual final SQL also returned exactly three rows for three synthetic employees with two tied salaries (previous defective SQL returned five).
- Q2: independent headcounts and salary totals/averages matched for all three fixture departments.
- Q3: both Engineering and Sales tables reached the UI and history, with one and two above-average employees respectively.
- Q4: all 36 requested months, zero-hire months included; cumulative hires reached five. The reviewed run recovered from four blocked warehouse calls before finishing.
- Q5: all three departments survived; Engineering and Operations each show 0→0 hires with null percentage change; Sales shows 3→0, change -3 / -100%.
- Q6: conventional medians were 125,000, 62,500, and 115,000 for Engineering, Operations, and Sales; salary shares matched their totals divided by 700,000 (subject to returned decimal precision).
- Ruff checks and `git diff --check` passed. Backend remains running on port 18104 with tracing enabled.

| Scenario | Duration | UI tables | Phoenix trace |
|---|---:|---:|---|
| Q1 | 13.0 s | 1 | `700e7d11be87076f6281c44bdfc58b37` |
| Q2 | 12.52 s | 1 | `cd5d334aa6b66b174814b0c9e032896b` |
| Q3 | 18.69 s | 2 | `ae74adc4ae2dc962cd73456b8eb22c73` |
| Q4 | 40.6 s | 1 | `b5bb2ac89dca60ca261bd1112aaa614c` |
| Q5 | 21.18 s | 1 | `43f7a74bc502bd143d021e551024b8f3` |
| Q6 | 14.24 s | 1 | `c9cee31e3bd0ef2a44e9399ff42068b7` |

The current tenant fixture contains no missing salaries or empty departments, so those branches are supported by query structure but are not empirically exercised with stored fixture rows. Tie behavior was separately exercised using read-only synthetic rows. Some model-generated queries still required repair or unnecessary reads; the harness changes do not claim to eliminate all model errors.

Private local artifacts: `/tmp/sql-repair-reviewed-results.json`, `/tmp/sql-repair-reviewed-spans.json`, `/tmp/sql-repair-reviewed-verification.json`, `/tmp/sql-repair-validated-rank-results.json`, `/tmp/sql-repair-validated-rank-spans.json`, and `/tmp/sql-repair-validated-rank-ties.json`. Original and intermediate probe artifacts remain available for comparison.
