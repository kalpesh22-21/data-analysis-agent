# Warehouse validation and judge audit — 2026-09-20

Tested `feature/multi-capability-agent` at `49ad7b2` with the working-tree B8,
routing, and outer-join safety changes. This is a validation report, not a claim
that those uncommitted changes or the updated corpus are deployed.

## Results and tracing

All seven live scenarios completed, received an affirmative final judge verdict,
and produced the expected source combination. All four warehouse answers matched
the independently verified, caller-scoped reference results. Every multipart
scenario declared and closed its deliverables. The scope refusal emitted exactly
one attributed event and did not prevent completing the supported part.

| Scenario | Outcome | Model completions | Trace ID |
|---|---|---:|---|
| Navigation only | Completed | 4 | `a5132115c886f2b9df37ddf45108f551` |
| Help Center only | Completed | 4 | `e06e91d0acfaae07f79ed560c30d69fb` |
| Warehouse only | Completed; blueprint false rejection repaired with SQL | 10 | `ce2a7a7ea52308cf8b0c4e19d7be94ad` |
| Warehouse + navigation | Completed | 7 | `12856d5323f193ecf1310d78236b1726` |
| Help Center + warehouse | Completed | 6 | `60e507b65b21358c08cec4603739d281` |
| Scope refusal + warehouse | Completed; blueprint false rejection repaired with SQL | 10 | `263987ea2c784212191b8254ff5dbf9e` |
| Help Center + action | Completed | 5 | `1cd837333fd98b091d714238ba1859d5` |

Phoenix exported 46 model completion spans and 295 runtime spans across these
seven turns. Each trace has one completed `agent.turn`, with no disconnected
spans. Span filtering and content redaction were disabled for this local test.
The matrix verified complete model inputs/outputs, tool spans, final-judge spans,
retrieval/embedding/reranking spans on routes that invoke retrieval, and named
measurement-review spans added by the test harness. External service internals
that do not emit spans are outside this assertion.

Blueprint retrieval was enabled and returned candidates in every warehouse case.
The updated headcount blueprint was seeded into the local Neo4j retrieval index;
this test does not deploy the sibling MCP container's corpus export. Direct
navigation/action routes may skip warehouse retrieval by design.

The runtime suite passed **3,881 tests, 5 skipped**. The new outer-join suite has
18 cases; the blueprint also passed read-only ClickHouse checks for an empty
population, partially active departments, duplicate employees, and distinct
codes sharing the same department name. The fixture mirror and its grain
expectation now match the updated canonical blueprint.

## How the judges work

The measurement reviewer runs before warehouse execution. It checks one requested
part against proposed SQL or blueprint arguments, recent catalog receipts, and
its assigned deliverables. Typed findings determine approval. Request alignment
alone is not approval: a matching request can still have blocking findings.
Structural SQL and grain checks are separate deterministic checks.

The final answer judge reviews the assembled answer, selected tables/options,
original question and clarifications, assumptions, ledger, scope-filtered result
previews, full fetched Help Center content when retained, capability definitions,
and measurement reviews. It can request bounded presentation or analysis repairs.
The passing routing checks above are not a general accuracy score.

## 1. Improve the evidence supplied to the judge

### Review resolved SQL, not an unbound template

In `ce2a7a7ea52308cf8b0c4e19d7be94ad`, the measurement reviewer rejected the
blueprint **before the executor ran**. It treated an omitted optional department
slot as an invalid department restriction, despite the receipt explicitly saying
omission means all values. The compiler correctly replaces the entire optional
predicate with boolean true; it does not compare a department name to a boolean.
The successful blueprint execution in `12856d5323f193ecf1310d78236b1726` confirms
the correct compiled behavior. This is a pre-execution review false rejection,
not evidence of a compiler or warehouse failure.

The same false rejection recurred in the scope-plus-warehouse trace. Both turns
recovered using equivalent raw SQL after unnecessary discovery and another review.
Two other blueprint executions were accepted with the same omission semantics.

A diagnostic replay kept the original warehouse-only question, scope and catalog
receipts, adding the actual resolved SQL and explicit omitted-filter metadata.
It received an affirmative, reviewed verdict in **14.344 seconds**, versus the
original **48.906-second rejection**. No runtime prompt or judge implementation
was changed for this replay. Its three spans are connected and its completion
is exported: `bb88e94cbeae144836d741806532f8ce`. One successful replay is useful
evidence for the input design, not statistical proof of improved accuracy.

