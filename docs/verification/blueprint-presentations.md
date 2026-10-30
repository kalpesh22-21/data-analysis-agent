# Blueprint presentation compatibility audit

The local corpus contains **188 blueprints**. These are reviewed candidate presentations, not fixed runtime routing rules: the user request, selected measure, grouping bindings and actual result determine the final view. Several measures mean alternatives, not automatic aggregation.

**Method:** read every terminal SQL projection (including the three composed blueprints), check exact selector names, then feed explicitly synthetic rows through the real proposal validator and document materializer. Exercise every declared group_by value, each listed measure, native and serialized numeric values, null measures, incomplete previews, descending time order and two-period lines. This is not SQL execution, a truth audit, or a claim that every possible production result will chart.

**Result:** 1765/1765 checks match the expected chart or explained table fallback.

Candidate families: 106 bars, 9 grouped, 16 line, 16 scalar, 41 table.

## Adaptations

- Compound bar selectors preserve all chosen dimensions as one label, without merging rows.
- Line points are ordered chronologically as complete value/label pairs; two distinct periods are sufficient.
- Facts accept up to 16 selected fields, including the 12-metric workforce KPI result; underscores in fact labels become spaces.
- The UI object shapes remain unchanged. The agent has a new optional category_columns selector for compound bars.

## Deliberate table fallbacks

Incomplete or more-than-25-row previews, null bar measures, unsafe numeric precision, duplicate categories or periods, ambiguous dates, mixed period granularity, and incompatible selectors retain their table fallback with an explanation for judge review. Multi-series results must not be flattened into one line; compound bars or a table preserve their grain. Record/detail blueprints remain table candidates. No result rows are dropped or aggregated to force a chart.

## Every blueprint

