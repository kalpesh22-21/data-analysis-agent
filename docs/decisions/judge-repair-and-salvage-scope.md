# Judge repair, partial recovery, and salvage

Status: Guard, partial recovery, salvage, and delivery tracing implemented and tested.
Date: 2026-10-23

Implementation was authorized on 2026-10-23. Sections 1–4 and 6 are implemented.
Section 5 has concise prompt guidance; runtime gap tracking remains separate work.
Exploration budgets are unchanged.

Implementation details: salvage uses the existing judge client with thinking disabled,
no transport retries, and the judge's configured per-call timeout. A relevance check
has its own bounded call; the overall recovery deadline permits both calls. Normal
review time does not gate either. Presentation repairs use salvage to select existing
executions and repair captions; neither path changes SQL. Numeric wording corrections
may remove repeated mentions but may not add, alter, or remove a distinct numeric value.
Failed salvage delivers a limitation and never releases an unapproved candidate.
The result-emitted event confirms server stream emission, not browser rendering.

## 1. Judge owns wording and presentation repairs

- Wording guards pass findings to the judge as signals. They must not send the
  agent another nudge to repair prose.
- The judge can shorten prose, remove unsupported explanations, qualify scope,
  and repair captions when the underlying measurement answers the request.
- The judge can select a different already-executed result when it clearly
  answers the request, then validate the complete response against that selection.
- Permit deletion of repeated numeric mentions. Do not permit introduction or
  alteration of numeric claims through the wording-correction path.
- Revalidate the complete corrected response and selected components before delivery.
- A caption change cannot conceal an incorrect measurement. If the user asks
  for leave taken, relabeling approved request hours does not answer the request.
- Distinguish caption changes from SQL result-column alias changes: changing
  executed SQL may require an agent/query repair. Do not demand that repair
  while simultaneously forbidding the necessary query.

## 2. Agent repairs analysis

Wrong calculations, filters, ranking SQL, or missing evidence return to the agent
with actionable feedback. The normal path retains one agent repair attempt and
two judge reviews, subject to the existing execution and review budgets.

Saving an approved partial does not end the repair opportunity. Continue repair
while its allowance and budgets remain. The saved partial is a fallback, not an
early exit. Remaining review time alone does not grant additional repair rounds.

## 3. Retain approved partials

Save the exact approved text, selected tables/cards, supporting result IDs,
all evidence IDs visible at approval, and access-scope fingerprint. Evidence
receipts must be immutable or compared using content fingerprints.

Do not clear candidate components before partial recovery. Deliver only the
validated, approved subset afterward. Do not silently discard an otherwise
supported partial for an editorial issue and misreport it as insufficient evidence.

At fallback time, after normal repair cannot continue:

1. If the access scope changed, invalidate the saved approval deterministically.
2. If no new evidence or relevant request clarification exists, reuse the exact
   approved partial without another call.
3. If new evidence exists, retain the snapshot and run a small non-reasoning
   relevance check using the approved partial, its supporting evidence, new
   evidence, and relevant request context.
4. If the evidence is unrelated, reuse the approval unchanged.
5. If it potentially affects the partial, or relevance is unclear, require judge
   revalidation through the salvage path. Checker failure or timeout must not
   be interpreted as unrelated evidence.

The relevance checker does not rewrite answers or approve new claims. Run it
only when considering reuse, not after every tool call. Revised prose is not new
evidence; new user clarification can change the requested scope and must be
considered before reuse.

## 4. One independent salvage judge call

When normal review cannot produce a deliverable answer, first consider a valid
saved partial. Otherwise use one non-reasoning salvage judge call.

- Give salvage its own timeout, independent of the normal review budget.
- Remove the fixed requirement for 30 seconds of remaining normal review budget.
  Exhaustion of that budget must not prevent salvage from starting.
- Supply the latest draft, earlier rejection, available evidence, and candidate
  components. This covers a corrected agent answer whose re-review timed out.
- Salvage cannot execute queries or restart the agent.
- Use an explicit salvage mode and structured response contract with three
  outcomes: full answer, supported partial, or no supported answer.
- Return approved text and selected results, plus concise reasons for any
  unsupported requested parts. Distinguish unavailable information, execution
  failure, and unresolved ambiguity when evidence establishes the reason.
  Otherwise say that the part could not be verified; do not invent a cause.
- Validate the returned response and send its approved content to the UI.
- If salvage fails, times out, or returns invalid content, send a clear fallback
  limitation rather than starting another recovery loop.

The existing partial-answer fields already express findings and unfinished
parts. The salvage contract should avoid treating an independently approved
salvage response as merely `approved=false` for the rejected original answer.
Exact schema and timeout configuration remain implementation details to settle.

## 5. Bound unproductive exploration

Track unresolved information gaps across tools, including exploratory runQuery
calls. Distinguish discovery, answer execution, and specific execution/analysis
repairs. Do not impose a blanket two-tool-call limit or classify SQL purpose
solely from its syntax.

Repeated attempts without progress should produce guidance along these lines:

> You haven't resolved this information gap yet. Continue if the evidence points
> to a concrete next step likely to resolve it. Otherwise, ask a focused
> clarification if the user can help, or explain the limitation and deliver what
> is supported. Avoid repeating searches or speculative queries without new evidence.

Exploration should remain possible when a concrete lead appears. Ask the user
only when clarification can unlock useful work. Do not turn absent evidence into
a claim of zero or nonexistence. Align agent prompts, tool guidance, and judge
expectations so justified limitations and clarifications are accepted.

## 6. Observability

Record these stages separately:

- Judge approved a full response or partial.
- Validation passed or failed, with the precise reason.
- Response persisted.
- Final result emitted to the UI stream.

Replace opaque partial-validation outcomes such as `no_valid_partial` with
specific diagnostics. Emission confirms backend transmission, not browser rendering.

## Delivery paths

1. Agent submits an answer; structural/evidence checks run and editorial findings
   are supplied to the judge.
2. Judge approves, directly repairs wording/presentation, or requests analysis repair.
3. If requested and available, the agent repairs once and normal review runs again.
   An independently approved partial may be retained during this work.
4. If normal delivery cannot finish, reuse a still-valid approved partial or run
   the independent salvage call.
5. Validate and deliver the resulting full answer, partial, or limitation.

## Validation scope

Cover direct wording/caption repairs, numeric repetition removal, existing-result
selection, genuine analysis repairs, saved partials surviving unrelated evidence,
changed scope and potentially contradictory evidence, relevance-check failures,
salvage after exhausted budgets/timeouts, retained tables, oversized partials,
bounded exploration, and specific delivery-stage diagnostics.

Previously reproduced failures to cover explicitly:

- Tables cleared before terminal partial recovery cause a table-bearing partial
  to fail validation and lose its text as well.
- After an oversized-answer nudge, a judge-approved partial can fail the runtime
  verbosity guard and be replaced with an evidence-related decline.

## Out of scope

Persistence-failure recovery and client-disconnect/reconnect recovery are deferred.
Their delivery-stage tracing is implemented. Commit and push remain out of scope.
