# 03 — `analysisState`

**Spec:** [§5](../release-1-routing-and-intent-coverage.md) · **Size:** L · **Depends on:** nothing · **Blocks:** 04, 05, 07

The largest deliverable. Five sub-parts: the data model, persistence, the tool, context rendering, and the late-init boundary.

---

## A. Data model

**File:** `src/data_agent/runtime/session/models.py`

```python
@dataclass(frozen=True)
class TrackedIntent:
    intent_id: str            # runtime-assigned; never model-supplied
    description: str          # FROZEN after initialization
    status: str               # pending | completed | blocked
    evidence_tool_call_id: str | None = None
    reason_code: str | None = None

    def to_doc(self) -> dict[str, Any]: ...
    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> TrackedIntent: ...


@dataclass(frozen=True)
class AnalysisState:
    turn_index: int
    intents: tuple[TrackedIntent, ...]

    def to_doc(self) -> dict[str, Any]: ...
    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> AnalysisState: ...
```

Frozen, `to_doc`/`from_doc` — the shape every other model in that file uses (`TurnMessage`, `TrailEntry`, `PauseCheckpoint`, `ResultPreview`).

**Id scheme.** Sequential per turn — `i1`, `i2`, … — assigned in the order the model proposed them. Deterministic, which matters: a D45 rebuild must produce byte-identical context, so ids cannot come from `uuid4()`.

**Closed enums** as module constants beside the dataclasses, so the validator and the tests share one definition:

```python
INTENT_STATUSES = frozenset({"pending", "completed", "blocked"})
MODEL_REASON_CODES = frozenset({"NO_ACCESS", "REQUIRED_DATA_UNAVAILABLE"})
RUNTIME_REASON_CODES = frozenset({"BUDGET_EXHAUSTED", "USER_STOPPED", "ENFORCEMENT_EXHAUSTED"})
REASON_CODES = MODEL_REASON_CODES | RUNTIME_REASON_CODES
```

Only two reasons are model-declarable (spec §6.2); the split must be structural, not a comment, or the validator will drift toward accepting runtime codes from the model.

---

## B. Persistence

**Finding: `SessionStore` is a `Protocol` with two implementations.** Four places, not one.

| File | Change |
|---|---|
| `session/models.py` | `SessionDoc.analysis_state: AnalysisState \| None = None`; round-trip in `to_doc` / `from_doc` (mirror the `pause_checkpoint` handling at models.py:299) |
| `session/store.py` | New protocol method `write_analysis_state(session_id, state) -> None` |
| `session/couchbase_store.py` | Implement via `_mutate_with_cas_retry` — the same helper `append_trail_entry` uses |
| `session/memory_store.py` | Implement in-memory |

**Latest-wins, single mutable field.** Never append-only trail entries: N rounds would put N copies inside the pinned region of `fit_request_to_budget` and reproduce unbounded context growth in structured form. This is the spec's load-bearing storage constraint.

**Cross-turn.** The field persists on the doc but is **not rendered** into a later turn's context (§D below). Do not clear it on turn boundary — `session_history` and future telemetry read it, and Phase 2's `coverage` will need it.

**TTL.** Inherits `SESSION_TTL` with the doc. Nothing extra.

---

## C. The tool

**New file:** `src/data_agent/runtime/composite/analysis_state.py`, modelled on `composite/record_assumptions.py`.

Registered in two places:
- `runtime/app.py` — `runtime_tools["updateAnalysisState"] = UpdateAnalysisStateTool(session_store=…)`, always wired (no backing stack ⇒ never `UNAVAILABLE`), alongside `recordAssumptions` and `answerWithTable`.
- `runtime/mcp/tool_schema.py` — new `UPDATE_ANALYSIS_STATE_TOOL_SCHEMA` appended to `_LOCAL_TOOL_SCHEMAS`. **Tool count 14 → 15.**

### Difference from `recordAssumptions`

`RecordAssumptionsTool` is stateless — the loop reads its arguments and accumulates. **This tool is not**: it owns validation, id assignment, and the write. It needs the `SessionStore` and the current `turn_index`.

`RuntimeTool.run(model_args, credentials)` carries no turn index. Two options:

- **(a)** Construct the tool per request in `app.py` (the dispatcher and read tools are already per-request) and pass `turn_index` in via a small context object.
- **(b)** Have the tool derive the turn index from the store — `doc.messages[-1].turn_index` — the same way `app.py` computes `turn_index_hint`.

**Prefer (a).** (b) re-reads the doc and duplicates a derivation the loop already owns; when the two disagree the state attaches to the wrong turn.

### Two modes

```
no state for this turn  →  INITIALIZE   (full declaration; descriptions only)
state exists            →  UPDATE       (batched; status/evidence/reason only)
```

Infer the mode from whether state exists — do not make the model declare it.

**Initialize** returns the assigned ids in the tool result. That result is the only way the model learns them, so it must contain the full state, not a bare confirmation.

**Update** is batched: several intents per call. Load-bearing — with automatic blueprint-flipping cut, one call per intent would reintroduce the bookkeeping overhead the trim exists to remove.

### Validation

Derive the guard from what reads the data downstream; do not spot-patch named fields.

| Rule | On failure |
|---|---|
| Payload is an object with the expected keys; **unknown keys rejected, not ignored** | `ANALYSIS_STATE_INVALID` |
| Intent count within a bound (8 is generous) | `ANALYSIS_STATE_INVALID` |
| `status` ∈ `INTENT_STATUSES` | `ANALYSIS_STATE_INVALID` |
| `reason_code` ∈ `MODEL_REASON_CODES` when model-declared | `ANALYSIS_STATE_INVALID` |
| Update references an unknown `intent_id` | `ANALYSIS_STATE_INVALID` |
| Update attempts to add, delete, or rewrite `intent_id`/`description` | `ANALYSIS_STATE_INVALID` |
| Model supplies `intent_id` on initialize | `ANALYSIS_STATE_INVALID` |
| Second initialize while state exists | `ANALYSIS_STATE_INVALID` |
| Initialize after the substantive boundary | `ANALYSIS_STATE_LATE_INIT` *(non-retryable)* |
| Evidence fails 04's validators | `ANALYSIS_STATE_INVALID` |

`ANALYSIS_STATE_INVALID` is **retryable** — the model can correct and re-send. `ANALYSIS_STATE_LATE_INIT` is **not**; the boundary has passed and no retry helps.

Both need `denial_mapping.py` entries. Both should carry specifics in `denial_detail` — *which* key was unknown, *which* id was unrecognised — because `user_message` never reaches the model.

**Immutability is the anti-evasion rule.** Without it, "full replace" is a free exit from finalization enforcement: the model drops the hard intent from the array and finalizes cleanly having answered two of three asks. Test it adversarially.

### Provenance

`frozenset()` — determined-empty, the `recordAssumptions` posture. The tool reads no warehouse data. **Not `None`**: `None` means undetermined, and `filter_trail`'s current-turn exemption is status-gated to `status != "ok"`, so an `ok`+`None` entry gets dropped and re-materialised as the D94 stranded sentinel — the model would see *"result withheld… Do not retry"* in place of its own state confirmation. That exact bug is documented at length in `record_assumptions.py`; do not repeat it.

### Loop registration

`runtime/loop/agent_loop.py`:
- Add to `_RUNTIME_TOOL_UNAVAILABLE_CODE`? **No** — always wired, so the unwired path is unreachable.
- **Exempt from `max_tool_calls_per_iteration`.** `capped_tool_calls = result.tool_calls[:self._max_tool_calls_per_iteration]` silently drops the overflow with no error to the model, so a state call plus eight substantive calls loses the eighth. Partition first, then cap only the substantive remainder.

---

## D. Context rendering

The model must be able to read the state — and its ids — every round.

**Ephemeral, not persisted-as-message.** Render from the store at context-build time, exactly like `discovery_canonical`: computed in `_run_loop_body`, threaded into `_build_canonical_messages`, spliced in. Never appended to `doc.messages`, or it surfaces in the `/session/history` transcript as something the user said.

**Role: `user`.** The base prompt must stay the sole `role:"system"` message — `retrieval/render.py` and the compaction summary both take this route deliberately, because some OpenAI-compatible endpoints honour only the first or last system message.

**Placement:** immediately before the current question, adjacent to the retrieval block, so it reads as context for this turn.

