# Live multi-turn verification — 2026-11-05

**Result: all 26 normal battery turns delivered judge-approved answers after retrying an interrupted provider/network run. Four additional labelled recovery-control turns also passed. One money-precision gap remains; two malformed search calls recovered.**

## Environment and scope

- Current agent source loaded into a fresh HTTP/SSE application; current API source copied into the dedicated `blueprint-battery-mcp` container and restarted. No source-file hash changed during this run.
- Live Fireworks DeepSeek V4.1 Flash, actual ClickHouse MCP calls, refreshed Neo4j: 188 blueprints and 12 knowledge chunks. Corpus SHA `737a56d63564acd0372035b861fa56de9fea8b08`.
- Six multi-turn sessions: workforce, time off, expenses, payroll, multi-value filters, and Help Center ON. First five use required-search mode; Help Center ON uses hints mode. Capability search is enabled for payroll and uses a local empty-card fixture.
- Sessions are sequential. Tool-call batching enabled (cap four) to exercise search-batching guidance. All chart components enabled for contract testing, including multi-series line.
- Warehouse is the local synthetic QA tenant. Help Center ON is a local synthetic HTTP article service, as previously authorized. Session persistence is the in-memory store. This is not production-data, Couchbase durability, Ember/browser rendering, or live-agent Trino verification.

## Checks and outcomes

- 26/26 normal responses: `done`, judge `approved`. 26 `record_judgement` calls; zero organic salvage calls in the completed healthy battery.
- All 27 normal `finalizeAnswer` calls passed raw schema/normalization checks; one extra submission was a grounding re-round, not a schema failure.
- Every emitted document was schema v2, referenced valid sources, preserved SQL for data sources, and matched session-history projection.
- All 20 follow-up first requests contained canonical previous `finalizeAnswer` history. No assistant `Delivered answer document:` blob appeared.
- All seven requested saved-result/no-refresh follow-ups executed zero warehouse queries.
- Required search gates were satisfied before work; payroll also searched capabilities. Blueprint execution was used in 12 of 19 turns that executed warehouse queries. There were nine agent-issued raw SQL calls. This is not a controlled usage-rate comparison to an earlier battery.
- Verified `blueprint_execution` SQL receipts in the trail and in both agent and judge inputs (payroll session).
- Time-off boundary: March 31 includes the 23:59:59.999999 record, excludes April 1 and payout events; result is Operations 72 hours. Q1 totals match 72/48/24 hours. Paid expense total/line count match $3,600/9; gross payroll matches $3,600/one employee.
- Salary and expense display reorders retained highest-value selection before alphabetical display. Multi-value department/status lists remained intact when adding Operations.
- Multi-series expense output used `multi_series_line` with one month selector and two series selectors. UI visual rendering was not tested.
- Help Center OFF: explicit deployment limitation alongside saved $3,600/9-line totals. Help Center ON: fetched the synthetic article, gave supported instructions, and reused totals on follow-ups.
- One legitimate pre-judge skip: Help Center follow-up first finalized before full-document grounding. The runtime emitted `finalization_block_granted`, the model fetched the article, resubmitted, and the judge approved. [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/96fd6710522bd1c4b123add5b79c549e)
- All 26 normal root traces and all four final control root traces were verified through the Phoenix API.

## Findings

1. **L1 — money precision, medium.** The multi-series expense query uses `sum(toFloat64(amount_requested))` and `sum(toFloat64(amount_approved))`, although the catalog declares `Nullable(Decimal(19,2))`. The judge approved it. Small fixture totals match, but a read-only synthetic ClickHouse counterexample yields exact `90071992547409.93` versus float `90071992547409.92`. Preserve Decimal arithmetic and add judge coverage for lossy money casts. [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/9ee5dae8af04c7760b6ee12169a9ef0d)
2. **L2 — recovered search argument errors, low.** Two calls supplied both `query` and `deliverables` to `searchBlueprints`; both received `RETRIEVAL_TOOL_INVALID_ARGS` and were corrected. The expense-total turn also hit `READ_REFETCH_LIMIT` on an extra search. These cost calls but did not prevent final answers.

## Recovery controls (injected; separate from organic outcomes)

