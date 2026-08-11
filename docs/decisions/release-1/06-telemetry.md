# 06 — Raw Routing Telemetry

**Spec:** [§8](../release-1-routing-and-intent-coverage.md) · **Size:** S · **Depends on:** 03, 05 for the emit sites · **Blocks:** 07

**Emit now, project later.** The projector is Phase 2; the events must exist in Release 1 or the data is lost while behaviour is stabilising. Nothing in Release 1 reads these events except the eval harness.

## The existing seam

There is one, already used everywhere — no new infrastructure.

`ToolObserver = Callable[[str, dict[str, Any]], None]`, threaded from `app.py` as `combine_observers(emitter.observe, _tracing_observer)` into `AgentLoop`, `ToolDispatcher`, `BlueprintExecutor` and the read tools. Emit with `self._observer("<event_name>", {...})`.

Two consumers today: `ProgressEmitter` (SSE to the UI) and the tracing observer (`observability/tracing.py`), which turns `loop_*` events into GUARDRAIL span events. Note `_tracing_observer` **ignores non-`loop_` events** — so name anything that should reach Phoenix accordingly, and check `OTLP_DROP_SPAN_NAMES` (default drops `context.assembly`, `loop_model_call_start`, `loop_turn_done`) is not filtering the new ones.

## D25 posture — non-negotiable

Shape only: counts, enums, tool names, ids the runtime generated itself.

**Never:** question text, intent `description` strings, SQL, cell values, resolved code strings, `column_scope` contents.

`intent_id` is runtime-assigned and carries no user content — safe. `description` is model-authored from the user's question — **not safe**, never emit it. This is the same line `resolveValues` telemetry already draws (counts and aggregate scores, never the resolved codes) and the reason `render.py` sanitises card fields.

## Events

| Event | Payload | Site |
|---|---|---|
| `loop_analysis_state_initialized` | `{intent_count, turn_index}` | 03 tool, on successful init |
| `loop_analysis_state_transition` | `{intent_id, from_status, to_status, reason_code}` | 03 tool + 05 forcing |
| `loop_intent_completed` | `{intent_id, evidence_tool_name}` | 03 tool. **`evidence_tool_name` is what makes `route` derivable** — `runBlueprint` ⇒ blueprint, `runQuery` ⇒ ad-hoc, `getTableSchema` ⇒ metadata |
| `loop_metadata_evidence_completion` | `{intent_id}` | 03 tool, when evidence is a `getTableSchema`. The mitigation for §6.1's accepted trade |
| `loop_evidence_reused` | `{intent_id, tool_call_id}` | 03 tool, when a `tool_call_id` already backs another intent |
| `loop_finalization_refused` | `{exit: "answer_with_table" \| "no_tool_calls", pending_count}` | 05, both exits |
| `loop_finalization_nudge_spent` | `{window}` | 05, when the per-window nudge is consumed |
| `loop_intent_force_blocked` | `{intent_id, reason_code}` | 05, all three forced paths |
| `loop_enforcement_exhausted` | `{intent_count}` | 05 — dedicated counter, per spec §7 |
| `loop_analysis_state_late_init_rejected` | `{proposed_count, blocking_tool_name}` | 03 boundary check. `blocking_tool_name` is which substantive tool had already run — diagnostic, and it is a tool name, not content |

## Already emitted — do not duplicate

These exist and the eval harness should read them rather than adding parallel counters:

- `tool_dispatch_start` / `tool_dispatch_ok` / `tool_dispatch_denied` / `tool_dispatch_error` — carry `tool_name`, so **`searchBlueprints` calls per turn** and **blueprint runs vs ad-hoc queries** are already derivable.
- `loop_repeated_idempotent_read_guarded`
- `loop_result_withheld_provenance`
- `blueprint_step`, `blueprint_rule_resolved`
- `loop_request_budget_trimmed`

**Re-derivation after an authoritative result** (spec §8's last line) needs no new event either: a `runQuery` dispatched after a `runBlueprint` whose trail entry has `authoritative=True`, within one turn, is visible in the trail. Derive it in the harness (07); do not add a loop-side detector, which would need to know which intent a query serves.

## The inferred corpus-gap signal

Spec §8 asks for what `NO_APPLICABLE_TOOL` would have asserted, without a model declaration. Reconstruct it from three existing signals:

```
turn contains a successful  searchBlueprints
  AND an intent completes with  evidence_tool_name == "runQuery"
  ⇒ the corpus was searched and fell through to ad-hoc
```

Inferred from what happened rather than declared by the model — the stronger form of the same fact, and it needs no field. Implement in the harness, not the loop.

## Where these are read in Release 1

Only the Layer-4 harness (07). No projector, no dashboard, no storage schema. If durable capture beyond the trace exporter is wanted, that is Phase 2 — say so rather than half-building it.

## Tests

| Test | Asserts |
|---|---|
| `tests/runtime/loop/test_analysis_state_telemetry.py` | Each event fires exactly once at its site with the expected keys |
| Same | **No `description` string in any payload** — scan every emitted payload for the fixture's intent text. This is the D25 regression guard |
| `tests/runtime/observability/test_tracing.py` (extend) | New `loop_*` events survive the tracing observer and are not caught by the default `OTLP_DROP_SPAN_NAMES` |

The description-leak scan mirrors how `test_tool_dispatcher.py` scans every `ToolResult` field for JWT substrings — the same technique, for the same reason.

## Done when

- [ ] Ten events emitted at the sites above through the existing observer seam.
- [ ] No new infrastructure; no duplicate counters for facts already derivable from dispatch events.
- [ ] D25 scan test green.
- [ ] Events reach Phoenix (not dropped by the default filter).
- [ ] Corpus-gap inference documented as harness-side, not loop-side.
