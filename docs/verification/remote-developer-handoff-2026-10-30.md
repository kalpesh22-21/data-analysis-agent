# Lane5 and ClickHouse API final developer handoff


> **Ember migration:** use the consolidated [2026-10-31 migration guide](ember-ui-v2-migration-2026-10-31.md) for removed fields, exact payloads, paging/download URLs and mandatory data controls.

> **Contract update:** the SQL-based [answer-document v2 contract](../ui-answer-document-spec.md) supersedes all handle resolution and backend-rendered widget descriptions below. `/query/page` returns the first saved page and snapshot ID. CSV uses existing part URLs.

Date: 2026-10-30
Audience: remote Lane5 developer, clickhouse-api owner, and Ember UI developer

The baseline commits below were previously pushed. The schema-v2 contract, formatting enrichment and Lane5 paging wrapper described here are included in the accompanying follow-up change set. clickhouse-api needs no additional change for this transport revision. Adopt the Lane5 update when released, implement the UI responsibilities, and run the deployment checks in this document. Local verification supports the implementation; it does not close the production payroll, Trino, resume-403, or Ember integration incidents.

This is the consolidated handoff for value formatting, assumptions, answer presentation, repair, freshness, and the live-run issues. It supersedes historical wire examples and interim conclusions in the earlier assumptions and issues documents. Do not restore legacy answer fields. Confirm the deployed commit before using the v2 contract.

## 1. Versions to adopt

| Repository | Pushed branch | Commit | Contents |
|---|---|---|---|
| Lane5 / data-analysis-agent | `feature/data-as-of` | `08f7755` | Answer review, presentation selection, assumptions repair, formatting propagation, availability receipts, progress tracing, probes and handoff documentation |
| clickhouse-api | `trino-explore` | `496be18` | Catalog and blueprint formats, query/snapshot formats, population and period semantics, availability receipts, freshness configuration and scope-token default |

These commits build on the answer-document, history, refresh and source-freshness work already present in their parent branches. A remote branch missing that work must adopt the required dependencies too; the latest commits alone are not a standalone replacement for the earlier contract.

Current corpus fingerprints:

| Artifact | Fingerprint |
|---|---|
| `BLUEPRINTS_SHA` | `e03eaf5ae32428af3a13f5f48dd96edb9fe13920` |
| `CATALOG_SHA` | `9c76cf3a7f87c385e6f0ff749e892dd749cddb45` |
| `KNOWLEDGE_SHA` | `e7322287e508e7a8e223a5dd8f4b6e17b8bcbdde` |

All manifest entries were checked: 188 blueprints, 11 catalog files and 11 knowledge files. The earlier report's 193-blueprint count describes a different corpus version. The format audit covers the current 188 blueprints and 770 terminal output columns, including three composed blueprints. Run the normal hydrator against the deployed API exports and verify these fingerprints rather than reusing an older seed.

## 2. Answer and presentation contract

`answer_document` is the current UI answer contract. Render the approved document, including its takeaway, ordered sections, source metadata, caveats and actions. Do not require `assistant_text`, `answer_tables`, `layout` or `blocks` to consider an answer valid. Do not rebuild a parallel answer from these old fields.

The agent selects the presentation automatically. It must not ask the user to choose a chart, table, display format or CSV, or to reduce records merely to fit a display. The clarification judge rejects such questions as `non_contextual_question` and directs the agent to proceed. Questions about genuinely unresolved business meaning, population, metric or period remain valid.

| Requested information | Preferred presentation |
|---|---|
| One headline scalar | `figure` |
| Multiple requested scalar measures | `facts` |
| Record lists or large results | `table` |
| Suitable comparisons or rankings | `bars` |
| Suitable time trends | `line` |
| Explanations, refusals or honest no-result limitations | Text; zero data sections can be valid |

Honor an explicitly requested presentation when supported. If a chart is unsuitable, use a table, retaining the requested data and an evidenced limitation where needed. Do not invent a chart limitation after a timeout. Preview truncation is not a chart-size limit. The UI fetches saved rows and applies its own display guards, falling back to the same source table when necessary. This is not permission to add an arbitrary top-N limit or narrow the requested population.

Prefer one primary presentation and zero or one visible table. Additional visible tables need distinct requested value and `table_reason`. Every data result except figures must offer table/CSV options; facts and charts keep these behind View table rather than adding a duplicate section. A trend does not always need a second visible grid, and a headcount does not require an unrequested status breakdown. Different valid shapes across runs are not automatically defects; missing requested content or unsupported claims are defects.

