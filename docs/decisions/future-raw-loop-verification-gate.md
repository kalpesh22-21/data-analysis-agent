# Future scope — raw-loop verification gate

**Status:** Deferred (out of Phase-0 scope). Tracked here so it is not lost.

## The gap
The D56 grain/invariant verification gate runs **only inside the blueprint fast path**
(`runtime/blueprint/executor.py`). Two consequences, verified against the code while locking
[UI Slice 1](ui-slice1-enriched-result-contract.md):

1. **Raw-loop answers are unverified.** When no blueprint answers the turn, the agent falls back to
   the raw loop, which has **no** grain/invariant gate — the answer is returned without verification.
2. **A failed gate never surfaces a failed answer.** A blueprint whose gate fails degrades to the raw
   loop rather than returning `passed:false`. So a `passed:false` badge never reaches a user; the
   UI's **"verified ✓"** badge means "verified blueprint fast path," and its *absence* means
   "raw-loop (unverified)," not "failed."

This is why the UI verification badge (`docs/08-ui.md` "passive verification badge" principle) is
honest but partial: it is present-only, and only for blueprint answers.

## What "verify every answer" would require
A new **raw-loop verification gate**: a code-computed grain/invariant check (and/or LLM review) that
runs on raw-loop answers before they are returned, producing a real pass/fail the UI could render as
a genuine badge (including a negative/"could not verify" state). Design questions to resolve when
picked up:
- What invariants are checkable without a blueprint's declared `result_grain`?
- Does a raw-loop gate *pause* on failure, silently retry, or annotate the answer? (The blueprint
  path degrades rather than pauses — the raw loop has nowhere further to degrade to.)
- Cost: an extra LLM round-trip on every raw-loop answer vs. code-only checks.

## Decision (2026-07-08)
Accepted blueprint-only verification for Phase-0. UI Slice 1 ships the honest present-only badge
(`verification: null` for raw-loop answers). Revisit when raw-loop trust becomes a priority.
