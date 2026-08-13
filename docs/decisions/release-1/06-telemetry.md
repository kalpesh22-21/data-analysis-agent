# 06 — Raw Routing Telemetry

**Spec:** [§8](../release-1-routing-and-intent-coverage.md) · **Size:** S · **Depends on:** 03, 05 for the emit sites · **Blocks:** 07

> **AMENDED after the live run (README finding 20).** Two additions for call-time intent tagging: `loop_intent_completed` and `loop_intent_blocked` carry `evidence_binding` — a closed enum — so route derivation knows how the binding was established, not just which tool produced it; and `loop_intent_tag_dropped{tool_name, reason}` fires when a `serves_intent` names an unknown or non-live intent (the work still runs, the entry is untagged). D25: `evidence_binding` is on `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST`; the dropped TAG VALUE is never emitted, because an invalid tag is arbitrary model text where a valid one is a runtime-assigned id.

> **AMENDED 2026-08-12 with the schema trim ([03 §C.3.2](03-analysis-state.md), [04 §B.7](04-evidence-validators.md)).** `evidence_binding`'s enum is now **`tagged` | `auto_bound`**. `declared` — the model citing a `tool_call_id` itself — was retired with the field and can no longer be produced; a member of a telemetry enum that nothing writes is a bucket that silently never fills, so it was removed rather than left in place. `auto_bound` is the WEAKER of the two by construction: the model named nothing and the runtime bound the one call that could have served the intent. One new event, `loop_analysis_state_auto_bound`, is added to the table below.
>
> ⚠ **The `loop_` prefix on that name is load-bearing, not a convention.** `guardrail_observer` drops every event that lacks it, silently — so an event named `analysis_state_auto_bound` fires in unit tests and reaches production telemetry never. It shipped that way for one review round and was caught on review, which is why `tests/runtime/observability/test_tool_span_wiring_e2e.py` now asserts this event **through `guardrail_observer` itself**: a raw-recorder assertion cannot fail for this reason.

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
| `loop_intent_completed` | `{intent_id, evidence_tool_name, evidence_binding}` | 03 tool. **`evidence_tool_name` is what makes `route` derivable** — `runBlueprint` ⇒ blueprint, `runQuery` ⇒ ad-hoc, `getTableSchema` ⇒ metadata |
| `loop_intent_blocked` | `{intent_id, reason_code, evidence_tool_name, evidence_binding}` | 03 tool, on every **model-declared** block. The block-side mirror of `loop_intent_completed`, and for the same reason: without `evidence_tool_name`, a `NO_ACCESS` manufactured from one `getTableSchema(<scratch_db>, …)` probe (04 §B.4's cheapest route) is **telemetrically identical** to one earned by a `runBlueprint` that hit `COLUMN_SCOPE_VIOLATION` on the user's own data — and the release keeps those holes open on the basis that they are *measured*. A separate event rather than a key on `loop_analysis_state_transition`, which 05's forcing path also emits with no evidence at all; `loop_intent_force_blocked` stays the runtime's counterpart. The evidence's `tool_call_id` is deliberately **not** emitted here. *(2026-08-12: "model-declared" now means the model declared the BLOCK; `reason_code` itself is DERIVED from the bound call — 04 §B.7 — so this event reports a runtime classification, which is strictly more trustworthy than the label it replaced.)* |
| `loop_metadata_evidence_completion` | `{intent_id}` | 03 tool, when evidence is a `getTableSchema`. The mitigation for §6.1's accepted trade. No block-side sibling is needed: `loop_intent_blocked.evidence_tool_name == "getTableSchema"` is the same signal, without a second event name |
| `loop_evidence_reused` | `{intent_id, tool_call_id}` | 03 tool, when a `tool_call_id` already backs another intent |
| `loop_analysis_state_auto_bound` | `{intent_id}` | 03 tool (2026-08-12), once per intent the AUTO-BIND BACKSTOP bound — i.e. the model closed an intent and had tagged nothing for it. Emitted **after** the state write, so a binding the merge then refused (an unknown id, 04 §B.3's distinctness rule) is not counted. **Read it as a warning, not a success**: the backstop rescuing a turn and the backstop papering over a model that never tags look identical without this counter, and a high rate means the tag is not landing. Shape only — no `tool_call_id`, no candidate count |
| `loop_finalization_refused` | `{exit: "answer_with_table" \| "no_tool_calls", pending_count}` | 05, both exits |
| `loop_finalization_block_spent` | `{window}` | 05, when a per-window forced re-round is consumed. Consumed **once per round-trip**, not once per refused call. Since §J.3 there are **two independent allowances per window** (`intents`, `answer_shape`), so this can legitimately fire twice in one window. WHICH one is derivable from the refusal event that follows it in the same round-trip — deliberately not duplicated onto this payload |
| `loop_zero_row_block` / `loop_zero_row_completion` | `{intent_id}` | 03 tool — the 04 §B.4 ratio: an empty result set treated as a block versus as an answer |
| `loop_intent_force_blocked` | `{intent_id, reason_code}`, **plus `budget_cap_reached: True` on the refused-round path only** | 05, all **four** forced paths (hard ceiling · `"stop"` resume · enforcement exhausted · budget cap reached during a refused round) |
| `loop_enforcement_exhausted` | `{intent_count}` | 05 — dedicated counter, per spec §7 |
| `loop_analysis_state_late_init_rejected` | `{proposed_count, blocking_tool_name}` | 03 boundary check. `blocking_tool_name` is which substantive tool had already run — diagnostic, and it is a tool name, not content |

| `loop_analysis_state_rejected` | `{reason: "<validation_rule>", intent_count}` | 03 tool, on **every** `ANALYSIS_STATE_INVALID`. `reason` is an enum of rule names, never the offending value |
| `loop_answer_shape_refused` | `{multi_row_calls}` | 05 §J (2026-08-12), when a bare-text finish is refused for holding untabled multi-row results. **`multi_row_calls` is the whole signal**: one untabled result is a model that forgot a table, five is a model that abandoned the format, and a bare count cannot tell them apart. It counts successful `runQuery`/`runBlueprint` CALLS with `row_count > 1` — not rows, not results, and never the SQL or a cell |
| `loop_answer_shape_exhausted` | `{}` | 05 §J, when a qualifying bare-text finish passes because **this gate's own** per-window allowance is gone — i.e. it already refused once in this window. Payload-free on purpose: the interesting fact is the count of these against `loop_answer_shape_refused`, and anything narrower belongs on the refusal. **It briefly meant two things** — the gate shipped sharing the intents allowance, so this also fired when the intents nudge took it, and that ambiguity is what hid the starvation in §J.3's traces. One meaning now |

Fifteen events (eleven at design time, plus `loop_intent_blocked` added when the adversarial pass found the block side unmeasurable, plus `loop_analysis_state_auto_bound` added with the 2026-08-12 schema trim, plus the answer-shape pair added with 05 §J). The rejection event was missing and the README's own convention requires it — *"every new failure path emits an observer event"* — and 03 §C.3 defines nine rejection rules landing on one code. The immutability violation is the single most interesting adversarial signal in the release (it is the model attempting the evasion 03 §C.4 exists to close) and would otherwise be invisible outside a test.

## ⚠ Emitting an event does not publish its payload

`_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` (`observability/tracing.py:598`) is a **strict attribute allowlist**, deliberately not a type filter — the comment explains that a bare `isinstance` check would have leaked `loop_paused_ask_user`'s `question`. It contains exactly: `window`, `tool_calls_made`, `tool_name`, `tool_call_id`, `deduped`, `guard_reason`, `dedup_target`, `note`, `database`, `table`.

Of every payload key proposed above, **only `window` is on it.** Every other event would reach Phoenix as a correctly-named GUARDRAIL span carrying **zero attributes** — including `loop_intent_completed`, whose entire purpose is the one attribute it holds.

**Extend the allowlist** with `intent_count`, `turn_index`, `intent_id`, `from_status`, `to_status`, `reason_code`, `evidence_tool_name`, `exit`, `pending_count`, `proposed_count`, `blocking_tool_name`, `reason`. Each addition is a deliberate D25 declaration that the key is shape-only — which is why the list is manual. **`description` must never be added.**

**Later addition: `budget_cap_reached`**, for [05 §F.0](05-finalization-enforcement.md#f0-the-fourth-path-is-enforcement_exhausted-reversed-2026-08-12-on-measurement). The cap-during-a-refused-round path now writes `ENFORCEMENT_EXHAUSTED` on the intent, because enforcement rather than capacity is what ran out there — measured: the answer existed at 25s, the turn capped at 61.6s on the **wall clock**, tokens moved +385 across the final three rounds. **The capacity fact still matters to an operator**, so it lives here instead: a bare `True` on `loop_intent_force_blocked`, present on that path and **omitted on the other three**, so their payloads are byte-unchanged and a dashboard split by `reason_code` is unaffected. D25-safe by shape — a boolean cannot carry user content, which is the same argument as `deduped`. This is the pattern to reuse when a reason code has to become more honest and a fact would otherwise be lost with it: **move the fact to telemetry, do not overload the code.**

**Later additions: `refetch_count` + `refetch_cap`**, for the trim-aware re-fetch exemption — small integers about the LOOP'S OWN decisions (no read arguments, no result content), and the capped event says nothing without them. **And `blueprint_id`**, for the getBlueprint-before-runBlueprint gate ([02](02-blueprint-card-enrichment.md#the-partial-reversal-getblueprint-before-runblueprint)). A blueprint id is CORPUS-AUTHORED, never user content — the same class as the `database`/`table` identifiers already on the list. Neither the blueprint's SQL nor the user's question is ever placed on a span.

**Later addition: `multi_row_calls`**, for the answer-shape gate ([05 §J](05-finalization-enforcement.md#j-the-answer-shape-gate-added-2026-08-12-on-measurement)). A count of the LOOP'S OWN bookkeeping — how many successful multi-row data calls the turn was holding when it tried to finish in prose. Not the row counts, not the SQL, not a cell. It is the only attribute either answer-shape event has, so without it `loop_answer_shape_refused` is a correctly-named span carrying nothing — which is the failure this section exists to prevent, and the reason the wiring is asserted through the real `guardrail_observer` in `tests/runtime/observability/test_tool_span_wiring_e2e.py` rather than through a recorder.

Note this is a different mechanism from `OTLP_DROP_SPAN_NAMES`, which filters span *names* one layer later. Passing that filter says nothing about whether the attributes survived.

**The tool must be constructed with `observer` and `tracer` — and that breaks the precedent 03 tells you to copy.** The two *composite* runtime tools take neither: `RecordAssumptionsTool()` (`app.py:576`) and `AnswerWithTableTool()` (`app.py:583`), and `RecordAssumptionsTool.run` is `(arguments, credentials)` only. `_run_runtime_tool` (`agent_loop.py:1120`) emits no dispatch events on their behalf either. It is the three *retrieval read tools* that take both (`app.py:589-612`). So mirroring `record_assumptions.py` literally produces a silently-mute tool: `UpdateAnalysisStateTool` must take `observer` and `tracer` **and** self-emit `tool_dispatch_start`/`ok`/`error` the way the read tools do.

## Already emitted — do not duplicate

These exist and the eval harness should read them rather than adding parallel counters:

- `tool_dispatch_start` / `tool_dispatch_ok` / `tool_dispatch_denied` / `tool_dispatch_error` — carry `tool_name`, so **`searchBlueprints` calls per turn** and **blueprint runs vs ad-hoc queries** are already derivable.
- `loop_repeated_idempotent_read_guarded` — now fires for `getBlueprint` too (it joined
  `IDEMPOTENT_READ_TOOLS`), carrying `blueprint_id` + `dedup_target`.
- `loop_blueprint_definition_not_read{tool_name, blueprint_id, reason}` — the gate refused a
  `runBlueprint` whose id this turn never expanded. **The signal to watch:** a nonzero
  rate means the model is still trying to run blueprints it has not read, and every one
  costs a wasted round-trip.
- `loop_trimmed_read_refetch_allowed{tool_name, dedup_target, reason, refetch_count, refetch_cap, …}`
  — a duplicate read was re-dispatched instead of deduped, because the result that served it is
  no longer readable in the rebuilt window (trimmed by `fit_request_to_budget`, or rendered as a
  data-free sentinel). Applies to every tool in `IDEMPOTENT_READ_TOOLS`. A rising rate means turns
  are running long enough for the budget to reach current-turn tool pairs.
- `loop_trimmed_read_refetch_capped{…}` — **the one worth an alert.** The same signature has
  exhausted its per-window re-fetch allowance, so the guard has taken over again. It means the turn
  is THRASHING: re-reading something the budget keeps dropping. The fix is upstream — pinning,
  summarisation, or the budget itself — not here. See [README finding 23](README.md) and its
  *pinning versus re-fetching* follow-up.
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

- [ ] Every event in the table above emitted at its site through the existing observer seam.
- [ ] No new infrastructure; no duplicate counters for facts already derivable from dispatch events.
- [ ] D25 scan test green.
- [ ] Events reach Phoenix (not dropped by the default filter).
- [ ] Corpus-gap inference documented as harness-side, not loop-side.
