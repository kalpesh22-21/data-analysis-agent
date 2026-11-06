# Blueprint sorting: 20-turn live battery — 2026-11-05

## Outcome

**19/20 turns met expected behavior; one salary-ranking turn returned a limitation instead of the requested ranking.** Expected behavior includes explicitly declining unavailable Help Center guidance while retaining the requested data answer. All 20 HTTP turns completed. All final review envelopes say approved, but the salary turn has `completion: none`; counting approvals alone would incorrectly report 20 successful answers.

Sorting correctness improved, particularly the expense follow-up. Blueprint usage did **not** increase. No code was changed, committed or pushed during this battery; this report is the only repository artifact added for the run.

## Setup and comparability

- Same 20 exact questions as `/tmp/blueprint-battery-20261105/questions.json`, four independent sessions of five sequential turns. No parallel model tool calls and no injected faults.
- Live Fireworks `accounts/fireworks/models/deepseek-v4p1-flash`, live local warehouse MCP at port 18097, synthetic local data. Actual HTTP `/turn` requests with in-memory session storage and Phoenix export.
- `retrieval_mode=required_search`; Help Center OFF. Capabilities ON for payroll only, with the same local capability HTTP fixture. All chart components enabled for contract testing. This tests the backend answer contract, not an Ember browser render.
- Neo4j at port 17687 refreshed first: 188 blueprints, 174 structured `sort_order`/`order_by` slots, 14 fixed-ranking blueprints without ordering controls. Corpus SHA `9aad443b2ad54e08ce5f74cc4a35e37917f60c12`.
- Agent HEAD `b8a4b99cc71deaeaeec47f11e4d3591ffd24db5d`; API HEAD `6934aa24e5a5ad85a8a5017d06e652ed9e4be915`, **plus existing working-tree changes**. Source hashes were recorded and remained unchanged throughout the run. HEAD alone does not identify the tested source.
- Judge timeout 30 seconds, loop wall-clock setting 180 seconds, at most one tool per model response. No clarification pauses occurred.
- The previous run completed 18 turns before billing blocked `gross_month` and `gross_reuse`. Compare the common 18, excluding those two billing outcomes. This is a single stochastic rerun, not a controlled statistical experiment; latency includes provider variation.

## Before / after on the same 18 completed questions

“Fresh-data turn” excludes saved-result summaries and the mixed saved-expense/Help Center question. “Used” means the delivered answer references a successful blueprint result, not merely that a blueprint was fetched or executed.

| Metric | Previous | Rerun |
|---|---:|---:|
| Fresh-data turns | 15 | 15 |
| Turns executing a blueprint | 13/15 (86.7%) | 10/15 (66.7%) |
| Turns using a blueprint result in the final answer | 9/15 (60.0%) | 8/15 (53.3%) |
| Model `runQuery` calls | 9 | 7 |
| `salvageAnswer` calls | 2 | 1 |
| Median turn duration | 53.23 s | 43.40 s |
| Sum of turn durations | 1,102.88 s | 974.02 s |
| Expense top-two alphabetical follow-up | 200.94 s, 2 salvage calls | 43.48 s, no salvage |

Across all 20 new turns: 16 fresh-data turns; 11/16 executed a blueprint and 9/16 used its result in the final answer. Total measured turn time: 1,040.36 seconds.

The lower execution rate is partly useful: the time-off top-two reorder, its single-day follow-up, and expense reorder now go directly to suitable SQL instead of executing an unsuitable blueprint first. A single native `ORDER BY ... LIMIT` cannot represent “select the highest values, then display those selected rows alphabetically.” The generated queries preserve ranking in an inner query and display order outside it. This is model-authored SQL, not the removed Lane5 sort wrapper. The loss of the initial salary answer accounts for one fewer final blueprint-backed answer.

## Findings

### B1 — Salary ranking was not delivered (High)

Trace: [salary turn](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/e948e517270f2b7e9797357b68d5248d).

The agent fetched `bp-compensation-ranking`, then explicitly bound `employee_status: All` despite the user's Active + On Leave request. The tool response already stated that omitting this slot applies `e.employee_status IN ('Active', 'On Leave')`; the information was not missing from context.

