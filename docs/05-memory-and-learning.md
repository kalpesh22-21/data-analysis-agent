# 05 — Memory & Learning

This is the most novel and the most dangerous part of the system: a **write-back learning loop**
that runs after every chat session and turns the transcript into durable knowledge.

## Memory layers (our mapping)

| OpenAI layer | Our home |
|---|---|
| Table usage metadata | Skipped (curated warehouse). |
| Human annotations | `getTableSchema` semantic YAML. |
| Codex enrichment | Skipped. |
| Institutional knowledge | **Global knowledge** RAG docs (`searchKnowledge`). |
| Memory | **Global knowledge** (lessons from past mistakes) + **Blueprints** + **User knowledge**. |
| Runtime context | Skipped; schema changes flow into `getTableSchema`. |

## Write targets

| Target | Store | Entity policy | Commit gate |
|---|---|---|---|
| **Blueprint** | neo4j | **Entity-agnostic** (literals lifted to slots) | **All mined candidates → human review by default**; leakage and dedup findings remain visible on the card |
| **Global knowledge** | vector index / RAG | **Entity-agnostic** | **Human review *before* retrievable (D58a)** — candidates land in the review inbox; `searchKnowledge` returns **only human-approved** statements. (Reverses the earlier "candidates retrievable" assumption for knowledge.) |
| **User knowledge** | per-user store | **Entity-bearing OK** (personal) | Auto-land (scoped to user) |
| **Schema edit** | `getTableSchema` source | Depends | **Human review queue — never auto-commit** |

> **The one crossing between the two knowledge rows (2026-09-01).** A reviewer can now list ONE
> named user's private facts in the inbox and **promote** one into a `global_knowledge` candidate
> by a button, and can edit a knowledge candidate's payload with an LLM assistant before approving
> it. Both write through a single re-adjudicating path: the extractor's own intake reader (so the
> closed, entity-free key set holds on the human path too), then the leakage gate's SCAN, stamped
> on a row that stays `in_review`. A promoted fact therefore arrives with a non-passing scan by
> construction — the card withholds the flagged text, and the entity has to be edited out before
> it can land. This is a deliberate, recorded exception to D17's "surfaced only in that user's
> context": the reviewer surface sees a named user's facts. Design + the corrected account of what
> the approve guard actually enforces: `decisions/knowledge-edit-and-user-promotion-design.md`.

> **Global learning kill-switch (D58c).** `LEARNING_ENABLED=false` disables the **entire** learning
> loop — write router **and** promotion scheduler — instantly, **without a deploy** (operational
> safety valve for a discovered leak / bad-blueprint pattern). **Reads are unaffected**:
> `searchKnowledge` / `searchBlueprints` over already-published artifacts keep working; only write-back
> and promotion stop.

Key rule: **global = entity-free; user = may carry entities.** "I usually mean EMEA" → user
knowledge. The *generalizable* pattern behind it → global knowledge candidate.

---

# The learning-loop architecture

Two cooperating processes:

1. **Per-session write router** (async job, one per closed session) — mines the transcript into
   gated **candidates**.
2. **Promotion scheduler** (background, store-wide) — moves candidates → `validated` over time and
   handles decay/retirement.

Neither is ever a model-callable tool. Both run inside the trust boundary and are traced into the
Phoenix `learning-loop` project (see [10-observability.md](10-observability.md)).

## Component / data-flow diagram

