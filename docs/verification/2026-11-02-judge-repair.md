# Judge repair and bounded recovery verification

The combined change preserves a granted repair across the same-round stagnation stop, recomputes wall-clock budget after review, and records authorization/refusal separately from an attempted repair model call. `repaired` remains the reserved one-repair allowance for persisted-state compatibility. `repair_pending` and `repair_attempted` distinguish scheduling from attempted model execution; neither proves corrected SQL ran. Successful execution receipts remain authoritative.

Recovery validates presentation as well as schema and permits one validation correction within the original deadline. All non-system input, including retry feedback, counts against the evidence budget. Transport failures are not retried by this correction mechanism. Partial recovery still requires a real unfinished outcome. Guidance explicitly handles interrupted analysis: retain independently supported findings, omit rejected uncorrected measures, and never invent execution or a failure cause.

Malformed finalizeAnswer calls retain their original identity and receive type feedback; historical metadata is labelled as context rather than an output template.

## Verification

- Combined runtime loop, selected observability, and context suites: 1,183 passed.
- After persisting attempted-repair state, focused judge-exit and salvage suites: 72 passed.
- Deterministic loop tests cover stagnation-boundary repair, continued no-progress after an unsuccessful repair, and wall-clock exhaustion during review.
- Real DeepSeek V4.1 Flash recovery calls with synthetic evidence: all three none/partial/full cases passed. These were model probes, not MCP queries.
- Real DeepSeek with local runtime HTTP/SSE and MCP: injected presentation rejection repaired and approved, with query reuse, persisted document, and connected tracing checks passing (30.55 seconds). This is a presentation-repair smoke test, not a live reproduction of the time-off measurement incident.
- clickhouse-api corpus suite: 472 passed. The approved-timeoff-hours template now requires Time Off Request events and declares event_type in uses. Its SQLite execution fixture distinguishes approved requests from payouts, pending requests, null hours and other periods.

Local artifacts: /tmp/repair-final-tests.log, /tmp/repair-followup-tests.log, /tmp/timeoff-final-tests.log, /tmp/salvage-stopped-analysis/results.json, /tmp/repair-live-final/results.json. These temporary artifacts are not committed.

Limits: no production deployment or production time-off delta measurement. The local MCP fixture lacks time_off_request. The frozen-answer-results proposal (A25) remains deferred.
