# Kimi conversation evaluation — 2026-09-25

The conversation retained follow-up context and completed a real clarification/resume,
but delivery was not consistently reliable. A supported May ranking was withheld after
a wording rejection because the corrected answer could not obtain a verdict within the
remaining review time. Several delivered answers also overstated what empty or stale
accessible data establishes.

Session: `kimi-conversation-7620131ebd96`. Five top-level questions plus one clarification
answer, all in the same session, through the real `/turn` and `/turn/resume` SSE endpoints.
Kimi model: `kimi-k2.7-code`; capabilities disabled; judge, retrieval, and progress summaries
enabled. Real MCP and Couchbase were used. The backend was restarted with the current local
changes; no new runtime implementation changes were made during this evaluation.

| Input | Seconds | Observed result | Review |
|---|---:|---|---|
| How many employees do we have? | 85.67 | Returned an active-headcount table with zeros. Interpreted employees as active employees. | Exhausted; no verdict |
| Who has been here longest? | 205.21 | Queried tenure and confirmed no accessible employee records; did not invent a person. | Exhausted; no verdict |
| Who has taken most leave this month? | 297.60 | Asked which types of leave to include, with five business-language options. | Approved question |
| All types of time off, ranked by total approved hours. | 248.41 | Resumed without repeating the clarification; queried the current month and disclosed that the latest record was July 3. | Approved answer |
| What about May? | 212.42 | Preserved the leave metric and changed the period; obtained a two-row ranking, but ultimately delivered only a generic refusal. | Rejected |
| What different categories do we have? | 232.58 | After an unnecessary clarification was rejected, interpreted categories as leave types and delivered five categories with a table. | Exhausted after an upstream failure |

The timings include evaluation pacing and are not production latency measurements.
Progress streamed throughout; final results followed the progress events. The live resume
kept the original turn and clarification together in session history.

## Independent data checks

Queries under the same scope and tenant returned no employee rows. The time-off records
range from March 11 through July 3, 2026, with no approved requests for September. May has
four approved requests totaling 32 hours. Re-executing the agent's May query independently
returned the same two-row ranking (24 and 8 hours). Re-executing its category query returned
five categories. These checks establish accessible-data results, not complete organizational
coverage. No data was seeded or changed to improve the evaluation.

## Findings

1. **Review time can destroy a supported conversational answer.** The first May judge call
   took approximately 23.09 seconds and rejected the claim that blank employee names proved
   the employee master table contained no records. It requested a prose repair: say names
   were unavailable or blank. Kimi made that correction and kept the same supported ranking.
   Only approximately seven seconds remained in the shared 30-second request-local review
   budget. The correction therefore received no usable verdict before the runtime deadline;
   the earlier explicit rejection remained binding and all answer components were withheld.
   The proxy eventually completed that second judge request after 30.88 seconds, after the
   runtime had stopped waiting. Its late verdict was not captured and is not claimed to be
   approval. The configured per-call judge timeout is 60 seconds, but the independent
   30-second aggregate limit takes precedence. Preserving explicit rejections is correct;
   budgeting the repair validation is the problem to address.

2. **Empty/stale data needs better wording.** The headcount answer said zero active employees
   “across the organization.” The September answer led with “No employees have approved time
   off this month,” then disclosed the July data cutoff. Both should lead with accessible
   data and coverage limitations rather than imply complete, current organizational facts.

3. **Follow-up understanding worked, but clarification discipline is uneven.** “What about
   May?” reused all leave types and approved hours without asking again or rediscovering the
   schema. “Categories” was ultimately interpreted correctly as time-off categories, with
   that assumption disclosed. However, Kimi initially asked “What kinds of categories are you
   asking about?” despite the preceding leave conversation; the judge rejected that question.
   That extra review consumed time otherwise available for the final answer.

4. **Some measurement labels remain imprecise.** The leave SQL labels `count(*)` as approved
   days even though it counts request rows and does not deduplicate dates. The categories
   answer calls categories “most frequently used,” while ordering all request statuses by
   request count. Prefer “approved requests” and “most frequently requested” unless actual
   distinct days or approved usage is measured.

5. **There are avoidable rounds.** The longest-tenure and resumed leave turns each emitted
   bare prose before using `finalizeAnswer`. The initial leave turn performed multiple
   discovery calls. One leave query also hit `AGGREGATION_RISK` on an outer-join aggregate;
   Kimi recovered by aggregating leave rows before joining employee details. This rejection
   did not cause the final delivery failure.

## Pacing and environment limitations

