# Learning loop: prior art + promotion rework — plan

**Status:** slices 1, 1.x, 1.5, **2**, **2b**, **3a**, **3b** and **4** built. Everything below them is designed, not built.
**Written:** 2026-08-10. Pick up from "Remaining slices" (next: 3c — composites, or 3d — restricted SQL restructuring).

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

**The judge drops outright above a configurable confidence bar.** User's call, taken with the risk stated (a wrong drop is invisible). Mitigation built in: every drop writes a durable record to `learning_audit` — session id, verdict, reason, covered-by ref, confidence — so drops are queryable rather than a log line. **As shipped this is latent: `LEARNING_JUDGE_SHADOW_MODE` now defaults to `true`, so nothing is discarded** — see the shadow-mode bullet in §"What the build found" below.

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

**3a — Vocabulary — BUILT**

Landed as `learning/extractor/prior_art.py` (query builder, fail-open lookup, card
renderer, `searchCorpus` argument guard), a `searchCorpus` tool in `extractor/schema.py`,
a bounded tool loop in `extractor/extractor.py`, and the widened `SLOT_TYPES` mirror.

- PRIOR ART pre-fetch block (mandatory — guarantees the model always sees the closest match) + `searchCorpus` tool capped at ~3 calls (covers the multi-candidate case a single fetch misses).
- **`SLOT_TYPES` mirror drift.** `learning/extractor/models.py` had `{string, entity, enum, period, as_of_date, list}`; `runtime/blueprint/models.py` adds `relative_window` and `period_range`. Three of ten canon blueprints were un-relearnable. The exposure was worse than a decline: the prompt enum omitted the correct answer, so a compliant model reached for `period`/`string`/`entity` and those extract cleanly with no warning. Parity test added — `NODE_KINDS` had one, `SLOT_TYPES` didn't.

Decisions taken while building, each deliberate:

* **Widening the mirror alone would have been a REGRESSION.** The tool schema marks
  `binds_to` required for every slot, while `SlotSpec.parse` REFUSES a `binds_to` on a
  windowed type ("a windowed-period slot consumes no domain"). Offering the type
  without carving `binds_to` out would have traded a clean extraction-time decline for
  a `BlueprintParseError` raised at LANDING. So `binds_to` is nullable,
  `WINDOWED_SLOT_TYPES` is the one place the rule lives, and `_validate_roles` enforces
  both directions (windowed ⇒ must be null; everything else ⇒ must be present).
* **Golden replay keyed off the slot types too, and was wrong for both new ones.**
  `_sample_value` had one default branch ("every other type ⇒ a synthetic STRING"),
  which is a ClickHouse parse error for a `relative_window` (`INTERVAL '<str>' MONTH`)
  and a date-comparison error for a `period_range`. Worse, `_slot_types` was keyed by
  slot NAME while `_sample_bindings` looks up by BIND TOKEN, so both halves of a
  `period_range` missed the map entirely. Fixed by keying on `slot_token_names`, the
  runtime's own anti-drift helper — not a fourth hand-written `_start`/`_end` copy.
  Without this `relative_window` extracts but can never PROMOTE — a silent dead end.
* **`period_range` is WITHDRAWN, not shipped — only `relative_window` is relearnable.**
  QA found a SIXTH consumer: `generalize/rewrite.py::rewrite_sql_to_template` stamps
  ONE placeholder per matched literal, named after the slot, while the runtime's range
  grammar is two tokens (`{name}_start`/`{name}_end`). Nothing in the extractor or
  generalize packages knows that grammar exists. Both shapes a model can emit dead-end
  at landing (duplicate slot name / undeclared bind tokens) — and golden replay reports
  `passed=True` on the way there, because the fake probe never executes the SQL.
  So the type is withheld from the prompt enum (`UNSUPPORTED_SLOT_TYPES`) and declined
  at validation with a reason naming S4. The MIRROR stays at parity: drift (silently
  disagreeing about which types exist) and withdrawal (knowingly not offering one we
  cannot generalize) are different statements, and the enum is now a derived
  subtraction so re-enabling is one line. `relative_window` was verified by QA end to
  end through the real generalize stage, the real binder, the real landing gates and
  real ClickHouse. Pinned by `test_period_range_s4_rewrite_gap_qa.py` (the gap) and
  `test_period_range_withdrawn_qa.py` (the withdrawal).
* **The prior-art rules are appended to the system prompt only when an index is
  wired.** Describing a block that never arrives and a tool that is never offered
  invites a call the extractor cannot serve — and with no index that call burns one of
  the three malformed-response retries. A deployment with no graph keeps the tool list
  and turn count it had before this slice and sees no prior-art surface at all. Its
  system prompt is NOT byte-identical, and no claim that it is should be made: rule 5
  gained the windowed slot-type instructions, which every deployment needs.
* **A card is untrusted input to a PROMPT, which is a threat the port did not have.**
  `_card_from_record`'s readers were logs and comparisons; a prompt is line-oriented
  and delimiter-fenced, so every rendered field is flattened (control/format/line/
  paragraph separators → space), length-capped, and stripped of `=` runs so no card can
  spell the fence. The renderer is also total over any field type, because
  `PriorArtIndex` is a protocol and only one implementation has the coercing mapper.
