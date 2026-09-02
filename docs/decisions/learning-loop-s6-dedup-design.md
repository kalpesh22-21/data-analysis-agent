# Learning-loop Slice 6 — dedup / conflict (D48)

**Status:** BUILT (Track B, Wave 1). Implements the D48 two-layer blueprint dedup as
a `CandidateStage` writing `envelope.dedup` (Contract C). Builds against the frozen
Wave-0 contracts ([learning-loop-contracts-design.md](learning-loop-contracts-design.md)
§3, §7, §11.1, §11.2) — it does not re-decide them.

Module: `src/data_agent/learning/dedup/` · Tests: `tests/learning/dedup/`.

---

## 1. What S6 is

The third write-router stage (`generalize → leakage → dedup → writer`). For each
freshly-extracted **blueprint** candidate it decides whether an equivalent artifact
is already landed, and stamps a typed `DedupVerdict` onto the envelope. It is the
only stage that reads the landed-corpus side; every other target passes through
untouched (`dedup` stays `None`).

## 2. Two layers (D48)

### Layer 1 — the canonical hard key (race-safe by construction)

`compute_canonical_key` (`dedup/canonical_key.py`) is the S4→S6 coupling point. The
key is:

```
sha256( canonical_json([ resolves , sorted(uses_rules) , result_grain , canonical_ast_norm ]) )
```

with the `sha256:` prefix, EXACT inputs and order per §3:

- `resolves` — `payload.resolves` (S3); `canonical_json` sorts its keys.
- `uses_rules` — `generalization.uses_rules` (S4), **sorted explicitly** before hashing.
- `result_grain` — `generalization.result_grain` (S4), the `{columns, verifiable}` dict
  (a defensive cross-check, subsumed by the AST — D48 N4 — kept for robustness).
- `canonical_ast_norm` — `generalization.canonical_ast_norm` (S4). **Treated as an
  opaque string. S6 NEVER re-parses it** (the whole freeze exists so S4 emits the exact
  string S6 hashes; a sqlglot bump must not silently re-render it — §11.2).

`canonical_json` mirrors the D96 §5 convention (`sort_keys`, `separators=(",",":")`,
`ensure_ascii=False`) so the digest is deterministic across processes and Python runs.

**Hard-key HIT** ⇒ `action="increment"`, `layer="hard"`, `similarity=1.0`,
`matched_id=<artifact.id>`: the stage bumps the EXISTING corpus artifact's `hit_count`
(via `BlueprintCorpus.increment_hit_count`) and returns `control="drop"` — the
duplicate is NOT persisted. `hit_count` lives on the corpus artifact, never the
envelope (§11.1). **Hard-key MISS** ⇒ fall to the soft layer.

**Race-safety.** Identical semantics hash to the identical key, so the hard layer is a
deterministic equality test — single-writer-per-key by construction. Two concurrent
consumers racing on the same new key can each read "miss" and both attempt an insert;
the real cross-process guard (a Redis lock / partitioned-by-key queue so one key is
only ever processed by one worker) is the **Layer-2 leg deferred to Wave 3**. This
slice provides the deterministic key + the single-writer-per-key semantics a
Layer-1 concurrent test can assert against; it does not add the distributed lock.

### Layer 2 — soft embedding near-miss (human-adjudicated)

On a hard-key miss (or a fail-soft skip) the stage embeds `payload.intent` via the
injected embedder seam (`runtime/model/embedding_client.EmbeddingClient`; a scripted
`FakeEmbeddingClient` in tests — **no network**) and compares (cosine) against each
landed artifact's `intent`. Provisional bands (constructor knobs, RuntimeSettings-style):

| similarity | action | layer | routing (by the S7 writer) |
|---|---|---|---|
| `≥ merge_threshold` (0.95) | `merge` | `soft` | inbox (mergeable variant — never auto-append, §3) |
| `[conflict_threshold, merge)` (0.83–0.95) | `conflict` | `soft` | inbox (partial overlap) |
| `< conflict_threshold` | `insert` | see below | auto-land eligible |