The configured account is paced at three requests per minute. Initially the proxy waited
21 seconds before forwarding each request. A roughly 14-second judge call therefore exceeded
the runtime's 30-second budget once queue waiting was included; this accounts for the first
two exhausted reviews. After those turns, the temporary evaluation proxy delayed release of
non-judge responses until the next request could start. It retained the same endpoint and
rate limit, without changing runtime budgets or review decisions. The clarification and
resumed September answer then received genuine approvals. The May repair still exhausted
the aggregate budget despite that adjustment. The categories judge attempt encountered a
proxy-recorded upstream connection failure (HTTP 503 after 10 seconds) and the answer shipped
under the configured no-verdict fail-open policy, not as approved.

Progress-summary tasks often hit their five-second deadline in this paced environment and
used fallback text. Their latency is also not representative of an unconstrained endpoint.

The backend remains serving on port 8000 (PID 94879), using the temporary paced proxy on
18007 (PID 95451). `/ready` returns 503: the retrieval graph readiness gate remains false,
although direct conversation endpoints and retrieval calls worked. This is a local evaluation,
not a deployment-readiness signoff.

## Suggested next changes

Make the aggregate review-time budget explicit and configurable, and reserve enough time
for validation after an allowed repair. Account for provider queue delays during evaluation;
do not bypass explicit rejections. Tighten wording around data coverage and distinguish
requests, approved hours, and distinct days. Strengthen use of recent conversation context
before asking another clarification, and reduce bare-prose completion attempts.

Private local artifacts: `/tmp/kimi-conversation-live/conversation.json` (SSE and history),
`session.json` (persisted tool trail/review state), and `oracle.json` (independent queries).
Model-call status/timing log: `/tmp/kimi-paced-model-calls.jsonl`. Backend log:
`/tmp/kimi-conversation-backend.log`. Temporary pacing harness:
`/tmp/kimi_conversation_paced_proxy.py`. These files may contain authorized fixture data;
the report omits employee identities and row payloads.

## Review-budget follow-up

The runtime now accepts `ANSWER_JUDGE_REVIEW_BUDGET_SECONDS` as a positive, finite
aggregate judge-call budget for each `/turn` or `/turn/resume` request. When unset,
it derives the total from three times `ANSWER_JUDGE_TIMEOUT_SECONDS`: time for a
clarification review, an answer proposal, and repair validation. With the evaluation's
60-second per-call timeout this provides 180 seconds total instead of the hidden
30-second limit; with the default 30-second timeout it provides 90 seconds.

One per-call timeout (capped at half the configured total) is reserved exclusively
for validation of an allowed answer repair. Ordinary clarification, proposal, and
terminal-delivery reviews cannot spend that reserve. Each call still obeys its
per-call timeout and remaining aggregate allowance; elapsed time includes provider
queueing and failed calls. Agent generation remains subject to the existing execution
budget, so this does not guarantee completion under arbitrary provider delays.

Explicit rejections remain binding until a corrected proposal receives a reviewed
approval. Durable review-call limits, repair limits, and no-verdict policy are unchanged.
The subsequent judge-owned wording correction is described below.

Deterministic regressions reproduce a 23.09-second rejection followed by a
30.88-second approval, test reserve isolation and configured limits, and retain the
failure case where repair validation times out. These use a simulated review clock;
no new live Kimi evaluation was performed for this change.

Validation: runtime and UI suites passed with **4,355 passed, 5 skipped**. Ruff
and whitespace checks passed.


## Judge-owned wording corrections

For a complete `finalizeAnswer` proposal that passes the existing evidence and shape
checks, the judge may now return `approved=true`, `repair_type=prose`, and a complete
`corrected_answer`, with empty violation and feedback. The correction is the exact
text approved for delivery; the runtime preserves table selections, evidence,
assumptions, and prepared UI components. No additional agent or judge round is needed.

The judge is instructed to make minimal prose changes only: remove unsupported
explanations, qualify data coverage, or align wording with an already correct metric.
Changes to calculations, requested metric semantics, SQL, evidence, assumptions, or
component selection still require a rejection and agent repair. Semantic assessment
remains the judge's responsibility; deterministic checks cannot prove prose equivalence.

Runtime guards reject changed numeric tokens, unusable or oversized replacement text,
and replacements that need scrubbing or violate the existing prose rules. Malformed
model output remains unreviewed and cannot clear an explicit rejection. Corrections
are disabled for clarification and terminal fallback reviews. Both original and
corrected answers and their proposal fingerprints are retained in the review state;
the final approval receipt is bound to the corrected proposal and delivery. Correction
text is cleared when restoring review state under a changed scope. Telemetry records
only the correction event and site, not the answer text.

The regression covers the May failure pattern: unsupported explanation for blank
names, retained ranking with values 24 and 8, exact corrected prose, one judge call,
and no extra agent call. Additional tests cover numeric changes, malformed responses,
prior rejections, substantive repair routing, scope changes, and fallback exclusions.
This is deterministic coverage; the live Kimi session has not been rerun.

Validation after wording corrections: **4,379 passed, 5 skipped** across runtime and UI
suites. Ruff and whitespace checks passed.
