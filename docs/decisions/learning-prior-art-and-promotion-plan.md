# Learning loop: prior art + promotion rework — plan

**Status:** slices 1, 1.x, 1.5 and **2** built. Everything below them is designed, not built.
**Written:** 2026-08-10. Pick up from "Remaining slices" (next: 2b or 3).

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
| `7860f45` | 1.5 fix | Total scan ordering behind a 3-key index; retracted a self-heal claim the system didn't have |
| `eebe516` | 2 | `PriorArtIndex` over the graph with the trust filter dropped; three-layer dedup; corpus status write-back |

**Slice 2 verified end to end against live data**, not just fakes: a candidate re-deriving a canon blueprint produces `action='redundant_with_canon', layer='structural'`. The cross-authoring-path key holds — hand-written canon YAML (`SUM(...)`, grain `[Department]`) and an LLM-authored template (`sum(...)`, grain `{"columns":["department"]}`) mint the same digest, and that digest is what the graph stores.

### Slice 2 follow-ups

- **The N+1 embed is back, deliberately.** The soft layer scans the whole corpus bucket again because a correct expensive answer beats a cheap wrong one. The principled shrink is to scan only the not-yet-landed subset — impossible today, because `CorpusArtifact.status` is written solely by the terminal transitions, so a landed artifact still reads `extracted`. Recording a landed stamp is the enabling fix.
- Untested: a stale-recipe split (canon re-derived under a new sqlglot while a landed learning node keeps an old digest). `structural_key_recipe` is stamped for exactly this and is still read by nothing.
- Untested: over-fetch truncation when the terminal post-filter eats a page. 12 nodes can't exercise it.
- Untested: two workers racing the same candidate through layers 2 and 3. Layer 1 is race-safe by hash construction; the others are not.

### Slice 2b follow-ups

- **An unbacked `scratch.<name>` read still loads on the DIRECT path.** Pre-existing, found by review of slice 2b but outside it. A blueprint whose template reads a canonically-spelled `scratch.<name>` with **no** backing table-consume declaring that placeholder loads clean — verified for both a leaf and a `composes` node. `_assert_source_tables_in_uses` deliberately skips `db == 'scratch'` (a table-consume is a session-gated source, not part of the warehouse `uses` footprint, §2.3), and nothing else on the direct path asks whether the placeholder is actually produced by an upstream node.
  **The runtime does NOT fail closed on this, and an earlier draft of this entry said it did.** `template._rewrite_scratch_tables` does raise on a placeholder missing from its binding map, but `bind_template` only calls it under `if table_bindings:` (`bind_template`, `template.py:484`) — and a blueprint with a `scratch.*` source and no table-consumes has an EMPTY map, so the guard is skipped entirely. Verified by execution, not read off the docstring (which overclaimed and has been corrected): `bind_template('SELECT x FROM scratch.nobody_makes_me', {}, table_bindings={})` raises nothing and emits the SQL unchanged. So the executor **dispatches a real query** at a nonexistent scratch table; it dies at the warehouse or at the MCP's scratch ownership check — two layers further out than "the rewriter refuses", and via a sent query rather than a refusal.
  **Still not a scope escape.** Every outer stop is an access gate, so the caller reads nothing it could not otherwise reach. The consequence is an *unrunnable blueprint the corpus advertises as `validated`* — offered by recall, chosen, dispatched, and failed at the far edge — plus the wasted round trip and a failure surfaced from someone else's error message instead of ours.
  **Why it is worth closing anyway.** It is precisely the principle slice 2b's new reference gate articulates ("a referenced template may not read `scratch.*`, because nothing in the referencing DAG can materialize it"), enforced on the reference path and left open on the direct one. The closing rule is the converse of gate (h): gate (h) says *every table-consume must appear as a `scratch.<placeholder>` source*; the missing half is *every `scratch.*` source must be a consumed placeholder*. Both halves read the same exact-match `_scratch_placeholder_names` set, so it is a small symmetric addition in `_validate_blueprint_dag` — deferred, not forgotten, because it can retire currently-loading fixtures and wants its own slice.

### Dev-environment hazards (found while verifying slice 2)

- **There is a concurrent writer on the dev graph.** `scripts/run_ui_runtime_real.py` has been up since mid-July holding a driver with the self-heal enabled; the graph mutates between consecutive read-only queries. Stop it before running destructive suites, and treat A/B live-test counts as noisy.
- **Several live suites are destructive and some have no teardown.** `test_catalog_graph_live` wipes the graph and leaves a phantom node; `test_governed_corpus_live` leaves two clone nodes; `test_learning_corpus_landing_live` leaves a node the mcp-scoped GC can never reap.
- **Never invent a corpus checksum to "restore" state.** `load_corpus` skips the entire embed-and-write when the stored sha equals the content sha, so a hand-written value can never match and every self-heal re-embeds the whole corpus. Seed with an empty sha and let the hydrator stamp the real one.
- **Four live failures are stale assertions, not regressions.** `test_learning_corpus_landing_live` ×2 and `test_learning_knowledge_landing_live` ×2 assert "a landed blueprint must be recallable", which stopped being true when the trust gate began serving only the trusted partition. Worth fixing so the noise stops masking real failures.

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

