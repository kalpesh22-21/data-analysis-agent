# Multi-value filter review follow-up — 2026-11-05

Historical engineering record. Localhost trace URLs and `/tmp` or gitignored artifact paths refer to the original test machine; those artifacts are not distributed with this repository. The observations below are recorded results, not a portable reproduction bundle.

Implemented in data-analysis-agent; no commits or pushes. Existing work in both repositories was preserved.

| Review finding | Resolution |
|---|---|
| String-list lookup miss pauses with only matched values as choices | Returns retryable `BLUEPRINT_ENTITY_NOT_FOUND` for a failed list element. No partial set executes. Regression uses Sales + Engineerng and a lookup returning Sales only, for both string and entity slots. Genuine multiple-match ambiguity can still ask the user. |
| Two stale tests | Updated the prompt assertion to the retained instruction and the replay list sample to use authored `IN ({x})`. |
| Duplicate list resolvers | One bounded resolver handles legacy list and multiple categorical slots. Both deduplicate, reject malformed/nested/empty/oversized lists and honor declared enum values/All-alone. Legacy scalar-to-string coercion remains; categorical multiple values require text. Updated tests that explicitly pinned old nested-list stringification and ignored enum declarations. |
| Double resolution and duplicate retryable conversions | Pre-lookup shape validation is separate from value resolution. Enum values are resolved once; scoped values resolve after lookup. One argument-error conversion handles sort and list formatting failures. No empty Allowed values list is advertised. |
| Revalidated sort default | Copy the already validated default, matching other slot types. |
| Duplicate search-batching guidance | Retain in the mode-specific system guidance; remove from the gate reminder. Guidance now explicitly applies only when searching, so matched hints remain immediately usable. |
| Dead retrieval budget marker | Removed the assistant context_kind=retrieval branch; actual user hint-block classification remains. |
| Unused SEARCHES constant | App and tests now import the gate module's shared constant. |
| Experimental legacy binding guard | Removed guard and tests for the abandoned output_order binding shape. Native sort_order validation remains. |
| Trivial effective_retrieval_mode wrapper | Call sites read retrieval_mode directly. Deprecated configuration input is still accepted/ignored with the existing warning. |
| Assembler flag semantics | Renamed to retrieval_hints_enabled; defaults to True, aligning direct construction with default hints mode. Updated call sites. |
| Missing load-time multi-value placement check | Corpus validation rejects each multiple slot occurrence unless it is the sole value placeholder in authored IN ({slot}). Equality, IN-left-hand use and mixed fixed/list value sites fail before hydration. |
| Misplaced learning comment | Moved sort_order decline before the unrelated period-range explanation. |

Explicit sort overrides still replace the default. Automatic tie-breaker appending was not added; callers can supply the intended multi-column order explicitly.

## Verification

- Blueprint, retrieval, context, required-search, column-context, prompt-contract, extraction, generalization and promotion suites: **4,177 passed, 3 skipped**.
- App, redaction app wiring, base-prompt budgeting and trace-turn fields: **54 passed**.
- Ruff on changed implementation/test files: passed. `git diff --check`: passed.
- Base system prompt remains 17,990 characters; this cleanup did not add instructions to it.
- Detailed local logs: `/tmp/review-cleanup-broad-final.log`, `/tmp/review-cleanup-app.log`.
- These are deterministic/runtime integration checks. No new live model battery was run for this cleanup. The prior search-batching live results are documented separately.
