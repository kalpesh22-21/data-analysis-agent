# Ignore extra citations and review evidence correctness and sufficiency

The reported leave-ranking failure exposed a remaining finalization defect: an extra
`resolveValues` citation caused the runtime to replace a supported draft with a generic
decline after its repair allowance was exhausted. Delivery review then rejected that
decline despite the retained table covering the request.

The evidence assessment checks references against the current-turn, scope-filtered
trail. Missing, inaccessible, malformed, and ambiguous references still block;
missing and inaccessible references deliberately share the same diagnostic.

Known ineligible receipts, including `recordAssumptions`, `resolveValues`, `sampleRows`,
and failed calls, are removed from affirmative evidence immediately, without blocking
or spending a repair allowance. This applies to top-level citations and deliverable
bindings, even when no eligible evidence remains. Failed calls can still substantiate
a blocked intent's limitation. The runtime submits the original draft and retained
evidence to the judge, which decides whether support is correct and sufficient. It
records `loop_answer_evidence_extras_ignored` with only the dropped count. Coverage
assembly does not reintroduce discarded bindings from the ledger or intent tags.

The judge explicitly allows redundant, irrelevant, or unused extra evidence. It
requests repair for incorrect measurements, unsupported claims, or insufficient
support, not citation cleanup. Failed attempts and control receipts do not establish
data absence. Optional evidence-type annotations cannot relabel a receipt: its actual
kind is authoritative. Missing capability preparation, incomplete intents, malformed
answers, and explicit judge rejections retain their existing guards. The existing
no-verdict exhaustion policy remains unchanged.

`updateAnalysisState` likewise ignores extra top-level and item metadata. Declaration
consumes descriptions and assigns runtime IDs with pending status; supplied IDs,
statuses, and evidence cannot override that initialization. Updates preserve the
original descriptions and still require valid IDs, statuses, and sufficient successful
evidence for completion. Missing required information and failed evidence bindings
remain errors.

Validation of the expanded policy: **4,388 passed, 5 skipped** across runtime and UI
tests. Regression cases cover control receipts with and without a ledger, removal
of all cited support followed by judge rejection, immutable descriptions with extra
state metadata, and sanitized evidence not being reintroduced into judge coverage.
Ruff and whitespace checks pass. This policy change has deterministic test coverage;
no additional live model probe was run for it.

Original validation: four original-draft regression variants fail against the pre-fix snapshot
and pass with this change. Fourteen focused tests cover ledger and ledger-less answers,
independently blocking preparation complaints, clean citations, explicit/prior rejections,
no-verdict exhaustion, scope-safe diagnostics, and ineligible deliverable bindings.
Full runtime and UI suites: **4,319 passed, 5 skipped**. Ruff and whitespace checks pass.
No live model or remote trace replay was performed for this deterministic runtime fix.

Related missing-field failure: a proposal containing `answer` and selected `tables`
must not fail solely because `capability_refs` or `evidence` was omitted. The advertised
schema now makes those fields optional. Dispatch defaults omitted capability refs to
an empty selection and omitted evidence to the explicitly selected table result IDs.
It does not select arbitrary previous results or cards. Explicit values, including
empty arrays, are preserved; malformed values still fail validation. Inferred IDs pass
the existing access, success, eligibility, and grounding checks before the original
draft reaches the judge. Terminal review remains mandatory, including runtime fallbacks.
Schema errors themselves do not spend the finalization allowance; the reported later
empty-answer refusal does. Thirteen additional tests cover omission, malformed inputs,
required content, invalid evidence, and explicit judge rejection.
