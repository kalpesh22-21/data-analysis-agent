# 02 — Enriched Blueprint Search Cards

**Spec:** [§4](../release-1-routing-and-intent-coverage.md) · **Size:** S · **Depends on:** nothing · **Blocks:** 07

## Why

Per-deliverable `searchBlueprints` becomes the default path (spec §3 step 3). Each thin candidate today costs a `getBlueprint` round-trip to evaluate, so a four-deliverable request can spend four extra round-trips just deciding.

## Current state (verified)

**`ThinCard`** (`retrieval/models.py:36`) — `{id, intent, slots_summary, score}`. Nothing else.

**`Candidate`** (`retrieval/models.py:24`) — `{id, kind, text, uses, payload: dict[str, Any], score}`. **`payload` is already a free-form dict**, so enrichment needs no dataclass change on the recall side.

**The recall Cypher** — `_BLUEPRINT_RECALL_QUERY`, `retrieval/vector_index.py:157` (there are two recall queries):

```cypher
CALL db.index.vector.queryNodes($index_name, $k, $query_vector)
YIELD node, score
WHERE node.embedding_model = $expected_model
  AND coalesce(node.status, 'validated') = 'validated'
  AND coalesce(node.drift_status, 'clean') <> 'suspect'
  AND node.source = 'mcp'
RETURN node.id AS id, node.intent AS text, node.slots_summary AS slots_summary,
       node.uses AS uses, score
ORDER BY score DESC
```

**The key finding: the enrichment data is already on the node and simply is not selected.** `_GET_BLUEPRINT_QUERY` (vector_index.py:280) reads `resolves_json`, `slots_json`, `uses_rules_json`, `composes_json`, `result_grain_json` from the same `:Blueprint` node — written there by `corpus_loader.py:580`. So enrichment is a `RETURN`-clause change — **no extra round-trips, no N+1, no corpus change**.

**Mapping** — `map_blueprint_record` (vector_index.py:227) already builds `payload={"intent": text, "slots_summary": …}`. (The detail mapper is `map_blueprint_detail_record`, vector_index.py:313.)

**The tool** — `SearchBlueprintsTool._execute` (`retrieval/tools.py:319`) builds `result_full["blueprints"]` from the four `ThinCard` fields.

## Changes

1. **Cypher** — add `node.resolves_json`, `node.slots_json`, `node.result_grain_json` to the `_BLUEPRINT_RECALL_QUERY` `RETURN`.
2. **`map_blueprint_record`** (vector_index.py:227) — decode via the existing `_decode_json` helper and put the decoded values in `payload`. Fail-soft: a corrupt or absent prop yields `None`, exactly as `map_blueprint_detail_record` already does.
3. **⚠ `RetrievalPipeline._to_thin_card`** (`pipeline.py:354`) — **the site the first draft missed.** This is where `Candidate.payload` becomes a `ThinCard`, and it hard-codes four fields. Build everything else without it and the new payload keys are dropped on the floor: `searchBlueprints` returns exactly what it does today, silently, with no error and no test failure unless something asserts the new keys end-to-end.

   It is also **the only place the slot projection is enforced.** `slots_json` stores the *authored* slots, including `binds_to: dbpcm_warehouse.employee.department_name` and `optional_pattern`. Project here to `{name, type, required}`, capped at 6 per card, and nowhere else — `payload` may carry the raw decoded slots, the card may not. Without this, fully-qualified column paths ship on every card, which is both the bloat this doc wants to avoid and the provenance problem below at full strength.
4. **`ThinCard`** (`retrieval/models.py:36`) — add three nullable fields, defaulted so every existing construction site stays valid:
   ```python
   resolves: dict[str, str] | None = None
   slots: tuple[SlotSummary, ...] | None = None   # frozen dataclass: name, type, required
   result_grain: tuple[str, ...] | None = None
   ```
   `BlueprintDetail.result_grain` is `list[str] | dict[str, Any] | None` (`retrieval/models.py:88`) and a dict-shaped `{columns, verifiable}` grain is legal (`blueprint/models.py:359`) — coerce to the column tuple, dropping the flag.
