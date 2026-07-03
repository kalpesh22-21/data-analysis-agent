# Learning-loop inter-stage contract freeze — Wave 0 (parallelize S4–S9)

**Status:** ACCEPTED (D102) (2026-07-03). This doc does not build anything. It **freezes the typed data
contracts** that flow between learning-loop stages S4–S9 so those slices can be built **in parallel
against fixtures** instead of sequentially. It concretizes the already-Locked contracts in
[05-memory-and-learning.md](../05-memory-and-learning.md) (candidate envelope; per-target payloads)
and [learning-loop-extractor-design.md](learning-loop-extractor-design.md) (the S3 `blueprint` PLAN)
— it does **not** re-decide them.

**The risk this closes.** Today `CandidateEnvelope.payload` is `dict[str, Any]`
([`candidate/models.py`](../../src/data_agent/learning/candidate/models.py):51). S4, S6, and S9 all
depend on the **sub-shape** of the enriched blueprint payload (S4 writes it, S6 hashes it, S9 replays
it). If that shape drifts after parallel builds start, three slices need rework. We freeze it now, as
Python-dataclass-level field specs, and hand each builder a fixture.

**Locks this builds on:** [D28](DECISIONS.md#memory--learning) (provenance envelope),
[D29](DECISIONS.md#memory--learning) (separate promotion scheduler), [D43](DECISIONS.md#blueprints)
(three drift probes / `drift_status`), [D48](DECISIONS.md#blueprint-dedup--resolvers) (canonical
dedup key), [D51](DECISIONS.md#blueprint-dedup--resolvers)/[D95](DECISIONS.md#memory--learning)
(evidence snapshot → `learning_audit`), [D52](DECISIONS.md#blueprint-dedup--resolvers) (shared
sqlglot parser; per-consumer fail behavior), [D56](DECISIONS.md#blueprint-silent-path-safety)
(grain-integrity verify gate), [D58a/b/c](DECISIONS.md#memory--learning) (knowledge human pre-gate /
blueprint sampled detection / kill-switch), [D69](DECISIONS.md#blueprints-uses-column-provenance)
(column-provenance `(database.table, column)` contract), [D87](DECISIONS.md#blueprints) (`uses`
byte-exact `database.table.column` scope keys + `:USES` edges), [D97](DECISIONS.md#memory--learning)
(total role classification), [D98](DECISIONS.md#memory--learning) (replay verifies structure not
values), [D101](DECISIONS.md#memory--learning) (`learning_candidates` store, queryable by `status`).
**New (this doc):** [D102](#9-new-decision) (Wave-0 contract freeze + additive envelope verdict
fields + the injected `CandidateStage` pipeline seam).

---

## 0. TL;DR — the five load-bearing freezes

1. **Envelope grows by ADDITIVE verdict fields, never by mutation of existing ones.** `entity_scan`
   (already present), `dedup`, `drift` become typed sub-records with a `pending`/`null` default so a
   candidate is valid at every stage. `payload` gains typed blueprint-enrichment fields (§1). The
   frozen S3 spine (`candidate_id`, `type`, `status`, provenance, `content_hash`) is untouched.
2. **S4's enriched blueprint payload maps 1:1 onto `runtime/blueprint/models.py::Blueprint`** (§1) so
   a promoted candidate is executable and S9 replay can run it with zero translation.
3. **Each stage plugs into ONE frozen seam** — an injected `tuple[CandidateStage, ...]` on the
   consumer, each stage defaulting to absent (§7). Builders add their stage in their **own module**;
   registration is a one-line composition-root change, so **no two builders edit `consumer.py`**.
4. **S9 is NOT a consumer stage** — it is the separate cron-scanned promotion scheduler (D29),
   reading `learning_candidates` by `status`. It shares the envelope contract but not the seam (§7.2).
5. **Every contract ships with a fixture** (§8) so S6/S9 build against a frozen S4 output before S4 is
   wired, and S5/S8 build with no dependency on S4 at all.

---

## 1. Contract A — S4-enriched blueprint payload (S4 WRITES · S6/S9 READ)

S3 emits a PLAN (`BlueprintPayload` in
[`extractor/models.py`](../../src/data_agent/learning/extractor/models.py):172). S4 is the
deterministic generalize + AST-rewrite + static-validate stage. It **adds** the fields below to the
blueprint candidate's `payload` and **mutates none** of the S3 fields. All new fields default to
`None`/empty so a pre-S4 candidate is a valid instance (fixtures + the S6/S9 read-side depend on
this).

```python
# src/data_agent/learning/generalize/models.py   (NEW — Track S4 owns this file)

@dataclass(frozen=True)
class BlueprintGeneralization:
    """What S4 computes and merges under payload["generalization"]. Entity-free
    by construction (it is derived only from the templated SQL, never the values)."""

    sql_template: str | None                 # single: top-level template. composite: None (per-node below)
    uses: tuple[str, ...]                     # BYTE-EXACT "database.table.column" scope keys (D87 /
                                              #   corpus_loader.BlueprintSeed.uses). Derived by joining
                                              #   the D69 provenance (database.table, column) pairs with '.'.
    uses_rules: tuple[str, ...]               # resolved catalog rule ids for role=rule locators (D48 input)
    node_templates: tuple[NodeTemplate, ...]  # composite only: one per composes[*].order (else empty)
    result_grain: ResultGrainStamp            # the D56 teeth — columns + verifiable (from result_signature.grain)
    static_validation: StaticValidation       # the dry-run stamp (below); gates promotion-eligibility
    canonical_ast_norm: str                    # sqlglot-normalized template text — the S6 hash input (D48)

@dataclass(frozen=True)
class NodeTemplate:
    order: int                                # matches ComposeNodePlan.order → runtime Node.order
    sql_template: str                         # this node's AST-rewritten template

@dataclass(frozen=True)
class ResultGrainStamp:
    columns: tuple[str, ...] = ()             # → runtime ResultGrain.columns / stored result_grain_json
    verifiable: bool = True                   # → runtime ResultGrain.verifiable (the §4.2 skip flag)

@dataclass(frozen=True)
class StaticValidation:
    explain_ok: bool                          # explainQuery dry-run parsed vs. current schema
    binds_to_subset_uses: bool                # every slot.binds_to ∈ uses (corpus_loader assertion)
    dag_ok: bool                              # composite: feeds_from/cycles/cap/terminal-approval/scalar-converge
    read_only_select: bool                    # single read-only SELECT; no '*', no dict-family funcs (D52)
    outcome: Literal["ok", "fail_to_review"]  # ANY false above ⇒ fail_to_review (D52/D97), never auto-promote
    reason: str | None = None                 # stable machine tag for the first failing check
```

**Mapping onto `runtime/blueprint/models.py::Blueprint` (the S9-executable target).** A validated
candidate promotes (Slice 9) into exactly this shape — no field is invented at promotion time:

| `Blueprint` field | Source | Notes |
|---|---|---|
| `id` | minted at S6 (`canonical_key`-derived) | — |
| `intent` | `payload.intent` (S3) | entity-free, embedded for retrieval |
| `resolves` | `payload.resolves` (S3) | — |
| `slots` (`SlotSpec[]`) | `payload.parameterization[role=slot].slot` (S3 `SlotPlan`) | 1:1; `SlotPlan` field names already match `SlotSpec` |
| `uses_rules` | `generalization.uses_rules` (S4) | — |
| `sql_template` | `generalization.sql_template` (S4) | single only |
| `composes` (`Node[]`) | `ComposeNodePlan` (S3) ⨝ `NodeTemplate.sql_template` (S4) | S3 gives step plan, S4 gives per-node template |
| `result_grain` | `generalization.result_grain` (S4, from `result_signature.grain`) | the D56 teeth |
| `:USES` edges (neo4j, not on the value object) | `generalization.uses` (S4) | written same-txn as the denormalized `uses` property (D87) |

**Stays in the S3 PLAN, NOT computed by S4:** `intent`, `kind`, `resolves`, `parameterization`
(roles), `source_tool_call_refs`, `accepted_signal`, `composes` step plan, `result_signature`
(shape + invariants + grain). **Computed by S4:** everything in `BlueprintGeneralization`. The
**golden = the entity-free `result_signature`** (D36/D98) — S4 does **not** add a separate golden
field; the recorded scalar answer is entity-bearing and never stored (D17).

**Fail-to-review is in-band, not an exception:** `StaticValidation.outcome == "fail_to_review"` is a
first-class value S7 routes on — a candidate that fails the AST rewrite is **not** dropped, it is
routed to the review inbox (D52). This is why `outcome` lives on the payload, not as a raised error.

**`when`-bearing composites are OUT of Wave-1 scope.** A composite node may carry a `when` precondition,
but the runtime's `WhenClause.parse` (`runtime/blueprint/models.py`) requires
`on_violation ∈ {abort, skip, ask}`, while the S3 plan's `ComposeNodePlan.when` is a **bare string** and
`BlueprintGeneralization.NodeTemplate` carries **no `when` field at all** — so a `when`-bearing composite
cannot promote 1:1 onto `Blueprint` without inventing a field. Therefore, in Wave 1, **S4 rejects a
`when`-bearing composite to review** (`fail_to_review`) rather than emitting a half-typed template; all
Wave-0 fixtures carry `when: null`. A typed `when` on `NodeTemplate` (bare-string → structured
`{expr, on_violation, message}`) is a deliberately-deferred later slice.

---

## 2. Contract B — Leakage verdict (S5 WRITES · S7 READS)

S5 (leakage gate, `GUARDRAIL` span, D58/D17) scans `payload.intent` + `payload.result_signature`
(blueprint) or `payload.statement` (global_knowledge) for entities. It writes the authoritative
`entity_scan` — the field **already exists** on the envelope, holding S3's preliminary `pending`
self-check ([`candidate/models.py`](../../src/data_agent/learning/candidate/models.py):56). S5
overwrites it. Freeze the shape:

```python
@dataclass(frozen=True)
class LeakageVerdict:           # serialized into envelope["entity_scan"]
    result: Literal["pass", "reroute", "quarantine", "reject"]
    hits: tuple[EntityHit, ...] = ()
    scanned_fields: tuple[str, ...] = ()   # e.g. ("intent","result_signature") — audit of what was scanned
    scanner: str = ""                       # "regex+ner+llm" — provenance of the verdict

@dataclass(frozen=True)
class EntityHit:
    field: str                              # where in the payload it was found
    kind: str                               # employee_code | dept_code | person | date | region | ...
    span: str                               # the offending substring (entity-bearing → audit store only if persisted)
```

- **`pass`** → continue (blueprint auto-lands `candidate`; sampled fraction + this-was-a-near-miss →
  inbox per D58b).
- **`reroute`** → the fact is entity-bearing but legitimately a `user_knowledge` fact → S5 spawns a
  linked `user_knowledge` candidate (`depends_on`) and marks this one `rejected`.
- **`quarantine`** → suspected leak, hold for human (near-miss → inbox, D58b).
- **`reject`** → hard entity in a global candidate → terminal `rejected`.

**Attaches at:** `envelope.entity_scan` (existing field; no envelope schema change). **Where hits'
`span` may live:** entity-bearing spans are **never** inlined into a global store, but the candidate
still sits in `learning_candidates` (audit posture, D101) pre-promotion, so a span may be recorded
there for reviewer context — it must be **stripped** before any promotion into neo4j/vector (D17).

---

## 3. Contract C — Dedup verdict (S6 WRITES · S7/S9 READ)

S6 is the D48 two-layer dedup. **Hard key** is race-safe by construction; **soft** embedding
near-miss goes to the inbox. Add a typed `dedup` sub-record (the 05 envelope contract names it;
today's envelope omits it — this is the additive add):

```python
@dataclass(frozen=True)
class DedupVerdict:             # serialized into envelope["dedup"]
    canonical_key: str          # hash(resolves, uses_rules, result-grain, normalized sql_template AST) — D48
    matched_id: str | None      # the existing artifact this collided with, or None
    similarity: float           # 0.0–1.0 (soft-layer embedding score on intent; 1.0 for a hard-key hit)
    action: Literal["insert", "increment", "merge", "conflict"]
    layer: Literal["hard", "soft"]   # which layer produced the verdict (hard = exact canonical_key)
```

**`canonical_key` hash inputs (D48, exact, order-fixed):**
`sha256( canonical_json([ resolves , uses_rules , result_grain , canonical_ast_norm ]) )` where
- `resolves` = `payload.resolves` (S3), keys sorted;
- `uses_rules` = `generalization.uses_rules` (S4), sorted;
- `result_grain` = `generalization.result_grain` (S4) — redundant defensive cross-check, subsumed by
  the AST per D48 (N4), kept for robustness;
- `canonical_ast_norm` = `generalization.canonical_ast_norm` (S4) — sqlglot optimizer/normalize
  output (identifier normalization, alias canonicalization). **This is the S4→S6 coupling point.**

**Actions:** `insert` (new key) · `increment` (hard-key hit → bump existing `hit_count`, DROP this
dup) · `merge` (blueprint near-match → merge golden) · `conflict` (knowledge contradiction / partial
overlap → **never auto-append**, → inbox). **Soft path:** embedding similarity on `intent` above the
near-miss band with a different hard key → `action` reflects the soft adjudication and the item is
routed to the inbox (human-adjudicated, race-tolerant). **Fail-soft (D52):** if the parser could not
produce `canonical_ast_norm`, S6 **skips the hard key** and falls to the soft layer — never a wrong
merge.

**Attaches at:** `envelope.dedup` (NEW additive field, defaults to `None` pre-S6).

---

## 4. Contract D — Review-inbox record (S7 WRITES/OWNS · human READS)

S7 provisions the review inbox and the writers. The inbox is a **projection over
`learning_candidates`** (queryable by `status`, D101) plus a typed view record — not a second store.
Freeze the reviewer-facing shape and the approve/reject transition:

```python
@dataclass(frozen=True)
class InboxItem:
    candidate_id: str                       # → the CandidateEnvelope in learning_candidates
    type: Literal["blueprint","global_knowledge","user_knowledge","schema_edit"]
    reason: Literal["knowledge_pre_gate",   # D58a — ALL global_knowledge
                    "schema_edit",           # D18 — never auto-commit
                    "leakage_near_miss",     # D58b
                    "blueprint_sampled",     # D58b
                    "dedup_conflict",        # D48 soft conflict/variant
                    "fail_to_review"]        # D52/D97 — un-rewritable / unclassifiable
    summary: str                             # entity-free one-liner (payload.intent / statement)
    payload_view: dict[str, Any]             # the (entity-free-where-required) payload for review
    evidence_refs: tuple[str, ...]           # KV keys into learning_audit (D51/D95) — reviewer fetches quotes
    entity_scan: LeakageVerdict              # what S5 found (drives reviewer attention)
    dedup: DedupVerdict | None               # what it collided with, if anything
    created_at: str

# The human action — the ONLY caller-driven state transition in the write router:
#   approve → status: in_review → validated  (knowledge becomes retrievable; blueprint confirmed)
#           → for schema_edit: opens the D53 bot PR (human MERGE is the real gate)
#   reject  → status: in_review → rejected    (archived as a NEGATIVE signal, D29)
#   retract → a post-promotion pull-from-index (leak found) → status: retired (D58b, D25 trace)
```

**Attaches at:** no new envelope field — `InboxItem` is derived from the envelope + its `status`.
S7's writer stage sets `status = in_review` for inbox-bound targets and `status = candidate` for
auto-landing blueprints (D58b) / `user_knowledge` (auto-commit, but that writer is S8's store).

---

## 5. Contract E — Promotion state machine (S9 WRITES · all READ)

S9 is the **separate** cron-scanned scheduler (D29) — not a consumer stage. It reads
`learning_candidates` by `status`, runs golden replay + hit-count + drift probes, and advances
`status` + stamps `drift`. Freeze the transitions and their guards (the `status` string set is
already frozen on `CandidateStatus`,
[`candidate/models.py`](../../src/data_agent/learning/candidate/models.py):22):

```
extracted ──(S5 entity_scan.result == pass)──────────────▶ candidate
extracted ──(S5 reject/quarantine)───────────────────────▶ rejected | quarantined
candidate ──(target ∈ {global_knowledge, schema_edit}    ▶ in_review        [D58a/D18: human pre-gate]
             OR reason == fail_to_review/near-miss/conflict)
candidate ──(golden-replay passes AND (hit_count ≥ T      ▶ validated
             OR human approval); guards below)
in_review ──(human approve)──────────────────────────────▶ validated
in_review ──(human reject)───────────────────────────────▶ rejected
validated ──(drift probes clean)─────────────────────────▶ validated  (drift.status = clean)
validated ──(drift probe suspect OR replay fails OR       ▶ candidate  (demote + review flag)
             user correction)
validated ──(schema/catalog drift makes it stale)────────▶ candidate | retired
* ─────────(retract: leaked artifact pulled from index)──▶ retired
```

**Guards on each promotion edge (D29/D98):**
- `candidate → validated` requires `generalization.static_validation.outcome == "ok"` **AND** a
  passing golden replay (`verify_result` green — grain-integrity + signature, D56/D98) **AND**
  (`hit_count ≥ threshold` **OR** human approval). **A single session's candidate stays `candidate`**
  (D98 layer iii) — replay alone never promotes.
- Replay verifies **structure, not values** (D98): a green replay is not a correctness proof. Do not
  add a value oracle (would breach D17).

**Drift stamping (D43)** — add a typed `drift` sub-record (NEW additive field):

```python
@dataclass(frozen=True)
class DriftStamp:              # serialized into envelope["drift"]
    status: Literal["clean", "suspect", "stale", "unchecked"] = "unchecked"
    last_drift_check_at: str | None = None
    probes: tuple[str, ...] = ()   # which of the 3 D43 probes ran: grain_integrity | catalog_conformance | rule_currency
    failed_probe: str | None = None
# silent_eligible ⇔ status==validated AND drift.status==clean AND fresh(last_drift_check_at) — D43
```

**Attaches at:** `envelope.drift` (NEW additive field, defaults `DriftStamp()` = `unchecked`).

---

## 6. The envelope, after Wave 0 (additive summary)

`CandidateEnvelope` keeps its frozen S3 spine and grows **three** typed verdict fields + typed
payload enrichment. `to_doc`/`from_doc` gain the three keys; all default so any stage's output is a
valid doc:

| Field | Type | Written by | Default (pre-stage) |
|---|---|---|---|
| `payload["generalization"]` | `BlueprintGeneralization` | S4 | absent |
| `entity_scan` | `LeakageVerdict` (was loose dict) | S5 | `{result:"pending"}` (S3) |
| `dedup` | `DedupVerdict \| None` | S6 | `None` |
| `drift` | `DriftStamp` | S9 | `DriftStamp()` (`unchecked`) |

**Rule (D102): additive only.** No stage rewrites another stage's field. `status` is advanced only by
the two lifecycle owners: the consumer pipeline (extracted→candidate/in_review/rejected) and the S9
scheduler (candidate↔validated/retired). This is what makes the stages commutative enough to build in
parallel: each owns one field + a `status` sub-range.

---

## 7. The wiring seam — one frozen injection point

### 7.1 Write-router stages (S4/S5/S6/S7) — the `CandidateStage` pipeline

Today `consumer._run_extractor`
([`consumer.py`](../../src/data_agent/learning/consumer.py):253) emits envelopes and `put`s them at
`status=extracted`. Freeze **one** new seam: an ordered, injected pipeline the consumer runs over
each freshly-extracted envelope, mirroring the S3 extractor DI (defaulted, stub-fallback, nothing
breaks unwired):

```python
class CandidateStage(Protocol):
    stage_id: str                                   # "generalize" | "leakage" | "dedup" | "writer"
    async def process(self, env: CandidateEnvelope, ctx: StageContext) -> StageResult: ...

@dataclass(frozen=True)
class StageResult:
    envelope: CandidateEnvelope                      # a NEW frozen envelope with this stage's field filled
    control: Literal["continue", "route_inbox", "drop", "halt"] = "continue"

# consumer __init__ gains ONE param, defaulted empty ⇒ current behavior unchanged:
#   stages: tuple[CandidateStage, ...] = ()
# _run_extractor, after put(extracted), runs: for stage in stages: env = stage.process(env, ctx)
```

**Why this freezes the seam:** each builder implements their stage in **their own module**
(`learning/generalize/`, `learning/leakage/`, `learning/dedup/`, `learning/writer/`) exposing a
`CandidateStage`. **Wiring is a one-line list registration** at the composition root (the consumer
factory / entrypoint), done **once** when the contracts land — builders never co-edit `consumer.py`.
Stage order is fixed here: `generalize (S4) → leakage (S5) → dedup (S6) → writer (S7)`. A stage
absent from the tuple is simply skipped (the current no-op).

**Ordering note:** S5 (leakage) reads only S3 fields (`intent`, `result_signature`), so it is
order-independent of S4 and could run first; we still place it after S4 so the single frozen order
serves every target. S8's `user_knowledge` auto-commit and `schema_edit` PR-bot are **writer-stage**
concerns (part of the S7 writer or a sibling writer stage), also plugged via `CandidateStage`.

### 7.2 The promotion scheduler (S9) — NOT a stage

S9 does **not** touch the seam. It is a standalone process (D29/D30: "cron-scanned state, not
queued") that reads `learning_candidates.list_by_status("candidate" | "validated")`, runs replay +
the 3 drift probes, and writes `status` + `drift`. It depends on the **envelope contract** (Contract
A payload for replay, Contract E for transitions) but shares no code seam with the consumer — so it
parallelizes cleanly against S4 fixtures.

### 7.3 Git-worktree isolation (recommended)

Give each track its own worktree so parallel builders never collide on the tree:
`git worktree add ../dae-s4 waveN/s4-generalize`, `../dae-s5-s8`, `../dae-s6-s9`, `../dae-s7-inbox`.
Each branches off the commit that lands **this contract doc + the additive envelope fields + the
`CandidateStage` seam** (the Wave-0 base). Merge order is contract-first, then any track order.

---

## 8. Dependency / parallelization matrix + fixtures

| Slice | Reads | Writes | True coupling | Build-in-isolation fixture |
|---|---|---|---|---|
| **S4** generalize | `payload` (S3 PLAN), catalog, sqlglot | `payload.generalization` (Contract A) | **Root of the fan-out.** Depends only on the frozen S3 payload (already shipped) + the D52 parser + catalog. **No downstream dep.** | `fixtures/s3_blueprint_plan.json` (a frozen `BlueprintPayload`, single + composite) — already emit-able from S3 today. |
| **S5** leakage | `payload.intent`/`result_signature`/`statement` | `entity_scan` (Contract B) | **Contract-INDEPENDENT** (confirmed). Reads only S3 fields; does not touch `generalization`. | `fixtures/s3_candidates_mixed.json` (a clean one + an entity-leaking one). No S4 needed. |
| **S6** dedup (hard) | `payload.resolves` + `generalization.{uses_rules,result_grain,canonical_ast_norm}`, embeddings | `dedup` (Contract C) | **Depends on S4 output shape** (confirmed) — the hard key needs `canonical_ast_norm` + `uses_rules`. Isolatable via the S4 fixture. | `fixtures/s4_enriched_blueprint.json` (a frozen `BlueprintGeneralization`) + `fixtures/existing_corpus_keys.json`. |
| **S7** inbox + writers | full envelope + `entity_scan` + `dedup` + `static_validation.outcome` | `status` transitions; `InboxItem` view (Contract D) | **Inbox store = independent infra**; the **blueprint-writer wiring waits on S4/S5/S6** (confirmed) because routing keys off their verdicts. Build the store + `InboxItem` projection now; wire routing behind the seam. | `fixtures/envelopes_each_reason.json` (one per `InboxItem.reason`). |
| **S8** user-store + schema-edit PR bot | `payload` (user_knowledge / schema_edit — Locked S3 shapes) | per-user store rows; D53 bot PR | **Contract-INDEPENDENT** (confirmed). Both payloads are Locked at S3 and carry no blueprint enrichment. | `fixtures/s3_user_knowledge.json`, `fixtures/s3_schema_edit.json`. |
| **S9** scheduler + replay | `payload.generalization` (template + grain + signature) | `status` (Contract E) + `drift` (Contract E) | **Depends on S4 output shape** (confirmed) — replay runs `sql_template`, verifies `result_grain`/signature. Isolatable via the S4 fixture + the existing `verify_result`. | `fixtures/s4_enriched_blueprint.json` (shared with S6) + a fake warehouse probe returning `(row_count, distinct_grain_count, columns)`. |

**Your analysis, confirmed with two refinements:**
- ✅ **S5 & S8 are fully contract-independent** — correct.
- ✅ **S6-hard-key & S9-replay depend on S4's output shape** — correct; both isolate against
  `fixtures/s4_enriched_blueprint.json`.
- ✅ **S7 inbox-store is independent infra; blueprint-writer wiring waits on S4/S5/S6** — correct.
- ➕ **Refinement 1:** S9 is a **separate process**, not a pipeline stage — its only coupling is the
  envelope contract, so it is *more* decoupled than the phrase "depends on S4" implies (fixture, not
  code, dependency).
- ➕ **Refinement 2:** S4 is the **single critical-path root**. Everything reduces to: freeze
  contracts → S4 + S5 + S8 + S7-infra start immediately (S4 only needs S3, which shipped) → S6 + S9
  start immediately too **against the S4 fixture** (they never wait for S4 to be *wired*, only for the
  fixture, which this doc supplies). So all six can start in the same wave.

**Recommended fan-out (3 builder agents):**
- **Agent 1 (critical path):** S4 generalize + static-validate → then S9 scheduler/replay.
- **Agent 2:** S5 leakage + S8 user-store/PR-bot (both independent, no waiting).
- **Agent 3:** S7 inbox store + writer seam + S6 dedup (S6 against the S4 fixture).

---

## 9. New decision + traceability

### Proposed **D102** (new — this doc)
**Wave-0 contract freeze.** (a) The candidate envelope evolves by **additive typed verdict fields
only** (`entity_scan`, `dedup`, `drift`) + typed `payload.generalization`; no stage mutates another
stage's field; `status` is owned by exactly two writers (the consumer pipeline and the S9 scheduler).
(b) Write-router stages plug into **one** injected `tuple[CandidateStage, ...]` seam on the consumer,
each in its own module, registered once at the composition root — so parallel builders never co-edit
the seam. (c) The S4-enriched blueprint payload (`BlueprintGeneralization`) is frozen and maps 1:1
onto `runtime/blueprint/models.py::Blueprint`. *Concretizes D28/D48/D101; does not re-decide them.*
No other new decision is required — every stage's semantics are already Locked (D43/D48/D52/D56/D58/
D97/D98); this doc only freezes the **wire format** between them.

### Proposed test slugs (TRACEABILITY rows to add on build)

| # | Invariant | Slug | Maps to |
|---|---|---|---|
| 1 | S4 `uses` is byte-exact `database.table.column` keys, `binds_to ⊆ uses` | `S4-uses-scope-key-subset` | D69/D87 |
| 2 | S4 un-rewritable SQL ⇒ `static_validation.outcome=="fail_to_review"`, never auto-promote | `S4-unrewritable-fails-to-review` | D52/D97 |
| 3 | S4 enriched payload maps onto `Blueprint.parse` with no missing field (round-trip) | `S4-payload-maps-to-runtime-blueprint` | D89/D102 |
| 4 | S5 entity in `intent`/`result_signature` ⇒ `entity_scan.result ∈ {reroute,quarantine,reject}`, never `pass` | `S5-leakage-blocks-entity` | D58/D17 |
| 5 | S5 `reroute` spawns a linked `user_knowledge` candidate + rejects the global one | `S5-reroute-to-user-knowledge` | D17/D58 |
| 6 | S6 identical semantics ⇒ identical `canonical_key` ⇒ `action==increment` (one create + one bump) | `S6-canonical-key-dedup` | D48 |
| 7 | S6 unparseable template ⇒ hard key skipped, falls to soft layer, never a wrong merge | `S6-failsoft-no-wrong-merge` | D48/D52 |
| 8 | S7 ALL `global_knowledge` + `schema_edit` route to inbox (`in_review`), never auto-retrievable | `S7-knowledge-schema-human-pregate` | D58a/D18 |
| 9 | S7 reject archives as a negative signal (`status==rejected`), not a delete | `S7-reject-is-negative-signal` | D29 |
| 10 | S9 single-session candidate stays `candidate` (replay alone never promotes) | `S9-replay-not-a-value-oracle` | D98/D29 |
| 11 | S9 `silent_eligible ⇔ validated AND drift.status==clean AND fresh` | `S9-silent-eligibility-predicate` | D43 |
| 12 | S9 suspect drift probe demotes `validated→candidate` + review flag | `S9-drift-suspect-demotes` | D43 |
| 13 | Envelope additivity: every stage's output is a valid `to_doc`/`from_doc` round-trip with the other stages' fields absent/default | `contracts-envelope-additive` | D102 |
| 14 | The `CandidateStage` seam: an empty stage tuple leaves S3 behavior behaviorally identical — no extra puts, additive keys only (stub fallback) | `contracts-stage-seam-noop-safe` | D102 |

---

## 10. Open questions to resolve BEFORE Wave-1 fan-out

1. **`hit_count` home & threshold (T).** S6 increments and S9 reads `hit_count` for the promotion
   guard, but no store field is specified. Is `hit_count` on the envelope (additive field), or on the
   corpus artifact once landed? And what is threshold `T` per target? (05 lists this as still-open.)
   **Blocker for S6↔S9 hand-off.**
2. **`canonical_ast_norm` normalization level.** D48 says "sqlglot optimizer/normalize". Exact passes
   (identifier normalize + alias canonicalize + qualify only, vs. full optimizer)? The hash is only
   as sound as this is deterministic across builds. **Blocker for S6 (and the S4/S6 fixture).**
   Recommend: pin the exact `sqlglot` transform list in the S4 fixture so S6 hashes a frozen string.
3. **`user_knowledge` auto-commit path (S8) vs. the seam.** User knowledge auto-commits (D17) — does
   its writer run as a `CandidateStage` (uniform) or a direct writer outside the pipeline? Recommend:
   a `CandidateStage` for uniformity, `control="drop"` after commit so it never reaches the inbox.
4. **Retraction mechanics (D58b/D25).** `retire` on a *post-promotion* leak needs to pull from
   neo4j/vector AND trace exposure (D25). Is that in S9's scope or a separate S-later? Flag scope now.
5. **Blueprint-sampled inbox fraction (D58b).** What sampled fraction of auto-landed blueprint
   candidates routes to the inbox? S7 needs the number (or a config knob) to build the router.
6. **`depends_on` gating in S9.** A blueprint `depends_on` a `schema_edit(add_rule)` (D35 missing-rule
   pairing) must be **blocked** until the rule lands. Which stage enforces the block — S7 (won't route
   to auto-land) or S9 (won't promote)? Recommend S9 guard: `depends_on` unresolved ⇒ stays
   `candidate`. Confirm before S9 build.

Questions **1 and 2 are hard blockers** for the S6/S9 hand-off and must be answered before those two
tracks start; 3–6 have safe recommended defaults and can proceed under assumption if needed.

---

## 11. Open-question resolutions (orchestrator, 2026-07-03) — FROZEN for Wave 1

All six OQs are resolved below. Values marked *(provisional)* are `RuntimeSettings`-style tunables
set to a default pending real traffic (same posture as the budget caps / resolveValues weights) — a
builder wires them as config knobs, not hard-coded constants.

1. **`hit_count` home & threshold T (blocker).** `hit_count` lives on the **landed corpus artifact**
   (the neo4j blueprint node, keyed by `canonical_key`) — **NOT** on the envelope. Rationale: it is a
   cross-session aggregate that accrues over the artifact's whole life, while an envelope is one
   session's candidate. S6 `action=increment` bumps the existing artifact's count (seeded at `1` on
   `insert`); S9's promotion guard reads the artifact's accumulated count. Thresholds:
   `blueprint_promotion_hit_threshold = 3` *(provisional)*; **`global_knowledge` + `schema_edit` never
   auto-promote by count** (human-gated, D58a/D18 — `T = ∞`); **`user_knowledge` auto-commits** (no
   threshold, D17). Human approval is always an alternative promotion path regardless of count.
2. **`canonical_ast_norm` normalization (blocker).** Deterministic, **schema-free**, pinned. S4
   produces it by: parse `sql_template` with sqlglot (`dialect="clickhouse"`) → `normalize_identifiers`
   (case-fold) → `normalize` (canonical boolean form) → alias canonicalization → render
   `.sql(dialect="clickhouse", normalize=True, pretty=False)`. **Do NOT run the full optimizer or
   `qualify`** — they need a schema and choke on slot placeholders, and are non-deterministic across
   sqlglot releases. **Slot placeholders normalize to a stable canonical token** (S4 defines the token
   form; it must survive round-trip unchanged). **Pin the `sqlglot` version EXACTLY in `pyproject.toml`**
   (`~=30.12`) so the hash is stable across builds/CI — a minor `sqlglot` bump can change the normalized
   render, silently mint a different `canonical_key`, and degrade a D48 `increment` into a spurious
   `insert` (a duplicate blueprint). The S4 fixture (`fixtures/s4_enriched_blueprint.json`) freezes the
   exact normalized string so S6 hashes a frozen value. Parse failure ⇒ **fail-soft** (skip hard key →
   soft layer, D52), never a wrong merge. **Composite join rule (S4 producer ↔ S6 hasher are different
   tracks — PIN it so the D48 key cannot diverge):** for a composite blueprint (top-level `sql_template`
   is `None`), `canonical_ast_norm` = the per-`NodeTemplate` normalized templates in ASCENDING `order`,
   joined by a single `\n` (newline). Single blueprints use the one top-level normalized template.
3. **`user_knowledge` auto-commit (S8).** Runs as a `CandidateStage` for uniformity, emitting
   `control="drop"` after the per-user-store commit so it never reaches the inbox. (Planner rec accepted.)
4. **Retraction mechanics (D58b/D25) — DEFERRED to a later slice (S10).** S4–S9 scope stops at S9
   *demoting* a drifted/leaked candidate (`validated→candidate`/`retired` status + review flag). The
   **physical pull-from-index of an already-promoted artifact + the D25 exposure trace** (who saw it) is
   a separate follow-up, not in this fan-out. S9 stamps `drift`/`status`; it does not implement index
   retraction. Flagged as a carried follow-up.
5. **Blueprint-sampled inbox fraction (D58b).** `blueprint_inbox_sample_rate = 0.10` *(provisional)* of
   auto-landed blueprint candidates → inbox, **plus 100% of leakage near-misses always** (a near-miss is
   never sampled out). S7 reads the knob.
6. **`depends_on` gating.** **S9 guard** (planner rec accepted): a candidate whose `depends_on`
   references an unresolved artifact (e.g. a blueprint depending on a not-yet-landed `schema_edit(add_rule)`,
   D35 missing-rule pairing) **stays `candidate`** — never promotes until the dependency resolves. S7
   still auto-lands it as `candidate`; S9 refuses to advance it.

**Wave-0 base commit (must land before any track branches):** this doc + the additive envelope fields
(`entity_scan` as the `LeakageVerdict` write-target shape, new `dedup: DedupVerdict | None`, new
`drift: DriftStamp`) with `to_doc`/`from_doc` round-trip + the `CandidateStage` seam on the consumer
(defaulted empty = current behavior behaviorally identical: no extra puts, additive keys only) + the
seven `fixtures/*.json` (§8's 7 files across 6 slices) + the `D102` rows in DECISIONS.md/TRACEABILITY.md.
No stage logic — pure contract + seam + fixtures.

**Carried follow-ups (not in Wave-0/1 scope):**
- **`halt` telemetry over-report (Wave-3 nit).** When a stage returns `control="halt"`, the consumer
  stops before the remaining candidates are built, so `learning.extract(candidate_count)` reports the
  *emitted* count, not the *processed-through-pipeline* count — a mild over-report. Harmless in Wave 0
  (no stage halts) and in Wave 1 (no stage is expected to halt mid-batch); revisit if a halting stage
  lands. Do not fix now.