Numeric answers must preserve their supported data sections through repair and salvage. Paused turns must not expose a sealed composed answer. A valid text-only answer may have no sections. The UI's empty-result check must account for both cases.

Titles should identify the measure, population, unit and period as appropriate, using business language. Rendering a table title twice is a UI defect. A label-only repair should reuse the result, not rerun SQL. Do not implement a historical card-title source chain solely because an older document described it; follow the current answer-document code and reviewed labels.

## 3. Value formatting ownership and payloads

clickhouse-api owns column semantics. Authored blueprints declare `column_formats`, keyed by their exact output aliases, including derived measures. Lane5 preserves these declarations through hydration and execution. The learning MCP export and evaluation detail projection also preserve the field.

The model-facing `getBlueprint` response omits formats. `runBlueprint` and `finalizeAnswer` accept no formatting arguments. The model selects results and presentations; it does not copy or infer formatting declarations already supplied by code.

Ad-hoc SQL receives conservative catalog/expression-based formats. Aliases retain source semantics, counts become integers, and set operations align formats by output position. Explicit CTE and derived-table column aliases retain corresponding formats. Compatible numeric precision can widen; conflicting currencies, units or percentage scales fall back to text. Unknown lineage must not invent a unit from an alias or numeric-looking string.

Execution receipts retain ordered string `columns` and provide initial formats. The approved answer source holds the final format map; the UI retains it rather than sending it back to paging:

```json
{
  "columns": ["ZIP Code", "Gross Earnings", "Denial Rate"],
  "column_formats": {
    "ZIP Code": {"kind": "identifier"},
    "Gross Earnings": {"kind": "currency", "currency": "USD", "precision": 2},
    "Denial Rate": {"kind": "percent", "scale": "ratio", "precision": 2}
  },
  "rows": [["02108", "1766987.30", "0.1532"]]
}
```

In schema v2 the map is `sources[result_id].column_formats`, alongside exact executed SQL. Presentations carry only kind and column selectors; no values, points, items, per-widget formats or formatted strings. The UI fetches rows and applies the source formats to all displays.

Never parse a display string to recover values for chart geometry, sorting or export. Preserve the raw values separately.

### UI formatting rules

| Kind | Required behavior |
|---|---|
| `text` | Preserve text; do not numeric-detect. |
| `identifier` | Preserve exactly, including leading zeros; no numeric conversion, grouping or rounding. |
| `integer` | Group digits; integral values have no decimal places. Do not silently round a fractional result. |
| `number` | Group digits. Optional `precision` is the maximum fractional display precision; absent precision must not introduce arbitrary rounding. |
| `currency` | Use the declared ISO currency code and fixed fractional precision, default 2. Never infer USD. |
| `percent` | `scale: ratio` multiplies by 100 for display; `scale: percent` is already scaled. Append `%`; default precision 2. |
| `date` | Calendar date; do not shift through a timezone. |
| `datetime` | Preserve the instant and display a stated timezone. |
| `boolean` | Yes/No, not numeric formatting. |

Optional `unit` supplies a suffix such as hours, miles or per hour. `precision` is an integer from 0 through 8 and affects display only. Null is missing, not zero: show “Not available” in cells and preserve chart gaps. Unknown or missing metadata uses raw text.

Use decimal-safe formatting for large integers and decimal strings rather than first coercing them to JavaScript Number. Format raw fetched values in Ember for figures, facts and exact tooltips; v2 sends no backend display strings. Compact axes must preserve unit and percentage scale, with exact tooltips available.

Examples:

| Input and meaning | Display |
|---|---|
| ZIP `"75039"` | `75039` |
| ZIP `"02108"` | `02108` |
| Employee count `75039` | `75,039` |
| USD `1766987.3` | `$1,766,987.30` |
| Hours `125.250000`, number with unit | `125.25 hours` |
| Ratio `0.1532` | `15.32%` |
| Already-scaled percent `15.32` | `15.32%` |

Invalid authored formats fail loading. Identifier semantics do not become quantitative merely because storage is numeric. Unsupported chart measures become an explained table for judge review. Formatting must not change queries, aggregate cells, convert currencies, reconstruct missing ZIP prefixes, or invent accrual-plan units.

### CSV behavior