```
 session close (idle TTL or explicit end)
        │  enqueue LearningJob{session_id, user_id, scope, trace_id, content_hash}
        ▼
 ┌──────────────┐   durable queue (Redis Streams; idempotent by content_hash; dead-letter stream)
 │   QUEUE      │   message = REFERENCE not transcript: {session_id, couchbase_doc_id+cas,
 └──────┬───────┘     user_id, scope_ref, trace_id, session_closed_at, content_hash}
        ▼
 (0) SESSION LOADER / NORMALIZER
     reads Couchbase transcript (messages + tool I/O trail; no thinking)
     → typed SessionSummary: turns, tool calls, blueprint usages (accepted/corrected),
       askUser Q&As, failed→fixed SQL, explicit signals (thumbs, "actually I meant…")
        │
        ▼
 (1) TRIAGE  (cheap LLM)  ── "is there anything worth learning?"  most sessions → STOP here
        │ flags candidate targets present: {blueprint?, knowledge?, user?, schema?}
        ▼
 (2) GROUNDED EXTRACTOR  (per flagged target, LLM)
     RAG-grounded on EXISTING blueprints/knowledge/schema so it won't re-propose known facts.
     emits typed Candidate envelopes; lifts literals→slots; routes entity facts→user knowledge
        │
        ▼
 (3) GENERALIZE & STATIC-VALIDATE   (blueprints)
     lift literals→slots · build composes DAG · compute USES (transitive tables/columns)
     · explainQuery dry-run (parses vs. current schema) · capture entity-free output signature (golden)
        │
        ▼
 (4) LEAKAGE GATE  [GUARDRAIL span]   (global candidates only)
     regex/NER (employee_code, dept codes, names, dates) + LLM semantic scan
       pass → continue   ·   entity found → reroute to user knowledge / quarantine / reject
        │
        ▼
 (5) DEDUP / CONFLICT
     embed candidate → semantic search existing store
       blueprint near-match     → increment hit_count + merge golden, DROP duplicate
       blueprint partial overlap → variant → review inbox
       knowledge duplicate       → merge/skip
       knowledge contradiction   → conflict → review inbox (never auto-append)
        │
        ▼
 (6) WRITERS (idempotent, keyed by content_hash)        each candidate carries provenance:
       blueprint        → REVIEW INBOX by default (+USES edges, +golden, hit_count seed)
                          ↳ leakage and dedup findings stay visible for reviewer repair
       global knowledge → REVIEW INBOX (human approval BEFORE retrievable — D58a)
       user knowledge   → per-user store (auto-commit, scoped)   {source_session, source_trace,
       schema edit      → REVIEW INBOX (human approval)            extractor_rationale, evidence}
        │
        ▼
 (7) REVIEW INBOX (human-in-loop): schema edits, ALL global-knowledge candidates (D58a),
       knowledge conflicts, all mined blueprints + leakage/dedup findings (D58b)
       approve → commit/version (knowledge becomes retrievable) ·  reject → archive as negative signal
       leaked artifact found → RETRACT (pull from index; D25 trace → who was exposed)
```

## Promotion scheduler (separate background process)

Repairable extractor declines are durable even when the novelty judge is unavailable. They enter
`awaiting_judge`, retry from the consumer with exponential backoff (five minutes initially, capped
at six hours), and after 24 hours move to `needs_parameterization` with
`route_reason=judge_unavailable`. This state is review-only and shares the six-month transient TTL.

Extractor corrections use independent bounded budgets: two structural, two SQL/rule, and one
semantic correction, with a hard total of five. A normalized failure repeated through two
corrective attempts stops that family early.

Candidates do not promote themselves. A store-wide scheduler advances state:

```
extracted ─(leakage pass)─▶ candidate ─┬─ golden-replay passes (scheduled re-run) ─┐
                                       ├─ hit_count ≥ threshold, no corrections     ├─▶ validated
                                       └─ human approval                            ┘
 validated ─ periodic DRIFT CHECK (3 probes) ─┬─ clean  ─▶ drift_status=clean (silent-eligible)
                                              └─ suspect ─▶ demote to candidate + review flag
 candidate/validated ─ user correction / golden-replay fails ─▶ demote or retire
 validated ─ schema/catalog drift makes it stale ─▶ re-validate or retire
```

- **Validation = ALL of:** golden-input replay, hit-count threshold, human review (any can promote;
  per-target policy decides which are required). See [04-blueprints.md](04-blueprints.md).
- **Drift attestation (D43):** the scheduler also runs the **three drift probes** (result
  grain-integrity, catalog-vs-warehouse conformance, rule-semantics currency) and stamps
  `drift_status` + `last_drift_check_at`. **Silent-eligibility** is `validated AND drift_status=clean
  AND fresh` — see [04-blueprints.md](04-blueprints.md) §Drift attestation. **Phased (per D56):** the
  D56 verify gate runs grain-integrity on **every** result in Phase 1 (no unverified silent return);
  the full freshness predicate (catalog-conformance + rule-currency probes) is **Phase 2** — accepted
  time-boxed drift risk until then.
- **Negative signals demote:** a corrected blueprint output, a failed golden replay, or a
  `drift_status=suspect` probe drops trust.

## Candidate envelope (data contract)

Every candidate, regardless of target, carries:

```yaml
type: blueprint | global_knowledge | user_knowledge | schema_edit
status: extracted | candidate | in_review | validated | quarantined | rejected | retired
payload: { … target-specific … }          # e.g. blueprint DAG, knowledge text, schema patch
provenance:
  source_session: <id>
  source_trace:   <phoenix trace id>
  evidence_ref:   <provenance-store key>   # D51 — snapshot lives in the audit store, NOT inlined
  extractor_rationale: <text>
entity_scan: { result: pass|reroute|quarantine|reject, hits: [...] }
dedup: { matched_id: <existing|null>, similarity: 0.0–1.0, action: insert|increment|merge|conflict }
content_hash: <idempotency key>
```

Provenance makes every learned fact auditable back to the session + Phoenix trace that produced it.

### Evidence snapshot vs. session TTL (D51)

