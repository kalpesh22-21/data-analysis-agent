# Blueprint mining tool — expert-authored corpus, pre-launch (design)

**Status:** PROPOSED. Nothing built. Two open questions (§9) are deliberately left open
for the reader to settle; both change the shape of §4 and §6.

**Purpose.** Give a field expert a way to turn a question they know the answer to into a
validated blueprint, in bulk, before go-live. The output is MCP-canon YAML plus a paired
eval fixture, delivered as one PR per mining session.

**The gap this fills.** Today a blueprint reaches canon exactly two ways: someone
hand-writes YAML into `clickhouse-api/app/corpus/data/blueprints/`, or the learning loop
extracts one from an accepted live session (`learning/extractor/` → `generalize/` →
`promotion/`). Neither serves a domain expert holding a question and the SQL that answers
it. Pre-launch there are no live sessions to mine, so the learning loop cannot run at all,
and hand-writing YAML skips every gate that makes a blueprint trustworthy.

**The core claim.** The valuable half of the learning loop is not the drafting — it is the
gauntlet between draft and canon (`generalize/validate.py`, `promotion/replay.py`). This
tool is a **human-in-front adapter onto that same gauntlet**: the expert supplies what the
extractor would otherwise have inferred from a session trace, and every downstream stage
runs unchanged.

Reference: `learning-loop-s4-generalize-design.md` (the rewrite + static-validate stage
this reuses wholesale), `learning-loop-s9-promotion-design.md` (replay + MCP emit),
`governed-corpus-design` (the trust partitions §8 touches).

---

## 1. Scope

**In scope.** A CLI that takes expert input, drafts a blueprint, validates it against the
live catalog and warehouse, checks it against existing canon, and emits YAML + an eval
fixture. Batch/session oriented: an expert works through many questions, one PR at the end.

**Out of scope.**

- Knowledge-corpus authoring. The second corpus (`data/knowledge/`, `KNOWLEDGE_SHA`,
  `text`-shaped entries, its own retrieval path) needs its own design. Naming it here so it
  is not mistaken for covered.
- Any change to the learning loop. This tool consumes its stages; it does not modify them.
- Git automation. Same posture as `promotion/mcp_export.py`: emit files and suggested PR
  metadata, never touch git.

**"Offline" is a misnomer.** Schema pull needs MCP, `explain_ok` needs the catalog, replay
needs the warehouse, prior-art needs the corpus. This is a *separate entrypoint*, not a
disconnected one. Calling it offline is what leads to skipping the gates.

---

## 2. Input — three modes

The expert supplies: the question, the steps to solve it, their assumptions, the tables to
use, and SQL at one of three fidelities.

| Mode | Expert gives | LLM writes SQL? | Gates are… |
|---|---|---|---|
| `sql` | Correct, runnable SQL | No — rewrite only | a check on their work |
| `pseudo` | Pseudo-SQL / sketch | Yes — completes it | the primary evidence |
| `steps` | Prose steps only | Yes — from scratch | the primary evidence |

The distinction that matters is **rewriting vs authoring**. Turning
`WHERE department_name = 'Engineering'` into `WHERE department_name = {department}` is a
transform on already-correct SQL and must be deterministic (§4.3) — an LLM re-emitting the
query can silently drop a cast, flip a join, or lose a `DISTINCT`, and nothing downstream
would catch it. Authoring SQL from steps is a generative task and the LLM does it.

The system already trusts LLM-written SQL under exactly this posture: the ad-hoc runtime
path is the model writing SQL, and the learning loop mines blueprints out of it.

**Extra step for `pseudo` / `steps`.** Replay proves a query executes and that its grain
holds; it proves nothing about whether the numbers are right. When the LLM authored the
SQL, the tool shows the expert actual result rows and requires an explicit accept before
emitting. For `sql` mode this is skipped — the expert has already seen it work.

---

## 3. Session model

