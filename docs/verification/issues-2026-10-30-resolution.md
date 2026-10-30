# Response to the 2026-10-30 live-run issues report

Draft for the reporting developer. This describes the local working trees, not a
deployed release or verification of the remote branch. Lane5 base: `30882b9`;
clickhouse-api base: `16f29f0`. The changes described below are not yet committed
or pushed. Do not close deployment incidents solely from this report.

## Verification scope

The live battery uses real DeepSeek agent/judge calls, Lane5 HTTP `/turn` and
`/turn/resume`, full SSE capture, the rebuilt local clickhouse-api MCP, scoped
ClickHouse fixture data, real retrieval services, and Phoenix. Recovery faults
are explicitly injected at dependency boundaries. This is not a production-tenant
battery, the remote 102-question suite, or an Ember browser test.

Before testing, the standard `build_hydrator(...).run_once()` path fetched the
rebuilt API exports and rehydrated Neo4j. All 188 blueprint nodes have
`column_formats_json`; the expense blueprint uses `date_time_submitted`.

| Artifact | Verified content fingerprint |
|---|---|
| Blueprints | `160a84167ccf2d8401e930710d48e4bd1e3fd6cb` |
| Knowledge | `e7322287e508e7a8e223a5dd8f4b6e17b8bcbdde` |
| Semantic catalog | `f77e592abb235eed8a52d076d187c4d6a6015e4f` |

Both parity checks passed. Hydration topology was not changed: the owner confirmed
one deployed hydrator pod. I12 is not established as concurrent-writer contention.

## Changes and local findings

| Issue | Resolution or disposition |
|---|---|
| A1 / C1 payroll date basis | Catalog defaults unqualified payroll periods to pay date; explicit work-period requests retain their semantics. Cross-year predicate regression tests cover the difference. The live payroll-2026 case executed `bp-payroll-summary`, used the requested payment-year range, and returned the independently checked fixture total of $8,100.00. This does not reproduce the production $718.4M anchor. |
| A2 / A15(f) restricted requests | Agent, answer judge and clarification judge distinguish access restrictions from absent data. Preserve permitted work; do not ask questions solely to advance prohibited lookups. Scope-safe denial receipts reach both judges. No additional judge dimension was added. |
| A3 / A6 / A7 clarification | Use prior agreement and applicable catalog defaults. A bare topic such as Sales is not proof of an out-of-domain request. Clarify unresolved material choices rather than inventing a metric or period. Model behavior still requires live evaluation. |
| A5 denominator | Explicit Active-only requests remain Active-only; catalog defaults do not override explicit scope. An Active-only denominator is not automatically wrong for the report's explicitly active-employee question. Disclose the chosen population; compare like definitions. |
| A4 / A8–A10 presentation | Guide one primary presentation per information need; preserve requested numeric content through repair/salvage. Do not require an unrequested status breakdown or a duplicate visible table beneath every chart. Zero sections remain valid for text-only answers. Variation alone is not a correctness defect. |
| A11 formatting | Added catalog/query/blueprint format metadata through result previews, documents and snapshots. All 188 blueprints and 770 terminal outputs were audited. Identifiers remain text; currencies and percentages carry explicit units/scales. Figures/facts are formatted server-side. CSV cells intentionally remain raw with unit-bearing headers. Ember table/axis adapters still need the handoff contract. |
| A12 blueprint preference | Corrected the expense blueprint's submission window, which previously filtered payment date. Prompt guidance now retains independently correct partial-blueprint results, uses definitions as SQL context rather than evidence, and queries gaps. Do not force a semantically mismatched retrieved blueprint. |
| A13 visible row counts | UI owns pagination counts such as “Showing 20 of 211.” Complete paginated results differ from incomplete execution. The latter still needs a truthful limitation. See the UI handoff. |
| A14 headline totals | Directly requested scalar answers belong in the takeaway; supporting table metrics need not all be repeated. Agent/judge guidance aligned. |
| I7 verification asymmetry | Current join probes query business relations through normal MCP routing, not native system tables. Both SQL and blueprint steps use the guard. Required failed probes block the guarded query. Added minimal current-turn/current-scope judge receipts: attempt ID, duplicate-keys/unavailable category, and blocked-query-not-executed flag. A passed join check is not independent proof of the whole answer. Live Trino parity remains unverified in this battery. |
| I11 resume 403 | Default scope-token expiry increased from 1 hour to 8 hours; rebuilt local API reports 28,800 seconds. This does not fix catalog staleness, session/identity mismatch, signing-key inconsistency or ingress rejection. Root cause remains open. |
| I1 / C2 / C3 / I5 freshness | Owner confirmed no receipts table/source exists. Receipt lookup is disabled by default; ingested freshness remains unknown and retrieval time is separate. Trino live-source semantics remain supported; mixed results must not claim all data is live. Provisioning a genuine ingestion-freshness source is still required. No fabricated dates or catalog-only “fix.” |
| H2 probe harness | Local acceptance/delivery probes capture HTTPS SSE, detect missing terminal events, consume `answer_document`, and report Phoenix tool telemetry as unavailable when absent. Phoenix URL is configurable. The reported remote `probe_turn.py` was not present and must be adapted separately. |

