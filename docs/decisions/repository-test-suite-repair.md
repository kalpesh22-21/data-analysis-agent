# Repository test-suite repair

Date: 2026-09-13

The requested backlog was the 297 failures reported by the earlier repository run.
The fresh run at the start of this repair reproduced 299 failures, 7,852 passes,
and 229 skips. The difference reflects changes since the earlier snapshot.

## What changed

The scripted conversations now follow the explicit final-answer contract. Test
fixtures wire the real answer handlers, advertise the MCP tools they exercise,
and cite received evidence or give an honest decline. Tests of downstream hooks
explicitly arrange a prior blueprint consultation; the receipt gate itself remains
covered by the existing end-to-end hook tests. No production gate was disabled.

Other fixture migrations cover:

- `result_id` bindings, plural `serves_intents`, and late declarations.
- One complete-proposal review after a batch and the durable two-review limit.
- Receipts for capped or paused calls, distinct from actually executed tool counts.
- Explicit empty UI selections, opaque discovery IDs, normalized clarifications,
  and the structural-verification status label.
- Environment isolation in the Vault fallback test, including empty and supplied
  environment values. Local Couchbase writer credentials no longer make it fail.

Runtime defects found and repaired:

- Final-answer tool arguments receive evidence provenance. New proposal receipts
  do not replay into later turns; their original prose is also withheld on a
  scope-changing resume. The final assistant message and legacy history projection
  remain separate from these control receipts.
- A denied execution can justify a blocked intent's limitation without becoming
  supporting evidence for an answer. Verified parts remain eligible to ship.
- Empty-response fallbacks close pending intents and clear unapproved UI selections.
- Shape review checks the current selection and available multi-row results. An
  earlier table proposal cannot exempt a later proposal that clears the table.
- Answer-rule, empty-answer, and judge-failure telemetry is emitted at the current
  finalization sites. Shape nudges include the bounded draft and current tool names.
- Optional progress summaries have a one-second timeout and are cancelled and
  awaited on timeout. Final-answer calls do not request a progress summary.
- Numeric corroboration reaches the judge again. Results omitted during repair
  are excluded from both the judge's source payload and the corroboration scan.

The evaluation metrics also read plural tags and current `result_id` bindings,
while retaining historical binding support.

## Validation

- Full repository, without opt-in Couchbase tests: **8,154 passed, 229 skipped**.
- Couchbase-enabled repository run after the Vault and metric fixes:
  **8,192 passed, 193 skipped**.
- Focused omission and regression checks after the final review correction:
  **17 passed**.
- Final reviewed-code run with local Couchbase enabled: **8,193 passed,
  193 skipped, 0 failures, 0 errors** in 74.45 seconds.
- Ruff checks and formatting pass for the changed Python files; `git diff --check`
  passes. No tests were disabled or marked as expected failures to obtain green.

The remaining skips are existing opt-in tests. These results describe the repository
suite and enabled local Couchbase tests, not a rerun of every optional service,
browser, or live-model evaluation battery.

The local backend was restarted on port 18104 with GPT-4.1, the judge enabled,
its 60-second timeout and 32,000-token evidence budget, and the previously requested
unredacted tracing. `/openapi.json` returns HTTP 200 and advertises `/turn`.

Private/local run artifacts:

- `/tmp/repository-fixes-initial.xml`
- `/tmp/repository-fixes-final.xml`
- `/tmp/repository-fixes-final.log`
- `/tmp/harness-backend-repository-fixes.log`