### 2 — `PriorArtIndex` — **BUILT**

Landed as `learning/priorart/` (port + card + in-memory fake + `Neo4jPriorArtIndex`), a third
DETERMINISTIC layer in `DedupStage` between the frozen hard key and the soft band, a neo4j
driver in the consumer entrypoint, and the scheduler's terminal-status write-back.

Deviations from the sketch below, each deliberate:

* **The soft layer is a UNION of two stores, not a fan-out behind the port.** The
  `PriorArtIndex` reads neo4j only; `DedupStage._soft_layer` unions its cards with a scan of
  the `learning_corpus` bucket and bands the merged set. The first cut treated the bucket as a
  FALLBACK (consulted only when the index raised) and that was a regression, not a residual
  gap: `_seed_on_insert` puts a candidate in the bucket long before it lands, so the bucket is
  the only place an in-flight sibling is visible, and QA measured a real paraphrase pair at
  0.9645 — above the merge threshold — silently becoming two inserts. The O(corpus)×embed cost
  is paid back deliberately. Shrinking it needs a landed stamp on the artifact (nothing
  distinguishes landed from unlanded today); that is the follow-up.
  The two sources are de-duplicated by the deterministic landing id (`bp::<canonical_key>`),
  with the graph copy kept — it is the richer projection.
* **`redundant_with_canon` fires on the STRUCTURAL KEY only, never on a cosine.** A cosine over
  intent prose is not an identity claim; a canon soft near-match routes to a human as `merge`
  and is counted separately (`action=merge AND prior_art_tier=mcp`).
* **`result_grain`, not `result_signature`, on the card** — the graph has no
  `result_signature` property, and the raw extractor field of that name is a leakage-scanned
  surface it would be dangerous to invite someone to populate the card from.
* **Verified live end to end.** All 10 canon blueprints carry a `structural_key` matching
  what the current recipe derives, and a learning candidate re-deriving `bp-overtime-by-
  department` drops with `action='redundant_with_canon', layer='structural'`. The
  cross-authoring-path fold holds against real data: canon YAML (`SUM(...)`,
  `result_grain: [Department]`) and an LLM-authored template (`sum(...)`,
  `{"columns":["department"]}`) mint the same key.

The only genuinely missing read is neo4j. `learning_corpus` already covers in-flight candidates, because `_seed_on_insert` registers every minted artifact at `hit_count=1` before anything lands.

- neo4j reader with **no source filter** — the deliberate inversion of the recall trust gate. Wants a loud comment; it looks like a bug next to every other neo4j read.
- Fan-out over neo4j + `learning_corpus` behind one port.
- **Cards only, never payloads.** `extracted`-status candidates have not passed the leakage gate (S5 is stage 2), and `extractor_rationale` is never touched by `strip_entity_bearing` — it only redacts `payload` string leaves and blanks `entity_scan.hits[].span`. Card shape: `{id, source, status, verified, drift_status, intent, result_signature, uses_rules, structural_key, embedding_model, score, model_matched}`.
- **Replaces `_soft_layer`'s brute-force scan.** `list_artifacts()` is `SELECT c.*` with no WHERE and no LIMIT, and the soft layer embeds every artifact's intent per candidate. This is running in production today (the Helm ConfigMap sets `EMBEDDING_API_URL`; only local/compose runs have it dark), so it is a live cost, not a future one. neo4j's `db.index.vector.queryNodes` does the same job in one call.
- neo4j driver into the consumer process + a `build_learning_consumer` param. Only the scheduler and inbox service have one today; the embedding client is already wired.
- **Embedding-model parity.** The hydrator preserves learning-tier nodes across model swaps *without re-embedding* (`_EXISTING_MODELS` is scoped to `source='mcp'`). A cross-tier search must drop recall's `embedding_model = $expected_model` filter — that is the point — so it must return `embedding_model` and discount mismatches rather than trusting the cosine.
- New `redundant_with_canon` verdict → drop, **counted**. A high rate there is a *retrieval* defect surfacing in the learning loop: the agent isn't recalling something it already has.

**Prerequisite:** `CorpusArtifact` gained `status`/`source` in slice 1 but nothing writes them. Wire the scheduler's terminal transitions to stamp `status`, so rejected artifacts stop surfacing as live prior art.

### 2b — Loader blueprint references — **BUILT**