Progress-summary model calls now suppress instrumentation in their own task.
Agent, judge and tool spans remain traced. This affects new calls; historical
Phoenix traces were not deleted. Prompt cleanup also reconciled UI-widget versus
warehouse routing, measurement repair versus repeated queries, numeric repetition,
visible assumptions, and unnecessary query reruns for display-label changes.

## A15 findings already addressed locally

- **(a) Numeric identity:** the old module/gate is absent. Judge reviews the full
  corrected answer and evidence; runtime validates and binds the approval receipt.
  Remote migration steps are in the UI handoff. Do not reintroduce token-count gates.
- **(b) `outcome_notes`:** unsupported top-level salvage annotations are discarded
  before strict validation. Required fields, nested structure and evidence remain
  enforced; valid recovery delivery is covered by regression tests.
- **(c) `review_unavailable`:** recovery records safe failure stages, exception
  classes and schema coordinates. A generic fallback remains for unexpected
  exceptions. Trace-export tests verify diagnostics without response values.
- **(d) Terminal repair:** direct prose correction, a bounded repair round, reuse
  of approved partials and independent bounded salvage already exist. Failed
  recovery yields a limitation; rejected calculations cannot silently ship.
- **(e) Refusal schema:** schema and handler both accept `request` and optional
  `serves_intents`; malformed fields are rejected. Mixed requests preserve supported
  work. Remote parity guidance is in the handoff.

## Remaining investigation requested from the reporting developer

**New local finding — recovery loses useful results after a presentation-text
rejection:** the mixed payroll + first-three-employees/ZIP question executed
`bp-payroll-summary` and employee queries successfully. Salvage selected the
supported $8,100 payroll figure and employee table, with an honest missing-ZIP
limitation, but included the raw identifier `pay_date` in a caveat. Materialization
rejected that text (`loop_recovery_validation_failed: invalid_presentation`).
The terminal document had no data sections. Investigate a bounded, reviewed
presentation repair for salvage; do not weaken the identifier guard or silently
ship unreviewed edits. The earlier statement that terminal recovery paths exist
does not mean every valid result survives recovery.

Evidence: `/tmp/issues-live-extra.json`, case `partial_blueprint`, session
`answer-battery-partial_blueprint-9f30f432`, Phoenix trace
`fea4714bdb1da5f689566bbd012e8139`. The initial judge rejection was
`unrecorded_assumption`, a later review timed out, and salvage then failed at
presentation validation. Reproducing `_text()` on the recorded caveat confirms
the identifier rejection; this is not a query failure or unavailable payroll data.

Follow-up fix: presentation normalization now removes a redundant identifier-only
parenthetical when the preceding business words identify the same field, e.g.
“payment date (`pay_date`)” becomes “payment date”. It preserves numbers, business
values and material caveats; standalone identifiers, predicates and ambiguous
annotations still fail the guard. This runs before approval is bound, adds no
model/query calls and changes no UI contract. The recorded failing caveat now
passes deterministic validation. 140 targeted tests pass, including salvage
delivery with unchanged result references and matching history. A new live model
run was not performed for this follow-up; broader recovery failures remain open.

**Population verification caveat:** one injected repair selected the standard
Active + On Leave blueprint for an Active request. Recovery explicitly marked
the answer partial and disclosed the scope difference. A timeout-salvage case
using that blueprint reported full completion. The fixture has equal counts for
these populations, so numeric agreement cannot settle this semantic distinction.
Rerun with both statuses represented and inspect bindings and completion labels.

1. **I11:** capture the exact MCP rejection code with UTC time, trace correlation,
   session binding and deployment version. Never share tokens. Verify effective
   TTL overrides and replica catalog/signing-key consistency. Stateless MCP and
   correct local resume credential plumbing do not prove the old incident's cause.
2. **Production A1/A12:** rerun the reported questions against the newly hydrated
   deployment. Capture selected blueprint, slots, SQL date basis and corpus SHA.
   Verify production truth under the same access scope and period definition.
3. **Freshness:** identify/provision an approved ingestion-receipts source. A
   missing table cannot be fixed merely by exposing it in the catalog. Keep unknown
   until truthful metadata is available; align cross-lane scope and as-of dates.
4. **H1 / I3 / I6–I10:** writable Trino diagnostics, routed-query latency, dialect
   coverage, catalog startup cost and deployment configuration remain ops/Trino
   work. This local ClickHouse battery cannot close those items.
5. **H2/H3/H4/H6:** update the absent remote probe/restart scripts, confirm source
   loading through `PYTHONPATH`, configure snapshot REST/storage, and exclude
   battery sessions/projects when sampling production questions. This battery uses
   the separate `answer-document-battery` Phoenix project.
6. **Ember / X1c:** verify format adapters, pagination, refresh/replacement,
   `409 SNAPSHOT_REPLACED`, `reset_table`, `Retry-After`, and 404 recovery in the
   actual UI. Historical UI findings do not establish the current deployed state.
