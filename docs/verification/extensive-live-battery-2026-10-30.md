# Extensive live answer-contract and judge battery

Date: 2026-10-30 local (2026-10-31 UTC). Local working tree based on `08f7755`; this is not deployed-production or remote Ember certification.

## Verdict

**Delivery and v2 transport passed; not all answer-quality goals passed. The matched judge rejection rate did not decrease.**

47 cases produced 53 terminal SSE responses: 51 `done` and 2 clarification pauses. Every HTTP exchange produced exactly one terminal result, with no SSE errors. All 53 Phoenix traces had connected parentage and delivery payloads exactly matching SSE. All 47 reference-UI browser checks passed.

| Check | Result |
|---|---|
| Runtime/UI regression suite | 6,934 passed; 5 skipped |
| Corpus presentation audit | 188 blueprints; 1,765 synthetic shape/type checks passed |
| Live transport | 53/53 terminal responses, persisted-document parity and schema-v2 checks passed |
| Saved paging / refresh / CSV | 44 sources; 54 CSV parts; paging, dedup, replacement and replay passed |
| Text/identifier fidelity | 71 CSV columns / 248 cells preserved exactly; browser checks covered ZIP, phones and a 19-digit text ID |
| Blueprint formatter bypass | 20/20 delivered blueprint results skipped the formatter |
| Formatter calls | 23/24 applied; one invalid extra-column response safely fell back |
| Formatter semantic annotations | 37/38 accepted column annotations correct (97.4%); derived currency was missed |
| Non-reasoning formatter | All 24 Phoenix formatter spans used reasoning_effort=none; zero reasoning tokens |
| Direct judge corrections | 9 takeaway replacements and 4 document replacements; no successful live clarification rewrite |

## Judge rejection comparison

Primary metric: fraction of matched ordinary cases with a genuine judge rejection. Injected failures, clarification variants and additional challenge questions are excluded from the matched comparison.

| Cohort | Cases rejected | Rejected review calls |
|---|---:|---:|
| Historical, same 14 questions | 1/14 (7.1%) | 1/14 |
| Current, same 14 questions | 1/14 (7.1%) | 1/15 |
| Current, all non-injected cases | 2/32 (6.25%) | 2/35 |

The matched case rate is unchanged. The smaller per-review percentage is a denominator effect, not evidence of improvement. The old rejection concerned population in the percentage question; the new matched rejection correctly caught a contact query missing the current-workforce filter. Neither baseline establishes a reduction in cosmetic rejections.

Historical matched median latency was 56.33 s; current was 48.63 s. These are descriptive only: the current battery used concurrent independent harness processes, historical runs were from another time, and model sampling is not deterministic. This is not a randomized before/after experiment.

## Remaining findings

| Finding | Evidence and impact | Suggested follow-up |
|---|---|---|
| Derived money loses currency | `production_cost` correctly returned $8,100 / 7 = $1,157.14, but `Cost Per Active Employee 2026` was `kind:number`. The UI receives no USD annotation for that fact. [Phoenix](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/98d629d021568703aeb1bcdc6dbfdef7) | Supply relevant established currency metadata/lineage to the formatter, including protected companion results. The explicit-USD variant produced correct currency metadata. Do not guess currency. |
| False identity/duplication rejection | `dates` had seven distinct employee codes with repeated names. The judge inferred possible duplicates from three names and requested analysis repair, then salvage delivered the correct rows. Identical `dates_repeat` later passed without rejection. [Phoenix](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/90214e0b8ccf33c5c14dda093ba8fa78) | Enforce catalog identity keys in judge reasoning; repeated names alone cannot invalidate distinct-key counts. Preserve checks for actual join multiplication. |
| Clarification task handling is inconsistent | Two setup-style prompts did not reach the intended count: one acknowledged the request without pausing; another asked what task was wanted and acknowledged the resumed status. The stronger explicit-headcount variant did pause and return 7. [Phoenix](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/c17ccb60b170f476c7168d23b1699591) | Test/clarify that “ask me X before counting” means ask that question, then continue the count. Keep these failures separate from numeric correctness. |
| Clarification self-correction missed | The explicit test injected `Should employee_status be Active or On Leave?`. The judge approved unchanged; scrubbing displayed `Should [schema detail withheld] be Active or On Leave?`. Resume still returned the correct count. [Phoenix](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/9a02c3861575a0b5876b7c23de9e875c) | Judge should rewrite schema-worded questions before sanitization, using `corrected_question`; do not add a retry for harmless style. |
| Formatter returned protected columns | One transformed-phone formatter response included EmployeeCode and Contact ZIP although only Contact Phone was eligible. The validator rejected that response and retained safe existing metadata; final ZIP/phone values were correct. [Phoenix](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/92e1418cc2ac4badf1383e1a8a8804b3) | Improve adherence to eligible_columns; retain the allowlist and safe fallback. |
| Pre-judge repair/salvage remains | `salary_rank` exhausted an `ungrounded_answer` finalization guard before an ordinary judge review. Salvage delivered the independently checked top three. This is not included in judge rejection rate. [Phoenix](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/b365ac6dc2b4ffdcca8d73dc6e8050dd) | Investigate the finalization evidence gate separately; a low judge rejection rate does not measure all repair overhead. Exact root cause is not established by this report. |