A mining session is a file. The expert appends questions over hours or days; the tool
processes each on submit; the session closes into one PR.

```
session.yaml
  author: <expert>
  started: <date>
  entries:
    - question / steps / assumptions / tables / sql / mode
      → status: drafted | needs-decision | validated | rejected
      → artifacts: blueprint yaml, eval fixture, validation record
```

Session-scoped state matters for one reason: **parallel experts collide**. The prior-art
check (§4.2) runs against canon *and* every entry already in this session, so two people
mining the same question surface each other rather than both landing.

---

## 4. The pipeline

Per entry, in order. Any stage can halt with a reason; nothing is emitted until §4.7.

### 4.1 Schema pull

Pull `getTableSchema` from the MCP for each table the expert named. This is context for
drafting only — it is **not** the source of `uses` (§4.4).

### 4.2 Prior-art check — runs FIRST

Compute `structural_key_from_templates` (`runtime/blueprint/structural_key.py`) once a
draft template exists, and additionally do a cheap intent/table-overlap lookup *before*
drafting, so near-misses surface early.

`structural_key` is the loose, cross-authoring-path identity built precisely so that a
hand-authored canon blueprint and a learning candidate describing the same query collide on
one hash. It is the right key here because this tool is the other authoring path.

Three outcomes:

- **Exact key match** → duplicate. Halt, show the existing blueprint, offer "extend a slot
  on the existing one instead."
- **Near-miss** (shared tables / overlapping `uses` / similar grain) → show the existing
  YAML and continue. This is not a blocker; it is the highest-value moment in the pipeline.
- **No match** → continue.

The near-miss case earns its place on its own. Worked example: an expert authoring
"overtime as a share of earnings" states the assumption `type_code = 'OT'`. The existing
`bp-overtime-by-department` carries a comment recording that the warehouse has *no*
dedicated overtime code — every `EARN` row is `type_code 'REG'`, so overtime is selected by
passing the pay-type code as a slot. The expert's premise is wrong and the corpus already
knew. A lookup caught it before anything executed.

### 4.3 Rewrite — deterministic, no LLM

`generalize/rewrite.py::rewrite_sql_to_template` performs the literal → slot AST rewrite,
and `generalize/canonical.py::canonical_ast_norm` pins the normalized form. Reused
verbatim. An unrewritable query halts with `REASON_UNREWRITABLE`, the same tag S4 uses.

### 4.4 `uses` — derived, never typed

`sqlparse::extract_column_provenance` derives `uses` transitively across every DAG node, as
`generalize/builder.py` does today. The expert's table list is **not** the input.

This is load-bearing: `uses` is byte-exact `database.table.column` and it is the
access-control scope pre-filter — "the corpus's highest-risk contract"
(`corpus/seeds.py:20`). Under-list and the blueprint is silently dropped from recall for
scoped users; over-list and it is an over-broad grant. In the worked example the expert
named two tables and the derivation found three columns they never mentioned
(`type_code`, `pay_period_end_date`, `register_type`).

### 4.5 LLM drafting — prose and structure only

The model writes:

- `intent` — one line, and the place assumptions currently land (§9.1)
- `slots_summary`
- slot names, `type` from the closed `SLOT_TYPES` set, `required`, `binds_to`
- `result_grain`
- `window_anchor` when the blueprint is windowed
- the DAG decomposition, derived from the expert's numbered steps

It does not write or rewrite SQL in `sql` mode; it authors SQL in `pseudo` / `steps` mode
(§2).

**Fields a drafting model omits unless forced.** `result_grain` (the D56 grain-integrity
teeth), `window_anchor` (J7 — the reason models were re-deriving queries they had been
handed), `slots_summary`. For DAG nodes: `order`, `feeds_from`, `consumes` with `$N` refs,
`output` kinds, and the `scratch.` namespace convention. The emitter validates presence
rather than trusting the draft.