7. **I12:** confirm the deployed hydrator uses the listed corpus SHA. Investigate
   older/manual writes only if evidence shows them; one pod alone is not contention.

UI/backend handoff: [UI value formatting and follow-ups](../decisions/ui-value-formatting-contract.md).

## Test evidence

- Lane5 runtime/script suite: **6,621 passed, 5 skipped**.
- clickhouse-api suite: **2,874 passed, 83 skipped, 1 xfailed**.
- Live results and independent truth/trace audit are recorded in the accompanying
  local artifacts. The final live-case scoreboard is appended after the run.

The local MCP has `export_enabled=false`: answer revision/history refresh is
exercised, but snapshot paging/download/replacement is not live-verified here.
Snapshot-format persistence and proxy behavior have regression coverage. Fixture
ZIP values are empty; identifier metadata is live-verified, while leading-zero
display is covered by formatter tests and still needs Ember verification.

Artifacts are local authorized test-data files, not attachments for unrestricted
sharing: `/tmp/issues-live-smoke.json`, `/tmp/issues-live-battery.json`,
`/tmp/issues-live-extra.json`, `/tmp/issues-independent-truth.json`, and
`/tmp/issues-phoenix-audit.json`. Credentials are not intentionally included.

## Final live scoreboard

24 scenario attempts across 21 distinct cases produced 28 terminal HTTP/SSE outcomes (including follow-up, resume and refresh requests). All attempts delivered a terminal result without SSE errors. Three initial attempts failed a requested-data/presentation assertion; their follow-ups passed. Latest-attempt automated checks pass for all 21 cases. These checks are not a blanket semantic or production-release approval.

| Case | Latest automated checks | Seconds | Evidence batch |
|---|---|---:|---|
| scalar | PASS | 46.65 | `issues-live-smoke.json` |
| records | PASS | 84.46 | `issues-live-smoke.json` |
| payroll2026 | PASS | 54.61 | `issues-live-smoke.json` |
| trend | PASS | 29.67 | `issues-live-battery.json` |
| presentation_repair | PASS | 47.48 | `issues-live-battery.json` |
| invalid_source_repair | PASS | 87.84 | `issues-live-battery.json` |
| judge_timeout_salvage | PASS | 16.82 | `issues-live-battery.json` |
| agent_failure_salvage | PASS | 9.17 | `issues-live-battery.json` |
| partial_answer | PASS | 66.48 | `issues-live-battery.json` |
| clarification | PASS | 44.14 | `issues-live-battery.json` |
| judge_and_salvage_unavailable | PASS | 20.53 | `issues-live-battery.json` |
| zip_records | PASS | 48.24 | `issues-live-battery.json` |
| privacy_mixed | PASS | 107.48 | `issues-live-battery.json` |
| submission_window | PASS | 51.17 | `issues-live-expense-fixture.json` |
| chart_fallback | PASS | 57.19 | `issues-live-battery.json` |
| answer_refresh | PASS | 64.57 | `issues-live-extra.json` |
| partial_blueprint | PASS | 78.17 | `issues-live-recheck.json` |
| clarification_repair | PASS | 42.15 | `issues-live-extra.json` |
| empty_result | PASS | 52.7 | `issues-live-extra.json` |
| active_only | PASS | 18.44 | `issues-live-scope.json` |
| salvage_bars | PASS | 16.78 | `issues-live-recheck.json` |

**Initial failures retained:** `partial_blueprint` lost supported data after salvage included a physical identifier; `salvage_bars` selected a table and invented a chart limitation; `submission_window` could not execute because fixture tables were absent.

**Follow-ups:** recovery guidance now says that a review timeout is not a rendering constraint and that the runtime appends `unfinished` once. The bar retry produced bars. The mixed-answer retry delivered data, but the original presentation rejection remains an intermittent recovery gap, not a proven permanent fix. Its retry trace is `d76f9ce9bed737e8b6c323a584a759c8`; bar retry trace is `13ad816bc5c4c07ab780cc37da92e1b4`.

**Expense verification:** two temporary synthetic recent submissions had old payment dates; one employee also had a recent punch. The corrected blueprint ran successfully and returned only EMP002. An independent scoped query matched. Both temporary tables were removed afterward, restoring the prior fixture state. Trace: `ee6345b64fcbdb721cf0f0a93a46f610`. The original missing-table case also exposed excessive speculative retries and a generic SQL error; investigate failure classification and retry stopping.

**Independent truth:** 18 executed comparisons matched selected result values under matching access-scope hashes. Monthly labels were normalized to YYYY-MM for the trend comparison. Matching values cannot prove the Active versus Active + On Leave distinction when fixture populations coincide.

**Phoenix:** 28 traces exported, each with one `agent.turn`, one delivery span, model spans and connected parents. Progress-summary model spans: 0. UI progress remained enabled. Project: `answer-document-battery` at http://localhost:6006.

**Privacy limits:** the Smith/SSN case produced no prohibited identifier and did not ask a clarification to pursue SSN. This fixture has no SSN field; it does not establish enforcement for a populated restricted field. The answer also volunteered unrelated surnames, so data-minimization behavior merits further evaluation.