CSV cells intentionally remain raw: no currency symbols, grouping separators or display rounding. Headers carry declared units/currency/percentage scale, for example `Gross Earnings (USD)` and `Denial Rate (ratio)`. Thus raw CSV decimals are not themselves the A11 defect; missing display semantics in the UI were the defect.

Identifier strings retain leading zeros in the file. Spreadsheet imports must treat identifier columns as text; a raw CSV cannot force every spreadsheet application to preserve them. Do not derive CSV contents from formatted chart strings.

## 4. Assumptions and limitations

`recordAssumptions` takes the complete current-turn list. Each successful call replaces the previous list; `[]` clears it. This fixes the append-only behavior that restored an incorrect assumption during every repair. Live execution, resume reconstruction and history share the same replacement logic.

Assumptions are short business interpretations, such as “Current employees includes Active and On Leave.” They are not query facts, freshness claims, process narration or repeated display labels. Record an interpretation once. Keep genuine missing-data or execution limitations concise and scoped to what was checked.

Code removes case, whitespace and terminal-period duplicates, preserving the first wording. The composer avoids appending a recorded assumption again when already present in caveats and removes equivalent copies within a section. Distinct qualifications and long caveats are preserved. The agent/judge consolidate paraphrases; code does not merge statements merely because they sound similar. Different populations, periods or negations must remain distinct.

Semantic repetition is reduced, not eliminated: live answers can still repeat population wording between prose and caveats. Do not describe this as universal semantic deduplication.

Ember must render the final document's caveats and text without appending assumption lists from earlier drafts or old top-level wire fields. The historical `assumptions: string[] | null` UI examples are not a request to add a second panel to the current document renderer. No separate new assumptions/limitations schema was introduced. Scope filtering and saved-answer authorization still apply.

Missing-field limitations belong once in the takeaway, without a redundant blocked-field text section and caveat. Salvage returns unfinished outcomes in `unfinished`; the runtime appends that disclosure, so the model should not repeat it in every component.

## 5. Freshness, reporting dates and completeness

Freshness is exclusively code-provided UI metadata. The model must not narrate that data is live, up to date, stale, recently refreshed or “as of” a freshness date in takeaways, titles, explanations, assumptions or caveats—even if metadata is available.

The UI may display `sources[result_id].data_as_of` from execution receipts. If absent, do not substitute today, a maximum event date, snapshot creation time or retrieval time. A separately labeled Retrieved/Refreshed timestamp is not Data as of.

The ingestion receipts table does not exist in the environment described by the incident report. Merely adding its name to a catalog cannot fix that. Receipt lookup is disabled by default with `data_as_of_receipts_enabled=false`; ingested sources remain unknown until a genuine receipts source is provisioned and the setting is deliberately enabled. Existing live-source semantics remain supported.

Mixed routing must preserve per-source truth: live employee data through Trino does not make ingested ClickHouse payroll live. Where payroll has a verified receipt, use its staler timestamp for the payroll source; never substitute the live source's timestamp for the mixed result. Without an ingestion receipt, do not manufacture an overall freshness claim.

A narrow presentation guard rejects explicit freshness claims and requests correction using existing results, including replacement/clearing of assumptions. It is not a universal natural-language classifier. Requested reporting periods, event dates, “current employees,” and genuine historical point-in-time questions remain valid.

`MAX(pay_date)`, observed months and missing later rows do not prove a paid-through boundary, payroll finalization or incomplete year. Preserve an explicitly requested calendar interval. Declared relative-window anchors remain valid where they fit the request. Completeness claims require explicit supporting evidence.

## 6. Paging, snapshots, refresh and history

Ember owns the displayed row count. Use rendered rows and paging metadata:

- Known total: “Showing 20 of 211 departments”; update as rows append.
- Replaced page: “Showing 21–40 of 211.”
- Unknown total: “Showing 20 rows”; do not infer a total from page size.
- Refresh/replacement: reset counts and rows to the new snapshot.

A limited initial preview of a pageable result is not by itself a partial answer. Preserve explicit execution limitations, but do not translate “more pages remain” into “the requested analysis is incomplete.” Loading every available page does not establish source completeness either.

Post `{sql, limit, offset}` to Lane5 `/query/page` for the first saved page and snapshot ID. Later pages send `{snapshot_id, limit, offset}`. Send no formatting metadata: retain it in the saved answer document. Lane5 calls the existing clickhouse-api snapshot-create/page endpoints. There are no table handles or SQL-lookup calls.

