# Runtime change review and live validation

Date: 2026-09-14

Reviewed the pending runtime harness, explicit finalization, measurement and SQL
repair, execution result references, capability preparation, conversation replay,
session persistence, hybrid blueprint retrieval, UI labels, and associated tests
against the agreed scope. This review includes both previously staged work and
the later blueprint recall changes.

## Finding and fix

Failed and skipped tool calls could replay argument values after a permission
change. Their empty result provenance did not cover values copied from earlier
results into proposed arguments. The current-turn error exemption also bypassed
provenance filtering during resume.

Failure receipts carrying model-response scope metadata now require the original
scope for replay, including when their result provenance is empty. This applies
to both current-turn resume and historical replay. Same-scope repair still sees
its receipts; stored audit records remain available. Legacy receipts without
scope metadata retain their existing provenance rules. Four regression cases
reproduced the defect before the fix and pass afterward.

Also applied Ruff formatting to the changed Python files.

## Validation

- Full repository suite with local Couchbase integrations enabled: **8,238 passed,
  193 skipped**, 35 warnings, no failures or errors (75.29 seconds).
- Ruff lint, formatting checks, and `git diff --check` passed.
- Initial test attempts encountered sandbox connection restrictions and then
  missing Couchbase credentials. The final run used service access and the
  documented local integration accounts; no tests were disabled to obtain green.
- Four real-model questions on the existing local runtime all completed and
  received final answer-judge approval:
  - Active headcount by department.
  - Salary and SSN for Smith: supported salary result retained; unsupported
    identifier component omitted following review, with an explicit limitation.
  - Separate active headcount and all-status average salary tables, with the
    populations correctly labeled.
  - Monthly distinct hires for 2020–2022, including zero months and a cumulative
    count. Inspected the generated calendar, date bounds, status handling, join,
    and running sum.
- A fresh runtime process loading the final code passed four further cases:
  headcount, separate Sales/Engineering tables, clarification and resume, and
  Position Management navigation. Checks confirmed distinct SQL executions,
  pagination, history matching the live tables, scope rechecking for pagination
  and history, and HTTP 409 on a second resume.

The live runtime used GPT-4.1, real token/MCP/warehouse/Neo4j/embedding/reranking
services, and in-memory sessions. Capability and Help Center endpoints were local
synthetic fixtures. Couchbase persistence was exercised by the repository suite.
The 193 skipped tests remain opt-in coverage. The local 12-blueprint corpus proves
retrieval integration, not a measured recall gain at production scale. Targeted
query reformulation remains outside the implemented scope.

Private local artifacts (not committed): `/tmp/review-all-final.xml`,
`/tmp/review-all-final.log`, `/tmp/review-live-answers.json`, and
`/tmp/review-fresh-harness.json`.
