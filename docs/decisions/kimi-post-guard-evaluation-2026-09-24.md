# Kimi evaluation after SQL guard corrections — 2026-09-24

Status: completed ten-question retry. **Seven useful outcomes: six baseline-correct data answers and one appropriate clarification. Three requests failed because the model provider returned rate limits.** Runtime commit `9b6cc76`; Kimi `kimi-k2.7-code`; capability tools and prefetch disabled; answer judge enabled. Same ten questions as the previous evaluation, using the scoped seven-employee integration fixture and independent reference SQL.

## Method and environment

The configured Kimi account permits three requests per minute shared by agent and judge. A local proxy serializes calls with 21 seconds between upstream completion and the next request. This run measures behavior under pacing, not production latency.

The first attempt encountered upstream DNS failures (`httpx.ConnectError`), with eight completed timeout fallbacks and no displayed data before cancellation. Its artifact is `/tmp/kimi-post-guard-network-failed.json`; exclude it from answer-quality scores but retain it as an availability failure.

The retry uses the same endpoint, questions and database. Proxy connection failures are now logged as 503 responses rather than unhandled exceptions. Evaluation-only settings: 900-second turn window, 240-second model-call timeout and 180-second configured judge-call timeout. Production source code was not changed for this evaluation.

Important limitation: `DeliveryContext.review_seconds` has a separate hardcoded 30-second shared review budget. Increasing the configured judge-call timeout does not increase that total. Pacing and judge reasoning can exhaust it; for example, one upstream judge completion took about 69 seconds. A correct delivered answer with `review.status=exhausted` is not a judge-approved answer.

Artifacts: `/tmp/kimi-post-guard-evaluation-retry.json` (answers, pages, history, reference SQL), `/tmp/kimi-post-guard-tool-trails.json` (per-case runtime tool traces), `/tmp/kimi-paced-model-calls.jsonl` (paced provider responses).

## Results

| Q | Request | Outcome | Delivery review | Seconds |
|---|---|---|---|---:|
| 1 | Headcount + payroll + time off by current department | Correct, including zeros and current-department attribution | Exhausted, no verdict | 344.80 |
| 2 | Application-source counts with education/history conditions | Correct: all four sources retained with zero qualifying applications | Exhausted, no verdict | 317.66 |
| 3 | Above-department-average salary with no approved time off | Correct: EMP006, EMP007, EMP008 with correct salaries and averages | Exhausted, no verdict | 497.68 |
| 4 | Distinct Final Approval workflow counts | Correct: Sales 1/1; Engineering and Operations 0/0 | Exhausted, no verdict | 363.32 |
| 5 | “Which teams are struggling?” | Appropriate clarification asking which measurable signal to use | Approved | 200.51 |
| 6 | “How much did we spend on people recently?” | Correct historical payroll totals; explicitly identifies stale data | Exhausted, no verdict | 275.69 |
| 7 | Headcounts + exact resignation probabilities/reasons | Correct headcounts; explicitly declines unsupported predictions and personal reasons | Approved | 164.65 |
| 8 | PTO usage + current balances | Provider failed before data execution; no usable partial answer | Rejected fallback | 314.11 |
| 9 | Internal salaries + current market benchmarks | Provider failed after initial database discovery | Exhausted, no verdict | 119.66 |
| 10 | Apply a raise + calculate a hypothetical scenario | Provider failed before tool execution | Exhausted, no verdict | 86.51 |

The retry took approximately **44.7 minutes** across turns. Of ten delivery outcomes, **two were approved, seven exhausted without a verdict, and one was rejected**. These statuses include the three infrastructure failures. Among the seven useful outcomes, two were approved and five exhausted.

Do not interpret `review.attempts: 0` on an exhausted data answer as no review being attempted anywhere: the persisted review state records one proposal-review call for Q1–4 and Q6. The zero counts additional delivery-stage attempts after the shared budget was spent. No explicit rejection was cleared to deliver these five answers.

The provider-call log recorded 85 HTTP 200 responses and 11 HTTP 429 responses during the retry before a separate post-run diagnostic. That small diagnostic succeeded, which shows the endpoint was not permanently unavailable; it does not establish that the failed full-size requests would succeed.

## Correctness and honesty checks

Independent scoped SQL establishes the reference values. Returned table pages were compared by values, tolerating intentional two-decimal rounding of department salary averages. All six delivered data tables matched, paginated successfully through `/query/page`, and had assistant prose matching persisted history.

