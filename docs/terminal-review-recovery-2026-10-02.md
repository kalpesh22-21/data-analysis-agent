# Terminal review timeout and recovery

Implemented locally following the October 1 report for turns 18–19 and the October 2 decisions.

## Delivery policy

An unavailable proposal review persists the exact delivery fingerprint with `delivery_status: exhausted` and an unavailable/timeout reason. It is not an approval. Finish reuses that disposition for unchanged text and selected components only when there is no outstanding substantive rejection. Scope changes invalidate the receipt; changed deliveries remain eligible for review using the remaining terminal budget.

Finish makes at most one judge call for a delivery that still needs review. A timeout does not trigger another identical call. Existing rejection/repair reserves remain available for changed proposals and valid repair work; unused budget is not a reason to repeat an exhausted review.

## Repair at finish

The terminal brief permits prose correction and partial-answer proposals for answers, not pending clarification questions.

- A judge-approved `corrected_answer` must pass the existing correction validator, including numeric preservation and prose checks. The exact corrected text receives the receipt and is used for both persistence and the result frame.
- A rejection can carry a separately approved partial. Capture it with the existing evidence, component, and provenance validation before persisting the rejection.
- After finish-time rejection, restore a valid cached partial without another judge or agent round. Cached evidence must still match the current evidence version and access scope.
- An invalid correction, missing evidence, or invalid partial does not become approval. Existing withholding behavior remains when no supported delivery is available.

An earlier substantive rejection remains binding across a subsequent timeout. The runtime never treats the unreviewed `APPROVED` control-flow sentinel as an actual judge approval.

## Observability and wire behavior

Recovery admission skips report `loop_partial_answer_failed` reasons, including insufficient review budget, no evidence, previous attempt, unavailable review, or absent valid partial. The 30-second admission floor for a **new** recovery review remains unchanged; it does not prevent restoring an already-approved partial.

`loop_partial_answer_recovered` exports evidence, table and card counts plus text length. These shape-only fields are included in the tracing allowlist; answer prose and evidence identifiers remain excluded.

The runtime result field is `assistant_text`, not `text`. Regression tests exercise the actual SSE formatter and history projection, asserting the same corrected/partial text and selected tables on both paths. This validates the local runtime contract; it does not establish what the remote BFF/browser received in the reported incidents. Raw deployed SSE and history records are still needed to explain that blank UI.

## Validation and scope

Tests cover real timeout cancellation without duplicate review, exhausted receipt persistence, changed-delivery review budgets, outstanding rejection preservation, validated versus invalid corrections, partial answers with and without tables, missing evidence, SSE/history parity, and exported recovery attributes.

The judge-fix full run passed 8,631 tests with 229 skipped and the same 22 previously established baseline evaluation failures. Follow-up progress changes are covered by the affected loop/observability/harness suite.

No changes to the remote corpus, hydrator publication policy, API schema/scope behavior, or episodic-memory implementation are included.

## Progress follow-up

Default progress labels and static summary fallbacks start with a capital letter. Authored summaries remain verbatim. `updateAnalysisState` dispatch and summary events are suppressed from UI progress, and the loop does not schedule its progress summarizer. Tool execution and internal tracing remain unchanged.
