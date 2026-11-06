# Multi-value implementation self-review — 2026-11-05

Historical engineering record. Localhost trace URLs and `/tmp` or gitignored artifact paths refer to the original test machine; those artifacts are not distributed with this repository. The observations below are recorded results, not a portable reproduction bundle.

Reviewed the multi-value resolver/executor/binder changes, load-time validation, retrieval-mode cleanup, assembler flag call sites, batching guidance, and their regression coverage. Existing staged work was preserved. No commits or pushes.

## Findings fixed

1. **Missing required enum misclassified as an argument error.** The shared retryable-error branch treated any enum-backed list failure as a model-format mistake, including an absent required business choice. Restricted that conversion to invalid/no-match failures. A missing required categorical enum again pauses for clarification without querying the warehouse.
2. **Incomplete runtime validation and traversal of list IN sites.** The binder checked only whether a list placeholder's parent was IN, allowing a list on the left-hand side or alongside fixed values to produce tuple-shaped SQL. Also, replacing an entire IN node during list expansion skipped traversal of other placeholders, so `{value} IN ({choices})` could leave `value` unbound. Runtime now rejects unsupported positions, preserves legacy `IN {slot}`, and expands the list on the existing transform node so other placeholders bind normally.

Five regression cases were added. Before fixes, four failed and the legacy bare-IN compatibility case passed. All five pass after fixes. The scalar-left-hand case executes in SQLite to verify the rendered query, rather than checking text alone.

## Verification

- Agent: **4,236 passed, 3 skipped** across blueprint, retrieval, context, required searches, column-context evidence, prompt contracts, learning extraction/generalization/promotion, app wiring, prompt budgeting and trace fields.
- API: **1,014 passed** across corpus and employee-population tests.
- Focused binder/resolver tests: **203 passed**.
- Ruff on files changed in this sweep: passed. Agent `git diff --check`: passed.
- Logs: `/tmp/self-review-broad.log`, `/tmp/self-review-api.log`, `/tmp/self-review-focused.log`.

No further failures were found within this review scope. This was code review plus deterministic/integration tests, not a new live-model battery. Automatic sort tie-breaker appending remains intentionally unimplemented: explicit order_by replaces the declared default. The base system prompt remains near its existing size limit.