Recommended change: build measurement evidence after slot resolution, using the
same compiler/executor plan rather than duplicating binding logic in the loop.
Include relevant rule predicates, column types, omitted filters, and bound values.
For composed blueprints, represent node dependencies without inventing unresolved
intermediate values. Current `catalog_evidence` keeps the last 12 successful
schema/blueprint/knowledge receipts rather than selecting execution dependencies.

### Replace the global numeric corroboration flag

`AgentLoop._corroborated_figures` searches serialized result JSON for any reported
number as a substring. Its one boolean does not identify the claim, metric,
department, period, or matched cell. Three local synthetic probes returned true
for an identifier-only substring match, a match for only one of two reported
figures, and a value assigned to the wrong department. This demonstrates weak
evidence supplied to the judge; it does not establish that the final judge would
accept all three answers when the relevant rows are visible.

Recommended change: attach claim-specific result IDs, grouping keys, metric
columns, values and match types. Preserve 'not checked' when evidence is missing;
a truncated preview is not proof that a claim is false. Link measurement contracts
explicitly to their execution/result IDs as well.

### Supply coverage and relevant supporting material

The mixed-case briefs correctly included completed ledgers, evidence bindings,
full short mock articles and selected UI definitions. No evidence truncation was
observed in these cases. Large real articles/results still need separate tests.
The current trimmer protects question/draft/ledger and prioritizes designated
results, but can still remove the rows required to verify a claim.

Some organization-wide warehouse prose omitted the accessible-records
qualification, and the final judge approved it. Provide truthful population
coverage metadata and test that consequential access limits are explained in
business terms. Do not expose policy internals or infer organization-wide coverage
merely because a scoped query succeeded.

## 2. Improve accuracy and review availability

- Keep deterministic checks for known SQL failure classes. The new guard rejects
  unconditional counts on the unmatched side of outer joins; DISTINCT does not
  prevent counting a default-filled nonexistent entity. It is deliberately
  conservative, not a proof of arbitrary predicates or all nested SQL lineage.
- Run cheap structural checks before spending a measurement model call where
  possible. The current raw-query order performs model review before cardinality
  validation.
- Evaluate contrast pairs: valid/invalid optional filtering, safe/unsafe outer
  joins, correct/wrong status codes, swapped group values, empty/zero populations,
  supported/unsupported UI claims, and complete/missing deliverables. Measure
  false accepts, false rejects and unavailable reviews separately.
- Preserve the difference between execution success and semantic correctness.
  A structurally verified blueprint is not proof that it answers the user's metric.
- Improve measurement-review observability: distinguish timeout, provider failure
  and malformed output, and wire its named span in production. The final judge
  already has a named span and failure events.

Measurement calls ranged from **6.999 to 48.906 seconds**; **4 of 6 exceeded the
normal 20-second measurement timeout**. Final-judge calls ranged from **3.476 to
9.473 seconds**. Both reviewers can fail open with approved=true, reviewed=false;
that is unavailable review, not a positive quality judgement. Consider a
configurable measurement timeout and a measured reviewer model/reasoning budget,
then validate at production limits. Increasing timeout alone does not correct
false rejections.

## Test conditions and limits

- Live Kimi 2.7 Code and local read-only MCP/ClickHouse; live local embedding,
  Neo4j and reranking; repository mock HTTP Help Center and capability services.
- Test-only 35-second request pacing, 90-second review limits and a 900-second
  turn budget. No pacing was added to production. Per-model timings exclude
  test queue waits; the finalizer's outer deadline still includes those waits.
- Six measurement reviews and seven final reviews returned usable verdicts;
  there were no reviewer-unavailable outcomes or identical repeated tool requests.
- A temporary report collector used obsolete intent attributes after mixed turns
  had finished. Ledger results were recovered from the actual persisted
  updateAnalysisState receipts and final judge briefs. Original collector errors
  remain in the local raw artifact; they were not runtime failures. The temporary
  collector has been corrected for future runs.
- Local access mappings were restored for the test principal. Their one-day TTL
  and disabled refresh remain configuration work; this was not a permanent fix
  for access-map maintenance.
- No keys, message payloads, SQL, or employee values are stored in this report.
  Detailed inputs, results and diagnostic scripts remain outside Git under
  `/private/tmp/judged-route-matrix-*` and related local diagnostic files.

Code pointers: `loop/measurement.py`, `loop/agent_loop.py` (`_judge_results`,
`_judge_brief`, `_corroborated_figures`), and `loop/answer_judge.py` (`JudgeBrief`,
`_fit_payload`, `AnswerJudge`).