| Control | Outcome | Trace |
|---|---|---|
| presentation_repair | Injected first presentation rejection; one repair authorized; final approval on attempt 2. Live model performed revision and real judge approved. | [trace](http://localhost:6006/projects/UHJvamVjdDozMg==/spans/e0c230beb3973f030ca843e986b457a3) |
| after_repair | Saved corrected answer reused without a new warehouse query; normal judge approved. | [trace](http://localhost:6006/projects/UHJvamVjdDozMg==/spans/c6676ef4adb3cfc773f5370c3221f981) |
| salvage_timeout | Normal judge timeout injected for both bounded attempts; one live salvage call produced a full approved answer. | [trace](http://localhost:6006/projects/UHJvamVjdDozMg==/spans/1d00200e178340586b367b7ed588893b) |
| after_salvage | Recovered answer reused without a new warehouse query; normal judge approved. | [trace](http://localhost:6006/projects/UHJvamVjdDozMg==/spans/b90a07ef02cb3f93956a3bb6c25743e4) |

No repair was preempted and no repeated salvage loop occurred in these controls. This exercises presentation repair, not a new forced SQL-analysis-repair scenario. An initial control-hook preflight did not inject because judges are constructed per turn; it was excluded and rerun with the hook active for the complete request lifecycle.

## Interrupted initial run

The first seven turns succeeded. The initial March 31 turn then had formatter/judge timeouts and a salvage timeout; subsequent turns recorded DNS `ConnectError: nodename nor servname provided, or not known` to the model service. A fresh model preflight returned HTTP 200, and affected sessions were rerun from their first question. The completed battery combines the original five-turn workforce session with the five fresh retry sessions. The timeout cause itself is not proven to be DNS; the subsequent connection failures are. [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/dc85b4c58d6d40714c1ff934096b6490)

Initial dependency-failed turns are retained in `results.json` under `initial_interrupted_run`; they are not counted as successful coverage. The local embedding service was also restarted before hydration because it was down.

## Exact normal questions and traces

| Session / turn | Question | Seconds | Trace |
|---|---|---:|---|
| required_search_workforce / 1 | How many Active employees are there by department? Show a bar chart. Include only Active employees. | 26.34 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/7deb0dec835c2eeb7a2e86ada59aebfb) |
| required_search_workforce / 2 | Show that same Active headcount breakdown as a table ordered alphabetically by department. Refresh it, keeping the same population. | 35.22 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/02b3d825449459a468fd0f9c9629e430) |
| required_search_workforce / 3 | Show the top 2 highest-paid employees separately within Salary and Hourly pay types, including names, departments, and annual compensation. Include Active and On Leave employees; annualize hourly pay at 2080 hours. | 35.3 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/da6603a96e29e6b4c72441edeee07b53) |
| required_search_workforce / 4 | Keep the top 2 highest-paid employees within each pay type, but display the returned employees alphabetically by employee name. Do not select the lowest-paid employees. | 55.43 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/f8b6b5781b2a1174f73fa6b855da6d24) |
| required_search_workforce / 5 | Summarize the compensation comparison you just showed using the saved results. Do not refresh or query again. | 30.65 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/f90e4c51f4426dae89b6efd6cb459dd5) |
| required_search_timeoff / 1 | Show the top 10 departments by approved time-off request hours from January 1 through March 31, 2026. Include all leave categories, exclude payouts, and rank by hours. | 51.27 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/00d3210e12fc78dfefd35ec6f11003fb) |
| required_search_timeoff / 2 | Change that to the top 2 departments by approved hours over the same dates, but display those two departments alphabetically. Keep the highest-hours departments, not the lowest. | 50.59 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/a3fa041d557e1a6a661c1d23f6588bc4) |
| required_search_timeoff / 3 | Now change that ranking to March 31, 2026 only. Include the complete day and use the same measure and population. | 48.39 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/1d357ba1ee16d40b6dfd5ec925af689c) |
| required_search_timeoff / 4 | Summarize that March 31 ranking using the saved results. Do not refresh it. | 39.32 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/e5738f0a10ccb058f6a3dcc0021290ae) |
| required_search_timeoff / 5 | Return to January 1 through March 31, 2026. Show all departments with approved time-off request hours, without a top-N limit, ordered by department name ascending. Exclude payout events and include all leave categories. | 23.54 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/789aefc1ca1da7bfb5a919ae22ca7b8d) |
| required_search_expense / 1 | Show total paid expense approved amount and paid line count for January 1 through March 31, 2026, using paid date. | 24.12 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/34b12f2d99f7bebfe12c7bc98e0731d8) |
| required_search_expense / 2 | For that same payment-date window, show the top 2 employees by approved expense spend. Include Approved, Paid and Marked as Paid statuses. | 27.73 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/6c2cab7e8f24d0bf7b7f5c0bfc60c9ad) |
| required_search_expense / 3 | Keep those top 2 highest-spend employees for that window and status population, but display them alphabetically by employee name. Do not switch to the lowest spenders. | 47.32 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/e6a0c015d9f8d18ab01ae20307ac4b1e) |
| required_search_expense / 4 | Show a multi-series line chart of requested and approved amounts for Paid expenses by paid month from January through March 2026. Use a wide result and the same date window. | 21.85 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/9ee5dae8af04c7760b6ee12169a9ef0d) |
| required_search_expense / 5 | Using the saved paid-expense totals from the first question, summarize the total and paid line count, and explain how to submit an expense reimbursement in Paycom. Do not refresh the data or perform an action. | 20.69 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/34daecaa52df3c358746a77119a02101) |
| required_search_payroll / 1 | What are total finalized gross earnings and distinct employee count for January 1 through March 31, 2026? Use the payroll register, EARN lines, pay date, and no employee status filter. | 39.88 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/25256e6520239349efd76a55bbebd9bc) |
| required_search_payroll / 2 | Break that finalized gross earnings total down by department for the same pay-date window and population. Include each department's amount and employee count. | 26.07 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/e60d80c90e1e286cd24c08f923a8cd29) |
| required_search_payroll / 3 | Show that same department breakdown in a table ordered by department name ascending. Refresh it without changing the measure, dates, or population. | 22.77 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/45f360cc1d18a9f8188b12334091e37c) |
| required_search_payroll / 4 | Now show finalized gross earnings by pay-date month for January through March 2026 as a line chart. Keep EARN lines and no employee status filter. | 25.73 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/ab90951911572040300056e495c90250) |
| required_search_payroll / 5 | Explain the trend you just showed using those saved monthly results. Do not refresh the data, and do not invent months missing from the result. | 23.18 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/ad54d4d637e041ace816783a5234c0e6) |
| required_search_filters / 1 | Show headcount by department for Sales and Engineering, including Active and On Leave employees. | 22.92 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/e4eb31e360065cdbada238ca14bec703) |
| required_search_filters / 2 | Refresh that breakdown and add Operations. Keep both Active and On Leave employees and show all three departments in alphabetical order. | 35.11 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/869e51c08716f5fd9f8ee798b70a1d94) |
| required_search_filters / 3 | Summarize the updated three-department breakdown you just showed, using the saved results without refreshing. | 41.66 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/437c82f83f553a56ed9a22fd6b4ecd5f) |
| hints_help_on / 1 | Show total paid expense approved amount and paid line count for January 1 through March 31, 2026 using paid date, and explain how to submit an expense reimbursement in Paycom. Do not perform an action. | 33.32 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/09917b4b1d6a019de4105062ca47a161) |
| hints_help_on / 2 | Keep those saved totals and add what happens after submitting the expense. Do not refresh the warehouse data. | 26.79 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/96fd6710522bd1c4b123add5b79c549e) |
| hints_help_on / 3 | Return to the saved paid-expense totals and explain the average approved amount per paid line. Calculate from the existing results without refreshing. | 36.51 | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/fe4998837873a0507f01deb610352cb5) |