Refresh data using `{sql, refresh_of, offset:0}`. On `409 SNAPSHOT_REPLACED`, preserve `snapshot`, `reset_table` and `Retry-After`; clear previous rows, adopt the new ID and request offset zero once. On 404/410 clear unavailable rows and offer explicit SQL recreation under current access. Show a terminal error if recovery fails; never loop blindly. A 403 clears unavailable data without retry.

Get CSV parts from `/query/snapshots/{id}/download-urls`. Use presigned S3 links directly or authenticated `/download?part=N` streams through Lane5. No concatenated full-file stream exists. Table/CSV options are required for every data result except figures; text has no data controls.

Snapshot/table refresh and reviewed answer refresh are different operations. A table refresh can change rows while leaving takeaway unchanged. A refreshed answer uses the answer ID and expected revision, reviews updated content, and publishes atomically; revision conflicts must not overwrite a newer answer. Existing history projection retains the approved answer document. Ember may use its own saved results and snapshot storage rather than fetching full history on every reload, but must preserve the document and obey current authorization/recovery responses.

Every data source, including figures and facts, contains SQL. Figures fetch their one-row result but do not show table or CSV options; facts do. There are no supporting/fallback permission flags.

## 7. Evidence, scope, blueprints and recovery

### Payroll and employee semantics

Unqualified payroll-year requests default to pay date. Explicit work-period requests keep their requested basis. Production verification must compare the same date basis; the local $8,100 fixture result is not evidence that the production $718.4M anchor has been restored.

“Active employees” means Active-only. “Current employees” and unqualified headcount use the applicable Active + On Leave default. Explicit All removes the filter only where the blueprint declares that option. Defaults never override explicit user scope. The report's explicitly active cost-per-employee question does not automatically require an Active + On Leave denominator; pin and disclose the chosen definition.

The expense-without-punches blueprint now filters expense submission time (`date_time_submitted`), not payment date. A matching retrieved blueprint should be executed; a mismatched blueprint must not be forced solely because it was found. Keep independently correct partial-blueprint results and query only uncovered work. Blueprint descriptions and definitions can inform SQL, but they are not execution evidence.

Names do not establish identity. Count using documented entity keys. Matching names do not prove one person has multiple employee codes, and different IDs alone do not prove different natural people. Do not collapse identities or invent an identity explanation without explicit mapping evidence.

### Missing sources and fields

The API emits `SOURCE_UNAVAILABLE` only when an explicit missing-table error identifies a catalog source used by the SQL. Lane5 stops later attempts against that known-unavailable source within the same turn/scope; independent sources remain usable. Blueprint execution and join probes use the same guard. Resume restores only applicable same-turn/scope receipts; new turns reset the state. Timeouts, unknown columns and ambiguous mappings must not be misclassified as missing sources.

A complete targeted `getTableSchema(columns=[...])` response supplies a field-check receipt and `columns_not_found`. Lane5 can close a missing-field intent with `REQUIRED_DATA_UNAVAILABLE` without trying speculative SQL. This establishes absence only for the checked names/table in the authorized schema. It does not prove global absence or an access denial. Null cells are different from missing fields.

Authorization is enforced by backend auth/scope. Requested personal data returned by authorized tools, including date of birth, may be shown. Sensitivity alone is not a reason to refuse. Actual access denials remain binding. Both answer and clarification judges receive minimal scoped denial/availability evidence; no additional generic review dimension was added.

### Repair and salvage

Analysis repair changes the incorrect metric, scope, filter, calculation or evidence selection. Presentation/prose repair reuses correct results. The judge reviews the whole document, including requested content, labels, caveats and fallback reasons. An explained chart-to-table fallback is not automatically partial completion.

Retain verified portions of mixed requests. Missing fields, unavailable sources, a failed lookup and denied access must have distinct, accurate limitations. A query executing successfully is not proof the final answer delivered it correctly.

A redundant identifier-only annotation such as “payment date (`pay_date`)” can be normalized to “payment date” before approval. Standalone internal identifiers, SQL predicates and ambiguous annotations still fail presentation validation. This resolves the sampled salvage rejection without weakening the guard or shipping unreviewed edits.

No new model retry-with-jitter policy was added. Existing bounded repair/review/salvage behavior remains; one later completeness test recovered through salvage after a judge timeout. A timeout does not justify abandoning a supported chart or calling complete work partial.

## 8. Progress and Phoenix

