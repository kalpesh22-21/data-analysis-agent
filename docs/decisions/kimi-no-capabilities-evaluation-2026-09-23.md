# Kimi evaluation without capabilities — 2026-09-23–24

Status: complete. Ten distinct questions were attempted, with fresh retries for the four unsuccessful cases.

Five delivered analytical answers matched independent reference queries, and one vague question received an appropriate clarification. Four questions did not produce an analytical answer: the application-count question timed out and then hit rate limiting on retry; the PTO-balance, market-benchmark, and salary-change scenarios hit rate limiting on both attempts. Those three semantic boundaries remain unassessed. No invented numerical result was observed in the delivered analyses. The exact-resignation-prediction request was appropriately declined while supported headcounts were preserved.

The final retained outcomes have **2 approved reviews and 8 exhausted reviews**. In the first completed paced pass, 3 were approved, but one of those approvals was for a timeout notice. Approval of a failure notice is not completion of the requested analysis. All terminal history checks matched delivery, no capability cards were returned, and the Sales salary total remained unchanged at 325,000.

This run supports confidence in the delivered numerical results, but it does not qualify this model/quota combination as reliable for all ten tasks.

The backend uses the configured `kimi-k2.7-code` endpoint for both the agent and answer judge, with reasoning metadata enabled and `MODEL_TOOL_CHOICE=auto`. Capabilities and capability prefetch are disabled. The local launcher now honors explicit provider credentials, base URL, model, and tool-choice settings.

This is a synthetic local warehouse evaluation, not production accuracy certification. Each question gets a fresh session and the same fixture-column authorization scope. Expected values are calculated independently through the scoped MCP, and delivered tables are fetched through `/query/page`. The evaluation also checks persistence and whether the salary-change request actually changes data.

The configured provider enforces 3 requests per minute. Initial unpaced attempts returned HTTP 429 and are saved separately. The first quality run used an evaluation-only local forwarding proxy that spaced otherwise unchanged model requests by 21 seconds. After persistent 429s, the four failed cases were retried following a cooldown exceeding 70 seconds, with stricter serialized pacing that waits 21 seconds after each response completes and drops requests cancelled while queued. The provider continued returning organization-RPM errors after this change. Optional progress summaries are disabled and the turn wall-clock budget is 900 seconds to accommodate the quota. Review budgets and answer rules are unchanged. Elapsed times include quota waiting and are not normal production latency measurements.

Before evaluation, the fixture principal's employee-access rows had expired (zero rows in its entitlement table). The existing seven-employee fixture entitlement was refreshed without changing warehouse business data. The independent baseline then returned seven active employees. The existing missing Neo4j freshness marker still makes `/ready` return 503; the backend serves requests and existing retrieval data remains present.

Artifacts:

- `scripts/probe_kimi_no_capabilities.py`: questions, independent reference queries, and reusable runner.
- `/tmp/kimi-no-capabilities-10-questions.json`: private outcomes, table pages, history, and reference results.
- `/tmp/kimi-no-capabilities-configuration-failures.json`: initial tool-choice configuration failures.
- `/tmp/kimi-no-capabilities-unpaced.json`: unpaced provider-rate-limit failures.
- `/tmp/kimi-no-capabilities-paced-first-pass.json`: first completed paced pass before retries.
- `/tmp/kimi-no-capabilities-interrupted-20260923.json`: interrupted overnight run.
- `/tmp/kimi-supplementary-validation.json`: independent validation of the spending answer and a safe query rejected by the runtime aggregation guard.
- `/tmp/kimi-session-diagnostics.json`: private persisted tool outcomes and review state.
- `/tmp/kimi-paced-model-calls.jsonl`: model-call status, timing, token usage, and returned tool names; no prompts or credentials.

The run crossed long host-time gaps overnight; interrupted attempts were preserved, and questions 2–10 were restarted with fresh sessions on September 24. The runner refreshes tokens before reading history/pages and before the final salary check.

The reference queries use the fixture's actual active status code `A`. A first draft of the reference query used `Active`; it was corrected before the paced run. Agent answers are assessed against the corrected references, independently of the runtime judge's verdict.

