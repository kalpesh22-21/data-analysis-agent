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
