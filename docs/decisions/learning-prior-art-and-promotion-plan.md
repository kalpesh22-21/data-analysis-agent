# Learning loop: prior art + promotion rework — plan

**Status:** slices 1, 1.x and 1.5 built and committed. Everything below them is designed, not built.
**Written:** 2026-08-10. Pick up from "Remaining slices".

---

## Why this exists

Two problems, found together.

**1. The loop cannot see what already exists, so it re-proposes it.** `DedupStage` compares a new candidate only against the `learning_corpus` Couchbase bucket — and that bucket is seeded *solely* by `DedupStage._seed_on_insert`. It therefore contains only what the loop itself minted. The neo4j `source='mcp'` canon and the `source='learning'` landed tier are invisible to it. A session that re-derives `bp-total-earnings-by-department` produces a duplicate candidate and nothing notices.

**2. `blueprint_hit_threshold = 3` is effectively unreachable.** The hard key is `sha256(resolves, uses_rules, result_grain, canonical_ast_norm)` — exact normalized-AST equality, not similarity. Three sessions must produce a byte-identical canonical AST. Two analysts asking the same business question through slightly different SQL mint different keys and never see each other.

The two compound: the loop pays a full extractor LLM call per session, produces candidates that duplicate existing artifacts, and parks them at a threshold nothing reaches. **No candidate has ever reached a human.**

### The reframe that drives the plan

`validated` in the learning tier lands as `source='learning'`, and recall serves only `source='mcp'`. The artifact is quarantined; nothing the agent does changes.

**So the hit threshold is not protecting the corpus. It is rate-limiting a human's attention.** That changes the question from *"is this correct enough to trust?"* — which the human verify/promote step answers — to *"is this worth 30 seconds of a human's time?"* For that question novelty is the right axis and frequency is a poor one.

---

## Status

| Commit | Slice | What |
|---|---|---|
| `79d318a` | 1 | Shared `structural_key` — a looser cross-tier key than the frozen D48 one; real embedder wired into `DedupStage`; `status`/`source` on `CorpusArtifact` |
| `f3c8949` | 1.x | Composite-path gaps between loop and runtime; the untrusted-JSON crash class |
| `fcb1808` | 1.5 | Replay cached to ~12h; scan rotates on a `last_scanned_at` cursor; `stamp_drift` narrow write |

Unrelated, committed alongside: `048876a` (prompt decomposition step), `3f76784` (backend/UI contract doc).

---

## Decisions, with rationale

These are the durable part. The slice list below is downstream of them.

**Volume is <20 sessions/day now, up to ~7000 later.** Everything threshold-shaped must be config, not a constant.

**Threshold → 1, ranked inbox, no cutoff.** At current volume a human skims the list in minutes. `review_score_cutoff` exists as a knob for when volume justifies one.

**The scheduler becomes a router, not a promoter.** Auto path is `candidate → in_review`; landing happens only in `apply_human_decision`. Rationale: `_recheck_validated` runs a golden-replay ClickHouse probe against every validated node. Auto-landing at T=1 would put hundreds of never-recalled nodes into that loop within weeks, and recall ignores them anyway.

**The judge drops outright above a configurable confidence bar.** User's call, taken with the risk stated (a wrong drop is invisible). Mitigation built in: every drop writes a durable record to `learning_audit` — session id, verdict, reason, covered-by ref, confidence — so drops are queryable rather than a log line.

**The judge emits a structured verdict**, `duplicate | existing-plus-delta | new`, as a typed field. This field *is* the dataset that decides whether atomic blueprints are worth building.

**Atomic blueprints in the learning loop: tabled.** Nothing has ever reached a human, so we would be spending a request-path change on a match-rate problem we have inferred rather than observed. **Trigger to revisit:** if judge verdicts skew to `existing-plus-delta`, atomic composition pays off; if they skew to `duplicate` or `new`, it buys only corpus tidiness.

**Runtime blueprint references: yes, via load-time inlining.** An early estimate assumed the executor would resolve references mid-DAG; that is the expensive version and it is not needed. Resolving at load and inlining the referenced SQL means the executor needs no changes, and the corpus is re-seeded as a unit so there is no staleness window.