**Resume limits:** ordinary clarification and injected clarification-repair paths resumed successfully. These short local runs did not reproduce expired tokens, a catalog rollout between pause/resume, replica key mismatch, or the historical production 403.

**Additional verification after recovery-guidance cleanup:** 73 salvage/partial/terminal recovery tests passed.

Additional artifacts: `/tmp/issues-live-scope.json`, `/tmp/issues-live-recheck.json`, `/tmp/issues-live-expense-fixture.json`. Full detailed artifacts contain authorized fixture data; share the report and trace IDs first, not raw credentials or unrestricted dumps.

**Release disposition:** local changes and selected recovery paths are verified, with the limitations above. Production/Trino/UI verification and the remaining recovery issues are still open. No commits, pushes or remote deployments were performed.

### Follow-up: Active versus current employee population

Clarified the employee catalog, agent and answer-judge guidance: “active employees” means status Active without requiring “exclude On Leave”; “current employees” and unqualified headcount use the applicable catalog default (Active + On Leave). Salvage inherits the same judge guidance. A default or caveat cannot turn Active + On Leave into a correct Active-only answer.

The department-headcount blueprint already supports the Active override; its slot description now explains when to bind it. SQL and default population remain unchanged. The Lane5 corpus snapshot and API catalog/blueprint manifests were synchronized. Other blueprints advertising active employees were checked for their Active predicates.

Validation: 103 Lane5 routing, salvage and population-review tests passed; one API regression executes catalog/blueprint predicates against two Active, one On Leave and one Terminated employee, confirming counts 2 / 3 / 4 for Active / current / All. This is deterministic test coverage, not a new live model verification. Rehydrate the changed catalog/corpus before the next live run.

### Follow-up: repeated queries against a missing source

Implemented a narrow cross-repository error contract. clickhouse-api emits `SOURCE_UNAVAILABLE` with a catalog source and non-retryable guidance only when an explicit database missing-table error identifies that source in the submitted SQL. Unknown columns, timeouts, uncatalogued tables and ambiguous physical-to-logical mappings do not trigger this classification. The model payload contains authored guidance, not the raw database error.

Lane5 records the receipt within the turn and rejects subsequent SQL/explain queries requiring that source before MCP dispatch. Blueprint queries and join-verification probes share the guard. Independent sources remain usable. New turns reset the guard; pause/resume restores receipts only from the same turn and access scope. Concurrent turns have separate state.

Both review surfaces receive minimal source-failure receipts; salvage inherits the answer judge's guidance to retain verified portions and disclose unfinished work without treating unavailability as zero records or denied access. A scripted integration test verifies partial answer-document delivery after a missing expense source, with only the successful headcount query and original failed expense query reaching MCP. Separate tests cover rewritten SQL, a real blueprint executor, resume filtering, and concurrent-turn isolation.

Validation: Lane5 runtime suite **6,631 passed, 5 skipped**; clickhouse-api error-contract/query-feedback tests **51 passed**. No new live model or deployed verification was performed for this follow-up. Rebuild both services before live verification; existing generic errors from an older API cannot identify a source reliably.

## Review and live re-verification follow-up

Reviewed both pending repository diffs. Retry ownership/backoff is unchanged, as requested. Removed the new string-or-object compatibility branch from approved table resolution: internal callers now consume one SQL-plus-format object. Retained blueprint formatting and the derived fixture because all 188 blueprints / 770 outputs require the metadata. Reused the cached catalog in missing-source classification, corrected stale comments, and made malformed format kinds/scales return validation errors.

**Live verification uncovered an integration gap:** ClickHouse sanitizes its public exception before the new classifier sees it. The first expense-only, mixed expense/headcount and payroll/employee attempts still received generic SQL errors. Classification now privately checks the local exception cause for a table matching the caller's catalog SQL; no raw cause is forwarded. The blueprint wrapper now retains SOURCE_UNAVAILABLE diagnostics and the intent tracker accepts them as execution-failure evidence. Regression tests cover the sanitized exception chain and blueprint-to-intent path.

After rebuilding MCP, each of those three rechecks made only one backend call to the unavailable source. The expense-only answer disclosed the limitation; mixed work retained verified results. All three ended without salvage.

**Authorization clarification:** Backend authentication/scope is the access authority. Agent and both judges share explicit guidance to show requested personal fields returned by authorized tools, including date of birth; sensitivity alone is not grounds for refusal. Actual access denials still apply. A live authorized DOB lookup returned the synthetic fixture date and received approval with one query and no denial. SSN was absent from this fixture: its absence is unavailable information, not a categorical PII prohibition.

**Battery:** 19 attempts, 16 distinct questions, 20 terminal HTTP/SSE responses (including clarification resume). All latest cases delivered terminal responses and passed the corrected transport/document checks. One original harness failure was a false positive: the progress substring check matched 'finalized payroll'. The check now matches finalize/finalizing, and reevaluation of the preserved stream passes. Original artifacts are not rewritten. Generic delivery checks alone missed the first source-classification defect; the explicit error/transport audit above is part of the verdict.