5. **`SearchBlueprintsTool._execute`** (`retrieval/tools.py:319`) — emit them in `result_full["blueprints"]`, omitting keys that are `None` so a blueprint with no stored DAG serialises exactly as before.
6. **Tool description** (`mcp/tool_schema.py::SEARCH_BLUEPRINTS_TOOL_SCHEMA`) — currently tells the model to "call `getBlueprint(id)` to expand the one you pick". Amend: the card now carries slots and pinned term resolutions, so `getBlueprint` is needed only for the full DAG or a composition summary. **Coordinate with 01** — `prompts.py:120` gives the model the same stale instruction, and the prompt is re-sent every round-trip.

## Two decisions to record

**Drop `status` from the card.** Spec §4 lists it, but recall filters `coalesce(node.status,'validated') = 'validated'` — every recalled card is validated by construction, so the field is constant and carries no information. It stays on `getBlueprint`, where a keyed fetch *can* return a non-validated blueprint. Including it in search results would imply a distinction that cannot occur.

**Slots are summarised, not full.** Emit `{name, type, required}` per slot — enough to judge applicability and to fill `runBlueprint`. Do **not** emit `binds_to`, `enum_values`, `optional_pattern` or the numeric bounds; those are execution detail, they are what `getBlueprint` is for, and they would inflate every card in a list of `k` (default 5, max 20).

## ⚠ Provenance — enrichment changes the search card's D44 posture

`_ok`'s provenance defaults to safe-empty `frozenset()` (`retrieval/tools.py:141`), and the docstring says why: that is *"correct for searchBlueprints/searchKnowledge … `getBlueprint`'s FOUND path overrides it with the blueprint's scoped `uses` footprint so the entry drops under a later scope narrowing"* (`tools.py:481`). The premise is that a search card carries **no column identifiers**.

Enrichment breaks that premise. `resolves` is term → **column name** (`salary: annual_salary`); `result_grain` is a column/alias list. With `frozenset()` provenance, `is_provenance_in_scope` returns `True` unconditionally, so a `searchBlueprints` entry naming those columns is **kept in replay forever**, including after the caller's scope narrows — exactly the leak `getBlueprint`'s override closes, reintroduced on the tool that returns *k* cards at once.

**Decision: set the search entry's provenance to the union of the returned cards' `uses`.** Every card returned is already in-scope (recall pre-filters on `uses ⊆ scope`), so the union is in-scope at write time; if scope later narrows past any of it, the whole entry drops. Whole-entry granularity is coarse but fail-closed, and it is the same granularity every other multi-column entry has.

*Alternative, if the coarseness proves painful:* restrict cards to `slots` only (`{name, type, required}` carries no column identifier once `binds_to` is excluded) and drop `resolves`/`result_grain`. That keeps `frozenset()` honest but gives up most of the routing value.

## Pre-injected cards

`RetrievalPipeline.retrieve` feeds the same `ThinCard` type into `render_retrieved_context`, so enrichment flows into the **pre-injected block** too. That is desirable — the model's first-round decision improves — but it grows a block that sits inside the pinned current-turn region of `fit_request_to_budget`.

`retrieval/render.py` applies `_MAX_FIELD_CHARS = 500` **per field** via `_sanitize` (`render.py:59,64`). Route the new fields through it — newlines in an interpolated field could otherwise forge message structure.

**But per-field capping does not bound the card.** Sixteen slots at 500 chars each is bounded per field and unbounded per card. Cap the *collection*: at most **6 slots per card** with a `(+K more)` marker.

**And the trail-replay cost is larger than the pre-injection cost.** With per-deliverable search as the default path, a four-deliverable turn persists four `searchBlueprints` trail entries of up to `k` cards each (`retrieval_search_default_k=5`, `max_k=20`), each replayed on every later round-trip of the turn. `_build_preview` sends that shape to `_cap_nontabular_result` with a 4,000-token cap — and because the result dict has no top-level `columns` key, the over-cap path is the **stringify-and-truncate** branch (`tool_dispatcher.py:178`), so the model receives a mangled JSON string instead of a card list. Add a test at `k=max_k` with a maximally-slotted blueprint asserting the preview is not truncated.

## Edge cases

