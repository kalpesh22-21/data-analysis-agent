# Runtime harness improvements — agreed decisions

Date: 2026-09-13

Status: Accepted decisions implemented. See [implementation and validation](runtime-harness-implementation-validation.md) for coverage, baseline test failures, and limitations.

This document records the user's decisions from the runtime review and subsequent
point-by-point discussion. It distinguishes accepted scope from rejected proposals
and newly identified findings awaiting discussion. Recommendations in other review
documents do not automatically become accepted scope here.

## Accepted decisions

### D1. Optional, provider-neutral reasoning metadata

Add a flag named `use_reasoning_metadata`, defaulting to `false`.

- Enabled: retain and replay returned reasoning metadata with its original assistant
  message across tool calls and resumed turns.
- Disabled: omit reasoning metadata.
- Do not make this behavior Kimi-specific or expose a Kimi-specific configuration.
- Models returning no reasoning metadata remain supported.
- Metadata stays internal; it is not user-facing answer content.
- Permission filtering and context trimming must account for retained metadata.

Preserve original assistant tool-call batches, report skipped calls explicitly, and
handle malformed tool arguments as repairable errors rather than silently converting
them to `{}`. These conversation-correctness changes apply independently of the flag.

The model conversation should preserve the original response structure; the existing
execution trail continues to support provenance, auditing, storage, and learning.
The precise storage representation and provider-field normalization remain design details.

### D2. Identify execution results independently of blueprint definitions

Each successful query or blueprint execution receives an immutable `result_id`.
The agent references that execution when selecting a table or supporting evidence;
`blueprint_id` remains lineage, not the identity of the latest result.

This fixes the confirmed repeated-blueprint case: Sales and Engineering executions
must remain distinct instead of both resolving to Engineering's SQL when it ran last.

**The UI contract remains unchanged.** The runtime resolves each result reference to
its execution's SQL and metadata, then sends the existing `answer_tables[].sql`
payload. The UI still receives separate Sales and Engineering queries and executes
each through the existing pagination endpoint.

- Keep SQL, bound parameters, provenance, and verification attached to the same execution.
- Reusing the tool-call ID internally is an implementation option, not a requirement
  to introduce a second independent identity system.
- Execution identity does not imply a frozen warehouse snapshot. Stored result
  snapshots are not accepted scope.
- Pagination must preserve the requested query meaning; a semantic top-N restriction
  is different from a transport preview cap. The precise representation remains open.

### D3. Check measurement meaning and aggregation safety

Retain existing output-grain and column-signature checks, but do not equate their
success with a correct answer to the user's question.

Add complementary checks before final prose review:

1. Meaning: the selected blueprint or generated SQL matches the requested metric,
   population, time window, units, and grouping.
2. Aggregation safety: joins do not duplicate records being counted or summed before
   aggregation. Use catalog relationships and targeted checks where cardinality can
   change the measure.

A compact measurement contract should communicate the intended calculation. The
specific implementation and when additional probes are required remain to be designed.
Verification labels must accurately describe what was checked.

### D4. Prepare capabilities without implicitly finishing the turn

Capability preparation becomes nonterminal. Use one explicit finalization tool to
combine answer prose, table result references, prepared UI option references, and
supporting evidence into the complete proposed answer.

- Finish all deliverables of a mixed request before finalizing.
- The runtime resolves references into the existing UI payload contracts.
- Preparing an option does not perform navigation or a business action. Activation
  remains a user interaction in the UI.
- Tool names and the exact unified schema remain implementation details.

### D5. Organize the main prompt around a procedure

Use this operating procedure:

1. Understand the ask: identify what the user expects.
2. Choose the source: blueprint, warehouse query, Help Center, or UI capability.
3. Check the fit: confirm meaning, requested population, period, and coverage.
4. Do the work: gather evidence for each requested part.
5. Deliver the answer: present supported results and disclose missing parts.

Preserve existing safeguards unless another accepted decision explicitly changes
them. Acceptance of this procedure is not authorization for every prompt or runtime
simplification proposed during the initial review.

### D6. Enforce discovery before SQL using information the model received

A blueprint search merely proposed in the current tool-call batch cannot satisfy the
pre-SQL discovery gate, regardless of its position in that batch.

Qualifying discovery must already have reached the model before it proposes SQL.
Applicable prefetched discovery can satisfy the requirement. Enforcement belongs in
runtime code; the prompt explains the procedure.

The representation of qualifying discovery and deliverable association remains an
implementation detail. Handling discovery failures must not falsely claim that the
model received usable search results.

### D7. Explicitly bind one execution to multiple intents

Support `serves_intents: [...]` so one execution can directly support multiple
deliverables without relying on ambiguous automatic binding.

