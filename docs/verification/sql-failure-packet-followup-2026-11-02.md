# SQL failure packet follow-up — 2026-11-02

Lane5 now projects structured ClickHouse diagnostics at context rendering as well as canonical message rendering. Projection is idempotent, retains retryability and safe cause metadata, and leaves persisted denial details intact for repair bookkeeping. Context/judge consumers no longer receive the redundant structured diagnostic fields.

Correction to the reported budget mechanism: current `AgentLoop` calls `fit_request_to_budget` after canonicalization. Final request accounting was already based on the compact packet; premature eviction from the larger diagnostic was not reproduced. This change closes the earlier-renderer consistency gap rather than claiming to fix demonstrated final-budget overcounting.

Failure evidence rules are hoisted into one shared system-prompt policy for all denials. Permission denials can support NO_ACCESS; SQL_REPAIR_EXHAUSTED can support EXECUTION_FAILED. Failures never establish data absence, zero rows or successful completion. Failure tool messages no longer repeat the policy. Clarification-control and discovery-specific guidance remain distinct.

clickhouse-api's UNSUPPORTED_METHOD hint now identifies possible tuple/list membership, correlated scalar-subquery and engine-operation issues, with instructions to preserve NULL behavior, cardinality, metric and scope. It explicitly does not claim a confirmed failing clause. Syntax guidance no longer tells the model to use a server position that was not supplied.

## Remaining precision limitation

Actual symbolic names and verified numeric codes are exposed. Raw DB exception bodies remain excluded because they may include expanded private views, identifiers and literals. Server coordinates are not known to map to submitted logical SQL. This follow-up uses authored construct guidance, not structural excerpt extraction or a source map. The exact unsupported operation can still be unknown. Safely extracting an actual operation from a verified error pattern would be better than generic hints; do not describe this hint improvement as that implementation.

## Verification

- Lane5: 144 tests passed across diagnostic replay, compact messages, context assembly, request budgeting, SQL recovery and clarification handling.
- clickhouse-api: 77 tests passed across public error rewriting, model payloads and query feedback, including exclusion of private SQL/literals and unmapped server positions.
- Live DeepSeek (`accounts/fireworks/models/deepseek-v4p1-flash`) with local HTTP/SSE runtime and real MCP: injected invalid function produced SQL_UNSUPPORTED_FUNCTION, followed by a successful corrected runQuery and delivered answer; 23.94 seconds. Contract/tracing checks passed.
- This live case tests compact-packet recovery, not UNSUPPORTED_METHOD root-cause diagnosis. API hint changes were unit-tested, not deployed in the live MCP service for this run.
- Local evidence: `/tmp/sql-packet-followup/results.json` and `run.log`. No production deployment performed.
