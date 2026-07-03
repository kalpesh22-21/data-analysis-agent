# Learning-loop Slice 4 — generalize + static-validate (design)

**Status:** BUILT (Track B / Wave 1). Branch `phase0/provenance-extractor` (S4 worktree,
off Wave-0 base `2083150`). This slice builds ONLY `learning/generalize/` +
`tests/learning/generalize/`; it consumes the frozen contracts and touches no other
track's module.

S4 is the deterministic (NO LLM) stage that turns the S3 blueprint **PLAN**
(`extractor/models.py::BlueprintPayload`) + the accepted SQL into a
`BlueprintGeneralization` (Contract A, `candidate/generalization.py`) merged under
`payload["generalization"]`. It is the root of the Wave-1 fan-out: S6 hashes its
`canonical_ast_norm` + `uses_rules`, S9 replays its `sql_template` + `result_grain`.

Reference: `learning-loop-contracts-design.md` §1 (Contract A + the 1:1 `Blueprint`
mapping), §8 (fixtures), §11.2 (the pinned `canonical_ast_norm` recipe + composite
join rule), §11.6/§1 (`when`-bearing composites out of scope).

---

## 1. Where the accepted SQL comes from

The S3 PLAN carries no SQL (D35: the extractor never emits SQL). The literal-bearing
`toYear(pay_period)` in the frozen output template proves S4 must read the **original
accepted SQL**, not reconstruct it from the plan. S4 reads it from the session tool
trail: `StageContext.summary.tool_calls[*].sql`, keyed by `tool_call_ref`.

- **Single:** the accepted SQL is the last `payload.source_tool_call_refs` entry that
  resolves to a non-empty SQL (the final accepted `runQuery` of the turn).
- **Composite:** each `composes[*].source_tool_call_ref` resolves that node's SQL.

## 2. The AST rewrite (`rewrite.py`)

Parse the accepted SQL with sqlglot (`dialect="clickhouse"`). For each S3
`parameterization` entry, locate the literal predicate (`column <op> value`, matching
by column — including function-wrapped columns like `toYear(pay_period)` — and literal
text) and:

| role | template effect | `uses_rules` |
|---|---|---|
| `slot` | replace the literal with a `:slot` placeholder | — |
| `inline` | leave the literal in place | — |
| `rule` | drop the predicate from the template | add resolved `rule_id` |

**Rendering `:slot` while preserving casing.** The ClickHouse dialect preserves
`sum`/`toYear` casing but renders a placeholder as the canonical token `{slot: }`; the
default dialect renders `:slot` but upper-cases functions. S4 renders with ClickHouse,
then maps the fixed `{slot: }` token → `:slot`. This yields the exact runtime-executable
`:slot` surface `runtime/blueprint/models.py` expects, with identifier casing intact.

**Single is strict, composite nodes are lenient.** A single blueprint must locate every
slot/rule literal (a miss ⇒ `RewriteError` ⇒ fail_to_review). A composite node's SQL
references only a subset of the shared top-level params, so an absent literal is skipped
for that node.

## 3. `uses` (`builder.py` + the reused provenance extractor)

`uses` = the byte-exact `database.table.column` scope keys (D87), computed by the
existing D69 `extract_column_provenance` (NOT reimplemented) over the rewritten
template(s), joined from each `(database.table, column)` pair with `.`, sorted +
de-duplicated. Running it over the TEMPLATE (placeholders parse cleanly) keeps the step
entity-free. Composite `uses` is the union across node templates. A provenance failure
(fail-closed, D63) drives `explain_ok=False`.

## 4. Static validation (`validate.py`)

`StaticValidation` = four deterministic checks; ANY false ⇒ `outcome="fail_to_review"`
with a **stable machine reason tag** for the first failing check (field order:
explain → binds → dag → read_only). Never raises, never auto-promotes — `fail_to_review`
is an in-band value S7 routes on.

- `explain_ok` — the template resolves against the current catalog (the provenance
  qualify IS the schema dry-run; an injected MCP `explainQuery` seam can replace it later).
- `binds_to_subset_uses` — every slot `binds_to` ∈ `uses`.
- `dag_ok` — single: trivially true. Composite: unique orders, valid + acyclic
  `feeds_from` (no dangling/self/forward edge), under the node cap (16), scalar-converging
  outputs.
- `read_only_select` — a single read-only SELECT; no `*`; no dict-family (`dict*`) funcs (D52).

Reason tags: `explain_failed`, `binds_to_not_subset`, `dag_invalid`,
`not_read_only_select`, `unrewritable_sql`, `when_bearing_composite`.

## 5. `canonical_ast_norm` (`canonical.py`) — the S6 hash input, PINNED

The exact §11.2 recipe (schema-free; no full optimizer / no `qualify`):