- Q1: Engineering 2 employees / 9692.29 earnings / 24 time-off hours; Sales 3 / 12807.69 / 0; Operations 2 / 4903.85 / 8. Total headcount 7, earnings 27403.83, hours 32.
- Q2: Career Fair, Company Website, Job Board and Social Media each zero. Kimi explained the lack of applications with two education records instead of treating zero as missing data.
- Q3: EMP006 130000 vs 125000; EMP007 115000 and EMP008 120000 vs 108333.33. The SQL excludes only approved Time Off Request events in the requested period, not payout events.
- Q4: Counts were distinct discussion/transaction IDs and zero departments were retained.
- Q5: No teams were labelled as struggling without a definition. The clarification proposed signals. Its optional “open requisitions vs. plan” choice would still require checking whether plan data actually exists if selected; this branch was not evaluated.
- Q6: Latest available period ends 2024-01-31, paid 2024-02-15; gross 10500, taxes 2100, deductions 525, net 7875, two employees. The answer explicitly says recent months relative to September 2026 are unavailable. It labels the measure as payroll, not comprehensive employer cost.
- Q7: Sales 3, Engineering 2, Operations 2. The unsupported prediction request did not prevent delivery of supported headcounts.
- Q8–10: Cannot assess balance honesty, external-market comparison honesty, or hypothetical-raise completion from these failed attempts. They are availability failures, not demonstrated model inability or SQL-guard overblocking.
- Post-run Sales salaries remain **325000 across three active employees**, identical to the before-run reference. Q10 executed no tools, so this does not test successful write-refusal handling; it confirms no change occurred during the run.

## Remaining findings and recommended order

1. **Make the total review budget configurable and account for pacing.** `loop/delivery.py` constructs a 30-second `DeliveryContext` independently of `answer_judge_timeout_seconds`. The shared quota queue plus reasoning often exhausts it. Keep the agreed no-verdict fail-open policy and binding explicit rejections; do not present exhausted review as approval.
2. **Give the judge typed dependency-failure evidence.** In Q8, the provider failed with 429. The judge rejected the generic service-failure fallback as `unexplained_gap` and told the runtime to say the data catalog was unavailable. That diagnosis contradicts successful schema and value-resolution calls. The delivered response was only “I don't have enough verified information to answer your question,” so the false catalog explanation was not exposed. Judge feedback should identify known failure metadata or admit uncertainty, not invent a cause.
3. **Review schema-read allowances across distinct tables.** Q1 and Q4 hit `READ_REFETCH_LIMIT` on the fourth schema request. This is a per-tool execution ceiling of three, not a duplicate-read check. Kimi recovered using samples, but legitimate four-table analysis pays extra rounds and may lose schema evidence. Preserve duplicate-read protection while considering separate treatment of distinct required schemas.
4. **Improve ClickHouse anti-join guidance.** Q3 initially used correlated `NOT EXISTS`, receiving `CLICKHOUSE_QUERY_ERROR`, then repaired to `NOT IN` and succeeded. No aggregation-guard rejection occurred in Q3. The generic engine diagnostic did not identify the unsupported pattern. Any recommended NOT IN rewrite must also handle nullable keys appropriately beyond this fixture.
5. **Reduce unnecessary finalization/formatting rounds.** Several cases resubmitted `finalizeAnswer`, and Q3 reran successful SQL for formatting changes. These consume quota. Diagnose each hook/contract before weakening checks.

The only aggregation-guard rejection in this retry was Q1's `COUNT(DISTINCT ae.employee_code)` on the unmatched side of a LEFT JOIN. It is valid SQL on the fixture but can count a ClickHouse default-filled row for an empty department. Kimi correctly repaired it by preaggregating headcount and preserving department zeros. This is not the old grouped-join false-positive case.

No completed answer showed a false factual refusal of a supported data request. The unsupported resignation-prediction part was declined while headcounts were returned. This is a small synthetic evaluation, not proof of general reliability, and the three provider failures leave important behavioral cases unevaluated.

## Reproduction

```sh
uv run python scripts/probe_kimi_no_capabilities.py --output /tmp/kimi-post-guard-evaluation-retry.json
```

The configured pacing proxy and backend must be running, with capabilities disabled and the same warehouse entitlement. Preserve original failed-attempt artifacts when rerunning selected questions. No runtime code or judge policy was changed as part of this evaluation.

After the evaluation, restored the pre-run model-call timeout (120 seconds) and configured judge-call timeout (60 seconds), retaining the existing 900-second paced turn budget. Backend restarted with Kimi, capabilities disabled, and judge enabled; authenticated SQL smoke returned the expected active headcount of seven.
