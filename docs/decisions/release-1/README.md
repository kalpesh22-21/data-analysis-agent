# Release 1 — Build Documents

Implementation guides for [release-1-routing-and-intent-coverage.md](../release-1-routing-and-intent-coverage.md) (the spec). The spec says *what* and *why*; these say *where* and *how*, verified against the code at `catalog_sha 1d86876c`, branch `phase0/provenance-extractor`.

| Doc | Deliverable | Spec § | Size |
|---|---|---|---|
| [01](01-prompt-rewrite.md) | Blueprint-first prompt rewrite | §3 | S |
| [02](02-blueprint-card-enrichment.md) | Enriched blueprint search cards | §4 | S |
| [03](03-analysis-state.md) | `analysisState` — tool, storage, context | §5 | L |
| [04](04-evidence-validators.md) | Completion + blocking validators | §6 | M |
| [05](05-finalization-enforcement.md) | Finalization enforcement | §7 | M |
| [06](06-telemetry.md) | Raw routing telemetry | §8 | S |
| [07](07-evaluation.md) | Layer-4 evaluation | §9 | M |

## Build order

03 → 04 → 05 are one coupled workstream and should land together; the validators are meaningless without the state, and enforcement is meaningless without the validators. 01, 02, 06 are independent and can land in any order. 07 depends on everything.

```
02 ─┐
01 ─┼─→ 03 ─→ 04 ─→ 05 ─→ 07
06 ─┘
```

## Findings from the wiring check

Five things the spec assumes that the code either already provides or does not. Each is expanded in its own doc.

1. **Card enrichment is a Cypher `RETURN` change, not N extra fetches.** `_RECALL_QUERY` (vector_index.py:158) selects only `id, intent, slots_summary, uses, score`, but `resolves_json` / `slots_json` / `result_grain_json` are already stored on the `:Blueprint` node. Adding them to the projection costs nothing per call. → [02](02-blueprint-card-enrichment.md)
2. **`status` is redundant on a search card.** Recall already filters `coalesce(node.status,'validated') = 'validated'`, so every recalled card is validated by construction. The spec lists it as an enrichment field; it carries no information there. → [02](02-blueprint-card-enrichment.md)
3. **`SessionStore` is a `Protocol` with two implementations.** Adding `analysisState` touches the protocol, `CouchbaseSessionStore`, `InMemorySessionStore`, and `SessionDoc.to_doc`/`from_doc` — four places, not one. → [03](03-analysis-state.md)
4. **The finalization nudge on exit #1 cannot be a tool message.** A `tool` message only ever reaches the model by expanding a trail entry into an `assistant(tool_calls) + tool` pair, so a standalone nudge would have to fabricate a `tool_call` the model never emitted. Use ephemeral `user`-role injection instead, threaded exactly like `discovery_canonical`. → [05](05-finalization-enforcement.md)
5. **The §5.1 late-init boundary is order-sensitive within one response.** Dispatch walks `capped_tool_calls` in array order, so the spec's own round-1 pattern (`updateAnalysisState` + 3 × `runBlueprint`) self-rejects if the model emits the blueprints first. Needs an intra-batch ordering rule. → [03](03-analysis-state.md)

## Conventions these docs assume

**Test layout mirrors the module tree.** `tests/runtime/<package>/test_<module>.py`, with adversarial cases in a sibling `*_adversarial.py` or `*_qa.py` file — see `tests/runtime/loop/` for the established pattern.

**Degrade-not-fail, never silently.** Every new failure path emits an observer event and logs server-side; nothing reaches the model as `str(exc)`.

**The model-facing error channel is `denial_detail`, not `user_message`.** `TrailEntry` has no `user_message` field, and `context/budget.py::_render_entry` regenerates model-facing text from `error_code` alone via `classify_denial`. A specific message set only on `user_message` is silently dropped. Every new error code needs a `denial_mapping.py` entry *and*, where the text must name specifics, a `denial_detail`.

**Telemetry is shape-only** (D25): counts, enums, tool names. Never question text, SQL, cell values, or resolved code strings.

**Provenance for tools that read no warehouse data is `frozenset()`, not `None`.** `None` means *undetermined* and is dropped fail-closed from replay — see the long note in `composite/record_assumptions.py` for what that cost when it was got wrong.

## Done criteria for the release

- [ ] All seven deliverables merged, `uv run pytest` green, `uv run ruff check` clean.
- [ ] `docs/02-tools-and-api.md` corrected: 12 → **15** tools, with `recordAssumptions`, `answerWithTable` and `updateAnalysisState` documented and `answerWithTable`'s terminal-exit behaviour described.
- [ ] `docs/04-blueprints.md` F2 note corrected (table intermediates are no longer rejected pre-dispatch when a scratch client is wired).
- [ ] Layer-4 harness runs the six cases and reports both metrics.
- [ ] No intent can end `pending` — asserted at Layer 1/2, not measured.
- [ ] One open spec item resolved before ship: whether enforcement exhaustion after a consumed `askUser` pause forces `USER_DECLINED_CLARIFICATION` rather than `ENFORCEMENT_EXHAUSTED` (spec finding 4, with the Lead).