## Artifacts

- [Results, findings, SQL and judgments](artifacts/live-multiturn-2026-11-05/results.json)
- [Exact questions](artifacts/live-multiturn-2026-11-05/questions.json)
- [Source fingerprints](artifacts/live-multiturn-2026-11-05/source-hashes.json)
- Full private model-request/SSE captures and harness scripts remain in `/tmp/live-recent-20261105/` (run files saved mode 600).

No product-code fix, commit, push, or deployment was performed. The earlier seven Trino slot-matrix failures remain separate unresolved findings; this live-agent battery uses ClickHouse.

## Search argument alignment replay — 2026-11-05

After aligning the advertised search schema, runtime validation, correction feedback, and read-attempt accounting, repeated the four expense turns in one fresh session using live DeepSeek, ClickHouse MCP and Neo4j. Help Center and capabilities were disabled. No responses or tool failures were injected.

| Question | Seconds | Review | Trace |
|---|---:|---|---|
| Show total paid expense approved amount and paid line count for January 1 through March 31, 2026, using paid date. | 31.45 | approved | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/8f3d50d8654ceab63b27371a855951ca) |
| For that same payment-date window, show the top 2 employees by approved expense spend. Include Approved, Paid and Marked as Paid statuses. | 38.73 | approved | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/a7cc4475dcb8aa4c098d1a7dfde38f08) |
| Keep those top 2 highest-spend employees for that window and status population, but display them alphabetically by employee name. Do not switch to the lowest spenders. | 25.5 | approved | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/72aeacc9cab968ba4a8e3618e7de8fd7) |
| Show a multi-series line chart of requested and approved amounts for Paid expenses by paid month from January through March 2026. Use a wide result and the same date window. | 23.0 | approved | [trace](http://localhost:6006/projects/UHJvamVjdDozMQ==/spans/6221e7b4e4eb272220da79911647c5b4) |

All 6 searchBlueprints calls conformed to the advertised schema. No malformed-search or other tool errors occurred. Each turn received one approved judge review; no salvage calls occurred. The first question returned $3,600.00 across 9 Paid lines. The alphabetical follow-up preserved the same two highest spenders. The chart used a wide requested/approved monthly result.

Focused validation: 2,187 agent blueprint/retrieval/receipt tests, 289 app/context/prompt/telemetry tests, and 1,204 clickhouse-api corpus tests passed. This replay is ClickHouse-only; it does not resolve the previously documented Trino matrix findings.

Full replay captures remain locally at `/tmp/search-alignment-retry/required_search_expense.json`. Generated `docs/verification/artifacts/` captures are intentionally gitignored in both repositories; earlier relative artifact links refer to local files. Product changes and reports were committed separately by work area; nothing was pushed in this follow-up.
