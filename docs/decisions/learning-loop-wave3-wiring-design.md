# Learning-loop Wave 3a — the composition-root factory (write-router wiring)

**Status:** ACCEPTED (2026-07-03). Layer-1 only (fakes); Layer-2 (docker/live infra) is a
separate follow-on. This note documents the composition root that wires the six write-router
stages (S4–S8) + the promotion scheduler (S9) into the consumer. It changes NO stage internal and
NO consumer seam logic — it is purely additive (`learning/factory.py` + a full integration test +
minimal `__init__` exports).

**Builds on:** [D102](learning-loop-contracts-design.md) (the frozen `CandidateStage` seam + the
frozen stage order), the S3 all-or-nothing config gating precedent
([`scripts/run_learning_consumer.py`](../../scripts/run_learning_consumer.py) /
`tests/learning/test_consumer_config_gating.py`), and D58c (kill-switch dormancy + the no-import
invariant).

---

## 1. What was built

`src/data_agent/learning/factory.py` — three composition-root builders:

| Builder | Returns | Role |
|---|---|---|
| `build_learning_consumer(settings, *, session_store, queue, model_client, audit_store, candidate_store, blueprint_corpus, user_store, catalog_schema, embedder, git_client, semantic_scanner, checks, sampler, known_rules, tracer, summary_loader, triage)` | `LearningConsumer` | Assembles the S3 extractor + the SIX stages (frozen order) onto the consumer's `stages` seam. |
| `build_promotion_plane(settings, *, candidate_store, probe, hit_counts, dependency_resolver, policy, clock)` | `tuple[PromotionScheduler, ReviewInbox]` | The S9 plane from ONE store (the preferred wiring — pins the shared singleton). |
| `build_promotion_scheduler(settings, *, candidate_store, probe, hit_counts, dependency_resolver, policy, clock)` | `PromotionScheduler` | The SEPARATE S9 cron process alone (not a stage, §7.2). |
| `build_review_inbox(candidate_store, *, scheduler)` | `ReviewInbox` | The inbox alone; asserts `candidate_store is scheduler.store` (fail-fast on a split store). |

Plus `LearningWiringError` (fail-fast on a partial pipeline). All four are re-exported from
`data_agent.learning`.

Every collaborator is passed IN (dependency injection); the factory constructs **no** infra
clients itself — Couchbase/Redis construction stays in the process entrypoint, so Layer-1 fakes
and the live stack travel the identical path.

## 2. The frozen stage order (honored exactly)

`build_learning_consumer` builds the tuple in the D102 frozen order and passes it to the consumer:

```
generalize (S4) → leakage (S5) → dedup (S6) → schema_edit_pr (S8) → user_commit (S8) → writer (S7)
```

stage_ids: `generalize`, `leakage`, `dedup`, `schema_edit_writer`, `user_knowledge_writer`,
`writer`. The two target-specific writers run BEFORE the terminal `writer` and handle-then-stop
their own type (`schema_edit_pr` → `route_inbox` after stamping the PR marker; `user_commit` →
`drop` after the per-user commit), so the terminal writer only ever routes `blueprint` /
`global_knowledge`. The integration test asserts the tuple's stage_ids equal the frozen order.

## 3. Shared singletons (no split-brain store)

The factory threads the SAME instances through every stage + the consumer:

- **candidate store** — the leakage gate's `candidate_store` and the consumer's `candidates` are one
  instance. (The terminal `WriterStage` takes NO candidate store — the consumer's `_run_stages`
  owns the persist, so the writer's "store" IS the consumer's `candidates` by construction.)
- **user-knowledge store** — the leakage gate's reroute commit path and the S8
  `UserKnowledgeCommitStage` share one instance, so a rerouted per-user fact and an auto-committed
  `user_knowledge` land in the same per-user bucket.

A split-brain store would strand candidates mid-flow; the test asserts the identity
(`consumer._candidates is candidates`, `gate.candidate_store is candidates`, `gate.user_store is
user_commit.store is user_store`).

## 4. All-or-nothing config gating (critical)

Extraction is a UNIT, exactly as the S3 config gating established: the model client + a durable
audit store + a durable candidate store. Two branches, never a third (partial) one:

- **`model_client is None`** ⇒ the consumer is built with `extractor=None` and an EMPTY `stages`
  tuple — the S2 `would_extract` stub (behaviorally identical to the pre-Wave-3 consumer). No stage
  is built.