## Verified corrections and recovery

- Review-stage wrong caption → approved corrected_document, with no new query or salvage. [Phoenix](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/cf2ce4118f3641c9f321b6ccbecb14d3)
- Review-stage false “999 active employees” → approved corrected_answer reporting 7, with no new query or salvage. [Phoenix](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/2f1d2dda3716946711f2128725c6dfae)
- Corrected caption followed by “what status did that table cover?” → next turn received the delivered document, answered Active correctly, and made no additional query. [Phoenix](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/76de341654da790aab79ad505fda5d99)
- Injected judge timeout and agent failure both recovered useful data through salvage. Injected unavailability of both judge and salvage delivered a safe text limitation.
- Injected payroll denial retained the supported department counts and disclosed the inaccessible part. Invalid source selection repaired without another query. Unsupported chart data fell back to a table.
- Deliberately changed blueprint population was detected/recovered before the final judge; final department counts matched independent same-scope truth. This does not prove a judge-specific wrong-population rejection.
- Requested facts fell back to a table in one matched case, just as in the historical run; the answer still reported Active=7 and On Leave=0. This is presentation fidelity, not a wrong answer or delivery failure.

## Accuracy method and limits

The model was `accounts/fireworks/models/deepseek-v4p1-flash`. Calls traversed real Lane5 HTTP/SSE, local token/MCP/catalog/retrieval, ClickHouse, Couchbase-backed saved results, and the actual reference UI in Chromium. The hydrator ran before the battery against the current formatted corpus. Browser BFF calls were bridged to the real local services; no mocked result rows were used. Fault injections were explicitly tagged in the battery traces.

Independent MCP truth queries used the same authorization scope. Twenty-nine mapped result comparisons matched; the two remaining answerable oracle cases were independently checked as Active=7/On Leave=0 and an empty 2099 hire-date result. One clarification-only outcome had no data result to compare. Additional shared oracle queries covered employee identities, salaries, dates, department counts and monthly payroll. Known anchors were 7 scoped employees, department counts 2/2/3, annual salary sum $700,000, 2026 gross earnings $8,100 and paid hours 480.

Formatter scoring compared the returned metadata against SQL semantics, requested units and established catalog/source formats. Identifier/text are both accepted for contact strings when they preserve values and prohibit numeric formatting. Date columns remain dates even when every sampled cell is null. The formatter returned 41 annotations: 40 had correct semantics, but 3 belonged to one rejected extra-column response. Among the 38 applied annotations, 37 were correct. These are sampled field annotations, not a claim of universal formatter accuracy.

The local fixture is small (seven authorized employee codes; six 2026 payroll months), with deliberately repeated employee names. ZIP/phone test values were temporarily populated and then restored to their saved original values. Long IDs were generated as text in SQL. Trino, production data volumes, remote Ember and S3-presigned byte delivery were not exercised; CSV bytes came through the existing local Couchbase export/proxy path. Corpus shape checks used synthetic cells and are not 188 conversational executions.

One pilot caption injection was superseded by an intent-finalization round before the judge saw it. Its failed “direct correction” assertion is a harness-seam limitation, not an application failure. It was retained as evidence and rerun successfully at the actual review seam. A clarification injection that never reached askUser is likewise not counted as a tested rewrite.

## Case scoreboard

