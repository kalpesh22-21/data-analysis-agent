# 02 — Enriched Blueprint Search Cards

**Spec:** [§4](../release-1-routing-and-intent-coverage.md) · **Size:** S · **Depends on:** nothing · **Blocks:** 07

## Why

Per-deliverable `searchBlueprints` becomes the default path (spec §3 step 3). Each thin candidate today costs a `getBlueprint` round-trip to evaluate, so a four-deliverable request can spend four extra round-trips just deciding.

## Current state (verified)

**`ThinCard`** (`retrieval/models.py:36`) — `{id, intent, slots_summary, score}`. Nothing else.

**`Candidate`** (`retrieval/models.py:24`) — `{id, kind, text, uses, payload: dict[str, Any], score}`. **`payload` is already a free-form dict**, so enrichment needs no dataclass change on the recall side.

**The recall Cypher** (`retrieval/vector_index.py:158`):

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

**The key finding: the enrichment data is already on the node and simply is not selected.** `_GET_BLUEPRINT_QUERY` (vector_index.py:283) reads `resolves_json`, `slots_json`, `uses_rules_json`, `composes_json`, `result_grain_json` from the same `:Blueprint` node. So enrichment is a `RETURN`-clause change — **no extra round-trips, no N+1, no corpus change**.

**Mapping** — `_to_candidate` (vector_index.py:242) already builds `payload={"intent": text, "slots_summary": …}`.

**The tool** — `SearchBlueprintsTool._execute` (`retrieval/tools.py:319`) builds `result_full["blueprints"]` from the four `ThinCard` fields.

## Changes

1. **Cypher** — add `node.resolves_json`, `node.slots_json`, `node.result_grain_json` to the recall `RETURN`.
2. **`_to_candidate`** — decode via the existing `_decode_json` helper and put the decoded values in `payload`. Fail-soft: a corrupt or absent JSON prop yields `None`, exactly as `_to_blueprint_detail` already does.
3. **`ThinCard`** — add three nullable fields, defaulted so every existing construction site stays valid:
   ```python
   resolves: dict[str, str] | None = None
   slots: tuple[SlotSummary, ...] | None = None   # name, type, required
   result_grain: tuple[str, ...] | None = None
   ```
4. **`SearchBlueprintsTool._execute`** — emit them in `result_full["blueprints"]`, omitting keys that are `None` so a blueprint with no stored DAG serialises exactly as before.
5. **Tool description** (`mcp/tool_schema.py::SEARCH_BLUEPRINTS_TOOL_SCHEMA`) — currently tells the model to "call `getBlueprint(id)` to expand the one you pick". Amend: the card now carries slots and pinned term resolutions, so `getBlueprint` is needed only for the full DAG or a composition summary.

## Two decisions to record

**Drop `status` from the card.** Spec §4 lists it, but recall filters `coalesce(node.status,'validated') = 'validated'` — every recalled card is validated by construction, so the field is constant and carries no information. It stays on `getBlueprint`, where a keyed fetch *can* return a non-validated blueprint. Including it in search results would imply a distinction that cannot occur.

**Slots are summarised, not full.** Emit `{name, type, required}` per slot — enough to judge applicability and to fill `runBlueprint`. Do **not** emit `binds_to`, `enum_values`, `optional_pattern` or the numeric bounds; those are execution detail, they are what `getBlueprint` is for, and they would inflate every card in a list of `k` (default 5, max 20).

## Pre-injected cards

`RetrievalPipeline.retrieve` feeds the same `ThinCard` type into `render_retrieved_context`, so enrichment flows into the **pre-injected block** too. That is desirable — the model's first-round decision improves — but it grows a block that sits inside the pinned current-turn region of `fit_request_to_budget`.

`retrieval/render.py` applies `_MAX_FIELD_CHARS = 500` per card field. **Confirm new fields are routed through the same sanitiser and cap** (`_sanitize`), or a blueprint with 16 slots becomes an unbounded card. Structural sanitisation is not optional here: newlines in an interpolated field could otherwise forge message structure.

## Edge cases

- **Blueprint with no stored DAG** — all three fields `None`, keys omitted, byte-identical output to today.
- **Corrupt JSON prop** — `_decode_json` returns `None`; card degrades, never raises.
- **`FakeVectorIndex` / in-memory index** (`retrieval/vector_index.py:408`) needs the same payload keys or Layer-1 tests will diverge from live behaviour.
- **Card-size ceiling** — `slots_summary` already exists as a short string. Keep it; the structured `slots` is additive, not a replacement, so nothing downstream that reads `slots_summary` breaks.

## Tests

| Test | Asserts |
|---|---|
| `tests/runtime/retrieval/test_vector_index.py` | Recall maps the three new props into `payload`; corrupt JSON → `None`, no raise |
| `tests/runtime/retrieval/test_tools.py` | `searchBlueprints` `result_full` carries the new keys; omits them when `None` |
| `tests/runtime/retrieval/test_render.py` | New fields are sanitised and length-capped like existing card fields |
| Live: `tests/integration/test_read_tools_live.py` | Enriched card round-trips against real neo4j |

## Done when

- [ ] Recall returns the three props; `payload` carries them; `ThinCard` exposes them.
- [ ] `status` deliberately absent from search cards, with the reason in a code comment.
- [ ] Slots summarised to `{name, type, required}` only.
- [ ] New fields sanitised + capped in `render.py`.
- [ ] Fake index parity.
- [ ] `searchBlueprints` tool description amended.