- **`model_client` present** ⇒ ALL of `audit_store`, `candidate_store`, `blueprint_corpus`,
  `user_store`, `catalog_schema` MUST be present, else `build_learning_consumer` raises
  `LearningWiringError` naming every missing piece. This is STRICTER than the S3 fallback (which
  fell back to the stub): a partial write-router pipeline would strand candidates part-way through
  the enrichment (the exact bug class S3's gating closed), so we fail fast at composition rather
  than silently drop stages.

A `{}` catalog is intentionally allowed (a degraded-but-safe choice: every blueprint then fails
static validation and routes to review, never auto-lands) — only `None` counts as missing.

**Deliberate stage-level fakes** (a stage's own null collaborator) default in place so the six
stages are always all-present-or-all-absent, never a per-stage gap:

| Stage collaborator | Default when unwired | Effect |
|---|---|---|
| leakage `semantic_scanner` | `NullSemanticEntityScanner` | regex/NER-only gate, fail-safe (no reroute) |
| dedup `embedder` | `_InsertOnlyEmbedder` (raises → soft layer degrades to `insert`, D52) | hard canonical-key dedup only |
| schema_edit `git_client` | `_NullGitPullRequestClient` (opens no real PR, returns a marker) | PR marker stamped + routed to review, never auto-commit |
| schema_edit `checks` | `AllPassChecks` | CI gate passes (real lint/dry-run deferred) |
| writer `sampler` | `WriterStage` default (random @ 0.10) | audit sample of clean blueprints |
| extractor `known_rules` | `frozenset()` — **logs a WARNING** at full-pipeline build | every rule-role blueprint plan declines `missing_rule` (MEDIUM-2). The entrypoint-adoption slice MUST pass `load_known_rule_ids()`; the warning makes the degraded default visible. |

## 5. Dormancy + the no-import invariant

The factory only **builds** the wiring; nothing runs until an entrypoint calls `run_forever` (or
`run_once`) AND the D58c kill-switch permits it. `LEARNING_ENABLED` is read FRESH per cycle inside
`run_once` (`config.learning_enabled()`, uncached) — the factory does not touch it. The integration
test proves that with the full pipeline wired, a disabled cycle runs no stage (empty candidate
store, work waits in the stream). The factory lives entirely under `learning/`; there is no
request-path (`runtime/`) import of the learning plane (the existing `test_kill_switch` structural
guard still holds).

## 6. The promotion scheduler + inbox linkage (S9)

The S9 scheduler is NOT a consumer stage (§7.2) — it is a standalone cron-scanned process, launched
by its OWN entrypoint via `run_forever(sleep=asyncio.sleep)` (or `run_once` from a cron trigger) — a
distinct process from the consumer daemon (D96 §g). Its warehouse probe, hit-count reader, and
`depends_on` resolver are injected (Layer-1 fakes = live path).

**`build_promotion_plane` is the preferred wiring.** The inbox reads the candidate store while its
`approve` DELEGATES to the scheduler's single `apply_human_decision` guarded path (R4), which
CAS-WRITES that store — so the inbox and scheduler MUST share one instance, else the approve reads a
stale envelope from one store and validates it in another (the same split-brain class the consumer
factory prevents). `build_promotion_plane(settings, *, candidate_store, ...)` takes the store ONCE
and returns `(scheduler, inbox)` wired from it, making the split impossible to express. The thin
`build_promotion_scheduler` / `build_review_inbox` remain for callers that need one alone;
`build_review_inbox` FAILS FAST (`LearningWiringError`) when `candidate_store is not scheduler.store`
(the scheduler exposes a read-only `store` property). The writer routes near-misses / human-gated
targets to `in_review`; the test drives a `global_knowledge` to `in_review`, then approves it through
the plane-wired inbox → `validated`, and pins the shared-store identity + the split-store refusal.

## 7. Integration test (`tests/learning/test_consumer_pipeline_integration.py`, 16 cases)

Drives a full KEEP session through `run_once` with the factory-built consumer (fakes for
model/embedder/warehouse/git). End-to-end assertions:

- clean blueprint → `candidate`, enriched (`payload.generalization` outcome `ok`, settled `pass`
  `entity_scan`, `insert` `dedup`, corpus seeded at `hit_count=1`);
- `global_knowledge` → `in_review` (human pre-gate), never `candidate`/`validated`;
- entity-leaking `global_knowledge` → `reject` terminal (settled `reject` verdict with hits);
- reroute (scripted `user_fact` scanner) → the per-user fact lands in the user store scoped to the
  session user (`user-42`), residual global → `in_review` (`reroute` scan);
- `schema_edit` → `schema_edit_review` PR marker + `in_review`;
- the frozen stage order, as its OWN independent guard (`test_stages_wired_in_frozen_order`);
- config gating (stub on no model client; `LearningWiringError` on a partial pipeline; the shared
  singleton identities);
- the promotion plane: guarded approve → `validated`, the plane pins ONE candidate store
  (`test_promotion_plane_pins_one_candidate_store`), and a split store is refused
  (`test_review_inbox_rejects_split_store`);
- S1 invariants with stages wired: kill-switch halts (no stage runs), CAS single-writer skip,
  redelivery `dedup_skip`, dead-letter after N (persistently-failing extraction).

## 8. Suite + lint

`uv run --extra dev pytest tests/learning -q` → **410 passed** (394 baseline + 16 new).
`uv run ruff check src/data_agent/learning` → clean. `ruff check` on the new test → clean.

## 9. Notes / follow-ons (not done here, by scope)

- **Entrypoint adoption.** `scripts/run_learning_consumer.py` is left UNCHANGED (it still does the
  S3-level gating and builds the consumer with an empty pipeline). Adopting `build_learning_consumer`
  there — and providing the durable `blueprint_corpus` + `user_store` + real catalog + git client —
  is a mechanical follow-on that also updates `test_consumer_config_gating.py` (which patches the
  entrypoint's module-level `LearningConsumer` and would need to patch the factory instead). Kept
  out of this additive slice to hold the baseline green.
- **`WriterStage` takes no candidate store.** The Wave-3a brief listed the writer as needing a
  candidate store; the actual `WriterStage(sampler, blueprint_inbox_sample_rate)` constructor does
  NOT — persistence is the consumer's `_run_stages` responsibility (`self._candidates.put`). So the
  singleton guarantee is satisfied by the consumer's `candidates`, and the writer composes cleanly
  as-is. No other stage constructor needed adaptation.
- **Layer-2.** A live docker run (real Couchbase candidate/audit/user stores, real Redis queue, real
  embedder/git) is the separate follow-on.