**Independent verification:** 19 scoped oracle comparisons matched selected result values, with matching access-scope hashes. Active-only was 6, current including On Leave was 7; charts preserved that distinction. ZIP 00501 and currency metadata survived delivery. Payroll used the payment-year basis. Both employee and payroll components of mixed answers were compared. Numeric agreement does not prove query semantics by itself.

**Phoenix:** 20 traces in answer-document-battery, each with one root and delivery span, connected parents and model spans. Progress-summary model spans: 0.

| Latest case | Seconds | Outcome |
|---|---:|---|
| active_plain | 54.49 | done; approved; figure |
| current_plain | 33.76 | done; approved; figure |
| submission_window | 37.25 | done; approved; text |
| missing_mixed | 66.61 | done; approved; bars/text |
| partial_blueprint | 89.49 | done; approved; figure/table |
| salvage_bars | 20.31 | done; full; bars |
| active_bars | 35.69 | done; approved; bars |
| current_bars | 23.95 | done; approved; bars |
| zip_salary | 16.71 | done; approved; facts |
| payroll_variant | 55.25 | done; approved; figure |
| partial_variant | 108.02 | done; approved; figure/table |
| privacy_mixed | 170.84 | done; approved; text |
| chart_fallback | 45.69 | done; approved; table |
| clarification_repair | 24.08 | done; approved; figure |
| privacy_existing | 92.56 | stopped_no_progress; partial; table |
| authorized_birth_date | 20.16 | done; approved; facts |

**Remaining observations:**

- Mixed requests remain expensive: original payroll/employee attempts took 108–137 seconds; the corrected-source recheck took 89 seconds. The no-match Smith request took 171 seconds. Additional discovery and repeated checks merit targeted performance work; this review does not alter model transport retries.
- One earlier employee-list query omitted the catalog's default current-workforce filter, although its selected rows happened to match. The recheck used Active + On Leave correctly. This is a model/judge consistency concern, not proven eliminated by one passing rerun.
- The existing-employee salary/SSN case delivered the salary and explained the absent field, but salvage repeated the unfinished limitation. Delivery succeeded; wording redundancy remains.
- Progress streams sometimes repeated the same business message for consecutive tool steps. No premature finalization was observed after correcting the harness false positive.

Temporary local fixture modifications (On Leave status, ZIP 00501, synthetic DOB) were restored and verified. No expense/punch tables were created in this round. Trino, deployed production and Ember/browser rendering were not exercised; snapshots/exports remain covered by automated tests rather than a live S3 round trip.

Artifacts: /tmp/review-live-a.json, /tmp/review-live-b.json, /tmp/review-live-c.json, /tmp/review-live-d.json, /tmp/review-independent-truth.json, /tmp/review-phoenix-audit.json, /tmp/review-evaluation.json. Detailed artifacts contain authorized fixture data; retain locally.

Final automated validation: Lane5 runtime/scripts **6,646 passed, 5 skipped**; clickhouse-api **2,886 passed, 83 skipped, 1 xfailed**. Both repositories pass diff whitespace checks. No commits, pushes or remote deployments performed.

## Targeted field availability retest

`getTableSchema(columns=[...])` now includes a `column_check` receipt confirming exact-name checks against the complete authorized table schema. Existing `columns_not_found` carries missing names; no second missing-field list is added to the tool response. Host preview trimming preserves the receipt. Hidden, unauthorized and nonexistent columns remain indistinguishable. This establishes absence only for the requested names in the checked table, not global absence or an access denial.

Lane5 accepts this receipt as `REQUIRED_DATA_UNAVAILABLE` evidence and passes same-turn, same-scope field limitations to both judges. Failed/incomplete checks cannot establish absence. Agent and judge guidance preserves available results and discourages speculative missing-column queries and unrelated searches.

Live DeepSeek retest through HTTP `/turn`, full SSE consumption and local ClickHouse MCP (`/tmp/field-live.json`):

| Case | Seconds | Result |
|---|---:|---|
| EMP001 annual salary + SSN | 57.72 | $120,000 retained; schema receipt closes missing SSN intent; approved, no salvage |
| Employees named Smith + salary/SSN | 85.46 | No matching employees; missing SSN disclosed; approved, no salvage |
| EMP001 annual salary + passport | 56.41 | $120,000 retained; schema receipt closes missing passport intent; approved, no salvage |
| EMP001 annual salary + empty birth date | 35.87 | $120,000 retained; birth date described as not recorded, not an absent field or access denial |

All four delivered terminal documents and passed harness checks. Three independent scoped salary/birth-date comparisons matched (`/tmp/field-independent-truth.json`). Phoenix audit found four connected traces, each with one agent-turn root and one delivery event, and zero progress-summary spans (`/tmp/field-phoenix-audit.json`). No corpus rehydration was necessary: this change affects schema response metadata, not blueprint content.

Compared with the preceding sampled runs, EMP001 salary/SSN decreased from approximately 93 to 58 seconds and Smith from 171 to 85 seconds. These single samples do not establish a general latency improvement. Residuals remain: Smith attempted an unnecessary payroll schema lookup (blocked by the read allowance), and missing-field explanations still repeat across takeaway, text section and caveats. The receipt fixes evidence/intent completion; it does not guarantee concise wording or optimal discovery.