* **`limit` is not a `searchCorpus` parameter.** The caller knows how many cards fit in
  the prompt; the model does not. One fewer untrusted number to clamp.
* **The search budget counts CALLS, not turns** (a model can request five searches in
  one turn, each an embed plus an ANN query per corpus), and a search turn does not
  consume a malformed-response retry (that budget is about bad output).

Not done, deliberately: `min_value`/`max_value` are not in the extractor's slot schema,
so a re-derived `relative_window` defaults to the resolver's `1..120` rather than the
canon's authored `1..36`. A loosening within the hard ceiling, not an escape — but it
means a relearned `bp-hires-per-month` is not byte-identical to the canon one.

**Net relearnability change: 1 of the 3 blocked canon blueprints, not 3.**
`bp-hires-per-month` and `bp-hires-projection` (`relative_window`) are now
relearnable; `bp-hires-in-range` (`period_range`) is not, and is blocked on the
rewriter slice below rather than on the mirror.

### 3a-follow-up — the two-token bind grammar in S4

Unblocks `period_range` (and any future multi-token type). NOT a suffix in the
rewriter:

- `parameterization` is one entry per literal predicate, each carrying its own `slot`,
  so there is no way to express "these two predicates are the two BOUNDS of one range".
  Needs a payload shape.
- `rewrite_sql_to_template` must emit `{name}_start`/`{name}_end` instead of `{name}`,
  which means it needs `slot_token_names` (or the arity it implies) rather than just
  `slot["name"]`.
- **The dangerous part is deciding WHICH bound each predicate is.** Inferring it from
  the operator (`>=` ⇒ start, `<` ⇒ end) is the obvious rule and it is wrong for
  `BETWEEN`, for reversed operand order, and for a range expressed with two `<=`. Get
  it backwards and the filter is silently inverted — the D56 wrong-answer class, on the
  learning plane where nothing re-checks values.
- Wants the equivalence check 3d builds (recompose and assert equality to the accepted
  SQL), which would catch an inverted bound mechanically. **Sequence it after 3d.**

**3b — The judge — BUILT**

Landed as `learning/judge/` (the two-stage `CoverageJudge`, its forced tool + response
guard, and the two briefs), `learning/audit/judgement.py` (the verdict vocabulary +
`JudgeRecord`), two new `AuditStore` methods, a `judge` field on `CandidateEnvelope`, a
`learning.judge` span, seven `LEARNING_JUDGE_*` settings, and the `learning_audit`
GSI + `query_select` grant that make the drop count a query.

- Pre-extraction: embed → retrieve → judge; skip extraction above `prior_art_skip_threshold`. A small judge call cancels a much larger extractor call, so at scale it *saves* money.
- Structured verdict (see Decisions). Drop above the confidence bar + durable audit record.
- Post-extraction adjudication only in the ambiguous cosine band (~0.70–0.97); outside it the answer is obvious and free.
- Keep the SHA-256 hard key as the deterministic race-safe layer. Record the verdict on the envelope so a redelivery never re-runs the LLM.
- Set the confidence bar **higher pre-extraction than post**: the pre-extraction judge sees a `SessionSummary` with raw SQL and literals, but no generalization, no parameterization, no `result_grain`. It is the cheapest place to drop and the least-informed one.

Decisions taken while building, each deliberate:

* **EVERY judgement is recorded, not only the drops.** The plan asks for a durable row
  per drop; recording only drops would have censored the dataset the field exists for.
  Drops are almost entirely `duplicate` by construction (that is the only verdict that
  may cancel work), so a drop-only store could never show an `existing-plus-delta`
  skew — which is the *stated trigger* for building atomic composable blueprints. The
  superset costs one KV upsert per judged session.
* **A verdict is never fabricated.** When the judge does not run (index unavailable, an
  empty corpus, a best score below the band) NOTHING is written, because a
  loop-invented `new` would be indistinguishable in the store from one a model gave.
  The consequence is that the store is the numerator and the `learning.judge` span is
  the denominator; the skip reasons exist only in telemetry.
* **The record is a PRECONDITION of the drop, not a consequence.** It is written first
  and the drop happens only if the write returned; a failed write converts a drop into
  a proceed. On the proceed path the same failure is swallowed — refusing to extract
  because an analytics row did not land would be a self-inflicted outage.
* **A drop needs five positive facts, not the absence of objections.**
  `verdict == duplicate` (only that one — `existing-plus-delta` says by construction
  there is an increment to keep), confidence at/above the bar, a `covered_by` that was
  actually in the block the judge was shown, a tier of `mcp`/`learning`, and an ORIGIN
  of `graph`. The membership test is the guard on the id and it handles `""`, an
  unknown id and a hallucinated id identically — derived from what `covered_by` is FOR
  (a human looks the artifact up), because an id nobody can resolve makes the audit row
  unauditable and the drop therefore unreviewable. The last two conditions are not new
  rules: `priorart/models.py` already states that an `unsourced` node "can never
  trigger the drop-the-candidate verdict" and that a `corpus` card (an unlanded
  in-flight sibling that may yet be rejected) "can at most route to a human". The
  origin is stored on the row, because it is the one drop condition a reader cannot
  infer from the others. A duplicate id resolves MOST-RESTRICTIVE-WINS, not last-wins,
  so a discard never depends on the order two producers were concatenated in.