The first judge review timed out and its bounded retry returned an analysis rejection. The runtime authorized and attempted a repair, with about 113.7 seconds remaining. The agent then repeated `getBlueprint` (correctly answered with `IDEMPOTENT_READ_ALREADY_SERVED`), recorded assumptions and resubmitted the same result. It did not execute corrected SQL. The later judge review timed out; delivery retained the earlier rejection and called salvage once. Salvage returned `completion: none` with an explicit limitation.

A status/pay-type probe had returned one aggregate row: Salary / Active / 7 employees. The recovery wording treated that single aggregate row as inadequate to confirm the ranking. All accessible fixture employees were Active, so this run does not establish that the numeric ranking changed from the incorrect binding. It does establish that a repairable answer was lost. The next turn recovered by omitting the status slot and running a separate display-order query.

This was **not** the old same-iteration stagnation preemption: the repair round ran. Follow-up should address the agent's failure to apply the requested binding repair, judge-timeout pressure, and recovery's handling of the population probe. Do not weaken population checks or count an approved limitation as completed analysis.

### B2 — Judge timeout pressure remains (Medium)

Eight turns emitted `loop_answer_judge_timeout_retry`: headcount, salary, salary_sort, timeoff_natural, timeoff_reuse, timeoff_all, gross_dept and gross_reuse. The configured bounded retry disables thinking. These retries terminated; no unbounded judge or salvage loop occurred. The salary turn additionally emitted `loop_answer_judge_failed(reason=timeout)` on its later review.

The time-off top-two reorder took 119.91 seconds despite one successful query and no repair: its LLM call producing `searchKnowledge` took 68.15 seconds and the successful judgement call took 22.40 seconds. These are model-call durations, not database or knowledge-search execution durations. The captures establish the timeout/latency symptoms, not whether provider congestion or reasoning behavior was the underlying cause. See `latency.json` for successful exported LLM spans; canceled attempts may not have a completed ChatCompletion span, so use runtime timeout events as well.

### B3 — Executed SQL is absent from the runBlueprint tool span (Medium, observability)

Trace: [headcount example](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/ec81fe94fa123b0c54c5defeef43b99d), `tool.runBlueprint` span `70cbdcb0e189321f`.

All 11 distinct successful blueprint model-response packets carried `execution.sql`, `execution.terminal_sql` and bound slots. The receipt is persisted on the trail. The model receives executed SQL.

However, the `tool.runBlueprint` Phoenix span contains status, columns and counts, with **no `output.value` or execution receipt**. `dispatch/tool_envelope.py:_stamp` stamps preview shape but does not export execution metadata. The SQL can be found in the subsequent model request and child query input, but not on the blueprint tool's own response. This is the visibility gap the user noticed. A focused tracing change should expose a bounded logical execution receipt on that span, with a regression test; this battery did not apply that change.

### B4 — Repeated semantic caveats (Low)

Trace: [mixed expense question](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/62bb39b904411461323f8212c8e0ab2f).

The mixed answer retained the correct saved totals and explicit Help Center limitation, but its data section repeats two facts in paraphrase: Paid workflow status/paid-date basis and approved reimbursement amount. Exact-text deduplication does not catch these pairs. This affects presentation, not the measured result.

## Checks that passed

- All 20 documents use schema version 2 with valid evidence/source mappings, source SQL, and history parity. All emitted `finalizeAnswer` arguments were accepted by normalization/validation. No parallel tool responses, search-gate violations, invalid sort bindings, or user-facing sort-format clarification pauses were observed. The live run therefore did **not** exercise the new invalid-sort retry branch.
- All 20 turns recorded a normal `record_judgement` result. The salary resubmission's later review failed on timeout as described above. One chart document correction and one prose correction were delivered. No judge-skip, no-progress stop or repeated salvage loop was observed.
- All four saved-result follow-ups performed zero warehouse queries: salary_reuse, timeoff_reuse, expense_mixed and gross_reuse. Required discovery searches still occur in those turns.
- Subsequent-turn first model inputs contain canonical `finalizeAnswer` tool history (one through four prior turns), not assistant-authored `Delivered answer document:` blobs.
- Headcount: Sales 3, Engineering 2, Operations 2, total 7; alphabetical refresh uses native `order_by: [{column: Department, direction: ASC}]`.
- Time off: Q1 Operations 72, Sales 48, Engineering 24 hours; payout events excluded. Top-two display sorting retains Operations and Sales. March 31 includes the 23:59:59.999999 event and excludes April 1; saved summary stays at Operations 72. Unlimited alphabetical output has no outer LIMIT.
- Expenses: Paid approved amount $3,600, nine lines. Highest-spend employees remain EMP005 $1,800 and EMP002 $1,200 after alphabetical display sorting. Multi-series line uses one wide result: requested 660/1320/1980 and approved 600/1200/1800 for Jan/Feb/Mar.
- Mixed Help Center OFF response reuses totals and explicitly declines documentation with `NOT_SUPPORTED_HERE`; it retains the data answer and does not invent product instructions.
- Payroll: register EARN, pay-date window, no status filter; total $3,600, one employee, Engineering. Monthly gross is $1,100/$1,200/$1,300; saved trend acknowledges those values without querying or inventing missing months.
- Fixture limitations: no Hourly employees in the accessible test population, identical names for the two highest-paid Salary employees, and only one department in the payroll result. These cases validate SQL and honest absence handling but do not prove all tie, pay-type or ordering permutations.