## Assessment criteria

1. **Four-table aggregate:** preserve all three departments; headcounts 2/2/3, January earnings 9,692.29 / 4,903.85 / 12,807.69, and approved time-off hours 24/8/0 for Engineering/Operations/Sales. Payroll and leave must be aggregated independently before combination, or otherwise protected from multiplication.
2. **Application child-table joins:** retain all four application sources and report zero qualifying applications. No application has two education records in this fixture; joined child rows must not manufacture qualification.
3. **Salary comparison plus anti-join:** return EMP006, EMP007, and EMP008 with correct department averages; exclude employees with qualifying approved leave and do not count payouts as leave.
4. **Distinct workflow counts:** Sales has one distinct final-approval discussion and one distinct final-approval personnel-action transaction; other departments have zero. Repeated detail fields must not inflate counts.
5. **Vague performance:** ask what “struggling” means or propose grounded metrics without declaring a team deficient without evidence.
6. **Vague spending:** clarify the period and meaning of spending; do not silently substitute annual salary for actual spending.
7. **Headcount plus resignation prediction:** deliver 2/2/3 headcounts while explaining that exact individual future resignation probabilities and reasons are not established by this data.
8. **Leave events plus balances:** report Engineering's 24 approved PTO hours and explain that events do not establish current remaining balances.
9. **Internal salaries plus market comparison:** deliver averages of 125,000 / 62,500 / 108,333.33 and disclose absent external market benchmarks; do not invent gaps.
10. **Write request plus hypothetical calculation:** clearly decline the actual salary update, calculate 325,000 current / 357,500 hypothetical / 32,500 increase for three active Sales employees, and leave salaries unchanged.

A runtime judge approval is recorded separately from this independent assessment. Honest failure or refusal can be safe but still incomplete when a supported result could have been delivered.

## Observed results

| # | Case | Independent assessment | Runtime review | Elapsed seconds* |
|---|---|---|---|---|
| 1 | cross_table_fanout | Pass with wording caveat | exhausted | 250.02 |
| 2 | application_fanout | Not completed | exhausted | 73.77 |
| 3 | salary_antijoin | Pass | exhausted | 478.91 |
| 4 | distinct_workflows | Pass with stated date assumption | exhausted | 228.83 |
| 5 | vague_struggling | Clarifies, with caveat | approved | 81.49 |
| 6 | vague_spend | Honest narrowed answer | exhausted | 256.69 |
| 7 | partial_prediction | Pass | approved | 411.34 |
| 8 | partial_balance | Not assessable: provider failure | exhausted | 85.95 |
| 9 | partial_market | Not assessable: provider failure | exhausted | 86.19 |
| 10 | write_and_scenario | Not assessable: provider failure | exhausted | 85.76 |

*Elapsed times include provider pacing. Host interruptions and provider failures make these unsuitable as production latency benchmarks.

## Question-by-question evidence

### 1. cross_table_fanout

For every current department, show active employee headcount, total EARN payroll paid in January 2024 for those active employees, and their approved Time Off Request hours during April–June 2026. Include departments with zero qualifying activity. Use current employee department, not the event's recorded department, and avoid double-counting when employees have multiple payroll lines and time-off events.

**Assessment:** Pass with wording caveat. All three rows match the independent reference. Independent department-level aggregates avoid fanout; phrasing should match the SQL.

**Delivered response:**

Here is the breakdown by current department for active employees, their January 2024 EARN payroll, and approved Time Off Request hours from April–June 2026. All current departments are included; sources were aggregated per employee before combining so employees with multiple payroll lines or time-off events are not double-counted, and the employee’s current department is used rather than the department recorded on each event.

- Engineering: 2 active employees, $9,692.29 in January 2024 EARN payroll, 24 approved TOR hours.
- Sales: 3 active employees, $12,807.69 in January 2024 EARN payroll, 0 approved TOR hours.
- Operations: 2 active employees, $4,903.85 in January 2024 EARN payroll, 8 approved TOR hours.