- **Blueprint with no stored DAG** — all three fields `None`, keys omitted, byte-identical output to today.
- **Corrupt JSON prop** — `_decode_json` returns `None`; card degrades, never raises.
- **`FakeVectorIndex`** (`retrieval/vector_index.py:61`) builds no candidates of its own — tests seed `Candidate` objects directly (`:87`), so parity is a **fixture** change, not an index change.
- **Card-size ceiling** — `slots_summary` already exists as a short string. Keep it; the structured `slots` is additive, not a replacement, so nothing downstream that reads `slots_summary` breaks.

## Tests

| Test | Asserts |
|---|---|
| `tests/runtime/retrieval/test_vector_index.py` | Recall maps the three new props into `payload`; corrupt JSON → `None`, no raise |
| `tests/runtime/retrieval/test_read_tools.py` | `searchBlueprints` `result_full` carries the new keys; omits them when `None` |
| `tests/runtime/retrieval/test_render.py` | New fields are sanitised and length-capped like existing card fields |
| Live: `tests/integration/test_read_tools_live.py` | Enriched card round-trips against real neo4j |

> **Sequencing note.** 03 §D promotes `retrieval/render.py::_sanitize` to a shared `runtime/context/sanitize.py`. If 03 lands first, import from there.

## Done when

- [ ] Recall returns the three props; `payload` carries them; **`_to_thin_card` (pipeline.py:354) projects them onto the card**; `ThinCard` exposes them.
- [ ] Slots projected to `{name, type, required}` in `_to_thin_card`, capped at 6 per card.
- [ ] Search-entry provenance set to the union of returned cards' `uses`; D44 narrowing test added.
- [ ] `status` deliberately absent from search cards, with the reason in a code comment.
- [ ] Slots summarised to `{name, type, required}` only.
- [ ] New fields sanitised + capped in `render.py`.
- [ ] Fake index parity.
- [ ] `searchBlueprints` tool description amended.

---

## The partial reversal: `getBlueprint` before `runBlueprint`

*Added after 02 shipped. **Decided by the user**, recorded here because 02 is the deliverable it partially reverses — and it is a partial reversal, not a full one.*

### What the user decided

**Before running a blueprint the model must call `getBlueprint(id)` and read the blueprint's SQL.** The runtime enforces it: a `runBlueprint` for an id this turn has not expanded is refused with `BLUEPRINT_DEFINITION_NOT_READ` — retryable, before the executor runs, naming the blueprint and the fix in `denial_detail`.

### Why

**A card carries no SQL.** That was a deliberate 02 decision and it stands for *choosing*: `intent`, `slots`, `resolves` and `result_grain` are enough to judge applicability. But it means the model has been **deciding to execute an analysis on the strength of an authored prose `intent` string**. If that string misdescribes the query stored underneath it — a corpus-authoring property no runtime check touches — the model runs the wrong analysis and reports the figure confidently.

**The D56 grain gate does not catch this, and it is easy to think it does.** `verification: {method: blueprint_gate, grain_checked: true}` verifies that the RESULT SHAPE matches the blueprint's OWN declared `result_grain`. It answers *"did this blueprint do what it says it does"*. It cannot answer *"does what it says it does answer the question the user asked"* — the deliverable is nowhere in that check. A blueprint can run cleanly, verify cleanly, earn `authoritative`, and be measuring the wrong thing.

The prompt already carried the general rule — *"Success is not proof of correctness: a query that runs proves the SQL was valid, not that it measured what was asked"* — but on the blueprint route it had **no object**: there was nothing for the model to check the deliverable against. The gate is what gives that line something to bite on, and the prompt now says so explicitly.

### What 02 still saves, and what it no longer saves

| | Before the rule | After the rule |
|---|---|---|
| Choosing among `k` search hits | 0 `getBlueprint` (the enrichment) | **0 `getBlueprint` — unchanged** |
| Running the one you picked | 0 `getBlueprint` | **1 `getBlueprint`** |
| A 4-deliverable request, 5 candidates each | 0 | **4** (one per blueprint actually run, not 20) |

**02's saving was never mostly about the run.** Its stated problem was *"each thin candidate today costs a `getBlueprint` round-trip to evaluate, so a four-deliverable request can spend four extra round-trips just deciding"* — evaluation, per candidate, across the whole result set. That saving is untouched: `slots`, `resolves` and `result_grain` are still on the card, the model still picks without expanding anything, and the enrichment is still what makes per-deliverable search affordable. **Do not read this section as "02 was pointless."** What is reversed is one sentence of 02 §Changes item 6 — that `getBlueprint` is needed *only* for the full DAG, a composition summary, or `slots_omitted`.