## Exact questions, outcomes and traces

All questions were sent serially. Each group starts a fresh session and subsequent rows in that group are follow-ups. No clarification answers were needed. “PASS” includes expected explicit handling of unavailable capabilities; salary is the only incomplete data request.

### Workforce

| Turn | Question (verbatim) | Outcome | Seconds | BP / raw calls | Trace |
|---|---|---|---:|---|---|
| 1 | How many Active employees are there by department? Show a bar chart. Include only Active employees. | PASS | 44.43 | 1 / 0 | [ec81fe94fa123b0c54c5defeef43b99d](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/ec81fe94fa123b0c54c5defeef43b99d) |
| 2 | Show that same Active headcount breakdown as a table ordered alphabetically by department. Refresh it, keeping the same population. | PASS | 30.27 | 1 / 0 | [80168042d58c855ce32d64232db7ad0f](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/80168042d58c855ce32d64232db7ad0f) |
| 3 | Show the top 2 highest-paid employees separately within Salary and Hourly pay types, including names, departments, and annual compensation. Include Active and On Leave employees; annualize hourly pay at 2080 hours. | INCOMPLETE: limitation | 105.51 | 1 / 1 | [e948e517270f2b7e9797357b68d5248d](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/e948e517270f2b7e9797357b68d5248d) |
| 4 | Keep the top 2 highest-paid employees within each pay type, but display the returned employees alphabetically by employee name. Do not select the lowest-paid employees. | PASS | 129.09 | 1 / 1 | [adb7bbb6b700a450ce19ed9f1a3603e5](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/adb7bbb6b700a450ce19ed9f1a3603e5) |
| 5 | Summarize the compensation comparison you just showed using the saved results. Do not refresh or query again. | PASS | 49.44 | 0 / 0 | [562fb6d16713ae961979abffdff7fc63](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/562fb6d16713ae961979abffdff7fc63) |

### Timeoff

| Turn | Question (verbatim) | Outcome | Seconds | BP / raw calls | Trace |
|---|---|---|---:|---|---|
| 1 | Show the top 10 departments by approved time-off request hours from January 1 through March 31, 2026. Include all leave categories, exclude payouts, and rank by hours. | PASS | 69.19 | 1 / 0 | [4df1fe26c11551b88911541e8452c942](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/4df1fe26c11551b88911541e8452c942) |
| 2 | Change that to the top 2 departments by approved hours over the same dates, but display those two departments alphabetically. Keep the highest-hours departments, not the lowest. | PASS | 119.91 | 0 / 1 | [6d0dd877e9fee6092a5e6121181620a4](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/6d0dd877e9fee6092a5e6121181620a4) |
| 3 | Now change that ranking to March 31, 2026 only. Include the complete day and use the same measure and population. | PASS | 40.10 | 0 / 1 | [fa0fc66f9af0b46c780f1fbf0952dbfe](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/fa0fc66f9af0b46c780f1fbf0952dbfe) |
| 4 | Summarize that March 31 ranking using the saved results. Do not refresh it. | PASS | 41.68 | 0 / 0 | [8dadd9e2659cf0e312734d66b2811e13](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/8dadd9e2659cf0e312734d66b2811e13) |
| 5 | Return to January 1 through March 31, 2026. Show all departments with approved time-off request hours, without a top-N limit, ordered by department name ascending. Exclude payout events and include all leave categories. | PASS | 43.31 | 1 / 0 | [1f48d3b229538fa9139a9db2d7049c5c](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/1f48d3b229538fa9139a9db2d7049c5c) |

