# Execution-scoped measurement review

The P07 salary query was rejected because it omitted SSN, even though a separate capability handled identifiers. Measurement review now receives the original request alongside explicit execution scope instead of receiving either an isolated intent description or the full question as interchangeable inputs.

## Behavior

- Scope includes existing `serves_intent` / `serves_intents` bindings, other declared deliverables, and successful capability receipts already available to the model in the current turn. Failed, previous-turn and unreceived capability calls are excluded. Prepared capabilities remain options requiring user interaction, not evidence of completed downstream execution.
- Unbound calls remain supported. The reviewer identifies the requested part from the original request and proposed analysis rather than assuming every execution must fulfill the entire request.
- The original request always remains visible. Declared intent descriptions cannot authorize unrelated analysis or override the user's metric, population, filters, period or units.
- Pydantic validates structured request alignment, findings and the measurement contract. The runtime computes approval. `other_deliverable_missing` is nonblocking; measurement errors, unrelated requests and unresolved request alignment block execution. Coverage observations remain recorded but are excluded from blocking repair feedback.
- Final answer review retains responsibility for complete coverage and disclosure of unavailable parts. Existing SQL permissions, cardinality checks and unavailable-review behavior remain in force.

## Validation

- 198 targeted tests passed, including the 87 remote-runtime contract tests. The new measurement scope suite contains 20 tests and passed again after final review.
- Five real GPT-4.1 boundary checks passed: salary without SSN accepted; wrong-employee query rejected; invented count intent rejected; silent SSN omission rejected by final review; explicit SSN limitation accepted by final review.
- Live P04 remained verified and approved.
- Live P07: measurement review returned `matches_requested_part` with only `other_deliverable_missing`. Salary SQL executed successfully and returned zero rows. Trace: `9925a939eb0a4bc5c3047ee93292b1bb`, session `bat10_p07` prefix.
- P07 did **not** pass end-to-end: final review subsequently rejected identifier-card prose, first as `internal_process_narration`, then as `unsupported_by_evidence`. The ship guard produced a decline. This is a distinct final-review issue; the original measurement rejection is resolved.

Local artifacts: `/tmp/measurement-scope-tests.log`, `/tmp/measurement-boundaries-results.json`, `/tmp/probe-bat10-results.json`, `/tmp/probe-bat10-spans.json`. Backend port 18104 remains running with the new implementation and unredacted nested judge tracing.
