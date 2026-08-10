# Slice 9 (S9) — promotion scheduler + golden replay (design)

**Status:** BUILT (Track B, Wave 1). Standalone worktree off the Wave-0 base (`2083150`).
Implements Contract E (the promotion state machine) from
[learning-loop-contracts-design.md](learning-loop-contracts-design.md) §5/§7.2/§11, and the
[05-memory-and-learning.md](../05-memory-and-learning.md) §Promotion-scheduler + D43 drift attestation.

This doc records what S9 builds, the exact guards it enforces, which drift probes are live vs
stubbed, and the frozen-contract ambiguities encountered. It does **not** re-decide anything; every
semantic is already Locked (D29/D30/D36/D43/D56/D58/D98/D101/D102 + the §11 OQ resolutions).

---

## 1. What S9 is (and is not)

S9 is the **separate cron-scanned promotion scheduler** (D29/§7.2), **not** a `CandidateStage`. It
does not touch the consumer pipeline seam. Each cycle it:

1. reads `learning_candidates.list_by_status("candidate")` + `list_by_status("validated")`
   (D101 — the store is queryable by `status`);
2. runs **golden replay** (reusing the D56 `verify_result` gate + the runtime template binder) and the
   **D43 drift probes**;
3. advances `status` and stamps `drift` (Contract E) via `store.put`.

It shares the **envelope contract** with the write-router stages but no code seam, so it parallelizes
against the frozen S4 fixture (`tests/fixtures/learning/s4_enriched_blueprint.json`). It is fail-closed
and kill-switch-gated exactly like the sweeper (D58c read fresh per cycle; one bad candidate never
aborts the cycle).

**Module layout (`src/data_agent/learning/promotion/`):**

| File | Role |
|---|---|
| `models.py` | `PromotionPolicy` (T + freshness knobs), `ProbeResult`, `CandidateDecision`/`PromotionSweep`, the three injected ports (`WarehouseProbe`, `HitCountReader`, `DependencyResolver`). |
| `replay.py` | `golden_replay(env, probe) -> ReplayOutcome` — reuses `bind_template` + `verify_result`; samples synthetic slot values (D17). |
| `drift.py` | `drift_from_replay`, `user_correction_stamp`, `silent_eligible` — the D43 probes + predicate. |
| `scheduler.py` | `PromotionScheduler` — `run_once` (cron), `apply_human_decision`, `apply_user_correction`, `run_forever`. |

---

## 2. The state machine (Contract E §5, exact)

```
candidate ─(static ok AND golden-replay pass AND deps resolved
            AND (hit_count ≥ T OR human approval))──────────────▶ validated   [promote]
candidate ─(any guard fails / single session / dep unresolved)──▶ candidate    [hold]
in_review ─(human approve)──────────────────────────────────────▶ validated   [approve]
in_review ─(human reject)───────────────────────────────────────▶ rejected    [reject]
validated ─(drift probes clean)─────────────────────────────────▶ validated   [drift_clean, re-stamp]
validated ─(drift suspect OR replay fails OR user correction)───▶ candidate    [demote + review flag]
```

**Guards on `candidate → validated` (all required, D29/D98):**
1. `generalization.static_validation.outcome == "ok"` (S4 stamp; D52/D97).
2. `depends_on` fully resolved (§11.6).
3. A passing **golden replay** (`verify_result` green — grain-integrity + signature shape; D56/D98).
4. `hit_count ≥ T` **OR** human approval.

**Replay alone NEVER promotes (D98 layer iii).** A single-session candidate (`hit_count < T`, no human
approval) with a GREEN replay **stays `candidate`**. This is the load-bearing invariant
(`S9-replay-not-a-value-oracle`): the promotion signal is cross-session recurrence (`hit_count`) or a
human, never the replay by itself.

**Review flag on demotion.** S9 owns only `status` + `drift` (D102 additivity). The "review flag" is
therefore carried by the demoted `status=candidate` **plus** `drift.status=suspect` (naming the
`failed_probe`). A downstream S7 inbox can route a `candidate` with a suspect drift to human review.

**Human approve/reject** (`apply_human_decision`) is the ONE caller-driven path (Contract D) — not the
cron scan. A blueprint approval still passes the static + replay safety guards (human approval
substitutes for the hit-count threshold, **not** for structural integrity — D98). A non-replayable
target (global_knowledge/schema_edit) approves directly (the human is the authority for the pre-gated
targets, D58a/D18).

---

## 3. Golden replay (D29/D36/D98) — structure, not values