A soft near-match **never auto-appends** (§3): both `merge` and `conflict` are left
for the writer to route to the review inbox under the `dedup_conflict` reason ("soft
conflict/variant"). The dedup stage itself only sets `control="drop"` for the hard-key
increment; every other verdict returns `control="continue"` so the writer owns the
inbox/auto-land decision (clean separation: dedup adjudicates, writer routes).

> **AMENDED (blueprint-review-rework slice) — a hard-key `increment` no longer always drops.**
> The matched artifact's `hit_count` is still incremented either way, but the candidate's fate
> now depends on the artifact's status:
>
> * matched artifact is **live** ⇒ `control="continue"`. The writer routes it to the inbox
>   under the new reason **`suppressed_duplicate`**, so a human can look at the match and revise
>   a genuine delta instead of the candidate evaporating. Production is human-gated; a duplicate
>   costs one skim, a silent drop costs the delta.
> * matched artifact is **terminal** (`rejected` / `retired`) ⇒ still **dropped**. A human said
>   no to exactly this thing; rejection is negative memory and re-surfacing it would relitigate a
>   settled decision.
> * `redundant_with_canon` (layer 2, structurally identical to an MCP canon blueprint) ⇒ still
>   **dropped**. There is no learning artifact to increment, and approving it would land on the
>   matched artifact's own node: `landing_id()` derives the node id from `dedup.canonical_key`.
>
> The writer keeps its `redundant_with_canon → suppressed_duplicate` mapping as fail-closed
> defence-in-depth: `DedupVerdict.from_doc` does not validate `action`, so a rehydrated verdict
> must land in review rather than fall through to auto-land.

## 3. Fail-soft (D52)

If `canonical_ast_norm` is absent/empty (S4 could not produce a template), the hard
key is **skipped entirely** — a missing template never mints a spurious key and never
collides. The candidate falls straight to the soft layer; the verdict carries an
empty `canonical_key` and `layer="soft"`. Additionally, an empty `intent`, an empty
corpus, or ANY embedder failure degrades to `action="insert"` — **never a wrong
merge**. `insert` verdicts carry `layer="hard"` when a real hard key was computed
(the AST established uniqueness) and `layer="soft"` when the hard key was fail-soft-skipped.

## 4. The corpus seam

`BlueprintCorpus` (`dedup/corpus.py`) is the landed-artifact port:
`get_by_canonical_key`, `increment_hit_count`, `list_artifacts`. `CorpusArtifact`
carries `(id, canonical_key, intent, hit_count, uses_rules)`. `InMemoryBlueprintCorpus`
is the Layer-1 fake (seedable from `existing_corpus_keys.json`, records
`increment_calls` for the "one create + one bump" assertion). The real neo4j-backed
corpus is a later wiring; the port is injected so nothing here reads global settings.

## 5. Tests (Layer-1)

- `S6-canonical-key-dedup` — `test_hard_key_hit_increments_and_drops`: identical
  semantics ⇒ identical key; pass 1 (empty corpus) inserts, the artifact is landed,
  pass 2 hits ⇒ increment (`control="drop"`, one `increment_call`, `hit_count 1→2`).
  Plus `test_identical_semantics_hash_to_identical_key` and
  `test_seeded_existing_corpus_hard_key_hit`.
- `S6-failsoft-no-wrong-merge` — `test_failsoft_absent_norm_skips_hard_key_no_wrong_merge`:
  a blanked `canonical_ast_norm` skips the hard key even when an artifact sits at the
  would-be key; the dissimilar soft layer yields `insert` (empty key, `layer=soft`),
  the would-be artifact is untouched. Plus `test_failsoft_embedder_failure_degrades_to_insert`.
- Soft-band coverage (`merge`/`conflict`) and non-blueprint pass-through.

## 6. Open items / ambiguity

- **`canonical_key` prefix.** The frozen `DedupVerdict` docstring says "sha256 over
  …"; the existing-corpus fixture uses a `sha256:`-prefixed synthetic key. S6 emits
  `sha256:<hexdigest>`, matching that convention. (The increment tests seed the
  corpus with S6's own computed key, so the prefix choice is internally consistent.)
- **merge vs. conflict split.** §3 fixes the two soft actions but not the exact bands;
  they are provisional constructor knobs (`merge_threshold=0.95`,
  `conflict_threshold=0.83`) pending real traffic — the same posture as the other
  §11 tunables. Both route to the inbox regardless, so the split is a reviewer-hint,
  not a correctness gate.
- The Wave-3 distributed single-writer-per-key lock (Layer-2 race leg) is out of scope.