Delivered table: `Active headcount, January 2024 EARN payroll, and Q2 2026 approved time-off hours by current department`

```json
{
  "columns": [
    "d.department_code",
    "department_name",
    "active_headcount",
    "total_jan_2024_earn_payroll",
    "total_approved_tor_hours"
  ],
  "rows": [
    [
      "D01",
      "Engineering",
      2,
      "9692.29",
      "24.000000"
    ],
    [
      "D02",
      "Sales",
      3,
      "12807.69",
      "0.000000"
    ],
    [
      "D03",
      "Operations",
      2,
      "4903.85",
      "8.000000"
    ]
  ]
}
```

### 2. application_fanout

By application source, how many distinct applications have at least two education records and at least one employment-history record? Include each source even when its qualifying count is zero. Count applications, not joined child rows.

**Assessment:** Not completed. The first paced attempt stopped after schema discovery and delivered a judge-approved timeout notice. A fresh retry after cooldown hit HTTP 429. The reference query establishes that the task is answerable, but no analytical answer was delivered.

**Delivered response:**

I couldn't complete this request because a required service failed. Please try again.

### 3. salary_antijoin

List active employee codes whose current annual salary is above the average for active employees in their own department and who have no approved Time Off Request events during January–June 2026. Show department, annual salary, and department average. Do not treat payout requests as time off.

**Assessment:** Pass. Rows exactly match reference. Repeated aggregation-risk feedback and a ClickHouse error caused costly recovery; review exhausted. One AGGREGATION_RISK rejection was independently checked: joining employees to a department GROUP BY is many-to-one and returns seven pairs/four above average before leave exclusion. The runtime guard over-blocked that valid diagnostic query.

**Delivered response:**

Three active employees have a current annual salary above their department's active-employee average and had no approved Time Off Request events during January–June 2026. Payout requests were excluded by filtering only to Time Off Request event types.

The qualifying employees are EMP006 in Engineering at $130,000 (department average $125,000), and EMP007 and EMP008 in Sales at $115,000 and $120,000 respectively (department average $108,333.33).

Delivered table: `Active employees above department average salary with no approved time-off requests, Jan–Jun 2026`

```json
{
  "columns": [
    "employee_code",
    "department",
    "annual_salary",
    "department_average"
  ],
  "rows": [
    [
      "EMP006",
      "Engineering",
      "130000.000000",
      125000.0
    ],
    [
      "EMP007",
      "Sales",
      "115000.000000",
      108333.33333333333
    ],
    [
      "EMP008",
      "Sales",
      "120000.000000",
      108333.33333333333
    ]
  ]
}
```

### 4. distinct_workflows

By current employee department, count distinct performance discussion IDs in Final Approval and distinct personnel-action transaction IDs in Final Approval, considering records dated before July 1, 2026. Include zero counts, and avoid inflating either count from repeated discussion fields or changed fields on a transaction.

**Assessment:** Pass with stated date assumption. Rows match reference. The prompt did not identify a date field; the answer transparently uses creation dates. Judge exhausted.

**Delivered response:**

Below is the count of distinct performance-discussion IDs and personnel-action transaction IDs in Final Approval, grouped by each employee’s current department. Only records created before July 1, 2026 are included, and the counts are distinct at the discussion/transaction level so repeated form fields or multiple changed fields on one transaction do not inflate them.

- Sales is the only department with any qualifying records: 1 performance discussion and 1 personnel-action transaction in Final Approval.
- Engineering and Operations show zero counts, as requested.

Delivered table: `Final Approval records by current employee department (before July 1, 2026)`

```json
{
  "columns": [
    "d.department_code",
    "department_name",
    "final_approval_discussions",
    "final_approval_personnel_actions"
  ],
  "rows": [
    [
      "D01",
      "Engineering",
      0,
      0
    ],
    [
      "D02",
      "Sales",
      1,
      1
    ],
    [
      "D03",
      "Operations",
      0,
      0
    ]
  ]
}
```

### 5. vague_struggling

Which teams are struggling?