**The extractor may restructure accepted SQL, never author it.** Extracted SQL inherits a human's acceptance — someone ran it, got an answer, accepted it. Authored SQL carries nothing, and there is no value oracle to recover it (D98). Splitting at a subquery boundary is restructuring; writing a new JOIN is authoring. The line is enforced by a recomposition-equivalence check.

**Composite edges are computed, not inferred.** The summary loader already fetches full results, so it can detect a scalar chain deterministically — query A returns a single cell, that value appears as a bare literal in query B, A precedes B. Hand the extractor the edges rather than asking it to guess. No verification gap, and the value never enters the prompt (pass refs, not the number).

---

## Remaining slices

### 2 — `PriorArtIndex`

The only genuinely missing read is neo4j. `learning_corpus` already covers in-flight candidates, because `_seed_on_insert` registers every minted artifact at `hit_count=1` before anything lands.

- neo4j reader with **no source filter** — the deliberate inversion of the recall trust gate. Wants a loud comment; it looks like a bug next to every other neo4j read.
- Fan-out over neo4j + `learning_corpus` behind one port.
- **Cards only, never payloads.** `extracted`-status candidates have not passed the leakage gate (S5 is stage 2), and `extractor_rationale` is never touched by `strip_entity_bearing` — it only redacts `payload` string leaves and blanks `entity_scan.hits[].span`. Card shape: `{id, source, status, verified, drift_status, intent, result_signature, uses_rules, structural_key, embedding_model, score, model_matched}`.
- **Replaces `_soft_layer`'s brute-force scan.** `list_artifacts()` is `SELECT c.*` with no WHERE and no LIMIT, and the soft layer embeds every artifact's intent per candidate. This is running in production today (the Helm ConfigMap sets `EMBEDDING_API_URL`; only local/compose runs have it dark), so it is a live cost, not a future one. neo4j's `db.index.vector.queryNodes` does the same job in one call.
- neo4j driver into the consumer process + a `build_learning_consumer` param. Only the scheduler and inbox service have one today; the embedding client is already wired.
- **Embedding-model parity.** The hydrator preserves learning-tier nodes across model swaps *without re-embedding* (`_EXISTING_MODELS` is scoped to `source='mcp'`). A cross-tier search must drop recall's `embedding_model = $expected_model` filter — that is the point — so it must return `embedding_model` and discount mismatches rather than trusting the cosine.
- New `redundant_with_canon` verdict → drop, **counted**. A high rate there is a *retrieval* defect surfacing in the learning loop: the agent isn't recalling something it already has.

**Prerequisite:** `CorpusArtifact` gained `status`/`source` in slice 1 but nothing writes them. Wire the scheduler's terminal transitions to stamp `status`, so rejected artifacts stop surfacing as live prior art.

### 2b — Loader blueprint references *(parallel with 2)*