**Sanitisation:** `description` is model-authored text re-entering model context. Route it through the same structural sanitiser `retrieval/render.py` applies — control characters collapsed, length-capped — so it cannot forge message structure.

**Cross-turn drop:** follow `_is_stale_assumptions_entry` (`context/assembly.py:490`). A prior turn's state must not render. The precedent's reasoning applies verbatim: descriptions are model-authored and may echo values the current `column_scope` no longer permits.

---

## E. The late-init boundary

**Locking set — exactly these four**, from spec §5.1:

```python
SUBSTANTIVE_TOOLS = frozenset({"runQuery", "runBlueprint", "sampleRows", "resolveValues"})
```

**Everything else is non-locking**, including `listDatabases`, `listTables`, `askUser`, `recordAssumptions`, `answerWithTable` and `updateAnalysisState` itself. State the default explicitly in code — an implementer guessing "unlisted = locking" would make `recordAssumptions` block initialization.

**The check:** on initialize, scan the current turn's persisted trail for any entry whose `tool_name ∈ SUBSTANTIVE_TOOLS`. One or more ⇒ `ANALYSIS_STATE_LATE_INIT`.

**Emulated discovery is safe.** Those entries are ephemeral and never persisted (`context/discovery_emulation.py`), so they cannot reach the trail the boundary reads.

### The intra-batch ordering rule

**This is finding 5 and it must be built, not assumed.** Dispatch walks `capped_tool_calls` in array order. The spec's own round-1 pattern is `updateAnalysisState` + 3 × `runBlueprint` in one response — if the model emits the blueprints first, a substantive entry lands before the state call is processed, initialization is rejected, and **the turn runs unprotected**. Array order would decide whether enforcement engages.

Partition each batch before dispatching:

```python
state_calls = [tc for tc in capped_tool_calls if tc.name == UPDATE_ANALYSIS_STATE]
other_calls = [tc for tc in capped_tool_calls if tc.name != UPDATE_ANALYSIS_STATE]
for tool_call in [*state_calls, *other_calls]:
    ...
```

This reinstates the intent of the Lead's original §36 barrier ("commit state, then dispatch") in sequential form. §36 was dropped along with bounded concurrency; the ordering guarantee it carried was never the concurrency part. Comment it as such so it does not read as drift.

---

## Tests

| File | Cases |
|---|---|
| `tests/runtime/session/test_models.py` | `AnalysisState`/`TrackedIntent` round-trip; unknown key rejected; enum split (model vs runtime reason codes) |
| `tests/runtime/session/test_memory_store.py`, `test_couchbase_store.py` | `write_analysis_state` latest-wins; survives pause/resume |
| `tests/runtime/composite/test_analysis_state.py` | Init assigns sequential ids; ids returned in the result; update is batched; mode inferred from existing state |
| `tests/runtime/composite/test_analysis_state_adversarial.py` | **Immutability**: add / delete / rewrite `description` / model-supplied `intent_id` / second init — each rejected · unknown key rejected · unknown `intent_id` rejected · runtime-only reason code from the model rejected |
| `tests/runtime/loop/test_analysis_state_loop.py` | Exempt from `max_tool_calls_per_iteration` (state + 8 substantive ⇒ all 9 dispatched) · **intra-batch ordering: state call dispatched first regardless of array position** · late-init rejected after a `runQuery`, permitted after `getTableSchema` |
| `tests/runtime/context/test_assembly.py` | State renders as `user` role, before the question, sanitised; prior-turn state dropped; base prompt still sole `system` message |

The ordering test is the one that matters most — it is the difference between the protection engaging and silently not engaging.

## Done when

- [ ] `AnalysisState` round-trips through both stores and a pause.
- [ ] Ids runtime-assigned, deterministic, returned on initialize.
- [ ] Immutability enforced and adversarially tested.
- [ ] Late-init boundary enforced; locking set exactly four; default documented in code.
- [ ] Intra-batch ordering enforced and tested.
- [ ] Exempt from the per-iteration cap.
- [ ] Rendered ephemerally as `user`, sanitised, dropped cross-turn.
- [ ] `frozenset()` provenance.
- [ ] Tool count 14 → 15 in `tool_schema.py`.