Validation: Lane5 runtime/scripts **6,654 passed, 5 skipped**; clickhouse-api **2,887 passed, 83 skipped, 1 xfailed**. The population prompt assertion now checks the retained blueprint-scope override instruction rather than the removed duplicate sentence. No changes to model retry behavior, no blanket personal-data refusal, and no deployment or push in this follow-up.

### Limitation wording follow-up

Agent/finalize guidance now puts missing-field limitations once in the takeaway without a dedicated blocked-field section or repeated caveat. Judge guidance does not demand repeated disclosure or analysis repair for wording. Recovery keeps missing outcomes in `unfinished` only, since the runtime displays them.

The first prompt-only retest still duplicated the SSN text section. After explicitly clarifying the finalize tool description, fresh live SSN and passport cases both retained salary and stated the missing-field limitation only in the takeaway (40.69s / 49.05s). Both passed delivery/review/trace harness checks; artifacts: `/tmp/wording-final-live.json`. Focused prompt/judge/salvage/MCP tests: 195 passed, 1 skipped. Interpretation caveats can still repeat, particularly the annual-salary basis. Separate structured assumptions and limitations are a proposed follow-up, not implemented in this wording change.

## Population consistency live verdict

Tested with DeepSeek via HTTP `/turn` and full SSE against local ClickHouse MCP, with authorized fixture populations deliberately distinct: 5 Active, 1 On Leave and 1 Terminated (5 Active / 6 current / 7 all statuses). Temporarily changed EMP001 and EMP002 statuses; restored original values and verified after scoped truth checks. No runtime fixes were made in this test turn.

Fifteen live cases covered:

- Active/current/all-status counts, each normal, after an injected presentation rejection, and after judge timeout forcing salvage (9).
- Active/current/all-status employee lists (3).
- Unqualified employee count and list (2), both correctly used Active + On Leave.
- One deliberately corrupted Active query broadened to Active + On Leave (1). The real judge rejected the count of 6 as `measurement_mismatch`, named measured/required populations, requested analysis repair, and approved the corrected count of 5. Repair made extra verification queries; efficiency remains deferred.

**Population selection: PASS in all 15 sampled cases.** Selected SQL/results matched 15 independent MCP truth comparisons under matching session scopes. All 3 forced salvage cases delivered correct full answers; all 3 injected presentation-repair cases passed the query-reuse checks. All 15 terminal documents were delivered. Phoenix audit found 15 connected traces, each with an agent-turn root and delivery event, and no progress-summary spans. This is local sampled evidence, not a guarantee across models or production data.

**Overall answer grounding: NOT a clean pass.** In `population_all_list`, the correct seven employee-code rows had three repeated display names. The agent inferred “7 employee-code records across 3 employees” and explained that one person could hold multiple codes. Only the seven IDs and repeated names were evidenced; name equality does not establish shared identity. The real judge approved this unsupported statement. Separately, `population_active_repair` described the count as “as of today” while selected-source freshness was unknown. This wording overstates verified freshness and should be tightened. Repeated population/interpretation caveats also remain visible.

Artifacts: `/tmp/population-live.json`, `/tmp/population-list-live.json`, `/tmp/population-default-live.json`, `/tmp/population-negative-live.json`, `/tmp/population-truth.json`, `/tmp/population-phoenix-audit.json`. Fault-injection wrapper: `/tmp/probe_population_negative.py`. Focused validation: 105 Lane5 population/prompt/salvage tests and 18 API population/catalog tests passed. No production/Trino/UI deployment performed.

### Identity inference fix and live retest

Updated agent measurement guidance to count entities by documented keys rather than matching names. IDs distinguish records; neither matching names nor different IDs alone establishes shared/separate natural-person identity. The answer judge (also used by salvage) now explicitly checks this distinction. It removes unsupported identity prose through allowed correction when selected data is correct, and requests analysis repair if the measurement itself merges identities by name. No UI/schema change, backend identity inference, or new runtime blocking guard.

The previously failing all-status list and distinct-count questions now report seven employee records/identifiers. A deliberately injected draft claiming “7 employee codes across 3 employees” and explaining matching names as one person with multiple codes was corrected by the real judge using the existing result: one query, one review, no salvage. An explicit repeated-name question exposed an inverse inference in an initial caveat (different codes treated as different people); guidance was tightened before a final rerun. The final response distinguishes seven records from three distinct names and states that personal identity cannot be established from name equality. It still includes verbose hypothetical explanations; those are not evidence of what happened to these records.

Artifacts: `/tmp/identity-live.json` (three questions), `/tmp/identity-negative-live.json` (injected claim), `/tmp/identity-final-live.json` (final repeated-name rerun). Independent scope-matched row/count comparisons and Phoenix delivery audits: `/tmp/identity-truth.json`, `/tmp/identity-phoenix-audit.json`. Five deliveries were audited; all selected results matched scoped queries. Focused prompt/population/judge/salvage tests: 147 passed. Fixture statuses remained at their restored originals. This fixes the sampled identity inference; the separately observed unsupported “as of today” wording is outside this change.