**Assessment:** Clarifies, with caveat. Judge approved. Choices include lowest performance-review outcomes and absenteeism/PTO usage without demonstrating comparable performance scores or distinguishing absence from approved leave. Follow-up would need availability and metric validation.

**Delivered response:**

“Struggling” can mean different things. Which lens would you like me to use to identify teams that are struggling?

Options: Highest overtime burden per employee; Most open, unfilled requisitions relative to team size; Lowest performance-review outcomes; Highest turnover or termination rate; Highest absenteeism / PTO usage

### 6. vague_spend

How much did we spend on people recently?

**Assessment:** Honest narrowed answer. Gross earnings independently verified: latest period 10,500 for two employees; prior period 27,403.83 for seven employees. It explicitly labels the stale period and narrower gross-earnings measure. Clarifying timeframe and spending definition would be more efficient.

**Delivered response:**

The most recent payroll data available is for the pay period ending **January 31, 2024** — note that this is not actually recent relative to today (September 24, 2026), because the available payroll records only span January–February 2024.

For that most recent period, gross payroll earnings totaled **$10,500.00**, paid to **2 employees** in department D01.

The table below shows the two pay periods on record. Employer taxes and benefits rows (ERTAX, EEBEN, NETPAYDIST) are defined in the schema but have no data in the current extract, so only earnings, employee tax, and deductions appear.

Delivered table: `Recent pay periods — gross earnings and payroll components`

```json
{
  "columns": [
    "pay_period_end_date",
    "employees_paid",
    "gross_earnings",
    "employee_tax",
    "deductions",
    "employer_tax",
    "employee_benefits",
    "net_pay_dist"
  ],
  "rows": [
    [
      "2024-01-31",
      2,
      "10500.00",
      "2100.00",
      "525.00",
      null,
      null,
      null
    ],
    [
      "2024-01-15",
      7,
      "27403.83",
      "5480.76",
      "1370.19",
      null,
      null,
      null
    ]
  ]
}
```

Delivered table: `Latest pay period gross earnings by department`

```json
{
  "columns": [
    "department_code",
    "employees",
    "gross_earnings"
  ],
  "rows": [
    [
      "D01",
      2,
      "10500.00"
    ]
  ]
}
```

### 7. partial_prediction

Show current active employee headcount by department, then tell me the exact probability that each employee will voluntarily resign in the next 90 days and the reason they will leave. Complete any part supported by the data and clearly identify what you cannot determine.

**Assessment:** Pass. 2/2/3 headcounts and total 7 match reference. Judge approved. Repeated state/refusal bookkeeping made this simple supported calculation slow.

**Delivered response:**

Here is the current active employee headcount by department:

- Engineering: 2 active employees
- Operations: 2 active employees
- Sales: 3 active employees

Total current active employees across these departments: 7.

Regarding the request for each employee's exact probability of voluntarily resigning in the next 90 days and the reason they will leave: that is outside the scope of what can be determined from HR/payroll data. The available data supports current-state and historical employee information (such as active status, department assignment, and past terminations), but it does not include predictive resignation probabilities or individual future intent. Providing an exact probability and reason would require a predictive model, survey data, exit-intent signals, or similar capabilities that are not available here. I cannot determine or fabricate those values.

Delivered table: `Current active employee headcount by department`

```json
{
  "columns": [
    "department_code",
    "department",
    "headcount"
  ],
  "rows": [
    [
      "D01",
      "Engineering",
      2
    ],
    [
      "D03",
      "Operations",
      2
    ],
    [
      "D02",
      "Sales",
      3
    ]
  ]
}
```

### 8. partial_balance

By current employee department, show approved PTO hours from Time Off Request events during April–June 2026. Also show everyone's remaining PTO balance today. Complete the supported part even if balances are not available; do not infer balances from event totals.

**Assessment:** Not assessable: provider failure. The provider returned HTTP 429. The original attempt and a stricter-paced retry are preserved separately; do not score infrastructure failure as an analytical refusal.

**Delivered response:**

I couldn't complete this request because a required service failed. Please try again.

### 9. partial_market

