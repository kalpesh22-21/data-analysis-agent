# Runtime harness implementation and validation

Date: 2026-09-13
Base revision: `4abf161`
Scope: [accepted decisions D1–D15](runtime-harness-improvements-agreed.md).

## Implementation

| Decision | Implementation | Main regression coverage |
| --- | --- | --- |
| D1 | Provider-neutral `use_reasoning_metadata=false` setting; original response batches, explicit unexecuted-call results, malformed-argument errors; scope-aware replay. The compatible chat adapter retains `reasoning_content`, `reasoning`, and `reasoning_details` when enabled. | Model adapter suite; complete/partial/narrowed batch replay; malformed arguments and paused batches. |
| D2 | Successful tool-call IDs are execution result IDs. Table references resolve to that execution's SQL and bound slots. Blueprint IDs remain lineage. The UI receives the existing paginated SQL contract. | Two executions of one blueprint; distinct department queries; top-N preservation; live pagination and history. |
| D3 | Pre-execution measurement contract review plus scoped cardinality probes for duplicate-sensitive aggregate joins, including blueprint DAG nodes. Verification labels say structural checks passed. | Fan-out refusal before execution; both join directions; independent join-key sets; semijoin repair; DAG execution suites. |
| D4 | Capability calls prepare options without terminating. `finalizeAnswer` explicitly selects prose, tables, capability references, and evidence. | Preparation stays hidden on pause; explicit selection; missing references; mixed live answers. |
| D5 | Main prompt follows understand → source → fit → execute → deliver. Tool schemas and repair instructions use the same finalizer and result references. | Procedure/schema contract tests; live source-routing scenarios. |
| D6 | Raw SQL requires discovery delivered in a previous model request or applicable prefetch. Proposing search in the same batch never qualifies. Received discovery-unavailable results permit fallback. | SQL-first/search-second; repeated SQL without discovery; unwired and failed discovery. |
| D7 | `serves_intents` binds one execution to multiple declared outcomes. | Explicit multi-intent tags and shared results. |
| D8 | Late initial declaration is accepted; results bind explicitly with `result_id`; descriptions and the intent list remain frozen. Automatic guessing is removed. | Late declaration; explicit reuse; current-turn and permission checks; intent adversarial suites. |
| D9 | Judge input contains per-deliverable evidence, measurement contracts, complete UI selections, and clarification answers. Output includes `intent_id`, `result_ids`, and `repair_type`. | Structured verdict validation; wrong evidence type; prose-only repair retains the query. |
| D10 | Persisted review state permits initial review and one final validation after one repair. Unresolved rejection never becomes approval merely because review is unavailable. | Approval/rejection/failure paths; exact review counts; actual pause/resume. |
| D11 | Final review runs after the entire batch. Review identity includes prose, assumptions, tables and selected options. An explicit empty table selection clears old tables in live output, history, and resume reconstruction. | Assumptions after finalizer; multiple proposals in one batch; removing a table during repair. |
| D12 | Direct and runtime-tool questions share business-language/choice normalization. More than five choices requests narrowing instead of truncating. Blueprint checkpoints preserve internal bindings. | Runtime pauses; excess choices; HTTP clarification/resume; duplicate resume rejection. |
| D13 | Runtime checks current, accessible, successful evidence and its type. The judge evaluates semantic support for each deliverable. Search snippets do not substitute for full Help Center content. | Warehouse evidence cannot be product evidence; emulated catalog evidence; missing Help Center results. |
| D14 | Session stores persist actual violation, feedback, result IDs, answer identity, assumptions and review consumption. Scope changes invalidate approval and remove sensitive repair context. | In-memory tests and a real Couchbase fresh-store round trip. |
| D15 | A fresh request registry reloads previously loaded capability definitions under the current JWT, registers available definitions, and reports unavailable ones. Restoration does not prepare or activate an option. | HTTP resume with definition available/unavailable; hydration only on explicit preparation. |

The public schema advertises only `finalizeAnswer`. Legacy answer-tool handlers remain for existing internal callers and historical records. New scripted tests use the unified contract; older contract tests were migrated where these decisions deliberately changed behavior.

## Additional issues found during implementation review