The session doc expires at `SESSION_TTL` (D44), but a candidate can dwell in the review inbox for
**days** — so storing evidence as bare *refs into the session* would dangle exactly for the
long-lived candidates that most need audit. Fix: **at extraction, snapshot the cited evidence**
(turn/tool-call quotes + `trace_id`) so the candidate is self-contained and outlives the session.

**Where the snapshot lives matters:** evidence quotes are **entity-bearing**, so they are **never
inlined** into the entity-free global stores (neo4j blueprints, knowledge vector index) — that would
breach the entity-agnostic guarantee (D17). Instead the snapshot is written to a dedicated
**access-controlled provenance/audit store** (in-boundary, same PII posture as the session store,
with its own retention ≥ candidate lifetime); the candidate carries only an `evidence_ref`. Global
candidates stay entity-free; audit stays durable.

## Extractor output contracts (per target)

The extractor emits zero or more typed candidates. Output is **forced via structured schema**
(tool-call validation, retry on mismatch) — no free-form text. A candidate with **no `evidence`
is rejected**, the main guard against hallucinated learning. The extractor proposes the **payload**;
later stages enrich it (it does not compute `USES` edges, golden fixtures, or final dedup/entity
verdicts — those are stages 3–5).

### Shared header (every candidate)

```yaml
type: blueprint | global_knowledge | user_knowledge | schema_edit
confidence: 0.0–1.0                 # extractor self-assessment
evidence:                           # MANDATORY — no evidence ⇒ rejected
  - { turn_ref, tool_call_ref, quote }
rationale: <why worth learning AND why it's generic / not already represented>
proposed_action: new | update_existing(<id>) | reinforce(<id>)   # dedup HINT (stage 5 authoritative)
entity_self_check: { contains_entities: bool, found: [...] }     # preliminary; leakage gate authoritative
```

### `global_knowledge` payload (entity-free — gate-enforced) — **Locked**

```yaml
statement: <entity-free business rule / lesson / definition>
knowledge_type: business_rule | metric_definition | caveat | join_guidance | data_quirk | lesson_from_mistake
scope_of_applicability: <when it applies, e.g. "payroll queries">
related_terms: [ ... ]            # retrieval aids
```

### `user_knowledge` payload (entity-bearing OK, scoped to user) — **Locked**

```yaml
user_id: <id>
fact_type: preference | default_resolution | frequent_entity | alias | output_pref
statement: <text>
structured:                       # optional machine-usable form
  # default_resolution: { term: salary, resolves_to: gross_pay }
  # alias:              { phrase: "my team", resolves_to: { department: "0420" } }
  # frequent_entity:    { entity_type: region, value: EMEA }
```

The only target allowed to carry entities — still PII-sensitive, stored per-user, surfaced only in
that user's context.

### `schema_edit` payload (→ human review, never auto-commit) — **Locked**

```yaml
target: { database, table, path }     # e.g. column gross_pay / a rule / an ambiguity
edit_type: add_synonym | add_description | add_rule | edit_rule | add_ambiguity | correct_enum | add_column_note
patch: |                               # YAML fragment to merge into the semantic doc
  ...
justification: <text + evidence>
risk: low | medium | high              # blast radius across all users
```

### `blueprint` payload — **Locked**

Two principles: **lift, don't generate** (parameterize the real accepted SQL from the session, never
synthesize — D34); and **LLM classifies, deterministic stage rewrites** (the extractor emits a
parameterization *plan*; a SQL-AST stage produces the template — D35). Only proposed when the session
shows an **acceptance signal**, using the **final corrected** SQL.

```yaml
intent: <nl, ENTITY-FREE, embedded for retrieval>
kind: single | composite                              # = execution topology (see below)
resolves: { <ambiguous_term>: <column_chosen> }       # inferred from (user NL term × column used)
source:
  tool_call_refs: [ <runQuery refs that produced the accepted answer> ]
  accepted_signal: no_correction | thumbs_up | explicit_confirm
parameterization:                                     # one entry per literal predicate
  - { locator: {table, column, value}, role: slot,
      slot: { name, type, binds_to:{table,column}, required, optional_pattern } }
  - { locator: {table, column, value}, role: rule,   rule_id: <existing catalog rule> }
  - { locator: {table, column, value}, role: inline, why: <reason> }
composes:                                             # kind=composite only — control-flow nodes
  - { order, node_kind, step_intent, feeds_from, consumes, output, source_tool_call_ref,
      when?, requires_approval? }                     # see 04-blueprints.md
result_signature:                                     # ENTITY-FREE — seeds golden (option A)
  shape: [ {column, type}, ... ]
  invariants: [ "row_count between …", "avg_salary > 0", "key cols non-null", "<grain>" ]
notes: <caveats / assumptions>
```

