# Live DeepSeek verification: recorded pay and SQL execution receipts — 2026-11-06

Historical engineering record. Localhost trace URLs and `/tmp` or gitignored artifact paths refer to the original test machine; those artifacts are not distributed with this repository. The observations below are recorded results, not a portable reproduction bundle.

## Result

11 completed turns across two serial multi-turn sessions, using live DeepSeek `accounts/fireworks/models/deepseek-v4p1-flash`, the local HTTP/SSE agent, real ClickHouse MCP and refreshed Neo4j. Required-search mode; hints, Help Center and capabilities disabled. This tests the backend answer contract, not Ember rendering. No model answers, judge verdicts or tool errors were injected.

Every completed turn returned `done` with one approved judge review. No salvage calls occurred. No malformed search calls or annualization queries occurred. Both explicit saved-result summaries made zero warehouse calls. The salary-only DAG turn recovered through direct SQL after three failed blueprint attempts; this is an open issue, not a clean DAG pass.

## Verified behavior

- 119 model/judge execution-packet observations (including repeated context) had only formatted `terminal_sql` or `steps`; five were DAG packets. No duplicated SQL array, bound-slot list, verification block or semantic-review marker remained inside these execution objects.
- Baseline: 7 salaried employees, no hourly employees. Exact annual median $115,000; annual mean $100,000; four employees above $100,000. The model disclosed the absence of hourly employees.
- Hourly control: temporarily changed two already-authorized synthetic employees to hourly rates of $25 and $50, with annual salary zero. Exact hourly median and mean were $37.50/hour. Remaining Salary population: five employees, mean $107,000 and exact median $120,000. The model used original units and separate annual-salary/hourly-rate columns.
- Populated DAG: Salary Engineering ($125,000) and Sales ($117,500) exceeded the Salary company average ($107,000). Hourly Operations ($50/hour) exceeded the Hourly company average ($37.50/hour). One successful blueprint call, one judge approval, no raw-query fallback.
- Hourly fixture fields were restored and compared with their saved originals. Two earlier inaccessible synthetic rows were deleted; zero remain.

## Open findings

1. **NULL intermediate blocks otherwise valid partial DAG work.** `bp-departments-above-company-average-salary` computes both Salary and Hourly company averages. When Hourly is absent, `company_hourly_avg` is NULL; the executor returns `RUN_BLUEPRINT_INTERMEDIATE_UNAVAILABLE` before calculating the valid Salary comparison. The live model retried three times, then answered correctly using raw SQL. The populated-hourly control proves the same DAG works when both intermediate averages exist. See `src/data_agent/runtime/blueprint/executor.py` NULL-scalar check around line 700.
2. **Specific failure feedback is lost.** The executor authored a non-retryable explanation naming the NULL intermediate, but the persisted trail has `denial_detail=None`. The next model input contains only `user_message="Something went wrong processing that request."` and the error code. It omits both the specific cause and retryability. This is a plausible contributor to the repeated attempts; model causality is not independently proven. No fix was applied during this battery.

Both findings occur in baseline turn 5: [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/3728b20d4a904b337108cf157dcb5a8b).

## Questions and traces

| Session | Question | Seconds | Trace |
|---|---|---:|---|
| required_search_compensation / ranking | Show the top 2 highest-paid employees separately within Salary and Hourly pay types. Include Active and On Leave employees, employee names and departments. | 47.03 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/67e94d0358f609afc63f52ff4d700925) |
| required_search_compensation / sort | Keep those same top 2 employees per pay type, but display them alphabetically by employee name. Keep the original recorded pay values and units. | 72.36 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/3729e7b4039fa718bea404707c3bfa95) |
| required_search_compensation / median | For all Active and On Leave employees, show the exact median, average, minimum and maximum salary separately for salaried and hourly employees at company level. | 45.82 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/dc006422b9fb99533a60c82279e83a0f) |
| required_search_compensation / departments | Now show average salary by department for that same population, keeping salary and hourly employees separate. | 40.15 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/5bd75790fa65827b7297d7c96fcc1834) |
| required_search_compensation / dag | Which departments are above the corresponding company average pay, separately for Salary and Hourly employees? Keep Active and On Leave employees. | 63.07 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/3728b20d4a904b337108cf157dcb5a8b) |
| required_search_compensation / threshold | How many Active and On Leave employees have recorded annual salary above $100,000? Do not count hourly rates as annual salary. | 30.2 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/a0683ceab8cafad6061a6c67811e8f52) |
| required_search_compensation / reuse | Summarize the department comparison you showed earlier using its saved results. Do not refresh or run another warehouse query. | 43.02 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/347b8f51ec2d6f4b89d1f54519ecf33d) |
| required_search_hourly_control / hourly_ranking | Show the top 2 highest-paid employees separately within Salary and Hourly pay types. Include Active and On Leave employees, names and departments. | 26.35 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/eac2b56b8a3fa982b0fe57d10a2fadcb) |
| required_search_hourly_control / hourly_median | For that same population, show the exact median and average pay separately for Salary and Hourly employees. Use recorded pay values, not estimates. | 46.77 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/7628b7f27ce3e5e52fa266e1f9370dc7) |
| required_search_hourly_control / hourly_dag | Which departments are above the corresponding company average pay, separately for Salary and Hourly employees? Keep the same population. Use the departments-above-company-average-salary blueprint if it fits. | 37.97 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/2ac4dfd95863639bb36512a1a2400051) |
| required_search_hourly_control / hourly_reuse | Summarize that department comparison from the saved results, showing the actual values and units. Do not refresh or query again. | 22.17 | [trace](http://localhost:6006/projects/UHJvamVjdDozMw==/spans/a3360f27eb0715e51fd859ff609a6b58) |

## Setup exclusions and artifacts

The first hydration request hit the local API during restart and failed; the premature test process was stopped and excluded. Hydration then succeeded for all 188 blueprints with SHA `073ddd6e4204b2cb9d4c5801476a3e398d890b1e`. A separate initial hourly fixture attempt inserted new employee IDs that were invisible to the test user, so its results are excluded from hourly coverage. It was terminated; its temporary records were deleted. The accepted hourly control used existing authorized synthetic IDs in a new session.

All 11 accepted agent-turn traces were confirmed in Phoenix. Summary evidence and source fingerprints are local under `docs/verification/artifacts/recorded-pay-live-2026-11-06/` (gitignored). Full model requests, SSE results, SQL and harness scripts remain under `/tmp/recorded-pay-live-20261106/`. This test did not commit, push or deploy to a remote environment. The dedicated local MCP test container and local Neo4j were updated for the run.
