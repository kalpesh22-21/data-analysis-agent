# R9 / P08: retain selected work when finalization repair is exhausted

The remote P08 report identifies a reproducible runtime defect. A scripted reproduction using the real agent loop, capability handlers, in-memory session store and an approving judge loses all three selected prepared cards on commit `922617e`. The same reproduction passes with this change. This validates the local mechanism; it does not independently validate the remote trace or business-data semantics.

## Confirmed failure chain

1. Invalid evidence references are detected, then a missing capability-preparation complaint overwrites that diagnostic.
2. The first refusal spends the `ungrounded_answer` repair allowance. The second refusal has no generic exhausted-allowance event.
3. The exhausted path preserves tables or chooses `decline_only`; card-only deliveries therefore lose their selected prepared cards before delivery review.
4. Without an analysis ledger, partial-delivery text is empty. The final review also lacks selected-component bindings and can approve a generic empty fallback.

## Changes

- Compose evidence and preparation complaints with the existing finalization nudge. Preserve the original gate precedence, per-kind allowances and specialized telemetry.
- Keep the existing evidence eligibility rules and diagnostics unchanged. Invalid references remain invalid; there is no fuzzy auto-repair.
- On exhaustion, preserve the intersection of explicitly selected, successfully prepared, current-scope capability executions, excluding components already omitted by review. Preserve selected tables alongside cards for mixed deliveries. No merely prepared or unselected card is promoted.
- Emit `loop_finalization_block_exhausted` with `window`, `turn_index` and `guard_reason` when the store denies an already-used allowance. A store failure retains its separate event.
- For ledger-less fallback deliveries, describe included components using their labels and distinguish a prepared view from a verified numerical answer. Disclose unverified remaining work without inventing intent-to-result associations.
- Give delivery review the retained execution IDs through its existing `selected_components` field. Existing card metadata already carries resolved filters and definitions. No new evidence-package format or inferred intent assignment is introduced.

Review remains mandatory under the existing policy: explicit rejection still withholds the delivery; no-verdict exhaustion may fail open. Preserving work for review is not automatic semantic approval. The remote example's compensation call with `employees:["all"]` requires checking the hydrated/resolved filter against the requested CEO population; preparation success alone cannot establish that match.

## Regression coverage

Eight real-loop scenarios cover:

- P08 card-only exhaustion: one combined nudge, one spent event, one exhausted event, three retained cards, component text and review of the actual retained payload.
- Mixed cards and table: neither representation erases the other.
- Still-unprepared card: not delivered.
- Prepared but unselected card: not delivered.
- Explicit delivery-review rejection: withheld.
- Persisted proposal-review rejection followed by malformed finalization: still withheld.
- No-verdict review exhaustion: retained components may be delivered under the agreed policy.
- Complete repair after the first nudge: normal proposal review, without exhaustion.

The allowance test now asserts the new exhaustion event. Existing gate-precedence tests remain unchanged.

Validation: original-commit reproduction fails on missing cards; fixed regression scenarios pass. Full runtime suite: **3,977 passed, 5 skipped**. Ruff and whitespace checks passed. No Kimi or remote deployment validation was performed for this deterministic state-machine fix.