**Division of labor — what stages 3–5 add (not the extractor):** final `sql_template` (AST rewrite
from `parameterization`), `explainQuery` validation, `USES` edges, DAG/topology + control-flow
validation, dedup, and `id`. The **golden fixture = the entity-free `result_signature`**; replay
**samples valid slot values at run time** (no stored entity inputs). **Fail-to-review (D52):** if the
SQL parser can't AST-rewrite the accepted SQL into a safe template, the candidate is **routed to
human review**, never auto-promoted.

**`single` vs `composite` = execution topology**, not SQL prettiness: emit `composite` only when the
session actually ran multiple queries whose outputs combined (large intermediates → scratch,
external-data join, genuine client-side combine). A one-query session stays `single`.

**Missing rule:** if a predicate should be a rule but none exists in the catalog, the extractor pairs
a `schema_edit(add_rule)` and the blueprint `depends_on` it (blocked until approved).

### Cross-target routing (the extractor's classifier)

| What the session reveals | Target |
|---|---|
| A generic business rule, metric def, join quirk, or lesson | `global_knowledge` |
| A fact that's really a hard truth about a column/table | `schema_edit` (not knowledge) |
| Anything tied to a specific person/dept/region/alias | `user_knowledge` |
| A reusable query plan / report | `blueprint` |

One candidate can spawn several linked by a `depends_on` reference (e.g. a blueprint needing a new
rule pairs with a `schema_edit(add_rule)` and is blocked until it lands).

## Why each stage exists

- **Triage first:** most sessions teach nothing; skip them cheaply before paying for extraction.
- **Grounded extraction:** the constraint is "capture only what's *not already represented*" — so the
  extractor must see existing stores (dedup-aware at the source).
- **Candidate, not trusted:** one session can be wrong; nothing global auto-promotes.
- **Leakage gate:** the hard guarantee that global stores stay entity-agnostic.
- **Dedup/conflict:** sessions repeat; collapse duplicates, use recurrence (`hit_count`) as trust,
  never silently append contradictions.
- **Schema = highest stakes:** `getTableSchema` grounds all users → human review only.

## Cross-cutting concerns

| Concern | Approach |
|---|---|
| **Idempotency** | Jobs + writes keyed by `content_hash`; re-running a session never double-writes. |
| **Failure / retries** | Durable queue with retries + dead-letter; loader/extractor are pure functions of the transcript. |
| **Cost control** | Triage gate short-circuits empty sessions; extraction batched per target. |
| **Concurrency** | Many sessions write concurrently. The dedup stage computes a **deterministic canonical key** (`hash(resolves, uses_rules, result-grain, normalized SQL AST)`) and serializes writes **single-writer-per-key** (partitioned write-stage queue / per-key Redis lock) so concurrent *equivalent* candidates resolve to one create + one increment — never a duplicate (D48). `content_hash` only covers identical transcripts; the canonical key covers semantic equivalents across different sessions. Soft embedding match → review inbox for near-misses. |
| **Versioning** | Blueprints/knowledge/schema are versioned; promotion creates a new version; rollback supported. |
| **Schema drift** | When `getTableSchema` changes, a maintenance job re-validates affected `USES` edges + golden replays and demotes anything broken. |
| **Observability** | Whole job = `CHAIN`; triage/extractor = `LLM`; leakage = `GUARDRAIL`; writers = `CHAIN`. |
| **PII** | Loader sees raw transcript (must, to extract) but stays in-boundary; nothing raw enters traces; global outputs are entity-free by the leakage gate. |

## Clarification answers as learning signal

`askUser` answers feed the same router with the same gates. Routing by content:
- "I meant EMEA" → user knowledge (entity-bearing).
- "Users saying 'headcount' mean active-status only" → global knowledge candidate (entity-free).
- A repeated slot-fill pattern → strengthens a blueprint.

## External-data sessions

Sessions involving uploaded CSV/xlsx are **weak blueprint candidates**. If one is learned, the
extractor must generalize the upload into a "bring-your-own fact table with columns {…}" slot —
never bake the specific file or its values. See [07-external-data.md](07-external-data.md).

## Evaluation

Following OpenAI: curated Q&A pairs with **golden SQL**, graded by **result-set comparison +
LLM grading** for acceptable variation, run continuously as **production canaries** (Phoenix
experiments). Golden replay also doubles as a blueprint validation method.

---

**Status:** Locked (architecture); stage-level prompts/thresholds Partial
**Open questions:**
- Session-close definition (idle TTL value vs. explicit end).
- Triage precision/recall target (how aggressively to skip).
- Extractor prompt + per-target output schema.
- Quarantine handling for leakage near-misses.
- Conflict-resolution UX for contradictory global knowledge.
- Hit-count thresholds + per-target required validation methods.