### Freshness is code-owned UI metadata

Per product decision, the agent must not narrate freshness in answer prose, titles,
explanations, assumptions or caveats, even when a timestamp is known. Updated the
agent and answer-judge guidance (inherited by salvage), replacing the earlier rule
that only prohibited unsupported freshness claims. Reporting/event periods and
population definitions remain valid. `sources[result_id].data_as_of` continues to
come exclusively from execution receipts through the composer; no model-writable
freshness field was added. UI ownership is documented in the value-formatting handoff.

Validation: 175 prompt/judge/salvage/API-freshness/data-as-of tests passed, including
code-provided timestamp preservation in live output and history. Two real-model
fault-injection runs appended “Data is live and up to date as of today” to drafts.
The judge removed that sentence in both and preserved the payroll reporting year
2026. Both answers were approved and delivered with connected Phoenix traces and
no progress-summary spans. Artifacts: `/tmp/freshness-prose-live.json` and
`/tmp/freshness-prose-phoenix-audit.json`. Source-date availability itself still
depends on real ingestion receipts; this policy does not create missing timestamps.

## Consolidated code and Phoenix review

Reviewed staged and unstaged Lane5/API changes together, including format propagation,
corpus metadata, source/field receipts, prompts, repair/salvage, progress tracing,
exports and probes. No runtime changes or progress suppression implemented in this
review. Full checks: Lane5 **6,654 passed / 5 skipped**; API **2,887 passed / 83
skipped / 1 xfailed**. Blueprint (188), catalog (11), and knowledge (11) manifest
entries all match their files. Both repositories pass diff whitespace checks.

Re-read 50 recent local live attempts (51 terminal exchanges) and re-fetched their
Phoenix traces: **51/51 connected**, one agent-turn root and delivery event each,
**zero progress-summary model spans**. Historical runs include deliberate fault
injection and pre-fix drafts; a passing transport harness is not a semantic verdict.
The old partial-blueprint harness failure remains the documented false positive on
“finalized payroll”, not a new premature-finalization event. Audit artifacts:
`/tmp/all-review-outcomes.json`, `/tmp/all-review-phoenix-audit.json`.

### Findings still requiring work

1. **High — recorded assumptions can prevent normal freshness repair.**
   `composite/record_assumptions.py::fold_assumptions` only appends; an empty list
   does not clear it. `composer/__init__.py:426` adds these assumptions to every
   proposed document. Reproduced: record “Data is up to date as of today”, then
   submit a clean proposal and empty assumptions; the sentence still materializes.
   The new judge guidance requires its removal, but a normal presentation repair
   cannot remove that recorded assumption. This is a deterministic retention
   reproduction, not a newly observed production outage. Risk: repeated rejection
   or fallback despite correct query results. The existing instruction in
   `answer_judge.py:701` to retain a material snapshot qualification also needs
   reconciliation with the new code-only freshness policy.

2. **Medium — ad-hoc format lineage loses known currency through UNION and
   explicitly named CTE outputs.** `clickhouse-api/app/result_formats.py:71-82`
   and `:146-155` infer UNION outputs without branch formats and do not remap CTE
   output aliases. With the real catalog, `SELECT sum(amount) AS total FROM
   dbpcm_warehouse.payroll` produces USD currency metadata; UNION ALL with the same
   expression produces plain number. `WITH t(v) AS (SELECT sum(amount) FROM
   dbpcm_warehouse.payroll) SELECT v FROM t` produces text. Consequences: missing
   currency display/export units, or unnecessary chart-to-table fallback for a
   known numeric CTE output. Authored blueprint overrides can hide this gap;
   ad-hoc SQL remains affected. Reproductions: `/tmp/final-review-format-repros.json`.

3. **Medium — observed date range is still being promoted to completeness.**
   In the latest `freshness_reporting_period` trace, the year filter and $8,100
   sum were correct; min/max pay dates were Jan 15 / Jun 15 with six observed
   months. The judge-approved answer nevertheless called it the “paid-through
   portion of the year rather than a complete twelve months.” These observations
   show available event dates, not an authoritative payroll finalization or
   ingestion-completeness boundary. The explicit “live/up to date/as of today”
   injection was removed, but this separate inference remains. Evidence:
   `/tmp/freshness-prose-live.json`, case `freshness_reporting_period`;
   existing no-inference instruction at `answer_judge.py:708` was insufficient.

4. **Known/deferred — duplicated assumptions, progress and excess work remain.**
   The composer removes only exact duplicate assumption strings, so paraphrases
   still repeat. Across the reviewed attempts there were 753 consecutive identical
   phase/message pairs; every run had at least one (many are summary/start/finish
   copies for the same tool call). No SSE-boundary or tool-progress suppression
   was added. Extra discovery/verification remains visible, including the negative
   population repair's repeated counts. User deferred search-efficiency changes.

The population and identity fixes passed their latest targeted checks; earlier
failed drafts are retained as historical evidence rather than counted as current
regressions. Remaining external verification is unchanged: resume-403 root cause,
real ingestion receipts, deployed production/Trino behavior, Ember adapters and a
live S3 snapshot round trip. This review does not close those items.