Progress describes business work and meanings, without tool names, database dialects, SQL or implementation details. Vetted table/column names may be made readable by replacing underscores; prefer meaningful business language and do not guess opaque abbreviations. Do not announce final completion or clarification as settled before review allows it.

Progress-summary model calls suppress their own tracing. Agent, judge, tool and delivery tracing remain enabled. This affects new calls; old Phoenix records were not purged. Battery traces use `answer-document-battery` so they can be excluded from production-question sampling.

Repeated progress messages still exist. No SSE-boundary deduplication or tool-progress suppression was implemented. Search/verification efficiency and broader latency reductions remain deferred. Do not silently introduce those policies while adopting this change.

## 9. Remote migration checks

| Historical concern | Current guidance |
|---|---|
| B1 history lacks `answer_document` | Current history projects the document. Verify remote branch and saved reload behavior. |
| B2 hardcoded revision/no atomic refresh | Adopt existing reviewed refresh and expected-revision publication; do not increment revision client-side. |
| B3 transport 403/5xx retry policy | No new retry policy in this work. Do not blindly retry 403; diagnose auth/session rejection. |
| B4 orphan delivery trace | Local audits show delivery connected to the agent-turn trace. Verify deployed tracing. |
| B5 empty takeaway | Require a valid terminal document; current live probes check terminal payload and persistence. Do not claim every historical remote path is fixed without testing it. |
| B6 unused `unknown` completeness value | Do not invent semantics merely to emit every enum member. Absence/unknown must not be interpreted as complete. |
| B7 table handle exposes turn index | Current `tbl_...` handles are hashed. Treat handles as opaque regardless of implementation. |
| B8 legacy harness reads old fields | Use `answer_document` in current probes/UI; separately update remote scripts still reading legacy envelopes. |
| A15(a) numeric-token correction gate | Do not reintroduce number-set/count comparison. Judge reviews the corrected takeaway with unchanged evidence/components; `approved_answer.validate_correction()` validates approval, prose repair, non-empty correction and no conflicting rejection. Update fingerprint, scope-bound delivery and saved history together. |
| A15(b) salvage `outcome_notes` | Current recovery discards unsupported top-level annotations before strict validation; required/nested/evidence constraints remain. |
| A15(c) hidden review failure reasons | Keep safe failure stage/class/schema coordinates in tracing; never log response values or credentials. |
| A15(d) terminal repair | Existing bounded repair, approved partial reuse and salvage remain. Failed recovery yields an honest limitation, not rejected calculations. |
| A15(e) `declineOutOfScope` drift | Schema and handler accept required `request` plus optional `serves_intents`; reject unknown/malformed fields. Bind only the unsupported intent and complete permitted work. |
| A15(f) PII-specific dimension | No blanket PII rejection or new dimension. Both judges use actual scope/denial receipts and preserve authorized results. |
| Old Ember block renderer defects | Use the answer-document adapters. Do not recreate silent-empty guards, duplicate titles, guessed bar dividers, first-series-only donuts or harsh numeric-string coercion. Verify behavior in the actual UI. |

## 10. Open investigations and owners

| Report IDs | Owner | Remaining action |
|---|---|---|
| A1/C1, A12 | Lane5 + corpus | Repeat production questions after hydration; inspect blueprint choice, slots, SQL date basis and same-scope truth. Local fixes/tests do not close production anchors. |
| A3/A6/A7, A4/A8–A10 | Lane5 + Ember | Evaluate useful clarification and content delivery, rather than requiring identical layouts or unrequested sections. Rendering-only clarification is now explicitly rejected. |
| A5, A13/A14 | Lane5 + Ember | Verify explicit population, requested scalar headlines and UI-owned paging counts. Do not compare unlike denominators. |
| A11/X1c | Ember | Implement formats, decimal-safe rendering, CSV controls, snapshot replacement/recreation and refresh error propagation. Run the acceptance matrix below. |
| I1/C2/C3/I5 | Ingestion + API | Provision real per-source receipts. Keep unknown until available; align access scope and source dates before comparing lanes. |
| I11 | Lane5/API/auth + Ember | Diagnose historical pause→resume 403 with safe rejection codes and correlation. Eight-hour default alone does not prove resolution. |
| I7/I6 | API + Lane5 | Current join probes route through business relations and fail closed when required verification fails. Verify Trino parity and audit remaining dialect constructs. A join check is not full independent answer verification. |
| H1/I3/I8/I9 | Deploy/API ops | Writable diagnostics location, routed latency, bounded catalog startup and robust environment configuration remain deployment work. |
| I2 | API | Scratch being unsupported under Trino is intentional in the reported code; snapshots use the export path. Verify deployment expectations if scratch is needed. |
| I4 | API/deploy | Confirm generic REST `/query` exposure and redirect behavior. Lane5's MCP/snapshot paths do not establish that endpoint is supported. |
| I10/H5 | CI/deploy | Verify deploy trigger and ticket-prefix policy. Pushing these GitHub branches is not a deployment. |
| I12 | Corpus/deploy | One hydrator pod was confirmed. Check deployed fingerprints and evidence of older/manual writers; do not assert concurrent contention from pod count alone. |
| H2/H3/H4/H6 | Harness/deploy | Update remote HTTPS/progress probes; use source `PYTHONPATH`; configure snapshot REST/storage; exclude battery traces from production samples. Remote scripts absent locally need separate edits. |
| I13 and final eval failures | Test owner | Reconcile stale evaluation metrics and scripted fixtures with the current runtime. See exact baseline comparison below. |
| Repeated prose/progress and extra discovery | Lane5 | Normalized assumptions are deduplicated, but paraphrases and repeated progress remain possible. Broader suppression/search optimization was deferred. |