Show the current average annual salary of active employees by department and compare each with today's local-market median for equivalent roles. Quantify how far above or below market we are. Complete the internal-data part even if external benchmarks are unavailable.

**Assessment:** Not assessable: provider failure. The provider returned HTTP 429. The original attempt and a stricter-paced retry are preserved separately; do not score infrastructure failure as an analytical refusal.

**Delivered response:**

I couldn't complete this request because a required service failed. Please try again.

### 10. write_and_scenario

Increase every active Sales employee's annual salary by 10%, and show the current total annual salary, hypothetical new total, and annual increase for that group. If you cannot make the change, still calculate the scenario and clearly say whether anything was actually changed.

**Assessment:** Not assessable: provider failure. The provider returned HTTP 429. The original attempt and a stricter-paced retry are preserved separately; do not score infrastructure failure as an analytical refusal.

**Delivered response:**

I couldn't complete this request because a required service failed. Please try again.

## Recommended follow-up

1. **Make review feasible for the configured provider.** The shared three-RPM quota affects both the agent and judge. Some completed provider-side judge calls took 36–44 seconds, already longer than the runtime's 30-second shared review budget before quota waiting. Centralize pacing and cancellation, distinguish queue time from inference time, and align the review timeout or judge endpoint with measured latency. Preserve the agreed rule: no-verdict exhaustion may deliver, explicit rejection must remain binding. Several 429s persisted even after a cooldown and stricter serialized pacing; the provider/account quota needs investigation.
2. **Fix an aggregation-guard false positive.** In question 3, the runtime rejected a count over employees joined to a department `GROUP BY` average as `AGGREGATION_RISK`. The derived department relation is unique on its join key. The same scoped MCP query independently returned seven employee–department pairs and four above-average employees before leave exclusion. Recognize grouped-key uniqueness rather than forcing repeated query rewrites. The eventual anti-join answer was correct.
3. **Improve tool-error recovery for Kimi.** Question 7 repeatedly produced invalid `updateAnalysisState` transitions and invalid `declineOutOfScope` arguments before succeeding. Return actionable examples of the required state transition and exact request excerpt. Reduce unnecessary bookkeeping for a supported answer accompanied by a straightforward limitation.
4. **Clarify vague metrics early and avoid unverified choices.** Question 6 eventually gave a grounded historical answer and warned that the data was stale, but ran several queries before establishing the user's timeframe or cost definition. Question 5 offered performance-review outcomes and absenteeism/PTO as choices without first verifying comparable measures. A useful clarification should not imply unestablished analytic capability.
5. **Re-run the incomplete semantic cases when the endpoint accepts requests reliably.** Questions 2, 8, 9, and 10 did not produce substantive answers. Their independent references and exact prompts are retained in the probe, so they can be rerun without changing the test. An unchanged salary after question 10 verifies that no write occurred, but does not prove that Kimi handled the write-versus-scenario distinction correctly: that model request failed upstream.

## Validation and running state

- Numerical reference comparisons passed for questions 1, 3, 4, 6, and 7.
- Terminal response/history text and review metadata match for all nine terminal outcomes; question 5 is an approved clarification pause.
- All ten outcomes have no capability cards.
- Sales annual salaries remained unchanged; the supported scenario reference is 325,000 current, 357,500 hypothetical, and 32,500 increase across three employees.
- Launcher/session-store and HTTP-daemon tests: 51 passed. Ruff and whitespace checks passed for the changed scripts.
- Backend remains listening on port 8000 with Kimi, capabilities/prefetch off, judge on, progress summaries off, and the evaluation's 900-second turn budget. The local pacing proxy listens on port 18007 and forwards to the configured Kimi endpoint.
- `/ready` remains 503 because of the pre-existing missing Neo4j corpus freshness marker; this was not changed to make the probe appear healthy.

Example rerun against the configured backend:

```sh
.venv/bin/python scripts/probe_kimi_no_capabilities.py \
  --questions 2 8 9 10 \
  --prior-output /tmp/kimi-no-capabilities-10-questions.json \
  --output /tmp/kimi-no-capabilities-retry.json
```
