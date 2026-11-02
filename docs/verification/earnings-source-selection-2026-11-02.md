# Earnings source selection — 2026-11-02

Implementation uses the existing model-based measurement judge and bounded repair
path. It is not a deterministic SQL equivalence checker or a universal guarantee.
Equivalent custom SQL is allowed; explicit user overrides remain valid. The judge
checks source, measure, date basis and population together, not mere blueprint usage.

Local live battery: DeepSeek `accounts/fireworks/models/deepseek-v4p1-flash`, real
lane5 HTTP/SSE, MCP, retrieval/hydration, ClickHouse and Phoenix. Synthetic data;
no production tenant, Trino rollout or Ember browser verification. API corpus was
copied into the local MCP container and rehydrated normally.

- Unqualified gross earnings 2026: `runBlueprint(bp-gross-earnings-summary)`, EARN
  payroll by pay_date, $8,100.00; approved and persisted (35.12s).
- Explicit period ending January 15: `runBlueprint(bp-gross-earnings-for-pay-period)`,
  payroll by pay_period_end_date, $1,100.00; approved and persisted (24.17s).
- Initial timecard test found the local fixture table absent. Source-unavailable
  limitation was delivered without substituting payroll. This is not a successful
  timecard execution. The repository fixture DDL was then installed locally with
  three synthetic entries (January $500, February $900) for the successful-path rerun.
- Combined request initially delivered the supported $8,100 payroll subtotal and
  withheld the combined total without a verified finalization/overlap boundary.

Separate live DeepSeek judge probes with synthetic evidence: a timecard total for
unqualified gross earnings was rejected as `measurement_mismatch`, analysis repair;
the same SQL/rows for an explicit timecard question were approved. These are model
probes, not additional warehouse executions.

Regression suites: 1,027 judge/blueprint tests and 26 judge-evidence tests passed.
API corpus/parity suite: 480 passed; the additional CASE/FINAL regression test also passed. New evidence test ensures a cross-source rule
reaches review even when the alternative relation is selected.

Raw local evidence: `/tmp/lane5-earnings-source/` (`results.json`,
`results-with-timecard.json`, `judge-results.json`, fixture SQL and logs).

Successful-path investigation: the first populated timecard run recovered via
semantically equivalent SQL and returned January $500, but the blueprint itself
failed with ClickHouse 24.8 ILLEGAL_COLUMN (simple CASE lowered to transform with
nonconstant branches). Changed the template to searched CASE and verified it
against real ClickHouse directly: January 10 $200, January 11 $300. The intervening
combined run overlapped the local MCP restart and is excluded from final evidence.

Final timecard rerun: `runBlueprint(bp-timecard-earnings-summary)` succeeded;
January $500 across January 10 ($200) and January 11 ($300). Approved, persisted,
HTTP/SSE and trace checks passed (65.64s); no fallback query was needed for the total.

The first combined-source run with both tables available was a **semantic failure**
despite generic harness checks passing: at 190.96s, timeout salvage displayed the
unsupported $9,500 direct sum with a caveat. Tightened agent, measurement-review and
salvage instructions to omit an unverified combined value/column even in a partial
answer. Separate subtotals remain eligible. Do not count generic transport checks as
semantic acceptance.

After the correction, separate real DeepSeek probes again rejected the wrong source,
approved the explicit timecard question, and salvage omitted the invalid combined
figure. Its synthetic evidence included the invalid combined column deliberately.
Final recovery-focused regression suite: 160 passed (overlaps the earlier suites).

Final end-to-end combined rerun: **PASS** (96.93s). Delivered payroll $8,100 and
timecard $1,400 separately; explained the missing overlap boundary and withheld a
combined number. Explicit assertions verify neither 9500 nor 9,500 appears anywhere
in the saved document. Both supported subtotals appear in the takeaway. HTTP/SSE,
persistence and trace checks pass. Evidence: `combined-fixed.json`.

Limits: these are single live model runs over small synthetic fixtures, not production
numeric reconciliation or proof of universal model reliability. The combined case
still took 97 seconds. Some duplicated caveat wording and technical source labels
remain; this change addresses measurement selection, not prose quality.

Correction after owner review: explicit FINAL was redundant; MCP already applies
ClickHouse final=1. Removed the added clause from the timecard summary, corrected
its catalog/knowledge guidance, and instructed agent and judge to leave storage
deduplication to MCP. Kept the searched CASE fix. Focused checks: 24 corpus tests,
21 API source tests, 70 agent judge tests pass. The live runs above predate this
redundant-clause removal; no new live-model run is claimed for this correction.