Landed as `corpus_loader.resolve_blueprint_references` (the first step of `load_corpus`'s
pre-write pass), `NODE_REF_KEY` + a reject-an-unresolved-reference backstop in
`blueprint/models.py::Node.parse`, and a canon conversion: `bp-employee-check-detail-for-period`
extracted from `bp-compare-employee-check-detail-two-periods`, whose nodes 0 and 1 now
reference it with different `period` mappings.

- Reference field on the node (`ref: {blueprint, slots}`); **load-time resolution and inlining**, executor untouched.
- Topological resolution, depth cap (4), **cross-blueprint** cycle detection (today's is per-blueprint).
- Slot mapping: parent slots → referenced blueprint's slots, plus collision rules. The fiddly part.
- **`uses` union rule, fail-closed.** `uses` is *authored*, not derived, and it mints the JWT scope — `corpus_loader._validate_blueprint_uses` calls it "the design's own highest-risk contract". The loader must compute the union of referenced footprints and **refuse to load** when a composite declares less. Get this wrong and a composite silently reads columns outside its declared scope. **This wants a security review, not a code review.**
- A reference to a missing or retracted blueprint fails the load.

Decisions taken while building, each deliberate:

* **A reference target must resolve to exactly ONE SQL statement** — a leaf, or a
  single-node composite. Splicing a multi-node child would mean renumbering orders,
  rewiring `feeds_from`/`consumes`, and merging the sink's `when`/`output` with the
  referencing node's; each merge is a place a control-flow gate can be dropped silently.
  The single-node-composite case is what makes a reference CHAIN possible at all (a leaf
  holds no nodes and therefore no `ref`), which is what keeps the depth cap and the
  cross-blueprint cycle check live rules rather than dead code.
* **No implicit slot identity, ever** — `employee: employee` must still be written. Slots
  resolve once per blueprint before the DAG walk, so after inlining only the PARENT's slot
  declaration exists; an implicit bind would let a parent-side rename silently re-point a
  referenced filter at a different domain. Arity mismatch (`period_range` ⇄ scalar) is a
  hard error; a `type`/`binds_to` divergence is a warning, since the parent is the
  authority on its own slots either way.
* **A dead mapping is an error**, not a no-op — an author believing a filter is applied
  when it is not is the D56 wrong-answer class arriving from the other direction.
* **Rules are NOT inherited.** A referenced template's `resolve_via` bind name must be
  re-declared by the composite, for the same reason `uses` is authored: a rule fires a
  warehouse probe, and a composite must state every probe it causes.
* **Two gates the requirement list did not name**, both derived from what the child's SQL
  can reach: a reference may not cross the `source` trust partition (inlining copies SQL,
  so an `mcp` composite pulling from the `learning` staging tier would launder unverified
  SQL into the partition recall serves), and a referenced template may not read
  `scratch.*` (nothing in the referencing DAG can materialize it, and
  `_assert_source_tables_in_uses` deliberately skips `scratch.*` — so the escape would be
  a scope check passing on a table that does not exist).

**Correction to the framing above.** `uses` does not mint the *runtime* query's JWT — the
caller's `column_scope` does, and the MCP enforces it server-side. What `uses` actually
governs is (a) the recall scope pre-filter, which drops a blueprint whose `uses` ⊄ the
caller's scope, and (b) `promotion/token_minter.mint(column_scope=<uses>)`, the offline
golden-replay token. So the concrete exposure of under-declaring is that a composite is
OFFERED to users whose scope does not cover what it reads (the pre-filter silently
defeated), not that the query gains access it would not otherwise have. The union rule is
still right and still fail-closed; the reason is narrower than "privilege escalation".

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
- A corrupt (array/object) cursor sorts *last* server-side and is permanently starved, while the in-memory fake ranks it first — so the two stores report opposites and no unit test can catch it. Only a hand-edited or foreign-written document produces this; repair is manual. Documented in `candidate/models.py`, not fixed.
- **Tied cursors do not rotate, and a tiebreak does not fix that.** The ordering is now total (`last_scanned_at, candidate_id`, backed by a 3-key index that keeps `index_order` and LIMIT pushdown — the obvious 2-key version silently loses both). But under a *pinned* clock every row ties forever, so a total order only makes the same 200 come back deterministically rather than arbitrarily. What actually rotates the scan is **the cursor advancing**, which confines ties to one clock tick; any clock that moves between cycles rotates fine, including a coarse one. The real fix for the pinned case is keyset pagination. Production is not exposed — `_now_iso()` moves — but do not read the tiebreak as having closed it.
- A far-future cursor starves that row permanently — the deliberate consequence of ordering-without-a-cutoff. A cutoff would *drop* rows instead of mis-ordering them, which is strictly worse.

**Deployment:** `scripts/learning-candidates-init.sh` must be re-run wherever the earlier two-key `idx_candidates_status_scanned` was provisioned — it no longer matches the query and would leave the rotation read doing a full sort. The script creates the replacement before dropping the old one, so there is no window without a rotation index.

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