- Associate the resulting `result_id` with each named intent.
- A tag alone is not proof of semantic coverage; coverage still needs checking.
- One result can serve several intents and appear only once as a final table.
- Existing single-intent payload compatibility remains an implementation detail.

### D8. Allow late intent declaration with explicit result binding

Continue to instruct the model to declare multi-part requests first. If it misses
that step, permit late declaration and explicit binding to existing results.

- Use the same result-binding mechanism for early and late declarations.
- Keep intent descriptions frozen once declared.
- Newly declared intents remain pending until explicitly resolved.
- Validate that referenced results belong to the current turn, remain accessible,
  and qualify as successful evidence.
- Do not guess which earlier result belongs to a newly declared intent.
- Meaning checks assess whether the bound results cover the declared deliverables.

Prefer replacing automatic-binding special cases with this explicit mechanism over
adding another competing binding path. Exact compatibility/migration handling remains open.

### D9. Accuracy-first judge feedback with minimal repairs

Prioritize wrong measurements, unsupported claims, missing deliverables, and
unsuitable UI options over minor presentation issues.

Return structured feedback identifying the affected claim or result and the smallest
necessary repair. Distinguish:

- Analysis/evidence repair: correct the work or disclose the limitation.
- Prose repair: correct the explanation using an existing valid result.
- Presentation repair: edit wording or formatting while preserving completed work.

A wording correction must not unnecessarily restart warehouse analysis. D3 owns
measurement checks; final review assesses whether the complete proposed answer
faithfully presents its evidence and covers the request.

The interaction between repair allowances and final approval is resolved by D10 below.

### D10. Allow one repair followed by final validation

Accepted during the interaction-findings discussion (F1).

- An initial approval permits shipping the reviewed answer.
- An initial rejection permits one repair, followed by a final validation.
- Spending the repair allowance must not prevent validating the repaired answer.
- Final validation may approve shipping but cannot grant another repair round.
- If final validation rejects the answer or is unavailable, use a safe partial answer
  or decline rather than treating the unresolved rejection as approval.
- A reliable deterministic check may replace the second judge call for applicable
  corrections. Otherwise reserve enough time for the final review.

This separates the repair allowance from validation while keeping the process bounded.

### D11. Review the complete response after the batch finishes

Accepted during the interaction-findings discussion (F2).

- Finish the tool batch and its bookkeeping before final review.
- Assemble all user-visible prose, tables, assumptions, and prepared UI options into
  one complete proposed response, consistent with D4's unified finalization tool.
- Validate and judge that complete response, then ship exactly what was reviewed.
- Any subsequent change to user-visible content invalidates the approval.
- A repaired response follows D10's bounded final-validation path.

An approval of a table answer during dispatch must not authorize assumptions or other
content added by later calls in the same batch.

### D12. Share clarification validation across direct and runtime-tool pauses

Accepted during the interaction-findings discussion (F3).

Route both direct `askUser` questions and runtime-tool `ToolPause` questions through
one clarification-validation path before showing or checkpointing the question.

- Express questions in business terms, avoiding internal schema and slot terminology.
- Present understandable choices, with human-readable labels alongside codes where needed.
- Handle more than five choices explicitly rather than silently truncating the list.
- Preserve the blueprint's execution checkpoint, slot bindings, and correct mapping
  from user-facing choices to underlying values when normalizing presentation.

This changes validation of the user-facing clarification, not the blueprint's execution
or resume semantics. The exact handling of excess choices remains an implementation detail.

### D13. Ground each deliverable and extend the judge contract

Accepted during the interaction-findings discussion (F4).

- Associate each deliverable's proposed answer with explicit evidence references in
  the judge input, using D2's result IDs and D7/D8's intent bindings.
- The runtime validates that references exist, succeeded, remain accessible, and
  are eligible evidence for the relevant kind of claim. Evidence types derive from
  the producing tools, not from model-authored tags alone.
- The judge evaluates whether the actual evidence supports the proposed claims for
  each deliverable. Successful warehouse work must not automatically ground product
  instructions or another unrelated part of the answer.
- Extend both the structured judge output schema and its validation model with
  targeted repair information: the affected intent/deliverable, relevant result
  references, repair type, and actionable feedback. The example field names and
  repair enum values discussed are illustrative; the exact schema remains open.
- Preserve supported parts when requesting repair of an unsupported part. The
  agent may obtain suitable evidence or disclose the limitation for that part.
- Single-deliverable requests use the same evidence checks without requiring the
  model to declare an intent ledger. Do not require bookkeeping for every sentence.

The current judge uses a `JudgeVerdict` dataclass and a `record_judgement` tool
schema. Changing this validated contract is accepted; adopting Pydantic specifically
is not required. Changing only the output schema without restructuring the judge
input would not satisfy this decision.