* **The envelope stamp is NOT the idempotency mechanism**, and the docstring says so.
  A redelivery re-extracts and mints a fresh envelope with the field unset; what
  actually prevents the second (non-idempotent) model call is a `learning_audit` key
  derived from a CONTENT fingerprint over the exact brief the judge was shown. Keying
  the post stage on `candidate_id` (the first cut) bound a POSITION —
  `candidate::<content_hash>::<ordinal>`, and `_run_extractor`'s own comment says a
  re-extraction can emit "a different count/order" — so a reordered redelivery could
  serve candidate A the verdict rendered about candidate B, with the audit `reason`
  describing B. A content-keyed MISS costs one small judge call; a position-keyed false
  HIT costs a session. What is REUSED is the model's assessment; the drop gate is
  re-applied every time, so retuning a bar takes effect on the next delivery instead of
  being frozen into a stored outcome.
* **Shadow mode is a real flag, and an earlier draft of this document was wrong about
  it.** That draft said the safe rollout was expressible as a confidence bar above 1.0.
  It is not: pydantic and `JudgeConfig.__post_init__` both refuse a bar outside `[0,1]`,
  at exactly 1.0 the comparison is inclusive so a model asserting certainty still drops,
  and disabling the judge records nothing at all. So the one safe rollout path — run for
  a week, read the distribution, discard nothing — could not be configured while two
  documents asserted that it could. `LEARNING_JUDGE_SHADOW_MODE` now runs everything and
  forces the drop to False; the row carries `would_drop` and `shadow`.
* **Shadow is now the DEFAULT, not a temporary rollout setting** (`learning_judge_shadow_mode
  = True` in `learning/config.py`, `LEARNING_JUDGE_SHADOW_MODE: "true"` in the learning chart).
  This is a deliberate posture, not an un-flipped switch: the loop is human-gated end to end, so
  a prior-art-covered session costs one extraction that a reviewer skims, while a wrong drop is
  invisible forever. Every verdict is still recorded to `learning_audit`; the drop is the only
  thing suppressed.

  **The accepted consequence.** `judge.py` computes `drop = would_drop and not shadow` and then
  `outcome = DROPPED if drop else PROCEEDED`, so under shadow a would-drop is recorded as
  `proceeded`. `_persist_declined_for_review` gates on exactly that value, so a session the judge
  flagged as already-covered can now ALSO produce a `needs_parameterization` review row if its
  extraction then declines on parameterization. That is intended — the reviewer sees the judge's
  verdict and `covered_by` on the card and can reject in one click — and it is written down here
  so the next reader does not "fix" it by tightening the gate to `would_drop == false`.
* **Judge verdicts get their OWN retention** (`LEARNING_JUDGE_RECORD_TTL_SECONDS`,
  3 years). Inheriting the D95 180-day evidence floor would have erased the dataset about
  as fast as the skew signal accrues — the questions are quarterly. An evidence quote is
  entity-bearing and should expire; a verdict row is scalars plus one capped reason.
* **The drop precondition is an ACK, not durability**, and the docstring says so rather
  than implying otherwise. A plain Couchbase upsert returns from the managed cache;
  persistence and replication are asynchronous, so a node failure shortly after the ack
  loses the record after the drop was taken. Closing it means a server durability level,
  which costs latency on every judgement and is not exercisable on the single-node dev
  cluster — named, not implied away.
* **The recorded `model` follows the CLIENT, not the setting.** The field exists so the
  dataset never silently pools two judges; reading it off `LEARNING_JUDGE_MODEL`
  unconditionally would have defeated that for every caller that passes one model client
  and never looks at the setting (both demo scripts), stamping a model id that did not
  answer. A configured-but-unused setting is warned about loudly instead.
* **The knobs are on `LearningSettings`, NOT on `PromotionPolicy`.** The plan lists
  `prior_art_skip_threshold` among slice 4's `PromotionPolicy` knobs, but that class is
  accepted by all three promotion factories and passed by no entrypoint, so a knob
  added there today does not exist in production. A knob that silently cancels
  extractions must be live from the first deploy. Slice 4 should either move them or
  leave them; they must not end up in both places.
* **The post-extraction judge lives INSIDE `DedupStage`, not as a new stage.** The
  stage order is frozen (D102 §7.1), and this is a better-informed version of the
  decision the soft layer is already making: it needs exactly the cards the soft layer
  just retrieved, and a separate stage would either re-embed and re-search or
  adjudicate a set the routing never saw. `_soft_layer` now returns the whole union as
  a third element — an `insert` returns no *matched* card by design, but a 0.75
  near-match is precisely the ambiguous case.
* **The judge may only ever REMOVE.** It never softens a `merge`, never turns a
  `conflict` into an `insert`, never advances anything past a gate. A dropped candidate
  also never reaches `_seed_on_insert`, so no `learning_corpus` artifact is left
  accruing hits for work nobody kept.
* **A `learning.dedup` span is emitted for a judge-dropped candidate exactly as it
  would be otherwise.** It faithfully reports what DEDUP decided; the drop is on the
  `learning.judge` span. So a dedup span is not a claim that the candidate survived the
  pipeline — worth knowing before writing a Phoenix query over `action=insert`.
