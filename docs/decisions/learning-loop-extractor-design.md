# Learning-loop grounded extractor — Slice 3 (S3) design

**Status:** DESIGNED (2026-07-02) — build-ready; no code yet. This doc CONCRETIZES an already-Locked
contract (the `blueprint` payload, D34–D36; the shared header, D31). It does not re-decide it.

**Scope:** Track B, Slice 3 — the RAG-grounded, structured-output **extractor** that replaces the
Slice-1 no-op consumer's `_do_work` (see [learning-loop-infra-design.md](learning-loop-infra-design.md)
§15). It turns one accepted session into zero-or-more typed **candidate** envelopes. It emits the
*plan* only; the generalize/validate/leakage/dedup/promotion stages (Slices 4–9) enrich and gate it.

**Non-goals (later slices, named so the S3 boundary is exact):** the deterministic SQL-AST template
rewrite + `USES` + `explainQuery` + DAG validation (Slice 4); the leakage gate (Slice 5); the D48
dedup key + `id` (Slice 6); the writers + review inbox (Slice 7); the promotion scheduler + replay
(Slice 9). S3 stops at "emit a structurally-valid candidate envelope."

**Locks this builds on:** [D31](DECISIONS.md#memory--learning) (structured-output; no-evidence⇒reject;
shared header), [D34](DECISIONS.md#blueprint-extraction-payload) (lift-don't-generate; accepted-signal),
[D35](DECISIONS.md#blueprint-extraction-payload) (LLM classifies / deterministic stage rewrites;
slot|rule|inline trichotomy), [D36](DECISIONS.md#blueprint-extraction-payload) (golden = entity-free
signature + sampled-input replay), [D48](DECISIONS.md#blueprint-dedup--resolvers) (canonical dedup
key), [D51](DECISIONS.md#blueprint-dedup--resolvers)/[D95](DECISIONS.md#memory--learning) (evidence
snapshot → `learning_audit`, entity-free global stores), [D52](DECISIONS.md#blueprint-dedup--resolvers)
(shared sqlglot parser; D35-consumer = fail-to-review), [D56](DECISIONS.md#blueprint-silent-path-safety)
(grain-integrity verify gate), [D58](DECISIONS.md#memory--learning) (leakage gate + human-gate
posture). **New (this doc):** [D97](DECISIONS.md#memory--learning) (total role classification, no
drop role, unclassifiable→fail-to-review), [D98](DECISIONS.md#memory--learning) (replay verifies
structure, not values).

**Aligns to existing typed models** so the Slice-4 build maps cleanly, byte-for-byte:
`SlotSpec(name,type,required,binds_to,enum_values,optional_pattern)`, `WhenClause`, `Node`,
`ResultGrain(columns,verifiable)`, `Blueprint` in
[`src/data_agent/runtime/blueprint/models.py`](../../src/data_agent/runtime/blueprint/models.py); the
write record + its validations `BlueprintSeed` / `_validate_blueprint_dag` / `_validate_blueprint_uses`
in [`src/data_agent/runtime/retrieval/corpus_loader.py`](../../src/data_agent/runtime/retrieval/corpus_loader.py);
the verify gate `verify_result`/`VerifyOutcome` in
[`src/data_agent/runtime/blueprint/verify.py`](../../src/data_agent/runtime/blueprint/verify.py).

---

## 0. TL;DR — the four load-bearing commitments

1. **Full output schema** (§2–§3): the shared header + `blueprint` payload as build-ready models that
   map 1:1 onto the existing `SlotSpec`/`ResultGrain`. The extractor emits a **plan**, never SQL.
2. **Role classification is TOTAL and has no "drop" role** (§4, D97): every literal predicate gets
   exactly one of `slot | rule | inline`. A caller/session-specific predicate becomes an **optional
   slot** (`required:false` + `optional_pattern`), never an AST deletion. Unclassifiable ⇒
   **fail-to-review**.
3. **Division of labor** (§5): the extractor emits the plan; Stages 4–5 add `sql_template` (AST
   rewrite touching only classified locators), `explainQuery`, `USES`, DAG validation, leakage gate,
   D48 dedup, `id`. Un-rewritable SQL ⇒ **fail-to-review** (D52).
4. **Replay verifies STRUCTURE, not VALUES** (§6, D98): the golden is the entity-free
   `result_signature` (shape + invariants + grain), never the recorded scalar answer. Replay
   samples slot values and asserts grain-integrity + signature shape. It **cannot** catch a
   semantically-wrong-but-structurally-valid template. Value-correctness is a *layered* protection,
   not a replay oracle. **This is a first-class invariant — do not overstate replay.**

---

## 1. Where S3 plugs in

```
Slice-1 consumer.run_once():
  XREADGROUP > → idempotency check → CAS queued→processing
      → _do_work(job)              ← Slice 1: no-op.  Slice 2: loader+triage.  SLICE 3: extractor.
      → CAS processing→done → XACK
```

By the time `_do_work` reaches S3, Slice 2 has already loaded the still-live `SessionDoc` (the D30
message is a *reference*), normalized it to a `SessionSummary`, run cheap-LLM **triage**, and
provisioned the `learning_audit` store + `evidence_ref` KV client (D95). S3 receives a session that
triage judged **worth learning** and:

1. **Grounds** — RAG over the existing neo4j blueprint corpus + knowledge index + semantic catalog so
   it proposes only what is *not already represented* (D27) and knows which catalog rules exist (for
   the `rule` role).
2. **Routes** (§7) — classifies what the session revealed into one or more targets.
3. **Emits** typed candidate envelopes via **forced structured output** (tool-call schema; retry on
   mismatch — D31). A candidate with **no `evidence` is rejected** at emit (D31).
4. **Snapshots evidence** to `learning_audit`, keyed by `evidence_ref` (D51/D95); the candidate
   carries only the ref, never the entity-bearing quote.

S3 writes candidates at `status: extracted`. Nothing global auto-promotes.

---

## 2. Shared header (every candidate) — build-ready

Maps directly to D31. `EvidenceRef.turn_ref`/`tool_call_ref` point into the `SessionSummary`; the
`quote` is snapshotted to `learning_audit` and the stored candidate carries only the `evidence_ref`.

```python
# src/data_agent/learning/extractor/models.py   (Slice 3)

CandidateType = Literal["blueprint", "global_knowledge", "user_knowledge", "schema_edit"]
AcceptedSignal = Literal["no_correction", "thumbs_up", "explicit_confirm"]

@dataclass(frozen=True)
class EvidenceRef:
    turn_ref: int            # SessionSummary turn index
    tool_call_ref: str       # tool_call_id in the D46 tool trail
    quote: str               # snapshotted to learning_audit; NOT stored on the candidate

@dataclass(frozen=True)
class EntitySelfCheck:
    contains_entities: bool  # preliminary; the Slice-5 leakage gate is authoritative
    found: tuple[str, ...] = ()

@dataclass(frozen=True)
class CandidateHeader:
    type: CandidateType
    confidence: float                       # extractor self-assessment, 0.0–1.0
    evidence: tuple[EvidenceRef, ...]        # MANDATORY, non-empty — else REJECTED (D31)
    rationale: str                           # why worth learning AND why generic / not already stored
    proposed_action: str                     # "new" | "update_existing:<id>" | "reinforce:<id>" — HINT only (Slice-6 authoritative)
    entity_self_check: EntitySelfCheck
    depends_on: tuple[str, ...] = ()         # sibling candidate ids this one is blocked on (§7)
```

**Invariant (D31):** `len(evidence) >= 1` is enforced at emit; a zero-evidence candidate is dropped
before it ever reaches the audit snapshot. This is the primary guard against hallucinated learning.

---

## 3. `blueprint` payload — build-ready (the depth target)

The extractor emits the payload below; §5 lists what the later stages add. Field names match the
**Locked** payload in [05-memory-and-learning.md](../05-memory-and-learning.md) §`blueprint` payload —
this is the concrete-model form of it, not a parallel schema.

```python
@dataclass(frozen=True)
class Locator:                               # identifies ONE literal predicate in the accepted SQL
    table: str                               # "database.table"
    column: str
    value: str                               # the literal as it appeared (pre-generalization)

@dataclass(frozen=True)
class SlotPlan:                              # role=slot → maps 1:1 onto blueprint/models.py SlotSpec
    name: str
    type: str                                # ∈ SLOT_TYPES {string,entity,enum,period,as_of_date,list}
    binds_to: str                            # "database.table.column" — MUST be ⊆ uses (Stage-4 asserts)
    required: bool                           # False ⇒ optional slot (must carry optional_pattern)
    optional_pattern: str | None = None      # SQL fragment used when an optional slot is ABSENT
    enum_values: tuple[str, ...] | None = None

@dataclass(frozen=True)
class ParamPlan:                            # EXACTLY ONE per literal predicate (totality, §4 / D97)
    locator: Locator
    role: Literal["slot", "rule", "inline"]
    slot: SlotPlan | None = None             # role=slot
    rule_id: str | None = None               # role=rule  → an EXISTING catalog rule
    why: str | None = None                   # role=inline → why it is structural / metric-defining

@dataclass(frozen=True)
class ColumnShape:
    column: str
    type: str

@dataclass(frozen=True)
class ResultSignature:                      # ENTITY-FREE golden seed (D36 option A)
    shape: tuple[ColumnShape, ...]           # → verify_result(expected_columns=…)
    grain: ResultGrain                       # → stored result_grain_json; the D56 teeth
    invariants: tuple[str, ...]              # "row_count == 1", "total_earnings >= 0", "<grain> non-null"

@dataclass(frozen=True)
class ComposeNodePlan:                       # kind=composite ONLY — the step plan, NOT sql_template
    order: int
    node_kind: Literal["query", "approval"]  # matches models.py NODE_KINDS (D59c: guard cut)
    step_intent: str                         # NL; Stage-4 lifts each node's accepted SQL to a template
    feeds_from: tuple[int, ...] = ()
    consumes: dict[str, str] = field(default_factory=dict)   # "$N.name" | "$N"
    output: dict[str, str] = field(default_factory=dict)     # name → "scalar" | "table"
    source_tool_call_ref: str | None = None
    when: str | None = None                  # entity-agnostic predicate (D59); Stage-4 validates
    requires_approval: dict | None = None

@dataclass(frozen=True)
class BlueprintPayload:
    intent: str                              # NL, ENTITY-FREE, embedded for retrieval
    kind: Literal["single", "composite"]     # = execution topology, not SQL prettiness (§3.2)
    resolves: dict[str, str]                 # {ambiguous_term: column_chosen}, from (NL term × column used)
    source_tool_call_refs: tuple[str, ...]   # the runQuery refs that produced the ACCEPTED answer (D34)
    accepted_signal: AcceptedSignal          # MANDATORY (D34) — no acceptance ⇒ no blueprint candidate
    parameterization: tuple[ParamPlan, ...]  # one entry per literal predicate (§4)
    composes: tuple[ComposeNodePlan, ...] = ()   # kind=composite only
    result_signature: ResultSignature | None = None
    notes: str = ""
```

**Deliberately absent from the extractor's output** (Stage-4/5 add them — §5): `sql_template`,
`uses` / `USES` edges, `uses_rules` resolved bindings, the golden fixture beyond the signature, the
D48 `canonical_key`, and `id`. The extractor never emits SQL (D35).

### 3.1 Worked example — payroll

**NL ask (final, accepted):** *"total earnings for the Analytics department in 2025"* — and the
session happened to run in an `NA`-region scope.

**Accepted SQL from the `runQuery` trail (what actually ran and was not corrected):**

```sql
SELECT sum(gross_pay) AS total_earnings
FROM   payroll.payroll_fact
WHERE  department  = '0420'          -- user parameter: "the Analytics department"
  AND  toYear(pay_period) = 2025     -- user parameter: "in 2025"
  AND  record_type = 'EARNING'       -- DEFINES the metric "earnings"
  AND  region      = 'NA'            -- caller/session scope, incidental to the question
```

**Extractor output (`blueprint` payload):**

```yaml
intent: "total earnings for a department in a given year"        # ENTITY-FREE
kind: single
resolves: { earnings: "payroll.payroll_fact.gross_pay",
            department: "payroll.payroll_fact.department" }
source_tool_call_refs: [ "tc_7f3a" ]
accepted_signal: no_correction
parameterization:
  # department='0420' → SLOT (required): the thing the question varies over
  - locator: { table: "payroll.payroll_fact", column: "department", value: "0420" }
    role: slot
    slot: { name: department, type: entity, required: true,
            binds_to: "payroll.payroll_fact.department" }
  # toYear(pay_period)=2025 → SLOT (required, period): the other question parameter
  - locator: { table: "payroll.payroll_fact", column: "pay_period", value: "2025" }
    role: slot
    slot: { name: year, type: period, required: true,
            binds_to: "payroll.payroll_fact.pay_period" }
  # record_type='EARNING' → INLINE: dropping/parameterizing it changes WHAT is measured
  - locator: { table: "payroll.payroll_fact", column: "record_type", value: "EARNING" }
    role: inline
    why: "defines the metric 'earnings'; parameterizing it would sum all record types"
  # region='NA' → OPTIONAL SLOT: caller-specific, not metric-defining, not user-asked
  - locator: { table: "payroll.payroll_fact", column: "region", value: "NA" }
    role: slot
    slot: { name: region, type: entity, required: false,
            binds_to: "payroll.payroll_fact.region",
            optional_pattern: "TRUE" }        # absent ⇒ no region filter (all regions)
result_signature:
  shape:      [ { column: total_earnings, type: Float64 } ]
  grain:      { columns: [], verifiable: true }     # bare scalar ⇒ empty grain (see note)
  invariants: [ "row_count == 1", "total_earnings >= 0" ]
notes: "region is an optional caller scope, not part of the question; record_type inline defines earnings."
```

**Grain note (honest, ties to §6).** A bare `SUM` with no `GROUP BY` returns one row, so the declared
grain is empty and D56's row-count teeth **skip** (`grain_checked:false`, per `verify.py` §4.2). The
`GROUP BY department` variant *("earnings per department in 2025")* would declare
`grain: {columns: [department], verifiable: true}` and *that* is where the fan-out teeth bite. The
scalar form makes §6's honesty point vivid: a fan-out `JOIN` or a mis-inlined filter changes the
**number** but not the one-row shape, so replay alone cannot catch it.

### 3.2 `single` vs `composite`

`kind` is **execution topology**, not SQL prettiness (Locked): emit `composite` only when the session
actually ran **multiple** queries whose outputs combined (large intermediate → scratch materialization,
external-data join, genuine client-side combine). A one-query session stays `single`, however complex
the single SQL. `composes[*]` carries the *step plan* (`step_intent`, `feeds_from`, `consumes`,
`output`, `when`, `requires_approval`) that maps onto `models.py::Node`; Stage 4 lifts each node's
accepted SQL to its `sql_template` and runs the DAG/topology validations in
`corpus_loader._validate_blueprint_dag` (feeds_from exists, no cycles, node cap, terminal-approval
rejection, scalar-only convergence).

### 3.3 The other three targets (by reference — depth is on `blueprint`)

These payloads are **Locked** verbatim in [05-memory-and-learning.md](../05-memory-and-learning.md);
S3 emits them unchanged. Summary only:

| Target | Payload (Locked) | Emit / routing note |
|---|---|---|
| `global_knowledge` | `statement` (entity-free), `knowledge_type`, `scope_of_applicability`, `related_terms[]` | Entity-free — the Slice-5 leakage gate is authoritative; lands in the **review inbox** (D58a), not retrievable until human-approved. |
| `user_knowledge` | `user_id`, `fact_type`, `statement`, optional `structured` | The **only** target allowed to carry entities; stored per-user (D17), surfaced only in that user's context. |
| `schema_edit` | `target{database,table,path}`, `edit_type`, `patch` (YAML), `justification`, `risk` | **Never auto-commits** (D18/D53) → bot-authored PR + CI + human merge. |

---

## 4. Role classification — the core (D97)

This is the highest-stakes part of S3. Mis-classifying a metric-defining predicate as an optional
slot **silently changes answers** while passing every structural gate.

### 4.1 Totality — one entry per literal predicate

`parameterization` has **exactly one `ParamPlan` per literal predicate** in the accepted SQL (WHERE
equality/`IN`, and constant JOIN-on predicates). The three roles are **TOTAL** over literal
predicates: every literal is placed into exactly one of `slot | rule | inline`. There is **no fourth
role and no "unplaced" predicate** — a predicate the extractor emits nothing for would be a silent
dropped filter, the exact D56 wrong-answer class.

### 4.2 The three roles

| Role | When | What the AST stage does | Example |
|---|---|---|---|
| **slot (required)** | The value is the **user's query parameter** — the dimension the NL question varies over. | Replace literal with `{name}`; declare a required `SlotSpec` bound to the column domain. | `department='0420'`, `toYear(pay_period)=2025` |
| **slot (optional)** | The value is **caller/session-specific context**, NOT metric-defining and NOT user-asked (a scope the session happened to carry). | Declare `SlotSpec(required=false, optional_pattern=…)`; at bind time the caller supplies a value **or** `optional_pattern` renders. | `region='NA'` |
| **rule** | The predicate is **resolvable via an EXISTING catalog rule** (semantic constant with a curated definition). | Reference the rule (`uses_rules` / `resolve_via` IN-list); no literal in the template. | `status IN (active_employee)` |
| **inline** | The predicate **defines the metric / semantics** — removing or parameterizing it changes WHAT is measured. | Leave the literal in the template unchanged. | `record_type='EARNING'` |

**Unclassifiable ⇒ fail-to-review (D97).** If the extractor cannot confidently place a predicate in
**exactly one** role — e.g. it is ambiguous between *metric-defining inline* and *caller-specific
optional-slot*, or it *looks* like a rule but **no catalog rule exists** — the candidate is routed to
**human review**, never auto-promoted. (Missing-rule has a specific handling: §7.)

### 4.3 Why there is NO "drop" role (the no-drop rationale)

A "drop" role — the extractor deciding a predicate is session-specific and **deleting it from the
template** — is deliberately absent. The generalization it would express ("sometimes apply this
filter, sometimes not") is legitimate, but it is expressed as an **optional slot**, not a deletion.
Reasons:

1. **No silent semantic change.** A drop is an **irreversible AST deletion** performed by the
   extractor and baked into the template — invisible to the caller and to reviewers. An optional slot
   is **declared in the template**, visible in review, and **caller-controlled at bind time**. The
   generalization stays reversible and explicit.
2. **Totality is a safety property.** Because the three roles are total, there is **no code path**
   that removes a predicate. The worst failure the extractor can produce is an **over-narrow but
   correct-shaped** template (a kept predicate that should have been a slot) — which a human can
   *loosen* — never a **silently-gone** predicate that returns a wrong number.
3. **The failure mode points the safe way.** Mis-classifying `record_type='EARNING'` (metric-defining)
   as an optional slot and omitting it at bind time sums **all** record types, not just earnings — a
   wrong number with an **unchanged shape and grain** (§6 shows replay cannot catch this). So the
   design forbids drop entirely and, when the choice between `inline` and `optional-slot` is not
   clear-cut, routes to **fail-to-review**. Bias: keep the predicate, ask a human — never delete.

Contrast: a hard-drop would make "predicate silently gone → wrong number that passes every gate" a
one-LLM-call-away outcome. No-drop makes that outcome **structurally unreachable** by the extractor.

---

## 5. Division of labor — extractor emits the PLAN only

The extractor emits `intent`, `kind`, `resolves`, `source*`, `parameterization` (the plan),
`composes` step plan, and `result_signature`. It emits **no SQL**. Everything below is added by later
deterministic stages, and the eventual write record must satisfy `BlueprintSeed` +
`_validate_blueprint_dag` / `_validate_blueprint_uses`.

| Stage | Adds | Mechanism / gate |
|---|---|---|
| **4 — generalize + AST rewrite** (D35/D52) | `sql_template` | Deterministic sqlglot rewrite that touches **only classified `locator`s**: `slot`/optional-slot → `{name}` placeholder; `rule` → the rule's `resolve_via` IN-list; `inline` → left literal. Must yield a **single read-only SELECT** (`parse_template` + `assert_read_only_select`); no `*`, no dict-family functions. **Fail-to-review (D52)** if it cannot. |
| **4 — static validate** | `uses` + `:USES` edges; `binds_to ⊆ uses`; DAG topology | `explainQuery` dry-run; the D52 column-provenance extractor for `USES`; the full `corpus_loader` suite (footprint ⊆ uses table-aware, required-slot-referenced, `binds_to` ⊆ uses, resolve_via col ⊆ uses, `consumes`/`feeds_from`/terminal-approval/`when` checks). |
| **4 — golden** (D36) | the golden fixture = the **entity-free `result_signature`** | shape + invariants + grain; **never** the recorded scalar answer (entity-bearing — must never enter the entity-free blueprint). |
| **5 — leakage gate** (D58/D17) | entity verdict | regex/NER + LLM semantic scan over `intent` + `result_signature`; entity found → reroute/quarantine; emits a `GUARDRAIL` span (D25). |
| **6 — dedup** (D48) | `canonical_key`, `id` | `hash(resolves, uses_rules, result-grain, normalized sql_template AST)`, single-writer-per-`canonical_key`; soft embedding near-miss → review inbox. |

**Fail-to-review is the shared safety valve** (D52): any stage that cannot produce a **safe single
read-only SELECT** (un-parseable, un-rewritable, star/dict construct, footprint escape) routes the
candidate to human review — it is **never** auto-promoted with a guessed template.

---

## 6. The replay check — HONEST scope (D98) — a first-class invariant

> **Replay verifies STRUCTURE (grain + result_signature shape), NOT VALUES. A green replay is not a
> proof that the template returns the right number. Do not overstate it.**

**The golden fixture is the entity-free `result_signature`** (shape + invariants + grain), **not** the
recorded scalar answer. The recorded answer is entity-bearing (it is *this* department's *this* year's
total) and must never be stored in the entity-free blueprint (D17/D36).

**What promotion replay does** (Slice 9 scheduler, D29/D36):

1. **Samples** valid slot values at run time — catalog-driven, from each slot's `binds_to` domain
   (`DISTINCT` probe). **No stored entity inputs** are used; the sampled values are not the session's.
2. **Binds + runs** the scope-enforced template (server-side params, D41/D49).
3. **Asserts** two structural properties via `verify_result` (`verify.py`):
   - **(a) D56 grain-integrity** — `row_count == COUNT(DISTINCT grain)` (the fan-out teeth).
   - **(b) result_signature** — column shape + declared invariants.

**What replay does NOT catch (state this loudly).** A **semantically-wrong-but-structurally-valid**
template passes replay. A dropped or mis-inlined value filter (e.g. `record_type='EARNING'`
mis-classified as an optional slot and omitted at sample time; or a fan-out `JOIN` on a scalar `SUM`)
changes the **number** but not the **shape or grain** — so `verify_result` returns `passed=True`.
Replay is blind to value-correctness by construction: it has no trusted expected value to compare
against (storing one would breach D17).

**Therefore value-correctness is a LAYERED protection, not a replay oracle:**

| # | Layer | Guarantee |
|---|---|---|
| (i) | **No-drop role** (§4, D97) | The extractor **structurally cannot** silently remove a predicate; the worst case is over-narrow, not wrong-numbered. |
| (ii) | **Lift only the accepted final SQL** (D34) | The number was **human-accepted** at capture time (`accepted_signal` mandatory); the template is a parameterization of a query that already returned a trusted answer. |
| (iii) | **candidate ≠ validated** (D29) | A single session's blueprint reaches `validated` only via replay **+ hit_count recurrence and/or human** approval; one session's candidate **stays `candidate`**. |
| (iv) | **fail-to-review** (D52/D97) | Un-rewritable SQL or an unclassifiable predicate → human review, never a guessed template. |
| (v) | **Sampled-input replay** (D56) | Still rules out the **fan-out double-count** class and **shape/column drift** under schema change — the regression role D36 scopes it to. |

Replay is layer (v): it defends the **grain + shape** frontier. Values are defended by (i)–(iv). No
future reader should treat a green replay as a correctness proof of the number.

---

## 7. Cross-target routing + `depends_on`

The extractor's classifier (Locked routing table, [05](../05-memory-and-learning.md) §Cross-target):

| Session reveals | Target |
|---|---|
| A generic business rule / metric def / join quirk / lesson | `global_knowledge` |
| A hard truth about a column/table | `schema_edit` (not knowledge) |
| Anything tied to a specific person / dept / region / alias | `user_knowledge` |
| A reusable query plan / report | `blueprint` |

One session can spawn several linked candidates via `depends_on` (header field). **Missing-rule
pairing (D35):** if a predicate *should* be a `rule` but **no catalog rule exists**, the extractor
emits a `schema_edit(add_rule)` candidate **and** the `blueprint` candidate `depends_on` it — the
blueprint is **blocked** (never promoted) until the rule lands. This keeps the "unclassifiable"
fail-to-review (§4.2) from being the only escape hatch for the recurring missing-rule case: it becomes
a *pair*, not a rejection.

---

## 8. Test matrix (what QA implements when S3 is built)

Layer-1 unless noted. Slugs are the TRACEABILITY tags.

| # | Invariant | Slug |
|---|---|---|
| 1 | **Role: slot** — a user-parameter literal → required slot bound to the column domain | `S3-role-slot-user-parameter` |
| 2 | **Role: rule** — a catalog-rule-resolvable literal → `rule` with the existing `rule_id`, no literal in plan | `S3-role-rule-catalog-resolvable` |
| 3 | **Role: inline** — a metric-defining literal (`record_type='EARNING'`) → `inline`, stays literal | `S3-role-inline-metric-defining` |
| 4 | **Caller-specific → optional slot** — `region='NA'` → `required:false` + `optional_pattern`, NOT deleted | `S3-caller-specific-to-optional-slot` |
| 5 | **Adversarial mis-classification** — metric-defining predicate offered as optional-slot is REJECTED to review; caller-specific offered as inline flagged | `S3-adversarial-role-misclassification` |
| 6 | **No-drop totality** — every literal predicate has exactly one `ParamPlan`; a missing entry fails validation | `S3-no-drop-totality` |
| 7 | **Unclassifiable → fail-to-review** — a predicate not cleanly slot/rule/inline routes to human, never auto-promotes (D97) | `S3-unclassifiable-fails-to-review` |
| 8 | **Evidence mandatory** — a candidate with empty `evidence` is REJECTED at emit (D31) | `S3-no-evidence-rejected` |
| 9 | **Lift-not-generate** — the (Stage-4) template derives only from the accepted SQL; `accepted_signal` required; no-acceptance session yields no blueprint (D34) | `S3-lift-not-generate-accepted-signal` |
| 10 | **Entity-free intent + signature** — `intent`/`result_signature` carry no entities (leakage) | `S3-entity-free-intent-signature` |
| 11 | **Fail-to-review on un-AST-rewritable SQL** — a query the sqlglot stage can't rewrite to a safe single SELECT → review (D52) | `S3-unrewritable-sql-fails-to-review` |
| 12 | **Replay catches grain fan-out + shape drift** — a fan-out template / column-shape change fails `verify_result` (D56) | `S3-replay-catches-grain-and-shape` |
| 13 | **Replay does NOT gate on values** — on a single session a semantically-plausible-but-value-changed template still leaves the candidate at `candidate` (encode the honest limit, D98) | `S3-replay-not-a-value-oracle` |
| 14 | **single vs composite topology** — a one-query session stays `single`; a genuine multi-query combine is `composite` | `S3-single-vs-composite-topology` |
| 15 | **Missing-rule pairing** — a rule-shaped predicate with no catalog rule → `schema_edit(add_rule)` + blueprint `depends_on` it (D35) | `S3-missing-rule-pairs-schema-edit` |

Row 13 is the load-bearing honesty test: it asserts a green single-session replay does **not** promote,
so the codebase encodes "replay verifies structure, not values" (D98) as an executable invariant.

---

## 9. New decisions + traceability

This doc adds two minimal decisions (§4 and §6 are genuine new commitments *beyond* D31/D34/D35/D36/
D52/D56, though each **concretizes** an existing lock rather than re-deciding it):

- **[D97](DECISIONS.md#memory--learning)** — total role classification; no drop role;
  caller-specific → optional slot; unclassifiable → fail-to-review. *Concretizes D35 + D52.*
- **[D98](DECISIONS.md#memory--learning)** — promotion replay verifies structure (grain + signature),
  not values; value-correctness is a layered protection. *Concretizes D36 + D56.*

**Doc/code updates on build:** add the two new TRACEABILITY rows for D97/D98 plus the 15 §8 slugs
(Status `⛔ not-built` → `🟡 unit-green` as Layer-1 lands); update
[learning-loop-infra-design.md](learning-loop-infra-design.md) §15 Slice-3 line to point here; a
WORKLOG entry when S3 is built.