**Reserved keys.** `source` and `verified` must never be written into the YAML —
`clickhouse-api/app/corpus/loader.py:63` fails loud on them; they are injected at export
time.

### 4.6 Halt for a human decision

Some gaps are not the tool's to close. The slot vocabulary is a closed set — `string`,
`entity`, `enum`, `period`, `as_of_date`, `list`, `relative_window`, `period_range` — with
no numeric/threshold type. An expert asking for a `threshold_pct` slot has to be told, and
the right answer is usually to drop the threshold and return the grain-complete result so
the caller filters. A generator that silently picked `string` would ship a broken blueprint.

### 4.7 Validate + replay — unchanged, non-optional

`generalize/validate.py`: `check_read_only_select` (single read-only SELECT, no `*`, no
dict-family funcs), `explain_ok` (the template resolves against the current catalog),
`binds_to_subset_uses`, `check_dag` for composites (`_MAX_NODES = 16`, no `when`-bearing
composites — `REASON_WHEN_COMPOSITE`).

Then `promotion/replay.py`: bind sampled synthetic slot tokens through the runtime binder,
execute, and check the result through the same D56 `verify_result` gate a live
`runBlueprint` uses. Structure, not values.

Failure returns the stable machine reason tag to the expert; nothing is emitted.

**Why this is non-negotiable pre-launch, even though nothing is live.** A seeded blueprint
becomes the thing everything else is measured against, and it teaches the learning loop not
to propose in that area — `structural_key` matching is what suppresses re-proposal, so a
wrong seed silently occupies its slot and blocks the correction. Post-launch a bad
blueprint has drift signals and a triage queue. Pre-launch it has neither. The warehouse is
right there and the check costs one query.

### 4.8 Emit

`promotion/mcp_export.py::build_promotion_emit` produces the YAML — block scalars for
`sql_template`, projected onto the exact MCP field set, learning-only provenance dropped.
Reused, not reimplemented: it is already parity-tested against the landing writer, so a
second serializer here would drift.

**Acceptance check: parse what you emit.** Run the emitted YAML back through
`clickhouse-api/app/corpus/loader.py` and `runtime/blueprint/models.py::Blueprint.parse`
before writing it. `Blueprint.parse` is fail-loud by contract; use it as the gate rather
than trusting the emitter.

### 4.9 Paired eval fixture — free

`tests/eval/fixtures/routing/case-*.yaml` binds a `question`, a `column_scope`, and a
`corpus` list. The expert supplied the question; §4.4 derived the scope; §4.2 found the
neighbours. Emit the fixture alongside the blueprint.

Pre-launch this is the single highest-leverage byproduct: the golden set gets built as a
side effect of mining, instead of being reconstructed from memory afterward.

---

## 5. Session close

1. Write blueprint YAMLs into `clickhouse-api/app/corpus/data/blueprints/`.
   **Flat files only** — `loader.py:78` globs `data_dir.glob("*.yaml")`, non-recursive. A
   subfolder is silently invisible: no error, the blueprint simply never loads.
2. Write eval fixtures into `tests/eval/fixtures/routing/`.
3. Run `clickhouse-api/tools/check_corpus_parity.py --write` to regenerate
   `MANIFEST.sha256` and `BLUEPRINTS_SHA`. These are content fingerprints; a file added
   without regenerating them fails parity. At mining volume, doing this by hand is a drift
   generator — the tool owns it.
4. Emit suggested PR metadata. One PR: N blueprints, N fixtures, one manifest bump.

---

## 6. Coverage map

At mining scale the interesting output is not the individual file — it is what is *missing*.

Derive from `uses` across the whole corpus: which tables and columns no blueprint touches,
and which question archetypes are unrepresented. Surface it at session close so the next
session has a target.

Without it, experts mine what is familiar and the corpus launches with five variants of
headcount and nothing on deductions.

---

## 7. Pre-launch drift sweep

`catalog_sha` is `''` in every canon blueprint today. Blueprints authored in week 1 against
a schema that moves by week 4 go stale silently.