`golden_replay` (in `replay.py`):

1. reads `payload["generalization"]` (Contract A) → the `sql_template` (single) or the **terminal**
   node template (composite: highest `order` — the node the grain/signature describes, the only node
   the D56 gate guards, §2.4);
2. **samples synthetic slot values** — `__replay_sample_<name>__` tokens minted here, **never** a
   stored entity input (D17: no entity input is ever persisted, so replay cannot and must not reuse
   one). The synthetic value proves the template BINDS and RUNS; its value is irrelevant;
3. binds via the reused `runtime/blueprint/template.py::bind_template` (F1/D10 typed-literal binding —
   the same binder the live executor uses);
4. hands the bound SQL to the injected `WarehouseProbe` (fake in tests — **no real ClickHouse**), which
   returns `(row_count, distinct_grain_count, columns)`;
5. calls the reused `runtime/blueprint/verify.py::verify_result` with the declared `result_grain` (the
   D56 teeth) + the `result_signature.shape` columns (the entity-free golden shape). Structure passes
   ⇒ replay passes.

**No value oracle (D98/D17).** The probe returns row/distinct/column *structure* only — never the
result number. A "value-changed but structurally-valid" replay still passes; the changed number is
never inspected. Adding a value oracle would breach D17 (the scalar answer is entity-bearing and never
stored). A green replay is not a correctness proof.

**Reuse, not reimplement.** The D56 verification is reused verbatim (`verify_result`); replay only
prepares the input (sample → bind) and hands the probe triple to the gate. The full `BlueprintExecutor`
is **not** stood up: the frozen fixture + spec fix the replay boundary at the `verify_result` triple (a
fake probe returning `(row_count, distinct_grain_count, columns)`), not the runQuery result shape, so
standing up a fake `ToolDispatcher`/`vector_index` would add infrastructure without adding fidelity.

---

## 4. Drift probes (D43) — live vs stubbed

D43 names **three** probes. Per D56's phased note (grain-integrity runs on every result in Phase 1; the
full freshness predicate is Phase 2):

| Probe | Phase 1 | How |
|---|---|---|
| `grain_integrity` | **LIVE** | The golden-replay verdict (reused D56 teeth + signature shape). A replay fail ⇒ suspect. |
| `catalog_conformance` | **STUBBED** (`unchecked`) | Needs a live catalog-vs-warehouse probe (not wired Phase 1). |
| `rule_currency` | **STUBBED** (`unchecked`) | Needs a live rule-registry probe (not wired Phase 1). |

`DriftStamp.probes` records **which of the three actually ran** — in Phase 1 that is only
`("grain_integrity",)`. The two stubbed probes are **absent** from the tuple (= not run, honestly
reported — never a silent "clean" claim for a check that did not happen). A `stale` status is only
reachable once `catalog_conformance` goes live (Phase 2), so Phase 1 produces `clean` | `suspect` from
the live grain probe (plus `suspect` from a user correction, which is a negative signal, not a probe —
`failed_probe` stays `None`).

**Silent-eligibility (D43):** `silent_eligible ⇔ status == validated AND drift.status == clean AND
fresh(last_drift_check_at)`. `fresh` = within `PromotionPolicy.drift_freshness_seconds` of now; a
missing/unparseable/naive timestamp is fail-closed (never fresh). A freshly promoted candidate is
stamped clean+fresh (the passing promotion replay IS the live grain probe), so it is immediately
silent-eligible.

---

## 5. The injected ports + policy

Three Protocols (Layer-1 fakes = live path; no real infra in tests):

- `WarehouseProbe.run(sql, *, grain_columns) -> ProbeResult` — the structure oracle.
- `HitCountReader.hit_count(canonical_key) -> int` — reads the cross-session `hit_count` from the
  **landed corpus artifact** (the neo4j blueprint node keyed by `canonical_key`, D-OQ1), **not** the
  envelope. The `canonical_key` comes from the S6 `dedup` verdict; no dedup yet ⇒ count 0 (cannot
  promote by count — human approval is the alternative).
- `DependencyResolver.is_resolved(ref) -> bool` — the §11.6 `depends_on` guard. No deps ⇒ trivially
  resolved; deps present but **no** resolver ⇒ fail-closed (unresolved), never promote an unverifiable
  dependency.

`PromotionPolicy` (config knobs, provisional per D-OQ1): `blueprint_hit_threshold = 3`,
`drift_freshness_seconds = 86_400`, `replay_recheck_interval_seconds = 43_200`, `scan_limit = 200`,
`promotion_interval_seconds = 300`.
`HUMAN_GATED_TYPES = {global_knowledge, schema_edit}` (T = ∞); `user_knowledge` auto-commits in S8, not
here.