* **No retries on the judge call.** The whole justification is that it costs less than
  the call it cancels; a retry budget spends the saving to salvage an optimization. The
  extractor retries because its output IS the product.

**What the unit suite does NOT establish** — pinned as a tripwire in
`tests/learning/judge/test_judge_limits_qa.py`, in the house convention for a named
limitation (plain passing assertions about today's behaviour, not an xfail):

A scripted model client proves that a verdict is parsed, guarded, gated, recorded,
honoured and made idempotent. It proves NOTHING about whether a real model would call
the right session a duplicate — the one question the judge exists to answer is the one
question no test in that directory asks. This is the same shape as the golden-replay
lesson (`passed=True` on SQL ClickHouse rejects, because the fake probe never
executes). What would establish accuracy: a labelled set of real sessions scored
against the judge; a live probe against the real corpus and a real model; or a period
running with the pre-drop bar at 1.0 so verdicts are recorded and nothing is dropped.
None of the three is in this slice.

**Deployment:** re-run `scripts/learning-audit-init.sh` wherever `learning_audit` was
provisioned KV-only. It now grants `query_select` and creates
`idx_audit_judge_verdicts (record_type, judged_at)`. Without it the drops are still
recorded and nobody can count them, which defeats the mitigation. QA confirmed the dev
stack's `learning_audit` currently has **no indexes at all, not even a primary**, so the
dataset queries there answer by full scan until the script is run.

**Found by review + QA and fixed during the slice** (all pinned by regression tests that
were strict xfails until the fix landed):

* the post-extraction key bound a position, not content (the blocker above);
* `CoverageAssessment.from_doc` coerced an unknown stored verdict to `new` — inside the
  vocabulary — and `_judge` re-persists a reused assessment, so a corrupted row would
  have been permanently rewritten as a verdict no model ever gave. It refuses now, and
  the caller re-judges;
* `timeout_seconds` bounded the model call only, so a hung audit read or write stalled
  the judge — and the consumer — indefinitely, against this component's own "must never
  be the reason a session takes longer than it used to". All three I/O paths are
  deadline-bound; a write timeout counts as a failed write and therefore refuses a drop;
* `begin_turn_client` and `parse_assessment` sat outside `_ask`'s try, so a Protocol
  implementation returning `None` or a foreign object went dark: no fail-open log and no
  `learning.judge` span, which blinded the numerator/denominator cross-check for the
  whole class;
* `parse_assessment` iterated `tool_calls` with no container check (the eighth sighting
  of that class here — the correct version was already in the same package, in
  `lookup_prior_art`), and `_best_card` claimed totality on a type check that said
  nothing about the FIELDS: a non-str `id` crashed only on a confidence TIE, i.e. a
  latent load-dependent crash;
* a `BaseException` from a model client escaped every handler and killed the whole
  consumer batch, stranding the session at `processing`. `_ask` now catches it and
  re-raises exactly `CancelledError`, `KeyboardInterrupt` and `SystemExit`;
* a raising tracer between the record write and the return left a `dropped=true` row for
  a session that was then extracted — telemetry rewriting the one dataset the mitigation
  depends on;
* `best_similarity` was the score that opened the band, which need not be the score of
  the artifact the verdict names; `authorizing_similarity` records the latter.

**Corrected reasoning:** the key's motivating comment named a PEL redelivery after a
crash between `processing` and `done`. QA traced it — there is no
`processing → processing` edge, so the reclaim CAS-mismatches and never reaches the
judge. The key earns its keep on re-enqueues, peer races, pipeline re-runs and
re-extractions instead.

**3c — Composites**
- `composes` in the tool schema + prompt (today there is a `kind: single|composite` flag with no field to describe steps).
- **Computed scalar chains** in `SessionSummary` (see Decisions). The realizable case is real: `bp-departments-above-company-average-salary` is a canon blueprint a two-query session can genuinely produce.
- Static edge verification: a `consumes` placeholder must appear in the consuming template; every non-slot placeholder satisfied by a `consumes`; every declared `output` consumed; `feeds_from` agrees with the `$N` refs.
- Table-passing composites remain unlearnable until 2b — see Known limitations.

**3d — Restricted SQL restructuring**
- Extractor names a split; S4 performs the transformation; **recompose and assert equivalence to the accepted SQL**, else decline.
- Fixes the `_find_literal` gap: `rewrite.py::_find_literal` only searches `_COMPARISONS`, so a slot outside a comparison predicate (`INTERVAL {window_months} MONTH`, `COUNT(...) / {window_months}`) is never parameterized and stays inline. This caps cross-tier matching at 8/10 regardless of key quality.

### 4 — Promotion policy — **BUILT**

Landed as `promotion/models.py::policy_from_settings` + seven `LEARNING_PROMOTION_*` /
`LEARNING_DRIFT_*` / `LEARNING_REPLAY_*` / `LEARNING_REVIEW_*` settings, a `route`
decision action on the auto edge, `inbox/ranking.py`, `candidate/signals.py`
(`SessionSignals` + `NoveltyStamp` as additive envelope stamps), a
`recurrence_count` on `CorpusArtifact` with its own port and increment, and a shared
`tests/learning/promotion/helpers.py::promotion_policy` builder every migrated test file
now uses.

- **`PromotionPolicy` is now wired from `LearningSettings`** at all three factories
  (defaulted at the composition root, so an explicit `policy=` still wins). Knobs:
  `routing_threshold`, `recurrence_weight`, `review_score_cutoff`, `scan_limit`,
  `promotion_interval_seconds`, `drift_freshness_seconds`,
  `replay_recheck_interval_seconds`.
- Threshold → 1; `candidate → in_review`; landing only on human approve. Every
  correctness guard (leakage, static validation, `depends_on`, golden replay) unchanged.
- Inbox ranking: `novelty × groundedness² × session-quality`, applied to the review queue
  ONLY (the rejected archive and the Phase-3 validated listing are untouched).
- Soft recurrence counter shipped at `recurrence_weight = 0.0` and ACCRUED anyway.
- Rejection as negative memory was already built in slice 2; re-verified here at the
  loosened gate (`test_rejection_is_negative_memory_qa.py`).
- **The user-correction erasure is FIXED**, as the predicted side effect. The former
  known-limitation tripwire is now a plain passing assertion of the new behaviour with the
  full history in its docstring.

Decisions taken while building, each deliberate:

* **The knobs listed in the original sketch that were NOT built.**
  `prior_art_skip_threshold` stays on `LearningSettings` where §3b put it — the plan said
  move them or leave them, never both, and two homes for one threshold means somebody
  tunes the copy nothing reads. `auto_land_score_threshold` was NOT built: it is an
  escape hatch back to the auto-landing this slice exists to remove, and building it
  would have meant keeping the `_recheck_validated` cost problem alive behind a flag.
  `recheck_verified_only` was NOT built: nothing is validated yet, so it is a cost lever
  for a cost nobody is paying.
* **`_corroboration` floors the hit count at 1.** `_read_hit_count` returns 0 when S6
  could not mint a `canonical_key`. At T=3 that was indistinguishable from "not
  corroborated yet"; at T=1 it would have been the one class of candidate that could
  NEVER reach a human — the bug of this slice, in miniature. The candidate in hand IS one
  sighting, and a keyed first sighting reads 1 only because `_seed_on_insert` wrote that
  1 on its behalf. It changes no outcome at any threshold above 1.
* **Novelty is stamped by S6, not computed in the inbox.** The dedup soft layer has
  already paid for the embed and the ANN query, so measuring it there is free; measuring
  it at list time would be an embed per row per page view, against a graph that has moved
  on. It is derived from the GRAPH half of the union alone — never the `learning_corpus`
  half — because a sibling candidate from a concurrent session is not something we own,
  and counting it would score the first sighting novel and its corroborations redundant.
* **`measured` is a first-class flag on both stamps.** "The graph could not be consulted"
  and "nothing like this exists" are opposite claims and must never render as the same
  number — the same reasoning `PriorArtUnavailableError` encodes one layer down. An
  unmeasured axis contributes a constant, which preserves the ordering of the axes that
  WERE measured, and `RankedScore.measured` says so on the wire.
* **Groundedness enters the score TWICE.** A plain `novelty × groundedness × quality` is
  symmetric in its first two terms, so a hardcoded one-off nothing resembles would tie
  with a reusable template of a familiar question. Novelty is scaled by groundedness
  before entering the product (`novelty × groundedness²`), which is what "gated by
  groundedness" has to mean arithmetically.
* **`review_score_cutoff` filters the LISTING, never the routing.** A routing-time cutoff
  would be a silent terminal state — a candidate discarded for a score nobody recorded a
  decision about. A hidden row is still stored, still `in_review`, and reappears when the
  knob moves.
* **The landing gate now guards the approve edge ALONE.** Gating the route edge on it
  would park every candidate at `candidate` with reason `landing_unavailable` in any
  deployment whose neo4j is not yet wired — the inbox would never fill, which is the exact
  symptom this slice removes. Routing means the queue fills and each approve 503s honestly.
* **`DecisionAction` keeps the now-dead `promote`,** and `PromotionSweep.promoted` now
  reads 0 permanently. Removing it would leave a reader unable to tell a retired edge from
  a broken counter; `routed` is the counter that moved.

**Measured live** (read-only against the dev graph + the real embedder; the MCP rejects
every call so the routing edge itself could not be driven end to end). 10 canon
blueprints, `all-mpnet-base-v2`, the real `Neo4jPriorArtIndex`:

| query | best cosine | novelty |
|---|---|---|
| an exact re-derivation of a landed intent | 0.9998 | 0.0002 |
| a plausible NEW HR question | 0.7411 | 0.2589 |
| a genuinely unrelated question | 0.5869 | 0.4131 |
| total nonsense | 0.5342 | 0.4658 |

The stamp discriminates correctly, and it does NOT use the top of its range: sentence
cosines over English prose floor around 0.53, so novelty lives in ~`[0, 0.47]` and a
PERFECT candidate scores about **0.26**. `review_score_cutoff` must therefore be set from
measured data — a moderate-sounding 0.5 would hide the whole queue. Documented in the
knob's own description and pinned in
`test_the_score_does_not_use_the_top_of_its_range_is_a_known_limitation`.

**Found by review + QA and fixed during the slice:**

* **The unmeasured-novelty neutral inverted the whole ranking.** An unmeasured axis
  contributes 1.0 while measured novelty tops out near 0.47, so a candidate nobody could
  measure outranked every candidate we knew something about — permanently, and by
  construction rather than by accident, since a writer-routed `global_knowledge` item has
  neither a dedup verdict nor a parameterization and is therefore unmeasured on two of
  three axes. A non-zero cutoff then removed the honest rows first. Fixed by PARTITIONING
  the sort on `RankedScore.measured` before any score is compared, and by exempting
  unmeasured rows from the cutoff (a cutoff judges a score; an unmeasured row has none).
  The original "a constant preserves the ordering of the axes that were measured" was
  true within one candidate and false across candidates — which is the only place a
  ranking exists.
* **A non-zero cutoff that empties a non-empty queue now WARNS.** The knob is easy to
  misjudge precisely because the score does not use the top of its range.
* **`ReviewInbox` re-created the split-policy trap one level down** — `policy or
  PromotionPolicy()` silently gave a directly-constructed inbox cutoff 0.0 even when its
  scheduler was configured. It now inherits `scheduler.policy`.
* **The correction is CARRIED to the reviewer, not merely survived.** Routing to review is
  what stops a corrected artifact re-landing, but it also makes a human the only remaining
  gate — and the approve path re-runs static validation and the golden replay, neither of
  which can see a value error. `_advance_candidate` now captures the user-correction drift
  stamp BEFORE Guard 3 overwrites it (a passing replay is the expected outcome, so the
  evidence is destroyed in-flight) and carries `route_reason="user_corrected"` onto the
  envelope, the route decision and the inbox item. STICKY — never cleared by a later clean
  cycle.
* **`ranking.py`'s title stated the plain product** the module exists not to implement.
* **THE APPROVE EDGE HAD NO LEAKAGE GUARD AT ALL** (QA; three strict-xfails, now plain
  passing assertions). `_entity_scan_is_clean` was referenced exactly once in the
  scheduler — the auto edge's Guard 0 — and this slice narrowed landing to the approve
  edge, so the only remaining door into the corpus was the unguarded one. An empty span
  set switches off BOTH D17 layers at once and silently: `strip_entity_bearing` becomes a
  no-op and the landing writer's last-gate tripwire receives nothing to check, so raw
  entity-bearing text lands in a corpus the agent recalls from.

  **Filed as an accepted limitation and rejected as one on review.** A strict-xfail says
  "we know, we accept this, tell us when it changes" — right for the scratch-join gap,
  wrong for a live path carrying unscanned entity data into the recall corpus. Nothing has
  leaked (no session has ever run), but "accepted limitation" was the wrong label to
  commit.

  Fixed with `_entity_scan_is_actionable` as the approve edge's Guard 2, positioned as the
  PRECONDITION of the span capture and the strip rather than beside the structural guards.
  **The predicate is neither `settled` nor `clean`, and the first attempt at it was
  wrong.** A "settled" guard closes the `pending` symptom and leaves open a settled
  finding that names no spans — a scanner asserting a leak it did not localize, which
  `gate._decide` really does emit (`reroute` on a `user_fact` classification, `quarantine`
  as its fallback, neither consulting whether `hits` is empty). Deriving from what the
  strip CONSUMES gives the rule that covers both: an empty span set is safe **iff a clean
  `pass` explains the emptiness**.

  The asymmetry with the auto edge is deliberate policy, not an oversight. The auto edge
  demands a clean `pass` because nobody is looking. The approve edge admits a LOCALIZED
  finding, because D58b routes 100% of leakage near-misses to a human precisely so a
  person decides — demanding a pass would make `reason=leakage_near_miss` a permanently
  un-approvable dead end — and because a localized finding is the state in which the
  machinery works and the reviewer is genuinely informed. It refuses an unsettled scan
  outright: `_leakage_view` renders one as `result="pass"`, so a human "deciding with
  their eyes open" is reading a pass for a scan that never ran.

  Consequence, deliberate: both refused shapes are REJECT-ONLY (there is no re-scan action
  in the inbox). Reject stays ungated — it writes no content and is the only terminal move
  those candidates have.

  **Enumerated rather than spot-fixed**, since QA named only the blueprint branch:
  content-creating corpus writes are `land()` alone, reached only via `_land_and_promote`,
  reached only from approve's two branches (blueprint and `global_knowledge`) — both
  covered, because guards 1-4 run before the type split. `update_status`, `mark_verified`
  and `set_status` write a status string or a bool keyed by a deterministic id and cannot
  introduce content. `apply_retract`, `apply_verify` and `apply_promote` each require
  `status == validated`, which only approve produces, so they INHERIT the guarantee — an
  inheritance now pinned by a test, because it stops holding the day any other edge learns
  to write `validated`.

**Residues, named:**

- A **paraphrase of a rejected idea still returns** (pinned in
  `test_rejection_is_negative_memory_qa.py`). Negative memory is keyed; a different
  derivation of the same question mints a different key, the graph holds no node (a
  rejected candidate never landed), and the soft layer deliberately skips the dead
  artifact. The only mechanism that could catch it is a cosine drop, which this codebase
  refuses everywhere else. The honest fix is a rejection REASON in the review UI.
- The user correction is no longer erased and IS now carried to the reviewer
  (`route_reason`), but it is still **not counted**: there is no
  `corrected_at`/`correction_count`, so a second correction of the same artifact is
  indistinguishable from the first and nothing escalates. The passing replay still
  overwrites the `suspect` drift stamp — unavoidable, since the replay genuinely ran and
  genuinely passed.
- The soft recurrence counter has **no per-sighting idempotency**: a re-processed
  candidate (redelivery, re-enqueue, peer race, pipeline re-run) re-bumps every near
  artifact, so the stored count is sightings plus redelivery noise. Harmless at weight
  0.0; closing it needs a per-`(artifact, content_hash)` marker, a store change rather
  than a knob change. Noted beside the knob in three places.
- **The inbox LIMIT is applied by the store, before the ranking.** Past 100 `in_review`
  rows the caller ranks the oldest 100, not the best 100. Identical below that; fixing it
  properly means materializing the score and ranking server-side.
- The new `LEARNING_PROMOTION_*` settings are **not in the Helm values** (nor are §3b's
  judge knobs). The shipped defaults ARE the intended posture, so this is a gap in
  discoverability, not in behaviour.

### Later, with triggers

| Work | Blocked on / trigger |
|---|---|
| S4 scratch handling — a `extract_column_provenance_for_template` entry point taking known-local relation names | **2b**. Unreachable today. |
| Atomic blueprints in the learning loop | Judge verdicts skewing to `existing-plus-delta`. **Now measurable** (3b): `SELECT verdict, count(*) FROM \`learning_audit\` WHERE record_type = 'judge_verdict' GROUP BY verdict`. |
| Guard-4 negative signal (`corrected_at`/`correction_count`) | CONFIRMED: slice 4's routing change fixes the ERASURE. What remains is the AMNESIA — nothing tells the reviewer the artifact was corrected. Trigger: a reviewer asking "why is this back in my queue?" |
| A rejection REASON in the review UI | A paraphrase of a rejected idea still returns (slice 4 residue). |

---

## Known limitations, and where they are pinned

One `strict=True` xfail remains — it flips to a CI failure the moment it is fixed:

- `tests/learning/generalize/test_scratch_join_s4_limitation_qa.py::test_the_loop_can_learn_the_canon_scratch_join_blueprint` → after 2b

FIXED in 3a: `tests/learning/extractor/test_slot_type_mirror_drift_qa.py::test_the_extractor_slot_type_mirror_matches_the_runtime` was the second one. It is now a plain passing parity assertion; the file keeps the full before/after history in its module docstring.

FIXED in 4: QA filed THREE strict xfails in `tests/learning/promotion/test_nothing_auto_lands_qa.py` for the missing leakage guard on the approve edge. They were rejected as accepted limitations — a live path carrying unscanned entity text into the recall corpus is not something to label "known and accepted" — and all three are now plain passing assertions under their original names, with the history and the two-shapes-of-fix story in the module docstring. The count is back to one.

NEW in 3a — `period_range` is withdrawn, pinned in two places rather than xfailed (both are plain passing assertions on the CURRENT behaviour, per the house convention for a named limitation):

- `tests/learning/generalize/test_period_range_s4_rewrite_gap_qa.py` — the gap itself, proved by driving the real rewriter and the real landing gates. `test_every_template_token_s4_emits_is_a_declared_bind_token` is parametrized over the offered enum, so re-enabling a withdrawn type without teaching S4 its grammar fails there immediately.
- `tests/learning/extractor/test_period_range_withdrawn_qa.py` — the extractor-side consequence: known in the mirror, absent from the prompt enum, declined with a reason naming S4.

Plus named `..._is_a_known_limitation` tests asserting current behaviour:

**Cross-tier key divergences** (`tests/runtime/blueprint/test_structural_key_known_gaps_qa.py`). Measured cross-tier rate after the aggregate-casing fold is **8/10** against a realistic twin pushed through the real S4 rewriter — and 8/10 is itself an upper bound, measuring a byte-identical query before these apply:

| Divergence | Exposure | Note |
|---|---|---|
| Rule predicates | 3/10 canon | ~~`rewrite.py:142` drops `role=rule` predicates; canon inlines them *and* declares the rule. The two paths disagree about what the template *is*.~~ **SUPERSEDED 2026-08-18 (ISSUES H5/H6):** the rewrite no longer drops them — `role=rule` KEEPS the predicate and records the rule id, which is what canon always did, so the two paths converge and this divergence is closed. It was never a key gap: no normalization can invent a predicate one side deleted. Pinned by `test_a_rule_predicate_is_no_longer_dropped_so_the_tiers_converge`. |
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

**User corrections WERE erased in ~5 minutes** — FIXED in slice 4, and the test in
`tests/learning/promotion/test_scan_rotation_and_correction_qa.py` is now a plain passing
assertion of the fix rather than of the limitation. The description below is the history,
kept because none of its facts changed; only the DESTINATION did. What still does not
happen is any durable record of the correction (see slice 4's residues). `apply_user_correction` demotes `validated → candidate` with a suspect stamp; the next cycle re-runs the guards, the replay **passes** (the correction was about a value; replay is structure-only by design), `hit_count` is unchanged, and it re-promotes. Confirmed end to end, with and without a landing writer. Slice 1.5's replay cache does **not** delay it — `user_correction_stamp` writes `probes=()` and verdict reuse requires `grain_integrity` among the probes, deliberately, so a correction cannot suppress the structural probe. **The feedback path does not currently work at all.**

---

## End-to-end run, 2026-08-11 — what actually happened

Ten questions grounded in the real warehouse schema, driven through the live agent, then swept and consumed by the real learning loop.

**Worked.** All ten answered correctly, row-level security enforced and reported. The sweeper claimed and enqueued every session and survived four mid-flight process kills without loss or double-processing. Triage kept every question. Prior art retrieved five cards per session from the canon. **The coverage judge dropped three sessions correctly** — matched to `bp-active-headcount-by-department` at 0.97–1.00 confidence, extraction cancelled, each with a durable record carrying verdict, artifact, tier, the threshold in force, the model, and its reasoning.

**Did not produce candidates.** The configured extractor model was unavailable on the endpoint at hand, so a substitute ran and flattened the response envelope — payload fields at the top level, no `type`, declined as malformed. The *content* was right (correct intent, correct metric column, correct grouping, evidence cited); only the packaging was wrong. Re-run with the configured model to get a real mining result; the sessions are still in the store.

**The finding that matters.** Getting there required fixing **seven paths that had never once executed**, every one failing in a way that looked like correct behaviour:

| Path | Looked like | Was |
|---|---|---|
| Golden replay | clean `probe_unavailable` hold | token missing three tenant claims |
| Replay sampling | `passed=True` | fake probe never executes SQL |
| Sweeper + 4 other stores | "retrying next interval" | cluster connect never awaited |
| Recall | `blueprints: 0` | corpus wiped, `/ready` false for hours |
| Consumer idle loop | retries, work still drains | socket timeout races the blocking read at the same value |
| Approve-edge leakage guard | guard present on the cron edge | never added to the edge that now does all the landing |
| Tenant readiness gate | gate exists | defaults are the dev seed; blanking the chart alone is a no-op, since the template omits empty values |

They all fail *closed*, which is correct design and exactly why none were noticed: a gate that holds because it cannot verify is indistinguishable from a gate that holds because verification failed. **The paths that worked were the ones someone ran interactively and fixed when they broke in the foreground; the paths that didn't were daemons and offline jobs, where the failure is a log line nobody tails.**

Open, from this run: a structurally malformed tool call is a hard decline while a non-tool-call response is retried — so model drift yields zero candidates and reads as "nothing worth learning". Worth a corrective retry and a logged decline.

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
- **"Derive from the read" means the PREDICATE, not just the field.** The seventh sighting (3a) was subtler than the first six: the field was the right one and the guard still wrong, because it was written from the English intent ("a windowed slot must not declare a binding target") as `if binds_to:` while the downstream read is `binds_to is not None`. The two agree on every input except `""` — which is exactly what an "emit null" instruction produces — so the fix for extraction-clean-then-landing-raise reintroduced it. Mirror the OPERATOR, not the sentence. Corollary from the same fix: two branches whose English reads symmetric can legitimately need different predicates (`""` is a refusal for a windowed slot and merely unusable for every other type), so a shared helper would have been the wrong tidy-up.
- **A skip is indistinguishable from an absence.** Any layer that can only DROP what it does not understand must not be the layer that decides whether something exists. 3a's renderer skips a non-card member; the block would then have said "the corpus was searched successfully and nothing close was found" for a port returning raw records instead of mapped cards. The available/unavailable decision belongs in the ONE place that already distinguishes the failure shapes, and it must check members, not just the container.
- **Enumerate consumers of the CONCEPT, not readers of the field.** 3a's blast-radius enumeration listed five consumers of a slot's `type`, a reviewer independently confirmed all five, and both missed a sixth — `rewrite_sql_to_template`, which is a WRITER. It never reads `slot["type"]`; it depends on the type only through the ARITY of the bind sites that type implies, and spells the placeholder from `slot["name"]`. No grep for the field can find that. When widening a vocabulary, ask "what does each value IMPLY downstream?" and enumerate against the implication (here: how many `{tokens}` does this type occupy?).
- **Replay correctness is structurally unreachable by the unit suite.** The sampler's only job is producing values a real warehouse ACCEPTS, and the fake probe never executes SQL — so golden replay reported `passed=True` on templates ClickHouse rejects, and no amount of unit testing could have said otherwise. Anything whose contract is "the far system tolerates this" needs a live probe (`tests/integration/test_windowed_replay_clickhouse_live.py` is the pattern) or an explicit tripwire admitting the gap. Treat a green `ReplayOutcome` from the unit suite as evidence about BINDING, never about EXECUTION.
- **Mirror constants drift.** `NODE_KINDS`, `SLOT_TYPES`, and `_TABLE_CONSUME_REF` (three copies) all drifted or duplicated. Prefer one definition plus an identity-based parity test over a copy plus a `.pattern` comparison.