```
parse_one(sql_template, dialect="clickhouse")
  → normalize_identifiers → normalize
  → .sql(dialect="clickhouse", normalize=True, pretty=False)
```

`:slot` → `{slot: }` survives the round-trip. **Composite:** the per-node normalized
templates in ascending `order`, joined by a single `\n`. **HASH-INPUT ONLY — never
re-parsed.** `sqlglot` is pinned `~=30.12` so the render (and therefore the D48
`canonical_key`) is byte-stable across builds. Verified byte-for-byte against the frozen
fixture (single + composite).

## 6. Fail-to-review is in-band (`builder.py`)

Un-rewritable / unparseable SQL, a missing accepted SQL, or a `when`-bearing composite
each return a valid `BlueprintGeneralization` with `sql_template=None`,
`node_templates=()`, `canonical_ast_norm=""` (an empty hash input ⇒ S6 fail-soft skips
the hard key), and `static_validation.outcome="fail_to_review"`. No guessed template is
ever emitted (D52/D97). The stage always returns `control="continue"` — the value flows
on to the S7 writer, never dropped, never raised.

## 7. The stage + the 1:1 `Blueprint` mapping

`GeneralizeStage` (`stage.py`) is the first `CandidateStage` in the frozen order
(`generalize → leakage → dedup → writer`, D102 §7.1), wired by a one-line registration at
the composition root (NOT edited here). It fills the additive `payload["generalization"]`
key, mutates no S3 field, and passes non-blueprint envelopes straight through.

`blueprint_from_generalization` (`mapping.py`) proves the §1 mapping table:
`id←minted · intent/resolves/slots←PLAN · uses_rules/sql_template/result_grain←S4 ·
composes←ComposeNodePlan ⨝ NodeTemplate (by order)` → `Blueprint.parse` with no missing
field (also reusable by S9 at promotion).

---

## 8. Tests (Layer-1, §9 slugs)

`tests/learning/generalize/` — 24 tests, all green:

- **`S4-uses-scope-key-subset`** (`test_uses_scope.py`) — byte-exact keys; `binds_to ⊆ uses`;
  a binding outside `uses` ⇒ `binds_to_not_subset` fail_to_review.
- **`S4-unrewritable-fails-to-review`** (`test_unrewritable.py`) — missing / unparseable SQL,
  an unlocatable slot literal, and an uncatalogued explain all ⇒ in-band fail_to_review;
  never raises.
- **`S4-payload-maps-to-runtime-blueprint`** (`test_maps_to_blueprint.py`) — single + composite
  round-trip onto `Blueprint.parse`, no missing field.
- **Golden** (`test_golden_canonical.py`) — reproduces the fixture `canonical_ast_norm`
  BYTE-FOR-BYTE (single + composite); composite generalization matches the whole block.
- **`when`-bearing composite** (`test_when_composite.py`) — ⇒ fail_to_review, no half-typed template.
- **Rewrite/validate units** (`test_rewrite_and_validate.py`) — role slot/inline/rule; dict-family
  + star rejection.
- **Stage** (`test_stage.py`) — additive fill, purity, non-blueprint passthrough.

---

## 9. FROZEN-CONTRACT FINDING — a fixture inconsistency (reported, not patched)

The single case of `tests/fixtures/learning/s4_enriched_blueprint.json` has
`generalization.uses_rules == ["rule.earning_record_type"]`, but its S3 input plan
(`s3_blueprint_plan.json` `single`) marks `record_type` as **role=`inline`** with
`rule_id: null` and declares **no** role=`rule` param. `uses_rules` is therefore NOT
derivable as that value from the frozen input:

- The **composite** fixture proves the derivation rule — it has no role=rule params and
  its `uses_rules == []`.
- The single template (and its `canonical_ast_norm`) keep `record_type = 'EARNING'` as an
  inline literal, consistent with role=inline — i.e. the *template* side treats it as
  inline; only `uses_rules` disagrees.

Every OTHER field of the single generalization — `sql_template`, `uses`,
`node_templates`, `result_grain`, `static_validation`, and the byte-exact
`canonical_ast_norm` — reproduces the fixture exactly. Only `uses_rules` cannot, and
hard-coding the phantom rule would mean inventing data with no locator in the input.

**Per the task's instruction I did NOT change the fixture.** S4 derives `uses_rules`
correctly-by-contract (role=rule `rule_id`s, sorted + de-duplicated), yielding `()` for
this input. Because `uses_rules` is a D48 `canonical_key` hash input, this must be
reconciled before S6 hashes against the fixture: either (a) correct the single fixture's
input to mark `record_type` role=`rule` with `rule_id="rule.earning_record_type"` (which
would also drop the literal from the template + `canonical_ast_norm`, so the whole
single fixture would need re-freezing), or (b) correct the output fixture's
`uses_rules` to `[]`. Recommend (b) — it is the minimal change and matches the
composite's proven rule and the current template.
