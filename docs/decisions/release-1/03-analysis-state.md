# 03 — `analysisState`

**Spec:** [§5](../release-1-routing-and-intent-coverage.md) · **Size:** L · **Depends on:** nothing · **Blocks:** 04, 05, 07
**Revised** 2026-08-11 after review — see [§H](#h-review-corrections) for what changed and why.

The largest deliverable. Five sub-parts: the data model, persistence, the tool, context rendering, and the late-init boundary.

Nothing is in production, so no migration or backwards-compatibility constraint applies to any schema, signature or helper named here.

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

Frozen, `to_doc`/`from_doc` — the shape every other model in that file uses.

### A.1 The live-state predicate — define once, use everywhere

**This is the single most important rule in the document.** `AnalysisState` persists on the session doc after its turn ends. It is therefore *history* for every later turn, and must be invisible to anything that enforces.

```python
def live_analysis_state(doc: SessionDoc, turn_index: int) -> AnalysisState | None:
    """The state that GOVERNS this turn, or None. A state from any other turn
    is history — never initialized against, never validated against, never
    enforced on."""
    state = doc.analysis_state
    if state is None or state.turn_index != turn_index:
        return None
    return state
```

Every read in 03, 04 and 05 goes through this. Without it two failures are reachable, and the second is the bad one:

- **Init dies after first use.** If "state exists" means `doc.analysis_state is not None`, then from turn 2 onward every multi-intent turn is refused initialization and the feature silently stops working.
- **A stale state blocks an unrelated turn.** Turn N is multi-intent, the model calls `askUser`, the user abandons it and asks something new. `AgentLoop.run` does not check for an unconsumed checkpoint, so turn N+1 begins with turn N's `pending` intents on the doc. Enforcement refuses turn N+1's finalization, burns its nudge, and writes `ENFORCEMENT_EXHAUSTED` onto **turn N's** intents — corrupting the abandoned turn's record and emitting bogus telemetry for the live one.

### A.2 Ids

Sequential per turn — `i1`, `i2`, … — assigned in proposal order.

Ids are **persisted**, so any scheme survives a rebuild, a pause and a restart; `uuid4()` would be equally stable. Sequential is chosen for token cost and model legibility, not for D45 determinism. (An earlier draft claimed the latter; it was wrong, and the rule read as load-bearing when it is not.)

### A.3 Closed enums

Module constants beside the dataclasses, so the validator and the tests share one definition:

```python
INTENT_STATUSES = frozenset({"pending", "completed", "blocked"})
MODEL_REASON_CODES = frozenset({"NO_ACCESS", "REQUIRED_DATA_UNAVAILABLE"})
RUNTIME_REASON_CODES = frozenset({"BUDGET_EXHAUSTED", "USER_STOPPED", "ENFORCEMENT_EXHAUSTED"})
REASON_CODES = MODEL_REASON_CODES | RUNTIME_REASON_CODES
```

The split must be structural, not a comment, or the validator will drift toward accepting runtime codes from the model.

### A.4 Bounds — reject, never truncate

| Bound | Value | On breach |
|---|---|---|
| Intents per state | 8 | reject |
| `description` length | 500 chars | **reject** |
| State calls per model response | 2 | reject the surplus |

`description` is **frozen** after initialization (§C). Silently truncating it would rewrite the record enforcement depends on, so a too-long description is a validation failure, not a trim. `recordAssumptions` truncates because its content is advisory; this content is not.

---

## B. Persistence

`SessionStore` is a `Protocol` with two implementations. Five places:

| File | Change |
|---|---|
| `session/models.py` | `SessionDoc.analysis_state: AnalysisState \| None = None` and `SessionDoc.finalization_blocks: dict[str, int] \| None = None` (05 §cap); round-trip both in `to_doc`/`from_doc`, mirroring `pause_checkpoint` at models.py:299 |
| `session/store.py` | New protocol methods (see the mutation contract below) |
| `session/couchbase_store.py` | Implement over `_mutate_with_cas_retry` |
| `session/memory_store.py` | Implement in-memory |
| `tests/runtime/test_couchbase_connect_gate.py` | Auto-parametrises over every public coroutine — the new methods need `await self._ensure_connected()` first, exactly like `append_trail_entry`, or that test fails |

### B.1 The mutation contract — a callback, not a precomputed value

`_mutate_with_cas_retry` documents its precondition at couchbase_store.py:146: *"`mutate` must be a pure in-place mutation of the freshly-read `doc` … it may be called more than once (once per retry) against a freshly re-read document, so it must not carry any state of its own across calls."*

A tool that loads the state, validates, computes a merged `AnalysisState`, and hands that object to a `setattr` callback **breaks that precondition**: on a CAS conflict the callback re-runs against a fresh doc but writes a value derived from the stale read, clobbering the winner. That is the lost-update class the helper exists to prevent.

So the store API takes the merge, not the result:

```python
async def apply_analysis_state(
    self, session_id: str, turn_index: int,
    merge: Callable[[AnalysisState | None], AnalysisState],
) -> AnalysisState: ...
```

`merge` receives the **live** state (per A.1, `None` if absent or from another turn) as read inside the retry, and returns the new one. Validation that depends only on the payload runs before the call; validation that depends on current state runs inside `merge` and raises to abort the write.

### B.2 Latest-wins, single field

Never append-only trail entries: N rounds would put N copies inside the pinned region of `fit_request_to_budget` and reproduce unbounded context growth in structured form.

### B.3 Turn boundary

The field is **not** cleared at turn end — but it is inert, because everything that reads it goes through A.1. Keeping it costs nothing and Phase 2's `coverage` will need it.

*(An earlier draft justified keeping it with "`session_history` reads it". That is false: `project_history` is pure, takes `(messages, trail, column_scope, pause_checkpoint, blueprint_terminal_sql)`, and never touches the field. Nothing reads it today.)*

TTL inherits `SESSION_TTL` with the doc.

---

## C. The tool

**New file:** `src/data_agent/runtime/composite/analysis_state.py`.

Registered in `runtime/app.py` — `runtime_tools["updateAnalysisState"] = UpdateAnalysisStateTool(session_store=…, observer=observer, tracer=tracer)` — always wired, so never `UNAVAILABLE`. Schema appended to `_LOCAL_TOOL_SCHEMAS` in `runtime/mcp/tool_schema.py`. **Tool count 14 → 15.**

> Spec §5.4 and §13 say the registry lives in `agent_loop.py`. It does not — `app.py:571` builds `runtime_tools` and passes it in. Follow the code.

**`observer` and `tracer` are not optional.** 06 assigns this tool six telemetry events, and every other stateful runtime tool takes both (`app.py:589-612`).

### C.1 Getting the turn index

The tool needs the current `turn_index` to write A.1-correct state. Two things it must **not** do:

- Take it from `app.py`. `runtime_tools` is built in `_build_agent_loop`, which has only `turn_index_hint` — documented at app.py:740 as *"best-effort … never load-bearing for correctness, purely a telemetry label."* Making it load-bearing introduces a TOCTOU gap against the loop's own computation.
- Re-derive it from the store. `/turn` and `/turn/resume` use *different* formulas (`messages[-1].turn_index + 1` vs `messages[-1].turn_index`), and duplicating the loop's derivation guarantees eventual disagreement.

**Thread the real index from the loop.** `_run_loop_body` already carries it as a parameter. Extend the `RuntimeTool` protocol (agent_loop.py:151) with a turn context and have `_run_runtime_tool` (agent_loop.py:1120) pass it:

```python
class RuntimeTool(Protocol):
    async def run(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult: ...
```

`TurnContext` carries `turn_index` and the current turn's trail (the evidence validators in 04 need the trail anyway, and the loop has already loaded it). Six implementers, no production constraint — take the explicit signature rather than a side channel.

### C.2 Two modes

```
live_analysis_state(doc, turn_index) is None  →  INITIALIZE
otherwise                                     →  UPDATE
```

Inferred, never model-declared.

**Initialize** returns the full state including assigned ids — that result is the only way the model learns them, so a bare confirmation is not enough.

**Update** is batched: several intents per call. Load-bearing — with automatic blueprint-flipping cut, one call per intent would reintroduce the bookkeeping overhead the trim exists to remove.

### C.3 Validation

| Rule | On failure |
|---|---|
| Object with expected keys; **unknown keys rejected, not ignored** | `ANALYSIS_STATE_INVALID` |
| Bounds per A.4 | `ANALYSIS_STATE_INVALID` |
| `status` ∈ `INTENT_STATUSES` | `ANALYSIS_STATE_INVALID` |
| `reason_code` ∈ `MODEL_REASON_CODES` when model-declared | `ANALYSIS_STATE_INVALID` |
| Update references an unknown `intent_id` | `ANALYSIS_STATE_INVALID` |
| Update adds, deletes, or rewrites `intent_id`/`description` | `ANALYSIS_STATE_INVALID` |
| Model supplies `intent_id` on initialize | `ANALYSIS_STATE_INVALID` |
| Initialize while live state exists | `ANALYSIS_STATE_INVALID` |
| Initialize after the substantive boundary | `ANALYSIS_STATE_LATE_INIT` *(non-retryable)* |
| Evidence fails 04's validators | `ANALYSIS_STATE_INVALID` |

`ANALYSIS_STATE_INVALID` is **retryable**; `ANALYSIS_STATE_LATE_INIT` is **not** — the boundary has passed and no retry helps.

Both need `denial_mapping.py` entries and both carry specifics in `denial_detail`, which is the only channel that reaches the model. For `LATE_INIT`, include **the proposed descriptions** (spec §5.1 asks for this): `denial_detail` renders only on non-`ok` entries and `filter_trail`'s current-turn exemption is status-gated, so it cannot outlive the turn.

### C.4 What immutability does and does not close

Immutability closes the cheap evasion: without it, "full replace" lets the model drop the hard intent from the array and finalize cleanly having answered two of three asks.

It does **not** close manufactured evidence. Nothing binds an `evidence_tool_call_id` to the intent citing it (04 permits reuse deliberately), so these survive everything here:

- complete three intents citing one `runQuery`;
- manufacture a block reason — `SELECT … WHERE 1=0` yields `row_count == 0` ⇒ `REQUIRED_DATA_UNAVAILABLE`; a deliberately out-of-scope column yields `COLUMN_SCOPE_VIOLATION` ⇒ `NO_ACCESS`.

**State the guarantee accurately: the model cannot *silently drop* an ask.** It can still produce a falsifiable reason cheaply. Whether that is acceptable is a spec-level question and is with the Lead. The adversarial suite must cover manufactured block evidence, not only the drop case.

### C.5 Provenance

`frozenset()` — determined-empty. **Not `None`**: `None` means undetermined, and `filter_trail`'s current-turn exemption is status-gated to `status != "ok"`, so an `ok`+`None` entry is dropped and re-materialised as the D94 stranded sentinel — the model would see *"result withheld… Do not retry"* in place of its own state confirmation. Documented at length in `record_assumptions.py`; do not repeat it.

### C.6 Loop registration

**Exempt from `max_tool_calls_per_iteration`, and partition before capping.** See §E.2 — the ordering and the cap interact, and getting them in the wrong order silently disables the feature.

---

## D. Context rendering

The model must read the state — and its ids — every round.

**Read it fresh every round-trip.** `ContextAssembler.assemble` already loads the full `SessionDoc` (assembly.py:235), so render from there: zero extra store reads, and correct by construction.

> **Not** "threaded like `discovery_canonical`". That value is computed **once per budget window** (agent_loop.py:1622, deliberately above `new_budget_window` so its round-trips fall outside the budget clock) and reused unchanged across every rebuild. `analysisState` changes *within* the window — every `updateAnalysisState` mutates it. An implementer copying that lifetime hoists the read above `while True:` and the model never sees its assigned ids on the round after initialize, which is the whole point of the initialize result. Share the splice site and the never-persisted posture; not the lifetime.

**Ephemeral.** Never appended to `doc.messages`, or it surfaces in the `/session/history` transcript as something the user said.

**Role `user`**, so the base prompt stays the sole `role:"system"` message.

**Placement:** immediately before the current question, adjacent to the retrieval block. **05 owns the final splice ordering** when a finalization nudge is also present — see 05 §splice-order.

**Sanitisation:** `description` is model-authored text re-entering model context. `retrieval/render.py::_sanitize` is private and takes a required `max_chars`; promote it to a shared helper (`runtime/context/sanitize.py`) so there is one implementation, and reuse the `_MAX_FIELD_CHARS = 500` cap.

### D.1 Cross-turn suppression — two mechanisms, both required

The rendered block is suppressed by A.1 (a non-current-turn state is not live).

**That is not sufficient.** Every `updateAnalysisState` call also leaves a `TrailEntry` whose `args` carry the descriptions, with `frozenset()` provenance — so `is_entry_in_scope` keeps it under **any** `column_scope`, in **every** later turn, and `_render_entry` replays `args` verbatim plus a `result_preview` that C.2 requires to contain the full state.

This is exactly the leak `_is_stale_assumptions_entry` exists to prevent (assembly.py:496: *"a sentence like 'employees earning above $100,000 were excluded' could outlive the caller's access to the column it was derived from"*), reproduced for text derived from the user's question.

`_is_stale_assumptions_entry` is hard-coded to one tool name at assembly.py:522. Generalise it:

```python
_STALE_CROSS_TURN_TOOLS = frozenset({"recordAssumptions", "updateAnalysisState"})

def _is_stale_model_text_entry(entry: TrailEntry, current_turn_index: int | None) -> bool:
    if entry.tool_name not in _STALE_CROSS_TURN_TOOLS:
        return False
    return current_turn_index is None or entry.turn_index != current_turn_index
```

Rename and widen — no production constraint. Add a D44 narrowing case to `tests/runtime/context/test_scope_narrowing_adversarial.py`.

---

## E. The late-init boundary

**Locking set — exactly these four:**

```python
SUBSTANTIVE_TOOLS = frozenset({"runQuery", "runBlueprint", "sampleRows", "resolveValues"})
```

**Everything else is non-locking**, including `listDatabases`, `listTables`, `askUser`, `recordAssumptions`, `answerWithTable` and `updateAnalysisState` itself. State the default in code — an implementer guessing "unlisted = locking" would make `recordAssumptions` block initialization.

**The check:** on initialize, scan the current turn's persisted trail for any `tool_name ∈ SUBSTANTIVE_TOOLS`. One or more ⇒ `ANALYSIS_STATE_LATE_INIT`.

**Emulated discovery is safe** — those entries are ephemeral and never persisted, so they cannot reach the trail the boundary reads.

**The failure is asymmetric, and the unsafe direction is silence.** A rejected initialization does not error the turn; it leaves the turn running *unprotected*. `sampleRows` and `resolveValues` are both plausible "look before you decompose" moves that the prompt does not forbid pre-decomposition, so if the set proves too aggressive the symptom will be unprotected turns, not visible failures. Watch `loop_analysis_state_late_init_rejected` (06) when tuning it.

### E.1 Ordering vs. the `askUser` short-circuit

`askUser` is scanned for and honoured **before** `capped_tool_calls` is even computed (agent_loop.py:1756): the whole response pauses and **nothing is dispatched**. So a batch of `updateAnalysisState` + `askUser` — the natural round-1 shape for "three asks, one ambiguous" — discards the state write entirely. No trail entry, no tool result, and D22 discards the surrounding free text, so after the resume the model has no record it ever tried. Spec §5.1's "clarification cannot broaden the protected set" then bites on a set that was never created.

**Dispatch state calls before honouring the pause.** Move the state partition above the `askUser` interception; commit the state, then pause.

### E.2 Partition before capping

`capped_tool_calls = result.tool_calls[: self._max_tool_calls_per_iteration]` (agent_loop.py:1791, default 8) **silently discards the overflow** with no error to the model. Partitioning the already-capped list therefore loses a state call that follows eight substantive calls — before any reordering can help — and the turn runs unprotected.

Partition the **raw** list, cap only the substantive remainder:

```python
state_calls = [tc for tc in result.tool_calls if tc.name == UPDATE_ANALYSIS_STATE][:MAX_STATE_CALLS]
other_calls = [tc for tc in result.tool_calls
               if tc.name != UPDATE_ANALYSIS_STATE][: self._max_tool_calls_per_iteration]
for tool_call in [*state_calls, *other_calls]:
    ...
```

`MAX_STATE_CALLS = 2`. The exemption must be bounded: unbounded, N state calls in one response are N full session-doc CAS read-modify-writes and N trail entries in the pinned budget region, with `guard.exceeded` checked only *after* each call. C.2 already makes batching mandatory, so more than one per response is the model misbehaving — reject the surplus with `ANALYSIS_STATE_INVALID`.

This reinstates the intent of the Lead's original §36 barrier ("commit state, then dispatch") in sequential form. §36 was dropped along with bounded concurrency; the ordering guarantee it carried was never the concurrency part. Comment it as such.

---

## F. Tests

| File | Cases |
|---|---|
| `tests/runtime/session/test_models.py` | Round-trip both new fields; unknown key rejected; enum split |
| `tests/runtime/session/test_memory_store.py`, `test_couchbase_store.py` | `apply_analysis_state` latest-wins; **merge callback re-runs correctly on a simulated CAS conflict**; survives pause/resume; `_ensure_connected` gate |
| `tests/runtime/composite/test_analysis_state.py` | Init assigns sequential ids and returns them; update batched; mode inferred via `live_analysis_state` |
| `tests/runtime/composite/test_analysis_state_adversarial.py` | Immutability: add / delete / rewrite description / model-supplied id / second init — each rejected · unknown key · unknown id · runtime-only reason code from the model · over-length description **rejected not truncated** · **manufactured block evidence** (`WHERE 1=0` ⇒ `REQUIRED_DATA_UNAVAILABLE`) recorded as known-permitted, with the telemetry assertion |
| `tests/runtime/loop/test_analysis_state_loop.py` | **Partition happens before the cap** (8 substantive + trailing state call ⇒ state call still dispatched) · state dispatched first regardless of array position · **`updateAnalysisState` + `askUser` in one batch ⇒ state committed, then pause** · late-init rejected after `runQuery`, permitted after `getTableSchema` · >2 state calls ⇒ surplus rejected |
| `tests/runtime/context/test_assembly.py` | State renders as `user`, before the question, sanitised · **state is re-read every round-trip, so ids appear on the round after initialize** · prior-turn state not rendered · base prompt still sole `system` message |
| `tests/runtime/context/test_scope_narrowing_adversarial.py` | **A prior turn's `updateAnalysisState` trail entry is dropped under a narrowed scope** (the D.1 leak) |
| `tests/runtime/mcp/test_tool_schema.py` | Count 14 → 15 — update the assertions at :129, :171, :188 and the name set at :111-127 |

The partition-before-cap and ordering tests matter most: they are the difference between protection engaging and silently not engaging.

---

## G. Done when

- [ ] `live_analysis_state` defined once; every read in 03/04/05 goes through it.
- [ ] Both new `SessionDoc` fields round-trip through both stores and a pause.
- [ ] Store API takes a **merge callback**; CAS-conflict test green.
- [ ] Ids runtime-assigned, sequential, returned on initialize.
- [ ] Immutability enforced and adversarially tested; guarantee stated as "cannot silently drop", not "cannot evade".
- [ ] Bounds reject rather than truncate.
- [ ] Turn index threaded from the loop via `TurnContext`; not from `app.py`, not re-derived.
- [ ] Late-init boundary enforced; locking set exactly four; default documented in code.
- [ ] **Partition before cap**; state calls bounded at 2; dispatched before the `askUser` pause.
- [ ] State re-read every round-trip, rendered ephemerally as `user`, sanitised via the shared helper.
- [ ] **`_is_stale_model_text_entry` generalised; the cross-turn trail-entry leak closed and tested.**
- [ ] Tool takes `observer` and `tracer`; 06's six events fire.
- [ ] `frozenset()` provenance.
- [ ] Tool count 14 → 15.

---

## H. Review corrections

Revised after review found the first draft would have shipped four silent failures. Recorded so the reasoning is not lost:

| Was | Now | Why |
|---|---|---|
| No turn scoping; "do not clear at turn boundary" justified by "`session_history` reads it" | **A.1 `live_analysis_state`** | The justification was false, and without the gate a stale state from an abandoned turn blocks the next unrelated turn and writes `ENFORCEMENT_EXHAUSTED` onto the old one |
| §E snippet partitioned `capped_tool_calls` | **E.2 partitions the raw list** | Contradicted this document's own §C; 8 substantive calls + a trailing state call lost the state call before any reorder |
| "Threaded exactly like `discovery_canonical`" | **D — read fresh every round-trip** | `discovery_canonical` is once-per-window; copying that lifetime means the model never sees its assigned ids |
| Cross-turn drop cited `_is_stale_assumptions_entry` | **D.1 — generalise it** | That function is hard-coded to `recordAssumptions`; the new tool's own trail entry replays descriptions under any scope, forever |
| Turn index from `app.py` (plan a) or re-derived (plan b) | **C.1 — `TurnContext` from the loop** | `app.py` has only the explicitly non-load-bearing `turn_index_hint`; the two endpoints use different formulas |
| Cap exemption unbounded | **E.2 — `MAX_STATE_CALLS = 2`** | Unbounded CAS writes and trail entries in the pinned region |
| `askUser` "non-locking" and nothing more | **E.1 — dispatch state before pausing** | `askUser` short-circuits the entire batch before dispatch; the state write was silently discarded |
| Store write as a precomputed value | **B.1 — merge callback** | Violated `_mutate_with_cas_retry`'s documented precondition; lost-update on conflict |
| Ids sequential "for D45 determinism" | **A.2 — for token cost and legibility** | Ids are persisted, so any scheme is stable; the stated reason was wrong |
| Immutability implied closure | **C.4 — names the surviving evasions** | Manufactured block evidence is cheap and unclosed; escalated to the Lead |