| Blueprint | Candidate views | Category dimensions | Measure choices | Checks |
|---|---|---|---|---:|
| bp-accrual-balance-summary | bars, facts | Group | Beginning Balance, Accrued Amount, Awarded Amount, Taken Amount, Source Balance Rows, Employee Count | 52/52 |
| bp-accrual-code-dictionary | bars | Accrual Code | Holder Count | 4/4 |
| bp-accrual-event-summary | bars, facts, line | Group, Event Status | Event Day Rows, Event Hours, Employee Count | 70/70 |
| bp-active-headcount-by-department | bars | department_code, department | headcount | 4/4 |
| bp-adjustment-line-volume | line | Month | Adjustment Lines, Adjustment Amount | 12/12 |
| bp-age-band-distribution | bars | Age Band | Headcount, Avg Age In Band, Median Age In Band | 12/12 |
| bp-approved-timeoff-hours | bars | Department | Approved Hours | 4/4 |
| bp-approved-timeoff-ranking | bars | Employee Code, Employee Name | Approved Time-Off Hours, Requested Days | 8/8 |
| bp-average-annual-salary-by-status | bars | Pay Type, Compensation Basis | Average Annual Compensation, Average Hourly Rate | 8/8 |
| bp-average-salary-by-department | bars | department, Pay Type, Compensation Basis | average_annual_compensation, headcount, Average Hourly Rate | 12/12 |
| bp-avg-request-size | bars | Earn Code, Leave Type | Average Hours, Request Rows | 8/8 |
| bp-avg-shift-length | bars | Department | Avg Shift Hours, Pair Count | 8/8 |
| bp-break-gap-median | bars | Employee Code, Punch Date | Median Break Gap Hours, Gap Count | 8/8 |
| bp-card-vs-personal-split | bars | Expense Type | Approved Amount | 4/4 |
| bp-category-by-department | bars | Department | Total Dollars | 4/4 |
| bp-category-earnings-per-employee | bars | Employee Code, Employee Name | Total Dollars | 4/4 |
| bp-category-hours-monthly | line | Month | Total Hours | 6/6 |
| bp-category-mom-volatility | bars | Category | Current Month Dollars, Prior Month Dollars, Dollar Change, Pct Change | 16/16 |
| bp-category-spend-over-threshold | bars | Employee Code, Employee Name | Total Dollars | 4/4 |
| bp-change-actor-mix | bars | Month, User Type | Change Events | 4/4 |
| bp-change-velocity-spikes | bars | Day | Change Events, Daily Mean, Daily Std Dev | 12/12 |
| bp-clock-time-profile | bars | Department | Median Clock-In Min, Median Clock-Out Min, Pair Count | 12/12 |
| bp-commission-by-employee | bars | Employee Code, Employee Name | Total Commission | 4/4 |
| bp-comp-range-exceptions | bars | Employee Code | Annual Salary, Range Min, Range Mid, Range Max, Compa Ratio | 20/20 |
| bp-compare-employee-check-detail-two-periods | bars | register_type, type_code, type_code_description | amount_a, amount_b, amount_delta, hours_a, hours_b | 20/20 |
| bp-compare-employee-paychecks-two-periods | bars | Pay Category, Pay Code, Description | Recent Amount, Recent Hours, Prior Amount, Prior Hours, Amount Change, Hours Change | 24/24 |
| bp-compensation-ranking | bars | Pay Type, Employee Code, Employee Name | Annual Compensation, Hourly Rate | 8/8 |
| bp-consecutive-day-streaks | bars | Employee Code, Streak Start, Streak End | Streak Length | 4/4 |
| bp-deduction-breakdown | bars | Deduction Code | Total Deductions | 4/4 |
| bp-denied-expenses | bars | Department | Denied Count, Denied Amount, Denial Rate | 12/12 |
| bp-department-directory | bars | Department Code, Department | Active Headcount, Total Headcount | 8/8 |
| bp-department-label-audit | bars | Department Name | Affected Employees | 4/4 |
| bp-department-ot-share | bars | Department | Overtime Dollars, Gross Earnings, OT Share % | 12/12 |
| bp-department-scorecard | bars | Department | Active Headcount, Avg Salary (Active), Avg Tenure Years (Active), Pct Female (Active), Pct Direct Deposit (Active), On Leave, On Leave Over 1 Year No End | 28/28 |
| bp-department-turnover-rate | bars | Department | Terms, Turnover Rate Pct | 8/8 |
| bp-departments-above-company-average-salary | bars | Department, Pay Type, Compensation Basis | Department Average Annual Compensation, Average Hourly Rate | 8/8 |
| bp-dol-status-per-work-location | bars | Work Location | Full Time, Part Time, Seasonal, None, Total | 20/20 |
| bp-duplicate-expense-flag-list | bars | Employee Code | Flagged Amount, Flagged Lines | 8/8 |
| bp-earning-category-dictionary | table | — | Record columns | 3/3 |
| bp-earnings-by-category | facts | — | Total Earnings, Total Hours | 4/4 |
| bp-earnings-by-department-via-scratch-join | bars | department | total_earnings | 4/4 |
| bp-earnings-by-dol-status | bars | DOL Status | Gross Earnings | 4/4 |
| bp-earnings-by-pay-basis | bars | Pay Basis | Gross Earnings | 4/4 |
| bp-earnings-by-work-state | bars | Work State | Gross Earnings | 4/4 |
| bp-earnings-category-mix | bars | Category | Total Dollars, Total Hours, Dollar Pct, Hours Pct | 16/16 |
| bp-earnings-category-trend | line | Month | Total Dollars | 6/6 |
| bp-earnings-monthly-trend | line | Month | Total Earnings | 6/6 |
| bp-earnings-quarter-to-date | facts | — | Gross Earnings, Net Pay | 4/4 |
| bp-effective-mileage-rate | bars | Expense Rule | Total Approved, Total Miles, Effective Rate | 12/12 |
| bp-employee-benefits-summary | bars | Benefit Code | Total Benefit Contributions | 4/4 |
| bp-employee-change-timeline | table | — | Record columns | 3/3 |
| bp-employee-change-volume | bars | Month, Changed Field | Change Events | 4/4 |
| bp-employee-check-detail-for-period | bars | register_type, type_code, type_code_description | amount, hours | 8/8 |
| bp-employee-coverage-matrix | table | — | Record columns | 3/3 |
| bp-employee-date-summary | bars, figure, line | Group | Employee Count | 18/18 |
| bp-employee-day-clock-span | table | — | Record columns | 3/3 |
| bp-employee-directory | table | — | Record columns | 3/3 |
| bp-employee-record-dq-flags | table | — | Record columns | 3/3 |
| bp-employee-summary | bars | Group, Pay Type, Compensation Basis | Employee Count, Known Compensation Count, Annual Compensation Sum, Average Annual Compensation, Minimum Annual Compensation, Maximum Annual Compensation, Average Hourly Rate | 84/84 |
| bp-employee-tax-withheld | bars | Tax Code | Total Tax Withheld | 4/4 |
| bp-employee-tenure-summary | bars, facts | Group | Valid Hire Date Count, Average Elapsed Years, Minimum Elapsed Years, Maximum Elapsed Years | 36/36 |
| bp-employees-in-inactive-seats | table | — | Record columns | 3/3 |
| bp-employer-tax-summary | bars | Tax Code | Total Employer Tax | 4/4 |
| bp-expense-approval-backlog | bars | Employee Code | Pending Lines, Pending Amount, Avg Days Pending | 12/12 |
| bp-expense-approval-trim | bars | Expense Rule | Approved Total, Submitted Total, Trimmed Total, Line Count | 16/16 |
| bp-expense-by-account-category | bars | Account Code, Account Description | Approved Spend | 4/4 |
| bp-expense-by-policy | bars | Policy | Approved Spend, Line Count | 8/8 |
| bp-expense-concept-spend | bars | Matched Description | Approved Spend, Line Count | 8/8 |
| bp-expense-cycle-time | line | Month | Line Count, Median Submission To Approval, Median Approval To Payment | 18/18 |
| bp-expense-line-size-distribution | bars | Account Description, Band Start | Line Count | 4/4 |
| bp-expense-monthly-trend | line | Month | Total Approved Spend | 6/6 |
| bp-expense-payroll-recon | table | — | Record columns | 3/3 |
| bp-expense-per-capita | bars | Department | Approved Spend, Active Headcount, Per Capita Spend | 12/12 |
| bp-expense-senders-no-punches | table | — | Record columns | 3/3 |
| bp-expenses-unreconciled-to-payroll | bars | Expense Status | Line Count, Total Amount | 8/8 |
| bp-fica-medicare-totals | bars | Tax Side, Tax Code | Total Tax | 4/4 |
| bp-future-dated-position-changes | table | — | Record columns | 3/3 |
| bp-future-hire-roster | table | — | Record columns | 3/3 |
| bp-future-termination-roster | table | — | Record columns | 3/3 |
| bp-gross-earnings-period-compare | line | Period | Gross Earnings | 6/6 |
| bp-gross-earnings-summary | figure | — | Total Gross Earnings | 4/4 |
| bp-gross-wages-date-window | figure | — | Total Gross Wages | 4/4 |
| bp-headcount-by-dol-status | bars | DOL Status | Headcount | 4/4 |
| bp-headcount-by-hire-era | bars | Hire Era | Headcount, Avg Tenure Years | 8/8 |
| bp-headcount-by-pay-frequency | bars | Pay Frequency | Headcount | 4/4 |
| bp-headcount-by-union | bars | Union Code | Headcount | 4/4 |
| bp-headcount-by-work-city-country | bars | Country, State, City | Headcount | 4/4 |
| bp-headcount-by-works-in-state | bars | Works In State | Headcount, Lives Elsewhere | 8/8 |
| bp-headcount-growth-rate | line | Month | Hires, Terms, Net Delta | 18/18 |
| bp-headcount-monthly-trend | line | Month | Active Headcount, Terms In Month | 12/12 |
| bp-hires-in-range | line | month | hires | 6/6 |
| bp-hires-per-month | line | month | hires | 6/6 |
| bp-hires-projection | facts | — | hires_in_window, avg_monthly_hires, projected_next_n_hires | 4/4 |
| bp-holiday-work-audit | line | Punch Date | Employees, Total Hours, Line Count | 18/18 |
| bp-inactive-supervisor-audit | table | — | Record columns | 3/3 |
| bp-inactive-without-leave-start | table | — | Record columns | 3/3 |
| bp-labor-allocation-coverage | bars | Mapping Status | Headcount | 4/4 |
| bp-labor-allocation-dictionary | bars | Allocation Code, Allocation Description | Usage Count | 4/4 |
| bp-labor-allocation-resolution | table | — | Record columns | 3/3 |
| bp-labor-allocation-rollup | bars | Allocation Code, Allocation Description | Active Headcount, Avg Salary | 8/8 |
| bp-latest-hired-employees | table | — | Record columns | 3/3 |
| bp-long-shift-days | bars | Employee Code, Punch Date | Daily Hours, Line Count | 8/8 |
| bp-longest-tenured-employees | bars | Employee Code, Name | Tenure Years | 4/4 |
| bp-manager-span-of-control | bars | Manager Code, Manager | Direct Reports | 4/4 |
| bp-median-annual-salary | facts | — | Median Annual Salary, Headcount | 4/4 |
| bp-median-hourly-rate-by-state-union | bars | Works In State, Union Flag | Median Hourly Rate, Headcount | 8/8 |
| bp-median-salary-by-position-level | bars | Position Level | Median Annual Salary, Headcount | 8/8 |
| bp-mileage-summary | facts | — | Approved Amount, Approved Miles, Submitted Miles | 4/4 |
| bp-missing-emergency-contacts | table | — | Record columns | 3/3 |
| bp-missing-receipts | bars | Expense Rule, Rule Description | Missing Receipts, Missing Share | 8/8 |
| bp-missing-supervisor-exceptions | table | — | Record columns | 3/3 |
| bp-name-identity-audit | table | — | Record columns | 3/3 |
| bp-negative-paycheck-employees | table | — | Record columns | 3/3 |
| bp-negative-pto-balances | table | — | Record columns | 3/3 |
| bp-net-pay-summary | figure | — | Total Net Pay | 4/4 |
| bp-new-hire-90d-attrition | facts | — | Hires In Window, Terms Within 90 Days, 90-Day Attrition Pct | 4/4 |
| bp-night-shift-share | bars | Department | Total Hours, Night Hours, Night Share Pct | 12/12 |
| bp-orphan-register-codes | bars | Employee Code | Register Lines | 4/4 |
| bp-ot-two-basis-tieout | table | — | Record columns | 3/3 |
| bp-overtime-by-department | bars | department | earnings | 4/4 |
| bp-overtime-hours-dollars | bars | Department | Overtime Dollars, Overtime Hours | 8/8 |
| bp-paid-hours-summary | figure | — | Total Paid Hours | 4/4 |
| bp-pairs-per-day-distribution | bars | Pairs Per Day | Employee-Days | 4/4 |
| bp-pay-timing-exceptions | table | — | Record columns | 3/3 |
| bp-payout-request-summary | facts | — | Payout Requests, Total Hours, Employees | 4/4 |
| bp-payroll-codes-unmapped | table | — | Record columns | 3/3 |
| bp-payroll-duplicate-line-audit | table | — | Record columns | 3/3 |
| bp-payroll-line-counts | facts | — | Register Lines, Distinct Checks | 4/4 |
| bp-payroll-setup-gap-scorecard | table | — | Record columns | 3/3 |
| bp-payroll-summary | bars, facts, line | Group | Payroll Amount, Source Line Count, Employee Count | 34/34 |
| bp-pending-timeoff-queue | table | — | Record columns | 3/3 |
| bp-placeholder-category-audit | facts | — | Distinct Earn Codes, Total Dollars, Total Hours | 4/4 |
| bp-position-change-activity | bars | Department, Changed Field | Change Events | 4/4 |
| bp-post-termination-accrual-audit | table | — | Record columns | 3/3 |
| bp-post-termination-changes | bars | Employee Code, Changed Field | Post-Termination Changes | 4/4 |
| bp-post-termination-pay-audit | table | — | Record columns | 3/3 |
| bp-punch-anomalies | table | — | Record columns | 3/3 |
| bp-punch-coverage-gap | facts | — | Active Employees, Employees With Punches, Employees Without Punches, Coverage Pct | 4/4 |
| bp-punch-pair-summary | bars, facts, line | Group | Pair Count, Employee Count, Observed Days, Paid Hours, Pair Earning Amount | 124/124 |
| bp-punch-plus-timeoff-overlap | bars | Employee Code, Employee Name | Overlap Days | 4/4 |
| bp-punch-source-audit | bars | Month, Table Source | Pair Count | 4/4 |
| bp-punch-type-mix | bars | In Punch Type, Out Punch Type | Pair Count | 4/4 |
| bp-quarterly-terminations-with-top-reasons | bars | Quarter, Term Reason | Terms | 4/4 |
| bp-records-per-status | bars | Employee Status | Records | 4/4 |
| bp-register-type-breakdown | bars | Register Type | Total Amount | 4/4 |
| bp-regular-vs-premium-hours-ratio | line | Month | Regular Hours, Premium Hours, Regular Pct | 18/18 |
| bp-rehired-employees | table | — | Record columns | 3/3 |
| bp-retroactive-change-share | bars | Changed Field | Retroactive Changes, Share Pct | 8/8 |
| bp-salary-distribution | bars | Pay Type, Compensation Basis, Lower Bound, Upper Bound Exclusive | Employee Count, Percent Within Pay Type | 8/8 |
| bp-salary-exact-median | bars | Group, Pay Type, Compensation Basis | Known Compensation Count, Exact Median Annual Compensation, Average Annual Compensation, Minimum Annual Compensation, Maximum Annual Compensation, Average Hourly Rate | 72/72 |
| bp-salary-histogram | bars | Salary Band | Headcount | 4/4 |
| bp-salary-threshold-count | bars | Pay Type, Compensation Basis | Employee Count | 4/4 |
| bp-salary-threshold-directory | table | — | Record columns | 3/3 |
| bp-shift-differential-spend | facts | — | Shift Differential Dollars, Shift Differential Hours | 4/4 |
| bp-supervision-anomalies | table | — | Record columns | 3/3 |
| bp-supervisor-chain-depth | bars | Chain Depth | Headcount, Share Pct | 8/8 |
| bp-tenure-band-distribution | bars | Tenure Band | Headcount | 4/4 |
| bp-termination-family | table | — | Record columns | 3/3 |
| bp-timecard-earnings-all-history | bars | Earn Code | Total Earnings, Total Hours | 8/8 |
| bp-timecard-earnings-summary | bars, facts, line | Group | Timecard Earning Amount, Paid Hours, Entry Count, Employee Count, Observed Days | 124/124 |
| bp-timecard-vs-register-recon | table | — | Record columns | 3/3 |
| bp-timeoff-by-leave-type | bars | Earn Code, Earn Description | Approved Hours | 4/4 |
| bp-timeoff-denial-rate | bars | Department | Decided Requests, Denied Requests, Denial Rate Pct | 12/12 |
| bp-timeoff-funnel | bars | Status | Request Count | 4/4 |
| bp-timeoff-lead-time | bars | Earn Code, Leave Type | Median Lead Days, Request Rows | 8/8 |
| bp-timeoff-seasonality | line | Month | Approved Hours | 6/6 |
| bp-timeoff-vs-paid-leave-tieout | table | — | Record columns | 3/3 |
| bp-timeoff-withdrawal-rate | bars | Department | Total Requests, Removed Requests, Withdrawal Rate Pct | 12/12 |
| bp-top-changed-fields | bars | Changed Field | Change Events | 4/4 |
| bp-top-codes-in-category | bars | Earn Code, Description | Total Dollars, Total Hours | 8/8 |
| bp-top-expense-spenders | bars | Employee Code, Employee Name | Approved Spend | 4/4 |
| bp-top-home-zip-city | bars | Zip Code, City, State | Headcount | 4/4 |
| bp-top-payroll-codes | bars | Type Code | Total Amount | 4/4 |
| bp-top-timeoff-users | bars | Employee Code, Employee Name | Approved Hours, Current Balance Hours | 8/8 |
| bp-total-earnings-by-department | bars | department | total_earnings | 4/4 |
| bp-unpaid-active-employees | table | — | Record columns | 3/3 |
| bp-unused-leave-roster | bars | Employee Code, Employee Name | Balance Hours | 4/4 |
| bp-upcoming-anniversaries | table | — | Record columns | 3/3 |
| bp-upcoming-timeoff-roster | table | — | Record columns | 3/3 |
| bp-weekend-work-share | line | Month | Total Hours, Weekend Hours, Weekday Hours, Weekend Share Pct | 24/24 |
| bp-weekly-overtime-hours | bars | Employee Code, Week Start | Weekly Hours, Overtime Hours | 8/8 |
| bp-worked-hours-daily | line | Punch Date | Total Hours, Line Count | 12/12 |
| bp-workforce-composition | bars | Employment Type, DOL Status | Headcount, Share Pct | 8/8 |
| bp-workforce-diversity-profile | bars | Gender, Marital Status, Age Band, EEO-1 Category, Leadership Cohort | Headcount, Share Pct | 8/8 |
| bp-workforce-kpi-bundle | facts | — | Active Headcount, Total Records, W2 Share Pct, Median Annual Salary, Avg Tenure Years, Pct Full Time, Pct Direct Deposit, Hires In Window, Terms In Window, Turnover Rate In Window, On Leave, Missing Emergency Contacts | 4/4 |
| bp-workforce-movements | table | — | Record columns | 3/3 |
| bp-ytd-earnings-per-employee | bars | Employee Code, Employee Name | Gross Earnings, Net Pay, Paid Hours | 12/12 |
| bp-zero-net-pay-employees | table | — | Record columns | 3/3 |

Reproduce with:

```sh
PYTHONPATH=src .venv/bin/python scripts/audit_blueprint_presentations.py --output /tmp/blueprint-presentations.json --markdown docs/verification/blueprint-presentations.md
.venv/bin/pytest tests/runtime/test_blueprint_presentations.py tests/runtime/test_chart_datatypes.py
```