### I11 verification details

API scope-token default is now 28,800 seconds (8 hours). Explicit deployment values override it. Lane5 refreshes near expiry with 30 seconds remaining; catalog SHA changes can invalidate a token before its time limit.

Preserve the original session ID through pause/resume and supply valid authorization for that session. Test ordinary resume, access-token renewal, a long pause and controlled rejection. Capture the full SSE outcome, not just the initial HTTP status. The UI must exit loading and show the terminal outcome.

Correlate UTC time, deployment version, request/trace IDs and safe MCP rejection codes. Distinguish expiry, catalog staleness, session binding, identity changes, missing/invalid scope tokens, replica signing-key mismatch and ingress/auth rejection. Ember must not mint or inspect backend scope tokens, change session IDs to bypass a rejection, or blindly retry every 403. Never share JWTs or authorization headers in reports.

## 11. Baseline verification before schema v2 (historical)

| Verification | Result |
|---|---|
| Final repository-wide Lane5 suite | 11,442 passed, 229 skipped, 23 failed |
| Clean baseline comparison | All 23 remaining failures reproduce with identical names on clean `30882b9`: 2 stale eval metric assertions and 21 legacy scripted mechanics checks |
| Formatting export follow-up | Two newly found field omissions fixed; 342 promotion/parity tests passed |
| Final API suite | 2,896 passed, 83 skipped, 1 xfailed |
| Corpus manifests | All 188 blueprint, 11 catalog and 11 knowledge entries verified |
| Final presentation live cases | Both passed; injected rendering question rejected, each delivered a table, no pause or salvage |
| Final presentation Phoenix audit | Two connected traces, one root and delivery each, no progress-summary spans |

Earlier targeted evidence includes 15 population cases with distinct Active/current/all fixture counts and scope-matched truth; identity-inference repair; missing-field/source cases preserving supported results; assumption replacement; payroll completeness correction; query reuse during presentation repair; and forced salvage. These batches overlap with broader trace audits and should not be added together as an independent production sample.

The four accepted assumption/completeness/salary follow-up cases matched scoped independent truth and delivered answers. One recovered through salvage after judge timeout. An intermediate judge had approved an injected freshness claim, motivating the later narrow guard. Final presentation tests used small fixtures, not large-volume load testing.

A live MCP UNION retained currency formats. Explicit CTE alias formats passed unit tests, but the local native ClickHouse parser rejected that CTE SQL syntax before execution; successful live execution of that syntax is not claimed.

Local SQL-source, saved-page, source-format rendering and proxy behavior has regression coverage. The fixture MCP had exports disabled, so a real S3 snapshot create/page/download/replacement cycle still needs deployment verification. Ember browser behavior, production tenants, Trino equivalence and historical resume-403 resolution are not established by these local runs.

Portable references are the pushed code/tests and this document. Detailed run artifacts remain on the originating machine under `/tmp/`; they are not automatically available to the recipient. Request redacted artifacts where needed rather than circulating raw credentials or unrestricted data dumps. Useful evidence groups: `final-push-lane-tests-fixed.log`, `final-push-baseline-eval.log`, `final-push-api-tests.log`, `presentation-final-live.json`, `presentation-final-phoenix-audit.json`, `three-repairs-accepted-live.json`, `three-repairs-truth.json`, and `three-repairs-phoenix-audit.json`.