### Expense

| Turn | Question (verbatim) | Outcome | Seconds | BP / raw calls | Trace |
|---|---|---|---:|---|---|
| 1 | Show total paid expense approved amount and paid line count for January 1 through March 31, 2026, using paid date. | PASS | 32.32 | 0 / 1 | [966165dbdb903061627e53eeb4eaf920](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/966165dbdb903061627e53eeb4eaf920) |
| 2 | For that same payment-date window, show the top 2 employees by approved expense spend. Include Approved, Paid and Marked as Paid statuses. | PASS | 37.79 | 1 / 0 | [9474b8f8c0493bfea2b5e5bd132e01e3](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/9474b8f8c0493bfea2b5e5bd132e01e3) |
| 3 | Keep those top 2 highest-spend employees for that window and status population, but display them alphabetically by employee name. Do not switch to the lowest spenders. | PASS | 43.48 | 0 / 1 | [93a25257a6751537a37f1bcd116190ee](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/93a25257a6751537a37f1bcd116190ee) |
| 4 | Show a multi-series line chart of requested and approved amounts for Paid expenses by paid month from January through March 2026. Use a wide result and the same date window. | PASS | 28.67 | 0 / 1 | [88ffa862e3642fb856dd4e2c97069487](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/88ffa862e3642fb856dd4e2c97069487) |
| 5 | Using the saved paid-expense totals from the first question, summarize the total and paid line count, and explain how to submit an expense reimbursement in Paycom. Do not refresh the data or perform an action. | PASS | 40.46 | 0 / 0 | [62bb39b904411461323f8212c8e0ab2f](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/62bb39b904411461323f8212c8e0ab2f) |

### Payroll

| Turn | Question (verbatim) | Outcome | Seconds | BP / raw calls | Trace |
|---|---|---|---:|---|---|
| 1 | What are total finalized gross earnings and distinct employee count for January 1 through March 31, 2026? Use the payroll register, EARN lines, pay date, and no employee status filter. | PASS | 54.23 | 1 / 0 | [650ed42f411dcd879e3d73aca245507e](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/650ed42f411dcd879e3d73aca245507e) |
| 2 | Break that finalized gross earnings total down by department for the same pay-date window and population. Include each department's amount and employee count. | PASS | 46.99 | 1 / 0 | [a4b73745b8bfb69b6f0db0d59a675d0a](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/a4b73745b8bfb69b6f0db0d59a675d0a) |
| 3 | Show that same department breakdown in a table ordered by department name ascending. Refresh it without changing the measure, dates, or population. | PASS | 17.15 | 1 / 0 | [d1946b6c694363faa7fb7d987df011c9](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/d1946b6c694363faa7fb7d987df011c9) |
| 4 | Now show finalized gross earnings by pay-date month for January through March 2026 as a line chart. Keep EARN lines and no employee status filter. | PASS | 21.53 | 1 / 0 | [61d7d1c05dbf7471701246601e64dc41](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/61d7d1c05dbf7471701246601e64dc41) |
| 5 | Explain the trend you just showed using those saved monthly results. Do not refresh the data, and do not invent months missing from the result. | PASS | 44.81 | 0 / 0 | [01e3c5c4903d04a60a1797577918a994](http://localhost:6006/projects/UHJvamVjdDoyOA==/spans/01e3c5c4903d04a60a1797577918a994) |

## Reproduction artifacts

Directory: `/tmp/blueprint-battery-rerun-sort-order/`.

- `run.py`, `questions.json`, `live.log`: harness, exact questions and run progress.
- `required_search_{workforce,timeoff,expense,payroll}.json`: per-turn responses, captured model messages, queries/results, history, trail, review state and span summaries.
- `hydration.json`: graph count and corpus SHA verification.
- `anchors.json`, `extra-anchors.json`: independently queried fixture references.
- `source-hashes.json`: working-tree source identity; no changes detected during the battery.
- `analysis.json`, `latency.json`: derived per-turn analysis and exported LLM timing.
- Phoenix project: `blueprint-battery-rerun-sort-order` (`UHJvamVjdDoyOA==`).

No application fixes or commits were made during this run.