- Reference field on the node; **load-time resolution and inlining**, executor untouched.
- Topological resolution, depth cap, **cross-blueprint** cycle detection (today's is per-blueprint).
- Slot mapping: parent slots → referenced blueprint's slots, plus collision rules. The fiddly part.
- **`uses` union rule, fail-closed.** `uses` is *authored*, not derived, and it mints the JWT scope — `corpus_loader._validate_blueprint_uses` calls it "the design's own highest-risk contract". The loader must compute the union of referenced footprints and **refuse to load** when a composite declares less. Get this wrong and a composite silently reads columns outside its declared scope. **This wants a security review, not a code review.**
- A reference to a missing or retracted blueprint fails the load.

### 3 — Extractor *(sub-sliced; the prompt and tool schema are rewritten once each)*

**3a — Vocabulary**
- PRIOR ART pre-fetch block (mandatory — guarantees the model always sees the closest match) + `searchCorpus` tool capped at ~3 calls (covers the multi-candidate case a single fetch misses).
- **`SLOT_TYPES` mirror drift.** `learning/extractor/models.py` has `{string, entity, enum, period, as_of_date, list}`; `runtime/blueprint/models.py` adds `relative_window` and `period_range`. Three of ten canon blueprints are un-relearnable. The exposure is worse than a decline: the prompt enum omits the correct answer, so a compliant model reaches for `period`/`string`/`entity` and those extract cleanly with no warning. Add a parity test — `NODE_KINDS` has one, `SLOT_TYPES` doesn't.

**3b — The judge**
- Pre-extraction: embed → retrieve → judge; skip extraction above `prior_art_skip_threshold`. A small judge call cancels a much larger extractor call, so at scale it *saves* money.
- Structured verdict (see Decisions). Drop above the confidence bar + durable audit record.
- Post-extraction adjudication only in the ambiguous cosine band (~0.70–0.97); outside it the answer is obvious and free.
- Keep the SHA-256 hard key as the deterministic race-safe layer. Record the verdict on the envelope so a redelivery never re-runs the LLM.
- Set the confidence bar **higher pre-extraction than post**: the pre-extraction judge sees a `SessionSummary` with raw SQL and literals, but no generalization, no parameterization, no `result_grain`. It is the cheapest place to drop and the least-informed one.

**3c — Composites**
- `composes` in the tool schema + prompt (today there is a `kind: single|composite` flag with no field to describe steps).
- **Computed scalar chains** in `SessionSummary` (see Decisions). The realizable case is real: `bp-departments-above-company-average-salary` is a canon blueprint a two-query session can genuinely produce.
- Static edge verification: a `consumes` placeholder must appear in the consuming template; every non-slot placeholder satisfied by a `consumes`; every declared `output` consumed; `feeds_from` agrees with the `$N` refs.
- Table-passing composites remain unlearnable until 2b — see Known limitations.

**3d — Restricted SQL restructuring**
- Extractor names a split; S4 performs the transformation; **recompose and assert equivalence to the accepted SQL**, else decline.
- Fixes the `_find_literal` gap: `rewrite.py::_find_literal` only searches `_COMPARISONS`, so a slot outside a comparison predicate (`INTERVAL {window_months} MONTH`, `COUNT(...) / {window_months}`) is never parameterized and stays inline. This caps cross-tier matching at 8/10 regardless of key quality.

### 4 — Promotion policy

- **`PromotionPolicy` is wired from nowhere.** All three factories accept `policy=`; no entrypoint passes one, so production runs on hardcoded defaults. Build it from `LearningSettings`. Knobs: `routing_threshold`, `review_score_cutoff`, `auto_land_score_threshold`, `recheck_verified_only`, `prior_art_skip_threshold`, `recurrence_weight`.
- Threshold → 1; `candidate → in_review`; landing only on human approve.
- Inbox ranking: novelty × groundedness × session-quality. Groundedness = share of parameterization that is `rule`/catalog-resolved vs. free-floating `inline` literals (computable today). Session quality = single-shot accept vs. long struggle, with `outcome == "corrected"` negative.
- **Novelty must be gated by groundedness.** The most novel candidate is usually the most idiosyncratic — high novelty + low groundedness is a hardcoded one-off, not a discovery. Measure novelty against *landed* artifacts, not sibling candidates, or the first sighting scores novel and its corroborations score redundant (order-dependent).
- Soft recurrence counter at `recurrence_weight = 0` — inert at 20/day, load-bearing at 7000. Build it now; retrofitting a counter with no history behind it is worse.
- **Rejection as negative memory.** The `BlueprintCorpus` protocol is `get / seed / increment / list` — no delete, no status write. A rejected candidate's artifact survives and keeps accruing hits, so the same declined idea returns indefinitely. **Prerequisite for loosening the gate.**
- Migrate ~14 test files off hardcoded `PromotionPolicy(blueprint_hit_threshold=3)`.
- **Verify this fixes the user-correction erasure** (see Known limitations) — it should, as a side effect.

### Later, with triggers

| Work | Blocked on / trigger |
|---|---|
| S4 scratch handling — a `extract_column_provenance_for_template` entry point taking known-local relation names | **2b**. Unreachable today. |
| Atomic blueprints in the learning loop | Judge verdicts skewing to `existing-plus-delta`. |
| Guard-4 negative signal (`corrected_at`/`correction_count`) | Confirm slice 4's routing change doesn't already fix it. |

---

## Known limitations, and where they are pinned

Two `strict=True` xfails — each flips to a CI failure the moment it is fixed:

- `tests/learning/extractor/test_slot_type_mirror_drift_qa.py::test_the_extractor_slot_type_mirror_matches_the_runtime` → 3a
- `tests/learning/generalize/test_scratch_join_s4_limitation_qa.py::test_the_loop_can_learn_the_canon_scratch_join_blueprint` → after 2b

Plus named `..._is_a_known_limitation` tests asserting current behaviour:

**Cross-tier key divergences** (`tests/runtime/blueprint/test_structural_key_known_gaps_qa.py`). Measured cross-tier rate after the aggregate-casing fold is **8/10** against a realistic twin pushed through the real S4 rewriter — and 8/10 is itself an upper bound, measuring a byte-identical query before these apply:

| Divergence | Exposure | Note |
|---|---|---|
| Rule predicates | 3/10 canon | `rewrite.py:142` drops `role=rule` predicates; canon inlines them *and* declares the rule. The two paths disagree about what the template *is*. |
| Slot naming | 9/10 | `{department}` renders as `{department: }`; the name survives, so `{dept}` mints a different key. Positional renaming would fix it; declined as a debatable semantic change. |
| Table aliases | 4/10 | `FROM ... AS e` normalizes differently from unaliased. |
| Operand order | unbounded | sqlglot canonicalizes boolean *form*, not operand order. |
| Grain qualification | — | `e.department` ≠ `department`. Left deliberately; stripping a qualifier is lossy. |
| `_find_literal` | 2/10 | The rewriter gap — a rewriter bug, not a key bug. → 3d |

**Scan-rotation tail** (`tests/learning/candidate/test_candidate_store_ordering_parity_qa.py`):
- A corrupt (array/object) cursor sorts *last* server-side and is permanently starved, while the in-memory fake ranks it first. Follow-up in flight.
- No secondary tiebreak: past `scan_limit` rows sharing a cursor value, the same prefix returns forever. Safe today only because `_now_iso()` has microsecond resolution. Follow-up in flight.
- A far-future cursor starves that row permanently — the deliberate consequence of ordering-without-a-cutoff.

**User corrections are erased in ~5 minutes** (`tests/learning/promotion/test_scan_rotation_and_correction_qa.py`). `apply_user_correction` demotes `validated → candidate` with a suspect stamp; the next cycle re-runs the guards, the replay **passes** (the correction was about a value; replay is structure-only by design), `hit_count` is unchanged, and it re-promotes. Confirmed end to end, with and without a landing writer. Slice 1.5's replay cache does **not** delay it — `user_correction_stamp` writes `probes=()` and verdict reuse requires `grain_integrity` among the probes, deliberately, so a correction cannot suppress the structural probe. **The feedback path does not currently work at all.**

---

## Measure before building

Both are cheap, and both would have changed a decision earlier in this work.

1. **`hit_count` histogram + candidates by status.** The baseline nothing has ever been measured against. Expect all 1s; if anything reached 3, that changes the story.
2. **Run a scalar-chain detector over existing sessions.** Counts how many could produce a composite. Decides whether 3c is worth building *before* building it.

---

## Method notes

Things that cost time this work, worth not repeating:

- **"Verified against pre-fix" claims did not survive checking, twice.** A 10/10 cross-tier match figure was circular — the twin was built by applying the transformation the fold inverts. A "7 of 18 tests fail pre-fix" figure was an 18/18 collection error. Confirm such numbers independently.
- **Live Couchbase verification is cheap here.** `docker-compose.integration.yml` ships couchbase 7.6.5. The MISSING-first collation the scan rotation depends on was settled by measuring it — `IndexScan3`, `index_order` keypos 1, no sort stage — rather than deferring to an integration test nobody runs.
- **Derive validation guards from downstream reads.** Four rounds on one slice kept finding the same class because each fixed the fields it was pointed at. See `untrusted-json-derive-the-guard` reasoning in `_plan_params_ok`'s docstring, which now carries the reader/operation table it was derived from.
- **Mirror constants drift.** `NODE_KINDS`, `SLOT_TYPES`, and `_TABLE_CONSUME_REF` (three copies) all drifted or duplicated. Prefer one definition plus an identity-based parity test over a copy plus a `.pattern` comparison.