| Case | Cohort | Seconds | Outcome / notable finding |
|---|---|---:|---|
| v2_share_table | historical_matched | 54.74 | PASS |
| v2_facts | historical_matched | 44.00 | Correct counts; facts → table fallback |
| v2_zip_phone | historical_matched | 28.21 | PASS |
| v2_browser_figure | historical_matched | 29.73 | PASS |
| v2_browser_bars | historical_matched | 33.15 | PASS |
| production_payroll | variants | 50.91 | PASS |
| contact_plain | variants | 18.24 | PASS |
| numeric_edges | variants | 63.01 | PASS |
| empty | variants | 36.29 | PASS |
| refresh_revision | transport | 65.64 | PASS |
| clarification_wording | injected | 14.55 | No pause/count; injection not reached |
| all_unavailable | injected | 14.03 | PASS |
| chart_fallback | injected | 47.79 | PASS |
| v2_blueprint_figure | historical_matched | 40.40 | PASS |
| v2_department_bars | historical_matched | 23.64 | PASS |
| v2_zip_phone_transform | historical_matched | 65.26 | Valid population repair; safe formatter fallback |
| v2_browser_line | historical_matched | 55.07 | PASS |
| v2_browser_percent | historical_matched | 41.72 | PASS |
| production_gross | variants | 57.00 | PASS |
| dates | variants | 92.38 | Correct rows; false duplicate-name rejection + salvage |
| hours | variants | 66.00 | PASS |
| followup_scope | conversation | 59.90 | PASS |
| correction_takeaway | injected | 23.28 | PASS |
| salvage_timeout | injected | 14.86 | PASS |
| partial_denied | injected | 52.59 | PASS |
| v2_monthly_line | historical_matched | 53.26 | PASS |
| v2_report_identifier | historical_matched | 66.69 | PASS |
| v2_integer_reference | historical_matched | 60.10 | PASS |
| v2_browser_facts | historical_matched | 88.15 | PASS |
| production_headcount | variants | 62.13 | PASS |
| production_cost | variants | 96.39 | Correct values; missing USD metadata on derived cost |
| salary_rank | variants | 55.65 | Correct ranking via salvage after pre-judge guard |
| null_contacts | variants | 37.25 | PASS |
| clarification_status | conversation | 62.54 | Intended count not returned |
| correction_caption | injected | 22.63 | PASS |
| salvage_agent | injected | 12.53 | PASS |
| invalid_source | injected | 37.15 | PASS |
| wrong_population | injected | 71.42 | PASS |
| correction_followup | injected | 38.11 | Pilot injection superseded; review-seam retest passed |
| identifier_edge | formatting | 25.08 | PASS |
| boolean_flag | formatting | 70.89 | PASS |
| review_seam_caption | injected | 33.92 | PASS |
| review_seam_takeaway | injected | 21.05 | PASS |
| review_seam_followup | injected | 50.32 | PASS |
| clarification_explicit | injected | 55.80 | Correct resume count; technical question not rewritten |
| cost_explicit_usd | formatting | 61.84 | PASS |
| dates_repeat | repeat | 56.28 | Same question passed without rejection |

## Artifacts and code changes

Artifacts: `/tmp/lane5-extensive-20261030/` contains `results0..5.json`, question specifications, `historical-baseline.json`, `summary.json`, `truth-comparison.json`, `independent-oracles.json`, `format-audit.json`, `phoenix-audit.json`, `blueprint-shapes.json`, and regression logs. Phoenix project: `answer-document-battery`.

Only the probe harness was extended in this task: same-scope oracle queries, follow-up capture, formatter-input capture, and labeled review/clarification/population faults. No product behavior was changed to make this battery pass. No commit or push performed.


## Follow-up: derived currency context

The formatter now receives established currency metadata, SQL when available, and source
provenance from selected successful receipts in the current turn. Protected query columns
and blueprint results can supply this read-only context without becoming formatting targets.
There is no additional model call or UI contract change.

A real DeepSeek non-reasoning replay of the original `production_cost` receipts now annotates
`Cost Per Active Employee 2026` as USD with two decimals, without adding USD to the question.
Additional replays retained plain numbers for unknown and conflicting currencies and used
percent/ratio for money divided by money. Each replay made one formatter call. These are
formatter replays of recorded evidence, not new SQL executions. The first conflict replay
picked USD incorrectly; explicit conflicting-source guidance was added, and all four replay
checks passed on the revised prompt.