If the judge is disabled or unavailable, deterministic checks can catch missing or
ineligible references but cannot establish semantic support for every statement.
Do not represent those checks as equivalent to a completed semantic review.

### D14. Persist and restore actual review state across pauses

Accepted during the interaction-findings discussion (F5).

Persist the actual violation, affected deliverable/result references, rejected
answer version, repair feedback, and consumption of repair/final-validation
allowances. Resume must restore those facts and continue D10's bounded repair flow.

- Do not reconstruct a generic capability-intent mismatch from a rejection counter.
- A pause must neither reset repair allowances nor change the reason for rejection.
- Restore the review state associated with the correct proposed answer version.
- Reapply current permission checks on resume; restored review state does not
  override access restrictions.

The persistence schema and migration handling remain implementation details.

### D15. Restore capability readiness on resume if the registry gap is confirmed

Accepted during the interaction-findings discussion (F6), with integration
verification before implementation because the gap is currently a code-level risk.

For capabilities loaded during the paused turn, restore readiness before the next
model call by fetching their definitions under current permissions and registering
those still available and valid. Explicitly inform the model when an option is no
longer available so historical `ready` messages do not imply present callability.

Restoration loads definitions only: it must not present an option, navigate, or
perform a business action. Confirm the fresh-registry resume failure with a targeted
integration test before implementing the restoration path.

## Excluded or not accepted

### Help Center re-chunking and section retrieval

The user clarified that ingestion guarantees chunks no larger than 4,000 tokens.
The proposed new article sectioning and section-fetch machinery are not accepted.
The earlier oversized synthetic article reproduction does not establish a production
truncation defect under that ingestion contract.

Checking whether runtime serialization preserves a valid chunk, and correcting
"complete article" wording if needed, were suggested but not explicitly accepted.
Do not silently include them in implementation scope.

### Explicit query refresh and unavailable-result recovery

The user explicitly rejected this proposal. Do not add a refresh policy or a
result-recovery tool as part of these changes. D2's result references do not imply
acceptance of these features.

### General lifecycle-hook hardening

The user excluded the proposed deadlines, structured hook outcomes, and additional
replacement validation for the dormant answer-table hooks. Leave those extension
hooks unchanged. This exclusion does not exclude reviewing interactions between
the runtime's behavioral gates.

### Blueprint version binding between inspection and execution

The user explicitly rejected the F7 proposal. Do not add inspection-version or
content-hash binding, definition-change refusals, or a new blueprint versioning
mechanism as part of this work. D2's execution result IDs remain accepted and do
not imply acceptance of blueprint version binding.

## New interaction findings — pending discussion, not accepted changes

The user requested that these findings be discussed one at a time after locking the
previous decisions. No resolution below is approved merely by inclusion here.

| ID | Finding | Evidence status |
| --- | --- | --- |
| F1 — resolved by accepted D10 | Judge rejection spends the repair allowance; corrected answer skips judging; shipping guard still requires a real approval and substitutes a decline or hedge. | Reproduced through the loop; an existing test acknowledges the hedge behavior. |
| F2 — resolved by accepted D11 | A table answer is judged before the batch finishes, allowing later assumptions to ship without appearing in that review. | Reproduced through the loop. |
| F3 — resolved by accepted D12 | Runtime-tool pauses bypass the direct `askUser` judge and pending-question scrubbing; choices are capped by truncation. | Bypass reproduced with a scripted pausing tool; real slot-question construction inspected. |
| F4 — resolved by accepted D13 | Successful SQL evidence can suppress the Help Center failure fallback for unsupported product guidance in a mixed answer. | Reproduced with the answer judge disabled. |
| F5 — resolved by accepted D14 | Resume reconstructs an outstanding rejection as a generic capability-intent mismatch rather than restoring the actual violation. | Code inspected and guard-level disposition change reproduced; full resume integration still needed. |
| F6 — conditional resolution accepted as D15 | Per-request capability registration can leave resumed history claiming an option is loaded while the new registry lacks its tool. | Code-level risk; targeted integration test pending. |
| F7 — excluded by user | Inspection and execution identify a blueprint by ID; if its definition changes under that ID, execution can differ from the definition reviewed. | Conditional code-level risk; version-binding proposal rejected. |

Review suggestion awaiting discussion: assemble a complete proposed answer after
work finishes, validate that exact version, allow bounded repairs, and preserve its
review state across pauses. This is not yet an independently accepted architecture.

## Validation notes

The latest focused interaction suites reported 66 passes and 38 failures. Several
failures use the obsolete expectation that plain assistant prose can finish a turn;
the failures should not all be treated as independent production defects. Update
tests to the current explicit-finalization contract and add real gate-chain interaction
tests when implementing accepted changes.

Local interaction reproductions used synthetic data and scripted model/judge results.
No live Kimi behavior or production service behavior has been established by them.