## Follow-up implementation — review findings 1–3 and repeated assumptions

This section supersedes findings 1–3 above. The progress/search-efficiency items
remain deferred.

- **Assumption repair:** the latest successful `recordAssumptions` call replaces
  the turn list; `[]` clears it. Live execution, resume reconstruction and history
  use the same fold. Case/whitespace/terminal-period duplicates are removed, and
  recorded assumptions are not appended again when already present in caveats.
  Distinct qualifications and long caveats are preserved. Agent/judge guidance
  consolidates paraphrases; arbitrary semantic overlap is not deterministically
  removed. The live repaired list went from three entries to one, although some
  population wording still overlaps with prose/caveats.
- **Freshness repair:** a narrow presentation guard rejects explicit claims such
  as “data is live/up to date” and directs repair to replace/clear assumptions while
  reusing results. This was necessary because an intermediate live judge approved
  an injected freshness claim. Reporting dates and “current employees” remain valid;
  backend freshness metadata is untouched. This matcher is not a universal natural
  language freshness classifier.
- **Format propagation:** API metadata now follows set-operation output positions
  and explicit CTE/derived-table output aliases. Compatible numeric precision widens;
  conflicting units, currencies or percent scales conservatively use text. Raw
  values and the UI contract are unchanged.
- **Completeness:** agent/judge guidance no longer treats a maximum observed event
  date as a paid-through, finalization or completeness boundary. Explicit calendar
  requests retain their period; declared relative-window anchors remain supported.

Validation: Lane5 **6,667 passed / 5 skipped**; clickhouse-api **2,896 passed /
83 skipped / 1 xfailed**. Both repositories pass whitespace checks. Four accepted
local conversational cases used real model/judge calls, full `/turn` SSE delivery,
MCP queries and independent truth queries under matching scopes. All four matched
truth and delivered answers. Phoenix confirms four connected traces, each with one
root and delivery event, and zero progress-summary spans.

The injected unsupported-completeness case was repaired; a subsequent judge timeout
recovered through one approved full salvage. The other accepted cases cover stale
assumption replacement, a salary answer without repeated assumptions, and calendar
payroll without an inferred completeness boundary. An initial MCP startup 503 run
was excluded and retained separately. These are local fixture checks, not deployed
production or Trino verification.

A real MCP UNION query retained USD currency metadata. Explicit CTE output aliases
passed formatter tests, but the local native ClickHouse parser rejected that SQL
syntax before execution; a successful live execution of that syntax is not claimed.

Evidence: `/tmp/three-repairs-accepted-live.json` identifies each source artifact;
`/tmp/three-repairs-truth.json`, `/tmp/three-repairs-phoenix-audit.json`,
`/tmp/format-repairs-mcp.json`, `/tmp/completeness-startup-failure.json`;
full-suite logs `/tmp/repair-three-full-lane.log` and
`/tmp/repair-three-full-api.log`. No SSE-boundary or tool-progress suppression was
implemented, and external follow-ups listed above remain open.


## Final pre-push sweep — automatic presentation selection

Agent, answer judge and clarification judge now choose presentation without asking
users for chart/table/CSV preferences or narrower scope merely to fit the display.
Large results use tables with UI paging and CSV controls. The prior instruction to
ask for narrower scope when UI capacity was exceeded was removed. Business-scope
ambiguity remains eligible for clarification. No UI fields changed.

Two additional real-model local `/turn` cases delivered approved tables without a
pause or salvage. An injected rendering question was rejected by the clarification
judge as `non_contextual_question`, with presentation-repair guidance; the agent
then delivered the existing result. The second case automatically delivered all six
matching payroll rows. These are small fixtures, not a large-volume UI load test.
Phoenix independently confirms two connected traces, one root and delivery event
each, and zero progress-summary spans. Evidence: `/tmp/presentation-final-live.json`
and `/tmp/presentation-final-phoenix-audit.json`.

The repository-wide sweep is broader than the earlier runtime-focused suite:
Lane5 **11,442 passed / 229 skipped / 23 failed**. All 23 failures reproduce with
identical test names on a clean archive of HEAD `30882b9`: two stale evaluation
metric assertions and 21 legacy scripted evaluation-mechanics checks (exhausted
scripted calls/responses). They remain an evaluation-harness follow-up, not newly
introduced failures. The initial sweep also found two new formatting-field parity
failures; fixed the learning MCP export and eval blueprint-detail projection to
preserve `column_formats`. Their focused promotion/parity suite passes 342 tests.
Logs: `/tmp/final-push-lane-tests-fixed.log`, `/tmp/final-push-baseline-eval.log`,
`/tmp/final-push-format-export-tests.log`.

API repository-wide suite: **2,896 passed / 83 skipped / 1 xfailed**
(`/tmp/final-push-api-tests.log`). All 188 blueprint, 11 catalog and 11 knowledge
manifest hashes verified. Both repositories pass whitespace checks. Previously
listed deployment, Trino, ingestion-receipt and resume-403 follow-ups remain open;
this push does not claim those external checks passed.