**Caveat: `PromotionPolicy` is never wired from settings.** All three factories accept `policy=`, and
no entrypoint passes one — production runs on the hardcoded defaults above. Wiring it from env is a
separate slice; until then, changing a knob means changing this dataclass.

### 5.1 Cost + fairness (the cron is a daemon, not a one-shot)

Every per-candidate cost is multiplied by 288 cycles a day, forever. Two properties keep that bounded:

**The golden replay is rate-limited; the cheap guards are not.** One replay = a JWT mint + two live
ClickHouse queries. Guard 3 reuses the STORED D43 verdict when it is inside
`replay_recheck_interval_seconds` (`drift.reusable_replay_verdict`), and the verdict is now PERSISTED
on a hold-after-replay so there is something to reuse. Guards 0–2 (`entity_scan`, static validation,
`depends_on`) are field reads and keep running on every examination, so a dependency that lands
mid-window is picked up on the next examination rather than the next window. Only a stamp that NAMES
`grain_integrity` is reusable — a `suspect` written by a user correction (`probes = ()`) says nothing
about whether the template still executes and never suppresses the real probe. On the validated side,
a fresh `clean` verdict skips the re-probe but the corpus self-heal re-assert still runs on every
examination (one idempotent Cypher; only the probe is expensive), and a `suspect` stamp is never
reused to demote — retracting a live recallable artifact must rest on a probe run now.

**The corpus re-asserts are keyed on STATUS, never on the drift stamp (§8.6/§9.4).** The
demote-direction re-assert in `_advance_candidate` fires for every landed-type candidate, not only a
`suspect` one. The old drift-keyed trigger could switch itself off permanently: a user correction
demotes with `suspect`/`probes = ()` while neo4j is down (write-back fails open, node left recallable),
Guard 3 correctly declines to reuse a correction stamp and probes for real, the replay PASSES (a
correction is about values, not structure — D98), the persisted verdict overwrites `suspect` with
`clean`, and the re-assert never fires again. Below the hit threshold nothing re-lands the blueprint
either, so the node would stay recallable forever against a store that says `candidate`. Every envelope
in that scan IS `candidate`, and a candidate-status landed node must be non-recallable whatever drift
says — a trigger a later write can erase is not a convergence guarantee.

**S9's bookkeeping writes are sub-document, not `put`.** `CandidateStore.touch_scanned` (scan cursor)
and `CandidateStore.stamp_drift` (replay verdict on a candidate S9 is not transitioning) are
`mutate_in` single-path writes with `preserve_expiry=True`. That closes three things a full-envelope
upsert would open: clobbering fields S9 does not own from a cycle-start snapshot; RESURRECTING a
document `supersede` deleted mid-cycle (`mutate_in` replaces, it does not upsert, so a missing document
is a swallowed no-op); and renewing the 90-day retention TTL on a schedule, which would make a
permanently parked candidate immortal. Genuine lifecycle writes (promote / demote / approve / retire)
still go through `put` and still renew the TTL — those are events, not bookkeeping.

