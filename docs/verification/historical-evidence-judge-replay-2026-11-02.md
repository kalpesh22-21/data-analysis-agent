# Historical evidence and judge replay — 2026-11-02

## Changes

`finalizeAnswer` can explicitly cite a result used in a prior delivered answer. The runtime binds a current-turn historical reference using the same eligibility rules as explicit analysis-state reuse. It requires an authorized delivered answer and a successful warehouse receipt with matching recorded scope; unknown provenance/scope, failures, scratch results and unverified blueprints remain ineligible. It preserves the original receipt, preview, retrieval timestamp and full-result pointer. The reference is labeled as reused, not freshly executed. Repeated bindings are idempotent, and successive follow-ups resolve back to the original receipt.

The saved source SQL is carried into the current answer's source map, including for verified blueprints. Only schema-defined reference fields are rewritten; prose, column names and SQL are not rewritten. The normal finalization and judge gates still apply. Requested fresh/current data or changed populations/periods require fresh execution under the agent/judge guidance.

Judge guidance distinguishes semantic title/caption contradictions from optional cosmetic edits. It also requires checking derived numeric claims and rounding against visible operands. Unsupported, unrequested derived claims can be removed directly; missing requested calculations require analysis repair. These are model instructions, not a deterministic arithmetic or semantic classifier.

## Verification scope

Live replay uses DeepSeek `accounts/fireworks/models/deepseek-v4p1-flash`, the local HTTP/SSE runtime and real local MCP backend with synthetic fixtures. Calls are sequential, with `parallel_tool_calls=false` and one tool per model iteration. The checks cover terminal answers, saved history, canonical historical `finalizeAnswer` pairs, source SQL, repairs, and traces. They do not verify Ember rendering or a production deployment.

Historical storage retention and prompt token limits are unchanged. This is not the deferred A25 UI snapshot fix: source SQL may still be re-executed by the UI's lazy snapshot path. No new RLS-revocation or snapshot-retention guarantee is introduced.

Full local replay artifacts: `/tmp/replay-serial/final-checks-results.json`; log: `/tmp/replay-serial/final-checks.log`. Artifacts are local and not committed because they include full model contexts.