The fresh blueprint KPI end-to-end case passed document, history, paging, reference UI,
and Phoenix delivery checks with zero formatter calls. The original ambiguous comparison
question instead paused to clarify gross/net/employer-cost basis; its requested-data checks
failed because it did not produce a data document. This is separate from currency enrichment.

Focused regression tests: 109 passed. Ruff and `git diff --check` passed.
Artifacts: `/tmp/lane5-currency-context/` (`replay.json`, `live.json`, and follow-up run files).
Original-result replay trace: `1eadcf195430d724f1f16dcb7b0d5199`;
blueprint end-to-end trace: `da1d8a2c70b146d7cd7842a53d7ecbf6`.

The explicitly scoped gross-earnings comparison (still with no currency named in the question)
passed all live checks in 86.02 seconds: $8,100 / 7 = $1,157.14, USD metadata in the delivered
source, saved-history parity, reference UI paging/controls, and exact Phoenix/SSE parity.
It used one formatter call. This run chose a SQL expression whose currency was already
resolved by deterministic metadata, so the recorded-receipt replay above is the direct
verification of the previously failing formatter path. Trace: `2fc8d8c15368cedf7589bd1ba3f5010e`.


## Follow-up: repeated names are not duplicate records

The answer judge now explicitly evaluates duplicates using the requested result grain
and documented keys, including tenant keys where applicable. Repeated display names
with distinct keys do not justify rejection, deduplication or extra identity queries.
The existing restriction against unsupported claims about natural-person identity remains.
Actual name-based counting/grouping mistakes and demonstrated join multiplicity still
require analysis repair. This is a judge-prompt change only.

Live DeepSeek judge regression checks approved two records named Alex Smith with distinct
employee codes and rejected a distinct-name count of one over those records with
`measurement_mismatch`. These were synthetic judge inputs, not database queries.
The original `dates` conversational question also passed end to end in 28.57 seconds,
including UI, persistence and Phoenix delivery checks. Focused tests: 100 passed; Ruff
and diff whitespace checks passed. Artifacts: `/tmp/lane5-identity-judge-check.json`
and `/tmp/lane5-identity-regression.json`.

Live dates trace: `9733037d376c1a06e9ca92b7b44862d3`.


## Follow-up: readable clarification labels

Standalone underscore-separated labels now become space-separated words in the existing
prose sanitizer: `employee_status` becomes `employee status`, rather than a withheld
placeholder. This is deterministic and adds no model call. The normalized question is
saved in the pause checkpoint as well as returned to the UI. Clarification choice values,
known authorized business values, filenames, and structured SQL remain unchanged.
Qualified database identifiers and corpus IDs retain their existing redaction rules.
Humanization is not counted as a redaction, so it does not cause presentation rejection.
Redundant identifier annotations can still be removed without deleting distinct caveats.


## Follow-up: clarification continuation

Resume already preserves the original task, clarification question and reply; the answer
judge also receives the original question and clarification answers. No new fields or calls
were added. Agent and judge guidance now requires completing the original requested analysis
after resolving a choice, while honoring explicit changes or cancellations. A request to
ask before counting is treated as a sequenced task, not merely a future preference.

The first live check passed ask → Active-only reply → count of 7, with persisted-document,
reference-UI and Phoenix delivery checks. Cancellation passed with zero warehouse queries.
The original “Before counting employees, ask me to choose Active or On Leave” wording
initially still produced an acknowledgment before any pause; explicit sequencing guidance
was then added to both prompts and that exact wording was retested.

Focused tests: 91 passed; Ruff and diff whitespace checks passed. Artifacts:
`/tmp/continuation-live.json`, `/tmp/continuation-variants-live.json`,
`/tmp/continuation-original-retest.json`. These are local scoped-fixture live runs, not a
production deployment.

The exact original wording passed after the sequencing update: paused with Active/On Leave
choices, then returned Active=7 in 46.94 seconds. Resume trace:
`1bec997033d47c70c2b058f0926baa0b`. The changed-status variant honored On Leave and returned
0 (resume trace `32688de8a3c1b50e14493c38bd1f34f0`). All final-case contract, reference-UI,
history and Phoenix delivery checks passed. These sampled runs establish regression
coverage, not deterministic model behavior across all wording.