`replay_recheck_interval_seconds` is deliberately a SEPARATE knob from `drift_freshness_seconds`, and
strictly smaller. They answer different questions ("how often do I pay" vs "how stale a verdict will I
trust"), and setting them equal guarantees a periodic gap: the stamp would expire exactly when the
re-check becomes due, dropping every validated blueprint out of `silent_eligible` for the scan lag,
every window. The invariant `replay_recheck_interval_seconds <= drift_freshness_seconds` is ENFORCED at
the point of use (clamped), so a misconfiguration can only make the scheduler probe more often — never
reuse a verdict beyond the window its own policy says it may be believed.

**The scan window rotates.** Both status reads use `list_by_status(..., order_by="last_scanned_at")` —
`ORDER BY last_scanned_at ASC, candidate_id ASC`, least-recently-examined first, never-examined
(MISSING) FIRST — and `_guard` stamps the cursor on EVERY examined candidate via
`CandidateStore.touch_scanned`. The `candidate_id` tiebreak makes the order TOTAL and identical in both
stores; it is not itself the fairness mechanism (fairness comes from the cursor advancing, which
confines ties to one clock tick — a clock frozen across cycles stalls the rotation by construction). The previous `created_at ASC` read handed
the bounded `scan_limit` window to the same oldest rows permanently, so past `scan_limit` held
candidates a newly extracted one was never examined at all (silent head-of-line starvation).

There is deliberately **no freshness predicate in the `WHERE`**. Ordering alone spends the LIMIT on the
most-overdue rows (nothing is fetched then discarded in Python), and the composite GSI
`idx_candidates_scan_rotation(status, last_scanned_at, candidate_id)` serves the equality + both sort
keys in index order with early LIMIT termination. Measured on couchbase 7.6.5: `IndexScan3
index_order=[keypos 1, keypos 2] limit=200`, no Order stage. All three index keys are required — with
the tiebreak against a two-key index the planner abandons it for the plain status index plus a full
sort, and `META().id` as the tiebreak keeps the index but still adds a sort. A cutoff would have to compare ISO-8601 timestamps as STRINGS, which is
only sound if every writer emits an identical offset format; one row stamped `+05:30` could be excluded
FOREVER, silently re-creating the starvation. It would also throttle the per-cycle demote-convergence
and self-heal re-asserts (§8.6) from every cycle to once per window, weakening a documented safety loop.

The cursor stamp makes a HOLD a write path, which it previously was not. It cannot clobber: it is a
sub-document write of one scheduler-owned path, issued AFTER the handler, on whatever the CURRENT
stored document is — so it can neither undo the handler's own `put` nor revert a concurrent S7 inbox
transition.

**Wording caveat.** Several comments (and earlier revisions of this section) say the re-asserts and
cheap guards run "every cycle". Post-rotation that is exact only while the backlog fits in
`scan_limit`; beyond that they run once per rotation period. Strictly fairer than before — the
overflow previously ran NEVER — but it is a rotation guarantee, not a per-cycle one.

---

## 6. Tests (Layer-1, §9 slugs)

| Slug | File | Proves |
|---|---|---|
| `S9-replay-not-a-value-oracle` | `test_replay_not_a_value_oracle.py` | single-session stays candidate; hit_count (not replay) promotes; value-changed-but-structural stays candidate; synthetic (not stored-entity) values bound. |
| `S9-silent-eligibility-predicate` | `test_silent_eligibility.py` | validated ∧ clean ∧ fresh ⇔ eligible; stale/suspect/candidate/unchecked each disqualify. |
| `S9-drift-suspect-demotes` | `test_drift_suspect_demotes.py` | suspect grain probe demotes validated→candidate + review flag; clean re-stamps; user correction demotes; phase-1 probe coverage. |
| `depends_on`-unresolved-stays-candidate | `test_depends_on_guard.py` | unresolved dep stays candidate; resolved allows promotion; missing resolver fail-closed; human-gated target never auto-promotes. |
| (state-machine coverage) | `test_human_review_and_guards.py` | human approve/reject; knowledge approves without replay; approval blocked on replay-fail; static-not-ok holds; kill-switch disables the cycle. |

All build on the frozen S4 fixture + a fake warehouse probe returning
`(row_count, distinct_grain_count, columns)`.

---

## 7. Frozen-contract ambiguities encountered

1. **Placeholder style mismatch (resolved locally, flagged).** The S4 fixture's
   `generalization.sql_template` uses sqlglot **colon** placeholders (`:department`), while the runtime
   binder (`template.py`) authors **brace** tokens (`{department}`) and the §1 mapping table maps
   `Blueprint.sql_template ← generalization.sql_template` directly. A brace-form template would not
   promote 1:1 onto the runtime binder without a normalization step. S9 normalizes colon→brace before
   reusing `bind_template`, so the one frozen style replays cleanly — but **S4 and the runtime loader
   should agree on ONE placeholder style** before promotion actually writes a `Blueprint` node.
   Recommend confirming with the S4 builder.
2. **`hit_count` has no fake store surface in Wave 0.** §11.1 puts `hit_count` on the landed corpus
   artifact (keyed by `canonical_key`), but no reader interface was frozen. S9 defines the
   `HitCountReader` port; the S6/corpus-loader track should expose a matching real reader.
3. **"Review flag" has no dedicated envelope field.** Contract E says "demote + review flag" but the
   additive envelope (D102) gives S9 only `status` + `drift`. S9 represents the flag as
   `status=candidate` + `drift.status=suspect`. If a distinct `needs_review` field is wanted, it must be
   added as another additive field (out of S9 scope).
4. **Retraction (D58b/D25) is out of scope (D-OQ4/S10).** S9 stops at *demoting* a drifted/leaked
   candidate; the physical pull-from-index + exposure trace is a later slice. `retire` is reserved in
   the `DecisionAction` set but not driven.
