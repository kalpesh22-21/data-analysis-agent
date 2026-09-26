# Kimi payroll and accruals probe — September 26, 2026

The five-turn run is complete. Backend restarted from the current working tree at PID 47976;
`/ready` returned HTTP 200. Model: `kimi-k2.7-code`; automatic tool choice;
60-second judge timeout; 900-second execution window. Telemetry redaction remains
explicitly disabled (`effective_llm_hide=False`); answer-prose scope guards remain
active. No application code was changed during this probe. The backend remains running and
its final readiness check returned HTTP 200.

Private artifacts: `/tmp/kimi-payroll-accruals-20260926/` (conversation SSE, scoped
session snapshot, independent read-only oracle, and shape-only review traces).

## Questions

1. For the latest available pay date, report gross earnings, employee tax withheld,
   employee deductions, net pay distributed, and employees paid; retain adjustments.
2. Compare that date with the preceding available pay date: dollar/percentage gross
   earnings change and the three departments with the largest increases.
3. Rank up to five employees by approved vacation hours for May 2026; show pending
   hours separately and retain employees with missing names.
4. Add remaining vacation balances as of May 31 and the value of unused hours;
   retain verified usage if balances or valuation data are unavailable.
5. Reconcile approved May vacation hours with vacation hours paid on May payroll,
   by employee and overall; distinguish timing/mapping limitations from errors.

## Independent evidence

The current authorized payroll scope returns **0 payroll rows**, with null minimum
and maximum pay dates. Consequently this run can test payroll absence handling,
context and review behavior, but cannot validate nonzero payroll calculations.
No alternate tenant or unscoped warehouse access was used.

May accrual records contain **24 approved vacation hours for one employee** and
**8 approved bereavement hours for another**. There are no pending vacation rows in
that month's accessible events. These are event records, not remaining balances.

The independent accrual query also encountered a scope-parser rejection when descriptive
aliases were applied to grouped source columns; the equivalent query leaving source
columns unaliased succeeded. This is an oracle-query observation, not an agent failure.

## Results

| Turn | Seconds | Observed result | Evaluation |
|---|---:|---|---|
| Latest payroll summary | 476.94 | Correctly states that accessible payroll has zero rows and no pay dates. Review exhausted; final prose contains `[schema detail withheld]`. | Absence claim matches independent query; answer polish and review completion fail. Payroll arithmetic cannot be assessed. |
| Preceding-pay-date comparison | 264.23 | Correct limitation, with no invented changes or department ranking. Judge rejected schema-heavy prose, then approved its own exact wording correction. | Supported limitation and judge-owned correction pass. |
| Approved/pending vacation ranking | 400.15 | Successful query returns one employee, 24 approved vacation hours, 0 pending hours, missing name retained. A later model-service failure prevents delivery; terminal review is exhausted. | Query semantics and displayed aliases pass independent checks. User-visible completion and partial recovery are not validated. |
| Remaining balances and valuation | 102.45 | Model-service failure before tools. Judge approves the service-error message. | Not evaluated; approved error wording is not task success. |
| Payroll/accrual reconciliation | 95.82 | Model-service failure before tools. Judge approves the service-error message. | Not evaluated. |

The first payroll query used a scalar latest-date CTE with a CROSS JOIN and was
rejected by the aggregation guard. A rewritten grouped query succeeded and returned
no rows. A separate count/min/max query established the absence of accessible
payroll rows; the failed first attempt was not used as evidence of absence.

The vacation query used separately aggregated approved and pending events, May's
requested-for dates, the resolved Vacation code, and LEFT JOINs for optional names.
It did not multiply event rows, include bereavement, or drop the employee without a
name. Final result column labels were `Employee Code`, `Employee Name`,
`Approved Vacation Hours`, and `Pending Vacation Hours` — none contain underscores.
This result matches the independent accrual oracle. It was not delivered as a table
because the later service failure occurred before finalization.

## Product findings, separate from model-service failures

1. **Unused reserves can strand ordinary final-delivery review.** The new terminal
   reserve protects partial recovery, but `review_delivery()` still invokes ordinary
   `review_once()` for normal delivery. With a 180-second total and two 60-second
   reserves, a 60-second initial timeout can leave 120 seconds unavailable to that
   path. The live trace shows one cancelled judge call followed by an exhausted
   delivery reporting two attempts; these are not two additional model reviews.
   A deterministic reproduction with 120 seconds remaining makes zero judge calls
   in ordinary mode and successfully calls the judge in terminal mode. The private
   reproduction is `budget-reproduction.json`. The next code change should allow
   final delivery to use the remaining reserved time once agent work has ended,
   while preserving prior explicit rejections and the aggregate deadline.
2. **Schema placeholders still reach users.** Turn 1 ends with “the most recent
   [schema detail withheld]”. Telemetry redaction is disabled, but answer-prose
   scrubbing is a separate mechanism. The model should use business labels without
   implementation instructions; an exhausted review currently lets this poor wording
   through. Turn 2 initially repeated SQL jargon even after explicit repair feedback.
3. **Judge-owned correction works when reviewed.** Turn 2's second verdict removed
   the remaining schema/process prose and supplied the exact approved answer without
   another agent round. The original/corrected pair is persisted in review state.
4. **Payroll fixture coverage limits the conclusions.** Empty payroll is a useful
   limitation-handling test, but cannot validate gross/net separation, negative
   adjustments, percentage calculations, or department drivers on populated records.
   Those require a separate authorized fixture with known payroll totals.

## Focused retry and limits

A fresh session retried the three accrual questions using the same backend, model,
settings, and tenant. Its first turn failed before any tools after 125.67 seconds.
The second turn was interrupted after the repeated pre-tool failure; the third was
not started. Artifacts are in `/tmp/kimi-payroll-accruals-focused-20260926/`.

The provider returned repeated HTTP 429 responses. Per the requested evaluation
scope, these are not counted as payroll/accrual reasoning defects. They do prevent
claiming that the new partial-answer path passed a live probe: there was no approved
partial snapshot in this run, and the terminal recovery review was unavailable on
the turn with the successful ranking query. No missing balance, valuation, or
reconciliation conclusion is inferred from a failed attempt.

The run establishes correct vacation-query semantics and successful judge-owned
wording correction, and exposes a reproducible local review-budget regression.
It does **not** establish a clean five-turn pass or live partial-answer recovery.