**And the round-trip cost is not per deliverable.** The gate is satisfied by any successful `getBlueprint` earlier in the turn, and the model may batch: `[getBlueprint(a), getBlueprint(b), getBlueprint(c)]` in one response, `[runBlueprint(a), runBlueprint(b), runBlueprint(c)]` in the next. **Three deliverables cost two round-trips, not six.** Measured, not asserted: `tests/runtime/loop/test_blueprint_definition_gate.py::test_batched_expand_then_run_costs_two_round_trips_for_n_deliverables` counts model calls at N=2 and N=3, and the A1 fixtures (cases 2, 3, 4, 7) were rewritten into exactly that shape.

### Two accepted costs, stated so they are not rediscovered as bugs

**1. Turn-scoping means a follow-up turn re-expands.** A `getBlueprint` from an earlier turn does **not** satisfy the gate. That is deliberate: context is rebuilt per turn and trimmed by `fit_request_to_budget`, so a definition fetched in turn 1 may have been summarized away by turn 5, and the rule is about what the model can read *now*, not what it once read. The cost lands on a real and common shape — *"now show me just Engineering's average"*, previously 2 tool calls (`runBlueprint` + answer) — which now pays one extra `getBlueprint` per turn. Accepted.

**2. A composed blueprint does not show its SQL.** `getBlueprint`'s FOUND path exposes `sql_template` for a single-node blueprint but replaces a composed blueprint's `composes` DAG with a `composition` summary (a step count and a note) — deliberately, since per-node SQL and `$0.x` refs invite the model to hand-run steps. So for a composed blueprint the model reads the intent, the slots, the `uses` footprint, the `result_grain` and the step count, **but not the per-node SQL**. The prompt is worded to match rather than to promise SQL that is not there. Whether composed blueprints should expose a read-only rendering of their SQL is a **follow-up question**, not something to change in passing: the hiding is load-bearing against a different failure.

### Mechanism, for a reader who has to touch it

- **Gate:** `loop/agent_loop.py::_run_loop_body`, in the per-tool-call dispatch loop, before `_maybe_start_summary` and before dispatch. Predicate: `tool_call.name == "runBlueprint"` and the tool is wired and `clean_blueprint_id(args["id"]) not in blueprint_definitions_read`.
- **`blueprint_definitions_read`** is seeded from the persisted trail (turn-scoped, `status == "ok"`, `tool_name == "getBlueprint"`) so it survives the D45 per-round-trip rebuild and a budget-window `continue` resume, and is folded from the current response only **after** the batch drains — a `[getBlueprint(x), runBlueprint(x)]` pair in one message is refused, because the result of the first call does not reach the model until the next round-trip.
- **Resumes are structurally ungated.** A mid-DAG checkpoint resume re-enters `blueprint_executor.resume(...)` directly from `_resume_blueprint` and writes its own trail entry; it never reaches the dispatch site. That is correct — a resume continues an already-gated invocation — and it is pinned by a test so a refactor cannot silently start gating it.
- **`getBlueprint` was added to `IDEMPOTENT_READ_TOOLS`** at the same time (it is a keyed fetch by id, and the new rule makes it the most repeated read of a blueprint turn). A dedup-guarded repeat **satisfies** the gate: being deduped means the definition is already in context. That is the opposite of [04](04-evidence-validators.md) condition 5, which rejects the same marker as completion evidence — the two gates ask different questions.
- **One exemption to the dedup guard, and it is load-bearing.** The guard's premise is "the already-served result is in the history above", which is true of the trail but not of the rendered window after `fit_request_to_budget` trims. So a `getBlueprint` repeat is re-dispatched for real when the entry that served it is no longer in the rebuilt window — otherwise the gate would pass on a definition the model can no longer read. **The same exposure exists for `getTableSchema` and is NOT fixed here** (the prompt tells the model to re-fetch a schema "only if it was summarized away", an instruction the guard makes unfollowable) — a pre-existing defect, reported separately.
