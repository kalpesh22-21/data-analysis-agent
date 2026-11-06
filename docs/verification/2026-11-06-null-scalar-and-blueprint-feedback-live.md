# NULL scalar and blueprint failure feedback — 2026-11-06

Historical engineering record. Localhost trace URLs and `/tmp` or gitignored artifact paths refer to the original test machine; those artifacts are not distributed with this repository. The observations below are recorded results, not a portable reproduction bundle.

Follow-up to [the recorded-pay live report](2026-11-06-recorded-pay-and-receipts-live.md). This resolves its two findings; the original report records the pre-fix behavior.

## Changes

- A valid one-row scalar result may contain SQL NULL. The executor preserves it through dependency bindings and checkpoint restoration; the binder emits a typed SQL NULL, never zero or a quoted string. Empty, multirow and malformed scalar shapes remain errors. Conditional expressions must guard NULL comparisons explicitly with `empty(...)`.
- Safe, runtime-authored blueprint failure explanations and retryability now survive trail storage and model replay, including resumed execution. Non-retryable failures tell the model not to repeat the unchanged blueprint. Raw backend error bodies are not newly exposed.

## Verification

- Regression suite: **1,629 passed, 4 skipped** across blueprint, context, session, read-tool registry, execution-receipt and compact-diagnostic tests. Ruff and diff whitespace checks passed.
- Live DeepSeek V4.1 Flash: two serial three-turn sessions through the local agent, real MCP and refreshed Neo4j; required searches enabled, hints/Help Center/capabilities disabled. These are synthetic local data tests, not production data or Ember rendering.
- Salary-only DAG comparison succeeded on the first blueprint call. Both mixed-pay blueprint executions also succeeded at the tool level. The salary-only terminal SQL contains `WHEN pay_type = 'Hourly' THEN NULL`; valid Salary departments still return.
- The mixed-pay comparison hit a separate judge failure: it treated step-1 computed averages bound into step 2 as arbitrary hard-coded thresholds. Both step SQLs were present in its input. The agent reran the blueprint successfully but cited the original rejected result again; salvage repeated the incorrect explanation and delivered a limitation (`completion=none`). Trace: `4195ccb0145d0ada0a1c333a3b5bbe1a`. This remains open; the battery is not a clean pass.
- The mixed-pay saved-result follow-up repeated the judge rejection, then made one new raw query despite the explicit no-refresh request. Salvage delivered the correct comparison after 173.76 seconds. This is a failed saved-result constraint test, trace `7f8a32781f7847ae7ceafc5c68536cef`. The salary-only saved-result follow-up correctly made zero warehouse calls.
- All six turns have status done, but the mixed comparison delivered a limitation and its follow-up violated the no-query constraint. Status alone does not establish success. Across six turns there were two salvage calls. No tool errors, malformed execution receipts or hourly annualization occurred.
- The salary-only median turn made two additional direct queries to confirm the absent Hourly population; this was not failure recovery.
- Safe failure-feedback persistence is covered by deterministic regression tests; the successful live runs did not trigger that error path.
- The two temporary hourly employee changes were restored and compared with their originals.

## Questions and traces

| Population | Question | Seconds | Trace |
|---|---|---:|---|
| Mixed pay | For all Active and On Leave employees, show the exact median and average recorded pay separately for Salary and Hourly employees. | 26.71 | [trace](http://localhost:6006/projects/UHJvamVjdDozNA==/spans/cf75293458ffb3618824ebfb477a6e39) |
| Mixed pay | Which departments are above the corresponding company average pay, separately for Salary and Hourly employees? Keep Active and On Leave employees. Use the departments-above-company-average-salary blueprint if it fits. | 79.49 | [trace](http://localhost:6006/projects/UHJvamVjdDozNA==/spans/4195ccb0145d0ada0a1c333a3b5bbe1a) |
| Mixed pay | Summarize that department comparison using saved results and original units. Do not refresh or run another warehouse query. | 173.76 | [trace](http://localhost:6006/projects/UHJvamVjdDozNA==/spans/7f8a32781f7847ae7ceafc5c68536cef) |
| Salary only | For all Active and On Leave employees, show the exact median and average recorded pay separately for Salary and Hourly employees. | 68.89 | [trace](http://localhost:6006/projects/UHJvamVjdDozNA==/spans/275bc6a6823f8638537a670cff547a70) |
| Salary only | Which departments are above the corresponding company average pay, separately for Salary and Hourly employees? Keep Active and On Leave employees. Use the departments-above-company-average-salary blueprint if it fits. | 45.26 | [trace](http://localhost:6006/projects/UHJvamVjdDozNA==/spans/6536143a9492657246b223659b2a8ac8) |
| Salary only | Summarize that department comparison using saved results and original units. Do not refresh or run another warehouse query. | 38.51 | [trace](http://localhost:6006/projects/UHJvamVjdDozNA==/spans/5c2129d14fabbaf934f4bb51e45d188b) |

## Artifacts

Full requests, results, SQL and harness: `/tmp/null-scalar-live-20261106/`. Summary evidence is copied to the gitignored `docs/verification/artifacts/null-scalar-live-2026-11-06/`. No commit, push or remote deployment was performed.