Before go-live, run one sweep: re-execute `explain_ok` + replay across the entire corpus,
and treat every failure as a launch-checklist item. Cheap, once, and it converts an unknown
into a list.

---

## 8. Trust partition — no change required

`BlueprintSeed` defaults to `source="mcp", verified=True` (`corpus/seeds.py:49`); the
learning landing writer overrides to `learning`/`False` so a landed node stays out of the
trusted recall partition until a human promotes it.

Expert-authored blueprints land as `mcp`/`True`, like every existing fixture. Pre-launch
this is correct and needs no governance layer — hand-authored *is* canon at this stage, and
there is no live user to protect. The replay gate (§4.7) is what earns that placement.

Recorded here only so the decision is explicit rather than inherited from a default. See
§9.2 for the one variant worth considering.

---

## 9. Open questions

### 9.1 Where do the expert's steps and assumptions go?

The blueprint schema has `intent` (one line) and nothing else. Multi-step reasoning and
stated assumptions have no home.

This is the most perishable thing the expert produces — the reasoning is in their head
today and gone at go-live — and in the worked example the *correction* to a wrong
assumption was the single most valuable output of the entry. Today it would survive only as
a YAML comment nothing reads at runtime (which is exactly where
`bp-overtime-by-department`'s `type_code` note lives).

Three options:

- **`intent` prose.** Zero cost, matches current practice (`bp-hires-in-range` encodes
  "rehires are not re-counted" this way). Caps out fast — one line cannot hold three steps
  and two assumptions.
- **Paired knowledge entry.** Uses the existing second corpus, retrievable, no schema
  change. Splits one authored thing across two files with no link between them.
- **New blueprint field.** Correct shape, highest cost: loader, retrieval projection,
  `Blueprint.parse`, and the MCP field set in `mcp_export.py` all change.

Settle before the first mining session — retrofitting means re-interviewing the experts.

### 9.2 Does generated-mode SQL get a different `status`?

A blueprint whose SQL the expert wrote and a blueprint whose SQL the LLM wrote from prose
steps pass identical gates but carry different evidence. Worth being able to tell them
apart later — for a targeted re-review, or when a class of failures turns out to correlate
with authoring mode.

Options: a distinct `status`, a `created_by` value (the field already exists on
`BlueprintSeed`, currently `"seed"` / `"learning"`), or nothing. `created_by` is the
cheapest and probably the answer, but it is a canon-visible field, so decide deliberately.

---

## 10. What is new vs reused

**Reused unchanged:** `generalize/rewrite.py`, `generalize/canonical.py`,
`generalize/validate.py`, `promotion/replay.py`, `promotion/mcp_export.py`,
`sqlparse::extract_column_provenance`, `runtime/blueprint/structural_key.py`,
`runtime/blueprint/models.py::Blueprint.parse`, `tools/check_corpus_parity.py`.

**New:** the CLI and session file (§3), the prior-art prompt and its near-miss presentation
(§4.2), the human-decision halts (§4.6), the result-row accept step for generated SQL (§2),
the eval-fixture emitter (§4.9), and the coverage map (§6).

The core is an adapter. Most of the line count is interaction, not machinery.

---

## 11. Tests

- **Unit.** Session-file round trip; prior-art outcomes (exact / near-miss / none) against a
  fixture corpus; the reserved-key and flat-file guards; the drafting-completeness check for
  the fields §4.5 lists as commonly omitted.
- **Contract.** Every emitted YAML parses through `loader.py` + `Blueprint.parse`. Emitted
  eval fixtures load in the existing `tests/eval` harness.
- **Live.** One end-to-end per input mode against the dev stack, asserting that a blueprint
  with a deliberately wrong column fails `explain_ok`, and one with a broken grain fails
  replay — i.e. that the gates are actually wired, not just called.
- **Regression.** A duplicate submission halts rather than emitting a second file.