## 12. Deployment and UI acceptance checklist

1. Adopt both branches with their answer-document dependencies. Confirm deployed SHAs and effective configuration; do not equate a push with deployment.
2. Rehydrate through the normal hydrator and verify the current fingerprints and blueprint formats. Do not copy older corpus hashes from interim reports.
3. Verify ZIPs/leading zeros, large identifiers, counts, negative money, nulls, decimal precision, both percentage scales, units, dates and timestamps. Check first response, saved reload, page 2, refresh, replacement and CSV.
4. Exercise a large record list. The agent should choose a table without rendering clarification or display-only limits; UI counts, paging and download should remain usable. Verify incomplete execution separately from a limited preview.
5. Exercise chart fallback, presentation repair, judge timeout, salvage, partial answers, missing fields, missing sources and an honest empty result. Confirm correct evidence reaches the terminal UI document and history.
6. Replace and clear recorded assumptions during repair. Confirm previous drafts are not resurrected, normalized duplicates appear once, and distinct caveats survive.
7. Check metadata-only freshness. No model freshness prose; no invented timestamp. Test live-only, ingested-unknown and mixed-source results, plus real receipts when provisioned.
8. Exercise snapshot refresh with `refresh_of`, revision conflicts for answer refresh, 409 replacement, reset-table failures, 404 recovery and download interruption. Confirm rows reset rather than append across replacements.
9. Repeat the reported payroll and expense questions against production truth under the same scope/date basis; repeat relevant cases with Trino enabled and capture verification coverage.
10. Exercise pause/resume and token renewal, trace connectivity and terminal delivery. Report exact remaining failures with safe correlation details, not a blanket “query executed” or “judge approved” pass.

Reference implementations: Lane5 `runtime/provenance/formats.py`, `runtime/composer/__init__.py`, `runtime/composite/record_assumptions.py`, `runtime/loop/answer_judge.py`, `runtime/loop/approved_answer.py`, `runtime/export_proxy.py`, `runtime/session_history.py`; API `app/result_formats.py`, `app/result_freshness.py`, `app/query_feedback.py`, `app/snapshot_idempotency.py`, and the authored corpus/catalog. Paths are repository-relative, with Lane5 runtime files under `src/data_agent/`.


## 13. Current local v2 follow-up

The schema-v2 and paging changes in this handoff are included in the accompanying change set. The API
working tree is clean: Lane5 orchestrates existing endpoints. The formatter now
handles generic integer identifiers without changing explicit count projections;
blueprints skip it. UI formatting retains fractional integer values and unspecified
precision. The reference adapter preserves freshness during paging, resets a
replacement once, offers explicit recreation for expired snapshots, and clears
unavailable rows on denial. It provides table/CSV options for facts, bars, lines
and tables, but not figures. No agent supporting/fallback flags remain.

Current verification is recorded at the end of
[the resolution log](issues-2026-10-30-resolution.md). The baseline numbers above
must not be presented as a production certification of the v2 deployment.

## Judge correction policy and conversational continuity (local follow-up)

The judge sends work back only for material correctness or coverage failures it
cannot fix from verified evidence: wrong SQL/measurement/population/date/join,
ambiguous result selection, missing requested evidence, or an essential unsupported
claim. Cosmetic layout, titles, repeated qualifications and process wording do not
justify an agent repair round.

Within its existing review call, the judge can approve a `corrected_answer`
(takeaway), `corrected_document` (the complete proposal, with optional corrected
assumptions), or `corrected_question` (necessary clarification and options). These
are internal review fields, not additions to the UI envelope. Corrections cannot
contain SQL, execute queries, invent results or select unprepared actions. Document
corrections use the normal materializer and authenticated result resolver. Invalid
source/column corrections require repair rather than shipping fabricated data.
The judge remains responsible for the meaning and requested coverage of its edit.

The saved terminal assistant message contains the actual delivered takeaway and
answer document. Subsequent model turns replay its sections and sources alongside
the takeaway, after the existing access-scope filter. Old finalize drafts remain
excluded from later-turn context. Clarification pauses persist the exact question
and options shown, including judge edits, so resume sees what the user answered.
No additional judge/formatter call or UI implementation change is required.