- A single execution's measurement reviewer incorrectly demanded coverage of the entire multi-part question. Review now assesses that execution; final review checks combined coverage.
- Original response replay could duplicate a batch when its first result was withheld. Reconstruction now groups it once and removes opaque reasoning/prose when the scope or complete batch no longer matches.
- Clarification answers were absent from judge context, and replay could encourage repeating a paused call. Both final and clarification review receive the user's answers; resumed model requests explicitly apply them to the original ask.
- Open-ended catalog and missing-feature questions could exhaust the loop on unrelated discovery. The procedure now gives stopping criteria and a short reminder after six consecutive discovery rounds. This is guidance, not a new permission gate.
- A repaired scalar answer could retain a table from its rejected draft. Explicit selection now replaces the complete table set, including an empty set, consistently across live output and history.
- Finalization refactoring initially lost scrub/refusal/enforcement telemetry and the stricter existing requirement for actual judge approval of data-display options. Those paths are restored and tested.
- Exceptions while assembling the initial judge brief now retain the existing fail-open behavior. A prior unresolved rejection still remains active if final validation fails.

## Automated validation

Commands use `.venv/bin/python -m pytest -q -o addopts=''`.

- Focused implementation/contract suites: **571 passed**. Includes **41 interaction tests** in `tests/runtime/test_harness_improvements.py`, model adapters, tool schemas, capabilities, aggregate/DAG execution, intent binding/adversarial cases, judge validation, verification roll-up, and all **87 remote-runtime contract tests**.
- Real Couchbase tests: **4 passed**, including fresh-store review-state restoration and exactly-once checkpoint consumption.
- Full runtime suite: **3,422 passed, 286 failed, 5 skipped**. The unchanged base revision, tested separately from an archived checkout, had **290 failures**. Every remaining failing test ID also failed on that baseline; four baseline failures now pass. There are **no newly failing test IDs** after contract migration. The repository-wide suite is therefore still not green.
- Ruff and `git diff --check` cover modified Python files and the patch.

Detailed local logs: `/tmp/harness-focused-final-tests.log`, `/tmp/harness-full-final-tests.log`, and `/tmp/runtime-baseline-tests.log`. These are local validation artifacts, not committed fixtures.

## Backend and live questions

Backend: `http://127.0.0.1:18104`, running `scripts.run_ui_runtime_real:app` with real JWT verification, retrieval, local warehouse services, Help Center and capability API fixtures, measurement review, and final answer review enabled. The session store for this live matrix is in memory; persistence was tested separately against Couchbase.

The live model used was `gpt-4.1`, using the available configured credentials. No Kimi endpoint was available for validation. Production behavior and the reasoning flag are not keyed to a model/provider name.

The reusable probe is `scripts/probe_harness_improvements.py`:

```sh
PYTHONPATH=src:scripts .venv/bin/python scripts/probe_harness_improvements.py \
  --base-url http://127.0.0.1:18104 \
  --output /tmp/harness-live-results.json
```

| Question family | Additional checks |
| --- | --- |
| Active headcount by department | Successful completion, paginated table, matching history |
| Sales and Engineering as separate tables | Two distinct SQL queries, pagination of both, narrower-scope denial and hidden history tables |
| Top three salaries | Ranking/table path; exact top-N query preservation also covered deterministically |
| Nonexistent/ambiguous department | Honest completion or business clarification |
| Empty future-date result | Completed empty-result answer |
| Available employee information | Business-description catalog answer without exhaustive schema exploration |
| Request time off | Help Center full-document path and applicable navigation |
| Nonexistent product feature | Unsupported-information handling without invented instructions |
| Position Management navigation | Definition loading, nonterminal preparation, explicit selection |
| Headcount plus time-off instructions | Separate warehouse and Help Center evidence in a combined answer |
| Salary and SSN request | Scoped data access or clarification; no access expansion |
| Off-topic weather request | Scope response |
| Explicit department choice, then Sales | Pause, resume, completion, history and pagination; a second resume returns conflict |

All 13 families passed both the complete run recorded in `/tmp/harness-scenario-matrix-v13.json` and the final-process rerun recorded in `/tmp/harness-scenario-matrix-final.json`. Raw answers are kept in private local files, not added to the repository. Live completion checks are supplemented by deterministic failure injection; natural-language questions alone cannot reliably force every hook.

## Limits

- These checks cover the agreed behavior and identified interactions, not every possible model output or SQL shape.
- Join probes are conservative and evaluate current warehouse data. They can reject valid complex joins that need a clearer grain-preserving rewrite. They do not create a frozen snapshot, and paginated reads can observe later data changes.
- An initial unavailable/disabled semantic reviewer cannot prove business correctness. Structural checks and typed evidence eligibility are not described as semantic approval. An unresolved final-review rejection remains guarded.
- Reasoning metadata is internal and permission-filtered, but enabling the flag still requires validating the chosen endpoint's compatible chat fields. Native reasoning formats outside the supported metadata fields are not inferred.
- Existing baseline failures remain. They must be resolved separately before requiring a green full runtime suite as a release gate.
- No deployment or commit was performed. Help Center sectioning, query-refresh tools, dormant answer-table hooks, and blueprint-version binding were not introduced.
