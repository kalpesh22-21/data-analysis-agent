# Difficult SQL live-stack probes — 2026-09-13

Ran six fresh sessions against http://127.0.0.1:18104 with GPT-4.1, answer judge enabled (60 seconds, 32,000-token evidence budget), and unredacted Phoenix tracing. Two concurrent requests. No runtime code or warehouse data changed. The last paused session was not automatically resumed.

Outcome: two successful scenarios on the current fixture, one fixture-green query with a reproduced tie defect, two completed but unsuccessful scenarios, and one budget-cap pause. HTTP 200 and status=done were not treated as correctness checks.

Independent checks used read-only ClickHouse queries with explicit session-tenant filtering (CLIENT_A / PC01) and reference calculations. Apparent duplicate department codes in an unfiltered scan belonged to separate tenants; they are not evidence of within-session fanout. Actual UI rendering/pagination execution was not browser-tested; the checks cover the SSE UI envelope and persisted history.

## Findings

1. **Ranking with ties — FAIL on a read-only counterexample.** The fixture output matched an independent dense-rank calculation (six rows), and the final judge approved. However, the generated query joins employees back to a non-distinct ranked salary relation. Two employees tied at 100 plus one at 90 produce five rows instead of three. This was reproduced by substituting a constant three-row relation into the actual generated SQL, without changing stored data. Rank employee rows directly, or deduplicate salary levels before joining. The explicit LIMIT 1000 also imposes a ceiling on the request to include everyone tied.

2. **Department directory join — PASS on fixture.** Headcounts and salary sums/averages matched independent employee-only aggregation. The session has one directory row per department; all three departments have active employees. Therefore the no-active-employees branch remains structurally inspected, not exercised. Judge approved; no ship guard.

3. **Separate Engineering and Sales results — PASS on fixture.** Both SQL statements reached answer_tables, with their own captions. Engineering returned one above-average employee and Sales returned two. Independent calculations matched. History preserved both tables. Judge approved; no ship guard.

4. **Zero-filled monthly series — FAIL.** Four warehouse tool denials involved blocked system.numbers access, including cardinality probes. The agent did not find a permitted calendar-series approach. It then failed twice to establish blocked intent evidence (ANALYSIS_STATE_INVALID), exhausted enforcement, and returned a generic limitation with no table. No answer judge ran. Additional confirmed persistence issue: SSE assistant_text was "I could not verify every requested part from the available evidence." while the history answer was null.

5. **Year-over-year hires and zero denominator — FAIL / partial UI result.** The WHERE restriction to 2021–2022 eliminated Engineering and Operations, which each have zero hires in both years in this tenant. Only Sales remained (3 versus 0, change -3 / -100%). Thus no zero-denominator row actually reached the UI. Both final-judge calls completed but rejected prose about original hires/rehires, not the omitted departments. The second judge also asserted cross-year rehire behavior not established by this employee snapshot query. The agent retained an original-hires assumption despite feedback to remove it. The ship guard used ship_tables_with_hedge: the UI and history received the one-row table, assumptions, and an unable-to-fully-verify message. Treat omission detection and judge-grounded feedback as separate concerns.

6. **Median, missing values, and share of company salary — FAIL / paused.** Fifteen runQuery calls failed with CLICKHOUSE_QUERY_ERROR; sampleRows additionally failed COLUMN_SCOPE_VIOLATION. Direct read-only replay established UNKNOWN_FUNCTION for COUNTIF/SUMIF (ClickHouse expects countIf/sumIf). The model-visible receipt supplied only "That query didn't run correctly. Let me fix it and try again.", with no useful engine diagnostic. Four distinct SQL variants retained the casing problem. Two blocked-intent updates also failed. After 32 tool calls and 90.62 seconds, the UI received paused_budget_cap with continue/refine/stop, no SQL table, and no answer judge.

## Suggested discussion order

1. Supply safe, structured engine diagnostics to the model and stop repeated equivalent failed SQL attempts. Keep the user-facing explanation separate from the repair diagnostic.
2. Make grain review cover joins back to derived/window relations, including ties; validate uniqueness at the actual join grain.
3. Provide a permitted zero-filled calendar-series procedure and clarify how denied results bind blocked intents.
4. Preserve fallback text consistently in session history.
5. Review department coverage including zero-activity groups, and ground judge feedback in the actual source grain instead of assumed event history.

## Exact questions and traces

### Q1

For each department, show the top two distinct annual salary levels among active employees, including everyone tied at either level. Include employee code, department, annual salary, and salary rank. Exclude missing salaries.

Session: `runtime-acceptance-q1-2cf6c5dbee`; trace: `d9a1b76515611872dcf0f1954ef0f13b`; duration: 11.14 seconds; status: `done`.

### Q2

Using the department directory for department names, show every department, including departments with no active employees, with distinct active headcount, total annual salary, and average annual salary. Ensure joins do not multiply employees or salary totals.

Session: `runtime-acceptance-q2-fb181de7ed`; trace: `b7d70946782ea25796d325df99355929`; duration: 15.35 seconds; status: `done`.

### Q3

Give me two separate tables: active Engineering employees earning above the active Engineering average salary, and active Sales employees earning above the active Sales average salary. Include employee code, salary, department average, and difference from that average.

Session: `runtime-acceptance-q3-23cf3954de`; trace: `a112dcb6bb912d5969e99948754ae22c`; duration: 12.82 seconds; status: `done`.

### Q4

For each calendar month from January 2020 through December 2022, count distinct employees hired during that month regardless of current status, include zero-hire months, and show the cumulative hires across the entire period.

Session: `runtime-acceptance-q4-9e703a68df`; trace: `365fe2392805802b3db92e66c8849a94`; duration: 40.51 seconds; status: `done`.

### Q5

By department, compare distinct hires during calendar 2021 versus calendar 2022 regardless of current status. Include both counts, the absolute change, and percentage change. When the 2021 count is zero, show the percentage change as unavailable rather than zero or infinity.

Session: `runtime-acceptance-q5-effcb05bf3`; trace: `d2d3e1144712f07d224fd7d4e693615a`; duration: 26.25 seconds; status: `done`.

### Q6

For each department, show active employee count, count with missing annual salary, median annual salary among known salaries, and the department's share of total known active annual salary across the company. Do not treat missing salaries as zero when calculating the median.

Session: `runtime-acceptance-q6-31dc02c339`; trace: `9ec82c4095851fdbbe8662b94a1f1f47`; duration: 90.62 seconds; status: `paused_budget_cap`.

## Trace and artifact checks

All five observed final judge calls completed with reviewed=true: one each for Q1–Q3 and two for Q5. Their hierarchy is agent.turn → answer_judge → Response. Q4 and Q6 exited before final judging. No judge timeout was observed. Only Q5 emitted loop_answer_judge_ship_guarded. Q1–Q3 and Q5 had matching SSE/history answer text and tables; Q4 text did not match; Q6 had no final answer in either channel.

Private local artifacts (mode 0600): `/tmp/sql-stress-results.json` (SSE outcomes, progress, history), `/tmp/sql-stress-spans.json` (full traces), `/tmp/sql-stress-verification.json` (tenant-filtered read-only comparisons), `/tmp/sql-stress-ties.json` (synthetic tie reproduction). Runner: `/tmp/probe_sql_stress.py`. The standard seven-question probe catalog was not edited.
