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
| ~~`build_promotion_scheduler(settings, *, candidate_store, probe, hit_counts, dependency_resolver, policy, clock)`~~ | ~~`PromotionScheduler`~~ | **SUPERSEDED (2026-08: deleted in cleanup Tier 2 — see docs/cleanup/WORKLOG.md #3; ISSUES L3).** Folded into `build_promotion_plane`. |
| ~~`build_review_inbox(candidate_store, *, scheduler)`~~ | ~~`ReviewInbox`~~ | **SUPERSEDED (2026-08: deleted in cleanup Tier 2 — see docs/cleanup/WORKLOG.md #3; ISSUES L3).** Folded into `build_promotion_plane`; the split-store guard was preserved verbatim there. |

Plus `LearningWiringError` (fail-fast on a partial pipeline). ~~All four~~ **The two surviving
builders** (`build_learning_consumer`, `build_promotion_plane`) are re-exported from
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
and returns `(scheduler, inbox)` wired from it, making the split impossible to express. ~~The thin
`build_promotion_scheduler` / `build_review_inbox` remain for callers that need one alone;
`build_review_inbox` FAILS FAST (`LearningWiringError`) when `candidate_store is not scheduler.store`
(the scheduler exposes a read-only `store` property).~~ **SUPERSEDED (2026-08: both thin builders
deleted in cleanup Tier 2 — see docs/cleanup/WORKLOG.md #3; ISSUES L3.)** No caller wanted one
alone, so `build_promotion_plane` is now the ONLY wiring and the split-store shape is not merely
guarded but unexpressible; the policy default and the split-store guard were folded in verbatim. The writer routes near-misses / human-gated
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

---

# Wave 3b-(i) — durable blueprint corpus + entrypoint adoption

**Status:** ACCEPTED (2026-07-03). Layer-1 build (fakes + a fake-cluster mapping test); Layer-2 live
tests written but env-guarded (no docker brought up here). Additive to the Wave-3a factory/stages —
no stage internal and no factory public-API change. This section closes the two Wave-3a §9 follow-ons
(the durable corpus + the entrypoint adoption).

## 3b.1 What was built

| File | Role |
|---|---|
| `src/data_agent/learning/dedup/couchbase_corpus.py` | `CouchbaseBlueprintCorpus` — the DURABLE `BlueprintCorpus` (replaces `InMemoryBlueprintCorpus` in production). |
| `src/data_agent/learning/config.py` | `learning_corpus_{connection_string,bucket,username,password,ttl_seconds}` (mirrors the candidate block; TTL default 0 = durable). |
| `scripts/run_learning_consumer.py` | Adopts `build_learning_consumer(...)` — replaces the hand-rolled S3-level construction. |
| `scripts/run_learning_scheduler.py` | The SEPARATE S9 promotion-scheduler process (D29), built via `build_promotion_plane`. |
| `scripts/learning-corpus-init.sh` | Provisions the dedicated `learning_corpus` bucket + a `learning_corpus_writer` RBAC user scoped to it only + a primary index. |

## 3b.2 The durable corpus — API + atomicity (the whole reason it is durable, D48)

`CouchbaseBlueprintCorpus(settings, cluster=None)` implements the `BlueprintCorpus` Protocol
(`get_by_canonical_key`, `seed_artifact`, `increment_hit_count`, `list_artifacts`) against its OWN
`Cluster` (authenticated as `learning_corpus_writer`), keyed by `canonical_key` under a namespaced KV
doc id (`corpus::<canonical_key>`). Import-guarded exactly like the candidate/audit stores. Two
load-bearing correctness invariants:

- **`increment_hit_count` is ATOMIC server-side.** It issues a sub-document counter mutation —
  `collection.mutate_in(doc_id, [subdocument.increment("hit_count", 1)])` — NOT a read-modify-write.
  Two concurrent sessions each hitting the same hard key both apply a server-side `+1`, so the count
  converges to `+2`; a read-then-put would race and lose an update, breaking the D48 "one create +
  one increment" invariant that feeds the T=3 promotion threshold. A hit on a vanished doc
  (concurrent retire) is swallowed as a tolerated no-op (`DocumentNotFoundException`), never a crash
  (fail-soft, D52).
- **`seed_artifact` is INSERT-WINS idempotent.** It does `collection.insert(...)` and catches
  `DocumentExistsException` → no-op, so two concurrent first-sightings yield EXACTLY one create. It is
  NEVER an `upsert` — a later seed attempt after an increment must NOT reset the accrued `hit_count`
  (`insert` is the invariant, not a style choice).

`get_by_canonical_key` is a KV get; `list_artifacts` is an N1QL scan (the soft dedup layer embeds
each artifact's `intent`). The store ALSO duck-types the S9 `HitCountReader` port
(`hit_count(canonical_key) -> int`, 0 when absent), so the promotion scheduler reads the SAME durable
artifacts S6 seeds + increments — one source of truth for the cross-session count. TTL defaults to 0
(no expiry): a landed artifact + its count must outlive any candidate/session.

## 3b.3 Consumer entrypoint adoption + the extended all-or-nothing gate

`scripts/run_learning_consumer.py` now builds the Wave-3 collaborators from real config and calls
`build_learning_consumer(...)`:

- `blueprint_corpus = CouchbaseBlueprintCorpus(settings)`; `user_store = CouchbaseUserKnowledgeStore`
  (its own `UserKnowledgeStoreConfig`); `catalog_schema = build_sqlglot_schema()` (the D69 loader);
  `embedder = HttpEmbeddingClient(...)` when the embedding API is configured else `None` (dedup
  degrades to hard-key-only, D52); `git_client` left to the factory null default (schema_edits route
  to review — real GitHub deferred, D53); `known_rules = load_known_rule_ids()` (the MEDIUM-2 fix);
  `sampler` default.
- **The all-or-nothing gate is EXTENDED** beyond the S3 (extractor/audit/candidate) unit to the full
  write-router: extractor model + durable audit + durable candidate + durable **corpus** + durable
  **user store** + catalog. Two branches only:
  - extractor model UNCONFIGURED ⇒ `build_learning_consumer(settings, session_store, queue, tracer)`
    with `model_client` omitted ⇒ the S2 would-extract stub, `extractor=None`, EMPTY `stages`.
  - extractor model CONFIGURED ⇒ every durable collaborator is built (`None` where its RBAC creds are
    absent) and handed to the factory, which FAILS FAST (`LearningWiringError`) on any missing piece.
    This is the stricter posture (vs. S3's silent fall-back): a partial write-router pipeline strands
    candidates mid-flow, so a partial config halts at composition rather than running degraded. Never
    a third, partial branch.

## 3b.4 The promotion scheduler process (D29, separate from the consumer)

The S9 scheduler is NOT a consumer stage (§7.2) — it is a distinct cron-scanned process. It is
launched by its OWN entrypoint, `scripts/run_learning_scheduler.py`, which builds the plane via
`build_promotion_plane(settings, candidate_store=CouchbaseCandidateStore, probe=..., hit_counts=corpus,
dependency_resolver=...)` (the plane pins ONE shared candidate store across the scheduler + inbox) and
runs `scheduler.run_forever(sleep=asyncio.sleep)` (or `run_once` from an external cron trigger). The
durable `CouchbaseBlueprintCorpus` is passed as the `HitCountReader`. It exits cleanly (a no-op) when
the candidate/corpus RBAC creds are unprovisioned. **Fail-closed + dormant:** the D58c kill-switch is
read fresh per cycle; and because the real `WarehouseProbe` (ClickHouse golden replay) and
`DependencyResolver` (neo4j `depends_on`) are DEFERRED infra with no production client yet, the
entrypoint wires fail-closed stubs — the probe raises, `golden_replay` CATCHES it and returns a clean
`reason="probe_unavailable"` outcome (so every blueprint HOLDS on the replay gate — never an uncaught
raise into the cron `_guard` traceback-spam path NOR out of the human `approve` path, §3b.9), the
resolver reports unresolved (⇒ dependent candidates HOLD). So the scheduler runs safely but
AUTO-PROMOTES NO BLUEPRINT until the real probe/resolver land. The human `approve` path SHARES that
replay gate for a blueprint (a blueprint approve is likewise blocked `probe_unavailable`, degrading
cleanly); only the human-gated targets (`global_knowledge`/`schema_edit`) approve without a replay and
so stay fully approvable via the S7 inbox. If the candidate/corpus RBAC creds are unprovisioned the
entrypoint exits NON-zero (a supervisor alerts on a misprovisioned deploy). The consumer entrypoint
runs the write-router; the scheduler is a separate process.

## 3b.5 Dormancy

Unchanged from Wave-3a: nothing runs until an entrypoint is launched AND `LEARNING_ENABLED` permits it
(read FRESH per cycle). `LEARNING_ENABLED` is not enabled by default; there is no request-path
(`runtime/`) import of the learning plane — both new entrypoints live under `scripts/` and import only
`learning/` + the shared infra clients.

## 3b.6 Tests

- **Layer-1** — `tests/learning/test_couchbase_corpus_mapping.py` (8 cases, fake async cluster, no
  infra): doc mapping round-trip + namespaced id; `get` maps/`None`; seed is insert-wins (a second
  seed does not clobber the count); increment issues ONE sub-document counter mutation (never a
  read-modify-write — no `get` is issued) and is fail-soft on a vanished key; the `HitCountReader`
  role. `tests/learning/test_consumer_config_gating.py` is UPDATED to drive the factory-based
  `_main()` (letting the REAL factory assemble the consumer, stubbing only `run_forever`): full
  six-stage pipeline when all collaborators are provisioned; stub (`extractor=None`, empty `stages`)
  when the model client is absent; `LearningWiringError` on a partial config (missing corpus / user /
  candidate / audit creds).
- **Layer-2 (written, env-guarded, NOT run here)** —
  `tests/integration/test_learning_corpus_store_live.py`: two CONCURRENT increments land EXACTLY `+2`
  (no lost update); two CONCURRENT seeds create EXACTLY one (no clobber); a late seed after an
  increment preserves the count; fail-soft increment on a vanished key; the RBAC boundary
  (`learning_corpus_writer` denied on `agent_sessions` + `learning_candidates`). Skips cleanly unless
  `RUN_COUCHBASE_TESTS` + the corpus env is set (no hang trying to connect).

## 3b.7 Suite + lint

`uv run --extra dev pytest tests/learning -q` → **422 passed** (410 baseline + 8 corpus mapping + the
gating test net +2 + 2 reviewer fold-in tests). Full suite `uv run --extra dev pytest -q` → **1841
passed, 98 skipped** (1829 baseline + 12). `uv run ruff check` → clean **repo-wide**. The corpus live
test skips cleanly with no env.

## 3b.9 Reviewer fold-ins (robustness — folded in before commit)

Two fail-soft holes the durable-corpus/scheduler wiring introduced, plus nits:

1. **Soft-layer `list_artifacts()` is now a fallible N1QL query — brought INSIDE the D52 degrade path.**
   With the durable corpus, `DedupStage._soft_layer`'s `await corpus.list_artifacts()` is a real scan;
   a missing primary index / down query service / malformed doc would previously ESCAPE the stage, fail
   the job, and dead-letter the session after `learning_max_deliveries`. Now it is wrapped in the same
   degrade-to-`insert` handling as the embed call (a DISTINCT warning naming the corpus index), so a
   corpus-scan failure degrades to `insert` (the race-safe hard-key layer already ran) and NEVER
   dead-letters. Guarded by `test_soft_layer_list_artifacts_failure_degrades_to_insert`.
2. **Deferred probe no longer raises through the human approve path.** `golden_replay` now catches ANY
   `probe.run(...)` failure → `ReplayOutcome(False, ..., reason="probe_unavailable")`, so BOTH the cron
   scan and `apply_human_decision` (→ `ReviewInbox.approve`) degrade to a clean hold instead of an
   uncaught `NotImplementedError` (a future inbox surface would have 500'd). This also removes the
   per-candidate `_logger.exception` traceback spam for the known deferred-probe hold (the raise is
   caught upstream, so `_guard` never sees it), while genuinely unexpected errors still hit `_guard`'s
   exception path. Guarded by `test_human_approve_blueprint_with_raising_probe_holds_not_raises`.
3. **Nits.** (a) `run_learning_scheduler.py` now exits NON-zero when the candidate/corpus creds are
   unprovisioned (a supervisor alerts rather than treating the immediate exit as a clean shutdown).
   (b) The traceback spam is resolved by fold-in #2. (c) `CouchbaseBlueprintCorpus.list_artifacts`
   carries a comment noting default (NOT_BOUNDED) query consistency + the O(corpus)-per-candidate scan
   as a known ceiling (the D27 RAG-over-corpus grounding replaces the brute scan, later).

## 3b.8 Notes / follow-ons (not done here, by scope)

- **Real `WarehouseProbe` + `DependencyResolver`.** The scheduler auto-promotion path stays dormant
  behind fail-closed stubs until a real ClickHouse golden-replay probe + a neo4j `depends_on` resolver
  are wired (they have no config/client surface yet).
- **Real GitHub PR client.** `git_client` remains the factory null default (schema_edits route to
  review) — real PR authoring is D53.
- **Corpus landing writer.** This slice provisions the durable corpus for READ (S6 lookup) + SEED +
  INCREMENT + hit-count read; the S9 promotion "land a validated candidate as a corpus artifact"
  writer (neo4j blueprint node materialization) is its own later concern.

# Wave 3b-(ii) — Layer-2 live proofs (S8 user store + full wired pipeline)

**Status:** ACCEPTED (2026-07-03). Layer-2 live: the two remaining Layer-2 gaps are now proven
against the real docker stack (l2-cb Couchbase, l2-redis Redis, l2-embedding). Closes the "Layer-2
(written, env-guarded, NOT run here)" deferrals from Wave 3a §9 and Wave 3b-(i) §3b.6.

## 3bii.1 What is now live-proven

1. **The S8 per-user knowledge store** (the ONE entity-bearing target, D17) against a real
   `user_knowledge` bucket — provisioned by the new `scripts/learning-user-init.sh` (bucket +
   `user_knowledge_writer` RBAC scoped to that bucket ONLY + a declared reader + primary index),
   mirroring `learning-corpus-init.sh` exactly.
2. **The full factory-built consumer pipeline** (generalize → leakage → dedup → schema_edit_pr →
   user_commit → writer) driving a KEEP session end-to-end through `LearningConsumer.run_once`
   against REAL infra — real Couchbase session + candidate + audit + corpus + user_knowledge stores,
   a real Redis jobs stream, a SCRIPTED extractor model double (deterministic, no LLM).

## 3bii.2 Provisioning — `scripts/learning-user-init.sh`

Creates bucket `user_knowledge` (128 MB), RBAC user `user_knowledge_writer` / `user-writer-pass`
scoped `data_writer[user_knowledge],data_reader[user_knowledge],query_select[user_knowledge]` ONLY
(no cross-bucket grant — the load-bearing D17 boundary), a declared `user_knowledge_reader`, and a
primary index (`list_for_user` is a `user_id`-parameterized N1QL scan). Idempotent. Verified live:
bucket present, both RBAC users set (writer with exactly the three scoped grants), primary index
`#primary` online.

## 3bii.3 Tests (live, skip-guarded, each RUN IN ITS OWN PYTEST PROCESS)

- `tests/integration/test_learning_user_store_live.py` — **4 passed**. A `UserKnowledgeRecord`
  round-trips (commit→get) with per-user scope/provenance/structured payload intact; two different
  `user_id`s NEVER cross-read (`list_for_user` returns only the asked-for user's rows — the D17
  no-cross-user-surface invariant on real N1QL); the RBAC boundary — `user_knowledge_writer` can
  write+read its own bucket (positive control) but is DENIED a write to `learning_corpus` AND
  `learning_candidates` (write probes, so a plain not-found can never masquerade as access).
- `tests/integration/test_learning_pipeline_live.py` — **3 passed**. Against fully real stores +
  queue:
  - a clean blueprint flows extract → generalize → leakage(**pass**, settled) → dedup(**insert**,
    corpus SEEDED at `hit_count=1` in real `learning_corpus`) → writer(auto-land **`candidate`**),
    readable back from the real `learning_candidates` store with `payload.generalization`
    (`static_validation.outcome == "ok"`), a settled pass `entity_scan`, and its `dedup` verdict;
  - a SECOND identical session (a different session id ⇒ a different D96 content hash, but the SAME
    deterministic `canonical_key`) INCREMENTS the durable corpus artifact to `hit_count=2` — the D48
    cross-session accrual, on real infra (the hard-key server-side counter);
  - a `global_knowledge` candidate lands **`in_review`** (the human pre-gate, D58a);
  - a reroute (scripted semantic scanner → `user_fact`) commits a per-user fact into the real
    `user_knowledge` store SCOPED to the session's AUTHENTICATED user (`user-42`, read back by its
    deterministic record id — R6/D17), and the residual global flows on to `in_review` with a
    settled `reroute` verdict.

## 3bii.4 Hermeticity notes (design decisions in the live test)

- **Per-run canonical keys.** The corpus is DURABLE (no TTL) and its `canonical_key` is DETERMINISTIC
  off the blueprint's resolved table/columns/AST. Each pipeline test parameterizes the payroll table
  with a per-run tag (`payroll.pf_<tag>`) so a leftover artifact from an interrupted prior run can
  never turn this run's genuinely-new `insert` into a hard-key `increment`. The two-session accrual
  test deliberately reuses ONE tag so the hard key collides and the count accrues 1 → 2.
- **Embedder left at the insert-only default (a DELIBERATE degrade, per the run guidance).** The
  insert/increment accrual assertions are a HARD-KEY proof; the S6 soft near-miss layer is not
  load-bearing for them. `EMBEDDING_TEST_URL` is OPTIONAL — wiring the real l2-embedding service was
  verified to also pass (the filtered corpus is empty in a hermetic run, so the soft layer
  short-circuits to `insert` and `embed` is never called). Omitting it is strictly MORE robust: the
  factory's `_InsertOnlyEmbedder` fail-softs a would-be soft comparison to `insert` (D52), so a leaked
  same-intent artifact can never spuriously `merge`. The real embedder path is proven separately by
  `test_embedding_api.py`. No store/pipeline/queue assertion was degraded — those are all real.
- **Cluster hygiene.** Each real store owns its own Couchbase `Cluster` (5 per pipeline test); the
  fixture CLOSES them all in teardown so the file never accumulates open clusters in one interpreter
  (the C-ext segfault guard). Each `*_live.py` is still run in its OWN pytest process, per the
  standing guidance.

## 3bii.5 Run env + results (exact)

- User store (own process): `RUN_COUCHBASE_TESTS=1`, `USER_KNOWLEDGE_CONNECTION_STRING=couchbase://localhost`,
  `USER_KNOWLEDGE_USERNAME=user_knowledge_writer`, `USER_KNOWLEDGE_PASSWORD=user-writer-pass` → **4 passed**.
- Pipeline (own process): `RUN_COUCHBASE_TESTS=1`, `COUCHBASE_CONNECTION_STRING=couchbase://localhost`,
  `COUCHBASE_USERNAME=admin`, `COUCHBASE_PASSWORD=password`, the `LEARNING_{CANDIDATES,AUDIT,CORPUS}_*`
  writer creds, `USER_KNOWLEDGE_*` writer creds, `LEARNING_REDIS_TEST_URL=redis://localhost:6379/0`
  (`EMBEDDING_TEST_URL` optional) → **3 passed**.
- Regression: `test_learning_corpus_store_live.py` re-run (own process) → **5 passed** (no
  interference from the new corpus traffic). Non-live suite `uv run --extra dev pytest tests/learning -q`
  → **422 passed** (unchanged — the new proofs are all env-guarded Layer-2, zero Layer-1 delta).
