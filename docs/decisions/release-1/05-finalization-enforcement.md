# 05 — Finalization Enforcement

**Spec:** [§7](../release-1-routing-and-intent-coverage.md) · **Size:** M · **Depends on:** 03, 04 · **Blocks:** 07
**Revised** 2026-08-11 after review — see [§I](#i-review-corrections). **Supersedes spec §7's exit-#1 mechanism** (the spec says "synthetic tool result"); the invariant is unchanged, only the delivery.

This is where `analysisState` gets teeth. All of it lives in `runtime/loop/agent_loop.py`. Nothing is in production, so schema and signature changes are unconstrained.

**Invariant (scoped — see §F):** no intent ends `pending` on any turn that reaches a terminal outcome.

> **AMENDED 2026-08-12 — the enforcement's scope now extends from intent COVERAGE to answer SHAPE ([§J](#j-the-answer-shape-gate-added-2026-08-12-on-measurement)).** The unconditional "present a table" prompt rule was the last strong rule in the release with no runtime enforcement, and it failed live in **1/8 (R4)** and **3/8 (R5)** expected-table runs — twice ending in an apology that the tool could no longer be called. Same exit, same mechanism; the shape gate shipped sharing the intents allowance and was **given its own the same day**, after live traces showed the intents nudge consuming it first in 2 of 4 multi-intent runs ([§J.3](#j3-the-gate-has-its-own-per-window-allowance-revised-2026-08-12-on-measurement)).

---

## A. Enforcement applies only to the live state

Read through `live_analysis_state(doc, turn_index)` (03 §A.1). A state from any other turn is history and is invisible here.

Without the gate: turn N is multi-intent, the model calls `askUser`, the user abandons it and asks something new. Turn N+1 begins with turn N's `pending` intents on the doc, so enforcement refuses an unrelated single-intent turn, burns its nudge, and writes `ENFORCEMENT_EXHAUSTED` onto **turn N's** intents.

**No live state ⇒ no enforcement.** Most turns are single-intent; that path must stay a local `is None` test (§E).

---

## B. The two terminal exits

`_run_loop_body` has exactly two paths to `status="done"`, and they need different mechanisms because only one has an error channel. (`resume()` has a third `done` return for the `"stop"` answer — §F.)

### B.1 Exit #2 — `answerWithTable`

agent_loop.py:1992. A successful call sets `designated_answer_text` and the turn ends after the batch drains. Note the full condition: `status == "ok"` **and** `isinstance(arguments, dict)` **and** `clean_answer_text(...)` non-`None` — a blank `answer` does not terminate.

Refuse by returning a retryable error `ToolResult` **before** the trail entry is written, copying `_answer_table_blueprint_not_run` (agent_loop.py:355):

- `status="error"`, `error_code=FINALIZATION_BLOCKED_PENDING_INTENTS`, `retryable=True`
- **What keeps the entry in scope IN-TURN is the status gate, not the provenance.** `filter_trail`'s current-turn exemption keeps any `status != "ok"` entry of the current turn *regardless* of provenance, and in-turn is the only place this entry must survive. `provenance=frozenset()` is still correct — the refusal reads no warehouse data, and `_compute_turn_provenance_union` is fail-closed, so a `None` here would collapse the whole turn's union and drop the turn's own answer from every later turn's replay — but it is **not** what makes the refusal visible.
- **The entry must be dropped cross-turn, and provenance cannot do that job.** `frozenset()` passes `is_entry_in_scope` under any scope, forever; `answerWithTable` cannot join `_STALE_CROSS_TURN_TOOLS` (its successful entries are the answer and must replay); and `_render_entry` has no turn awareness. So `context/assembly.py::_is_stale_model_text_entry` also matches on **error code** (`_STALE_CROSS_TURN_ERROR_CODES`), and the refusal — its `denial_detail` naming dead intent ids, its `args` carrying the refused draft prose — leaves with its turn. **This is the third instance of one defect class in this release** (README findings 9 and 11); see 03 §D.1 for the general rule, and the note there that provenance is the wrong channel for "do not replay this later".
- **`denial_detail` names the pending intents.** `_render_entry` (budget.py:280) builds model-facing text as `entry.denial_detail or classify_denial(entry.error_code).user_message` — never from `ToolResult.user_message`, which has no `TrailEntry` field. Register the code in `denial_mapping.py` or the fallback string says nothing actionable.
- **Failure containment.** `claim_finalization_block` is a CAS read-modify-write and can raise (five lost retries, a transient connection error). Catch it, log server-side, emit `loop_finalization_block_claim_failed`, and treat it as **`False`** — force-block and finalize. Letting it propagate aborts the turn at the moment the model has a finished answer; treating it as `True` grants a re-round whose consumption was never persisted, which is unbounded.

**Run the finalization check before `_resolve_answer_sql`** (agent_loop.py:1884). That call fires the two dormant `hooks/answer_table.py` seams; a designation that is about to be refused should not fire the answer-table lifecycle, and the blueprint-not-run nudge is moot on a turn that cannot finalize. Checking afterwards would also clobber that more actionable message.

### B.2 Exit #1 — a model turn with no tool calls

agent_loop.py:1719. The model produced prose, not a call: **no error channel.**

`_assembled_to_canonical` (agent_loop.py:532) only ever emits a `tool` message by expanding a trail entry into an `assistant(tool_calls) + tool` pair, so a nudge cannot stand alone.

Fabricating a pair *is* an established pattern — `discovery_canonical` does exactly that for `listDatabases`/`listTables` calls the model never made (agent_loop.py:1622, ids like `emulated-listTables-<db>`). So "it would fabricate a call" is not the reason to reject it. The reasons that hold:

1. The nudge answers no call, so the fabricated function name must be one the model can see in its tools list or the endpoint rejects or is confused by it.
2. A fabricated pair must be re-spliced, deduped and pinned on every rebuild (agent_loop.py:823-882, plus `fit_request_to_budget` invariant 7) — heavy machinery for something that should live one round-trip.
3. `_render_entry` would route its text through `classify_denial` anyway.

**Use ephemeral `user`-role injection.**

- Persist nothing — not the assistant answer, not the nudge. Both are within-turn control flow. A persisted nudge would appear in `/session/history` as something the user said.
- Role `user`, so the base prompt stays the sole `role:"system"` message.
- **Placement: the tail**, after the current question. Safe because `_current_turn_start` (budget.py:449) anchors on the first `user` after the last plain-assistant answer — a trailing `user` message does not move the pin — and budget.py:582 classifies it as a current-turn non-tool-pair unit, which is pinned.
- **Lifetime: exactly one round-trip** (§D).

### B.3 Carry the draft back

Exit #2's refusal preserves the model's prose automatically: it lives in `tool_call.arguments`, persisted as `TrailEntry.args` and replayed by `_render_entry`. Exit #1's preserves nothing — the prose is not persisted, and D22 discards free text around tool calls, so on the re-round the model has no record it just wrote a final answer and must regenerate from scratch.

**The nudge carries the draft back:** *"You drafted: `<draft>`. Intent i2 ('…') is still pending — resolve it, then re-send your final answer."* Ephemeral text, same turn, same `column_scope` as the draft it quotes, so no D44 exposure.

**Also decide `last_assistant_text`.** It is set at agent_loop.py:1717 before any refusal and returned as `assistant_text` on the hard-ceiling (2063) and budget-cap (2088) paths — so a refused, incomplete answer can still reach the user there while never appearing in history, making live and history disagree on exactly the enforcement path. **Clear it on refusal.**

---

## C. The block counter

**One forced re-round per budget window**, shared by both exits.

### C.1 It must be persisted

A counter local to `_run_loop_body` does **not** give "per window". `_run_loop_body` is entered once per `run()` **and once per resume of any kind**, while `window_count` only increments on a budget-cap `"continue"`/`"refine"`:

- agent_loop.py:709 — an `askUser` resume keeps `window_count` unchanged.
- agent_loop.py:1330 — `_resume_blueprint` re-enters with `window_count` unchanged.

So a local counter resets on every askUser and every mid-DAG blueprint resume while the window number stands still, and forced re-rounds become unbounded (user-paced, but unbounded). `seen_read_calls` is precedent for the fix, not for a local: agent_loop.py:1655 seeds it from the persisted trail *precisely because* "a fresh `_run_loop` window starts here with an empty in-memory set".

An exit-#1 refusal leaves **no** persisted artifact by design, so it cannot be reconstructed from the trail. **Persist it:** `SessionDoc.finalization_blocks: dict[str, int] | None` keyed by **`(turn_index, window)`** — `{"0:2": 1}`, formatted by `session/models.py::finalization_block_key` — so "one per budget window" is literal *within a turn*. A field on `SessionDoc`, **not** on `AnalysisState`, which is model-writable and unknown-key-rejecting. Added to 03 §B's table.

**The turn index is required, and keying by window alone was a defect.** This map persists on the session document and is never cleared at a turn boundary, but `AgentLoop.run` starts **every external turn at `window_count=1`** — so turn N+1's window 1 collided with turn N's. Measured: from a session's second block-spending turn onward, the first finalization attempt of every turn was refused a re-round it had never had, emitting no `loop_finalization_refused`, building no nudge, and writing `ENFORCEMENT_EXHAUSTED` for intents the model was never asked twice about — which also silently inflates 07 §E's headline rate. Same class as the bug §A's turn gate exists to prevent: a per-turn value persisted on the session doc with no turn gate. Keying beats clearing; a clear would need a turn-boundary hook that does not exist and must not fire on the resume paths that keep `window_count` standing still.

### C.2 It is consumed per round-trip, not per refused call

Exit #2 refusals happen inside the per-tool-call loop, which processes up to 8 calls from **one** model response. A model emitting `[answerWithTable, answerWithTable]` would burn both chances in a single round-trip, force-block on the second, and finalize — having been given **no re-round at all**, with `ENFORCEMENT_EXHAUSTED` written for intents it was never asked twice about.

Gate on a window-local `finalization_refused_this_round: bool`, reset next to `designated_answer_text` (agent_loop.py:1714). Second and later refusals in the same batch return the same retryable error but do not advance the persisted counter.

### C.3 A forced re-round is charged to the budget

Exit #1 sits at agent_loop.py:1719, **before** `guard.record_iteration(...)` at 2056. Today it always returns, so it never needs to charge. If enforcement makes it `continue`, the round-trip is free — no iteration, no tokens — and `BudgetGuard.exceeded` can only trip on the 60s wall clock, which every resume restarts.

Record before looping back, and let the result flow into the existing ceiling/cap logic:

```python
guard.record_iteration(tokens_used=int(result.usage.get("total_tokens") or 0))
if guard.exceeded:
    ...            # existing hard-ceiling / budget-cap handling, incl. force-block
continue
```

Exit #2 needs nothing: its refusal is inside the batch, the batch drains, and `record_iteration` fires normally.

---

## D. Nudge lifetime — one round-trip

`discovery_canonical` is computed once per window and re-spliced into every rebuild. Copying that lifetime would repeat the nudge forever, including after the intents are closed — and because it sits at the tail and is ephemeral, it would migrate to be the newest message on every rebuild, appearing after tool results it predates. agent_loop.py:851 records exactly that failure for discovery emulation.

Share the splice site and the never-persisted posture; **not** the lifetime:

```python
finalization_nudge: str | None = None
while True:
    canonical = await self._build_canonical_messages(..., finalization_nudge=finalization_nudge)
    finalization_nudge = None          # one round-trip only
    result = await model_client.send_turn(canonical, tools)
```

### D.1 Splice order

`_insert_retrieval` anchors on `_last_user_index` (assembly.py:424) — **the last `user` message**. If the nudge is appended first, it *becomes* the last user message and 03 §D's state block lands after the question instead of before it.

**This document owns the ordering**, being the later insertion:

1. locate the current question,
2. insert the `analysisState` block immediately before it,
3. append the nudge at the tail.

Assembly test: with both present, the state block precedes the question and the nudge is last.

---

## E. Where the check reads from

The state changes mid-turn, so a once-per-window read is wrong — but a store read at each exit would cost a round-trip on **every** turn, including single-intent ones.

**Load once at the top of `_run_loop_body`** (next to the trail load at agent_loop.py:1664) into a window-local, and **refresh it in place from each `updateAnalysisState` tool result**. 03 §E.2's partition guarantees state calls are dispatched before anything else in the batch, so the local is current by the time either exit is reached. The fast path is then an `is None` test on a local.

---

## F. Terminal escapes, and the honest invariant

Four runtime-forced paths, each marking every surviving `pending` intent `blocked` before finalization proceeds:

| Trigger | Site | Forced code |
|---|---|---|
| Hard ceiling | agent_loop.py:2059 | `BUDGET_EXHAUSTED` |
| Budget-cap resume answered `"stop"` | `resume()`, agent_loop.py:695 | `USER_STOPPED` |
| Block counter spent, intents still pending | this file | `ENFORCEMENT_EXHAUSTED` |
| Budget cap reached *during* a refused round | via C.3 | `ENFORCEMENT_EXHAUSTED` **+ `budget_cap_reached` on the telemetry event** |

**`ENFORCEMENT_EXHAUSTED` means "enforcement could not establish a disposition"** — **not** that the system proved the intent impossible (Lead, 2026-08-11). Word it that way in `denial_mapping.py`, in telemetry, and in 07's metric report. Claiming proof would overstate what the runtime knows: zero rows is often a correct answer, and some denial probes cost one metadata call (04 §B.4). A user who withdraws an ask mid-clarification also lands here, and does so legitimately under that reading.

### F.0 The fourth path is `ENFORCEMENT_EXHAUSTED` (reversed 2026-08-12, on measurement)

This row said `BUDGET_EXHAUSTED` through two review rounds and shipped that way. **It is reversed here**, so the reasoning is recorded rather than the conclusion alone.

**The evidence.** Session `s412e8424614e465bbd26d7a2a1400ebe`, trace `647416592aff2225d1903ae7c82b8396`:

- the answer was **computed at 25s**; the turn capped at **61.6s**, on the **wall clock** (`max_wall_clock_seconds=60`) — not on tokens;
- tokens moved **+385 across the final three rounds**, which is nothing;
- those 36 seconds went entirely to **two rejected `updateAnalysisState` calls and one refused `answerWithTable`**;
- both intents were recorded `BUDGET_EXHAUSTED`.

**More budget would have changed nothing.** The turn was not short of capacity; it was going in circles inside enforcement, and the wall clock stopped it. `BUDGET_EXHAUSTED` tells whoever reads [07 §E.2](07-evaluation.md)'s buckets a **capacity story**, and sends them to raise a ceiling that is not the problem — the most expensive kind of wrong label, because acting on it looks like progress. `ENFORCEMENT_EXHAUSTED` — *"enforcement could not establish a disposition"* — is literally true here, and equally true of the ambiguous last-straw case where the cap and the refusal arrive together and the cause genuinely cannot be separated.

**The capacity fact is kept, in telemetry, not on the ledger.** `_force_block_pending_intents` takes `budget_cap_reached: bool = False`; the refused-round caller is the only one that passes `True`, and it is emitted as a bare `True` on `loop_intent_force_blocked` (allowlisted in `observability/tracing.py`; a boolean is D25-safe by shape) and **omitted** on the other three paths, so their event payloads are byte-unchanged. An operator watching budget pressure still sees the cap; the intent record carries the honest cause. See [06 §telemetry](06-telemetry.md).

**Scope of the reversal — one branch.** The ordinary hard ceiling keeps `BUDGET_EXHAUSTED` (there the ceiling genuinely *is* the cause: the turn exhausted every window it was allowed) and the `"stop"` resume keeps `USER_STOPPED`. Both are untouched and both are still asserted exactly, including the *absence* of the new flag. `RUNTIME_REASON_CODES` stays at three — this is a relabel, not a fourth code, and [§F.2](#f2-no-fourth-code) is unaffected.

**Note the seam this sharpens rather than closes.** `test_a_budget_cap_during_a_refused_round_terminally_disposes_a_live_turn` still records that a terminal disposition lands on a turn the user may yet continue, and that the disposition is not restored when they do. Under the old label that test asserted a capacity cause and then, in its own second half, extended the budget and finalized anyway — the counter-evidence sat inside the assertion. The seam is unchanged; only the claim about *why* is now defensible.

**The `"stop"` path returns `done` from inside `resume()`**, before `_run_loop` is ever entered, with `tool_calls_made=0`. It needs its own force-block call; it inherits nothing from the loop body. Apply the §A turn gate there too.

### F.1 Three ways a turn ends with `pending` intents on the doc

None of these is a dropped intent; all are **non-terminated turns**:

1. **Abandoned `askUser`** — checkpoint written, `resume()` never called (agent_loop.py:1757).
2. **Abandoned budget-cap pause** — same shape (agent_loop.py:2074).
3. **CAS conflict / already-consumed resume** — `resume()` raises before reaching any escape (agent_loop.py:666); `app.py:228` frames it as an SSE error and the turn is over. On the losing side of a race there is no path to a force-block.

*(A `runBlueprint` mid-DAG `ToolPause` that is never resumed is case 1 by another route.)*

So the invariant must be scoped, here and in the README and in 07:

> **No intent ends `pending` on any turn that reaches a terminal outcome** (`done` or `stopped_hard_ceiling`). A turn abandoned at a pause, or whose resume loses a CAS race, leaves its state `pending`; that is a non-terminated turn, and 07's assertion must exclude it.

Unscoped, spec §9.2's "mechanically zero" claim and 07's Layer-1/2 assertion produce false failures against any real store.

### F.2 No fourth code

A fourth runtime-forced code (`USER_DECLINED_CLARIFICATION`, for a turn whose `askUser` pause was answered and whose intent is still pending) was considered through two review rounds and **rejected 2026-08-11**. `ENFORCEMENT_EXHAUSTED` already describes that case accurately once it is read as *"enforcement could not establish a disposition"* rather than as agent failure. `RUNTIME_REASON_CODES` is final at three.

---

## G. Interactions

**Pause paths are not finalization.** `askUser`, budget-cap and `runBlueprint` `ToolPause` all return non-`done` statuses with intents legitimately `pending`. Not gated.

**`_resume_blueprint`** re-enters `_run_loop`, so its eventual exit is gated by the same code.

**Batch with both.** 03 §E.2's partition commits `updateAnalysisState` before `answerWithTable` in the same response, so the model can close its last intent and finalize in one round rather than being refused into an extra one.

> **The model is now TOLD to do this** (2026-08-12, [01a §13](01a-prompt-draft.md)). The affordance existed here from the start and the prompt never mentioned it, which turned out to be the whole defect rather than a missed optimisation: measured over three live runs of one three-intent question, the model's last call was `updateAnalysisState` and it then wrote prose — **zero `answerWithTable` calls of any kind**, while a single-deliverable control on the same build finalized correctly. Closing the ledger was being read as finishing the turn. The instruction to send both in one response is in the tracking section, the table section and both tool descriptions.

---

## H. Tests

`tests/runtime/loop/test_finalization_enforcement.py`:

| Case | Expect |
|---|---|
| No live state, exit #1 / #2 | Finalizes; no extra store reads |
| **Prior-turn state with pending intents, current turn single-intent** | Finalizes; prior state untouched; no telemetry |
| All intents `completed` | Finalizes |
| One `pending`, exit #2 | `FINALIZATION_BLOCKED_PENDING_INTENTS`, retryable, `denial_detail` names it, turn continues |
| One `pending`, exit #1 | Answer not persisted; `last_assistant_text` cleared; nudge injected with the draft; loop re-enters |
| Nudge | Absent from `doc.messages` and `/session/history`; **present for exactly one round-trip** |
| **Refused round** | `record_iteration` charged; a refused round can trip the cap; the cap-during-refusal writes **`ENFORCEMENT_EXHAUSTED`** with `budget_cap_reached: True` on `loop_intent_force_blocked` (§F.0) |
| Second attempt, still pending | `ENFORCEMENT_EXHAUSTED`, finalization proceeds |
| Counter across an `askUser` resume | **Not reset** — still one per window |
| Hard ceiling / `"stop"` resume with pending | Forced `BUDGET_EXHAUSTED` / `USER_STOPPED`, **unchanged by §F.0**, and the new flag asserted ABSENT on both |
| `askUser` pause with pending | **Not** gated |
| Batch = state update closing the last intent + `answerWithTable` | Finalizes in one response |
| Assembly with state block + nudge | State block before the question, nudge last |

`tests/runtime/loop/test_finalization_enforcement_adversarial.py`:

| Case | Expect |
|---|---|
| **Two `answerWithTable` in one batch, one pending** | Refused twice, counter advances **once**, turn continues |
| Model drops the pending intent and finalizes | Rejected by 03's immutability; assert end-to-end |
| Model completes an intent citing `searchBlueprints` | Rejected by 04; still refused |
| **Manufactured block evidence** (`WHERE 1=0` ⇒ `REQUIRED_DATA_UNAVAILABLE`) | Permitted today — assert the telemetry, per 03 §C.4 |
| Pending + hard ceiling + `answerWithTable` in one batch | Terminates once; no double-write |
| Abandoned pause, then a new turn | Prior state stays `pending`; new turn unaffected |

---

## J. The answer-shape gate (added 2026-08-12, on measurement)

**The enforcement's scope extends from INTENT COVERAGE to ANSWER SHAPE.** Everything above asks *"did you do the work you said you would?"* This section adds *"did you deliver it in the form the prompt requires?"* — same exit, same mechanism, same single grant.

### J.1 The measurement

[01a](01a-prompt-draft.md)'s *"Presenting a table"* rule is **unconditional**: a multi-row answer goes through `answerWithTable`. It was the only strong prompt rule in the release with **no runtime enforcement**, and live it does not hold:

- **R4: 1 of 8** expected-table runs finished without the table.
- **R5: 3 of 8**, two of them ending in an **apology**.

The apology is why this is a runtime problem and not a prompt problem. Verbatim, from a turn that had every tool available and had been refused nothing:

> *"the requested results are multi-row tables and must be returned through the table-rendering path, but that final table call was not made before the tool session ended."*

The model is not disagreeing with the rule — it is **restating the rule correctly** and then asserting a false fact about turn mechanics: that its opportunity to call the tool has passed. Nothing had ended. **A prompt cannot correct a belief the model holds while it is holding it**; a stronger instruction is read by the same model that already believes it is out of turns. Only the runtime can say otherwise, and it says it the way §B.2 already does: refuse the bare-text finish **once** and hand back a round.

### J.2 The condition

At exit #1 (`not result.tool_calls`), **after** the pending-intents check, refuse when **all** hold:

1. **No successful `answerWithTable` this turn.** Any successful designation satisfies this, including a blank-`answer` one — its tables still reach the user through the envelope, which is what the gate protects.
2. **The turn's trail holds ≥1 successful `runQuery`/`runBlueprint` with `result_preview.row_count > 1`.**
3. **The window's one forced re-round is unspent.**

**`> 1`, not `>= 1`, is the whole safety margin.** A **zero-row** result is a correct prose answer ("no employees match") — 04 §B.4 already treats an empty set as an answer, not a failure — and a **single row** is a single figure. Live q6 is the regression case and answers correctly in prose today. **`sampleRows`/`getTableSchema`/the listings never count**: they are discovery, and a model that peeks at ten sample rows before answering one number is doing exactly what it was told.

Both facts are **turn-scoped window-locals seeded from the persisted trail**, for the reason `seen_read_calls` is (§C.1): a budget-cap `continue`, an `askUser` resume and a mid-DAG blueprint resume each start a fresh `_run_loop_body`, and a gate that forgot the rows already in hand would go silent on exactly the long turns that produce several tables. The trail walk is turn-filtered, which is also the cross-turn protection — a multi-row query from turn 3 cannot gate turn 4's prose.

### J.3 The gate has its OWN per-window allowance (revised 2026-08-12, on measurement)

**This reverses the shared-grant decision below, which shipped and was measured.** The history is kept because the original reasoning was sound and the thing it got wrong was a fact about live behaviour, not an argument.

**What shipped first.** One `claim_finalization_block` key, one forced re-round per window, both gates — chosen so the worst case stayed exactly what it was before the shape gate existed: **one** extra round-trip per window, never two. The cost was written down and accepted: *"a window whose grant went to the intent nudge leaves the shape gate silent."*

**What the measurement showed.** That cost is not occasional; on the multi-intent questions this whole release exists for, it is the **normal** path. The two gates do not compete for one window — they fire in **sequence**:

1. the model finishes in prose with an intent still pending → **intents** nudge, allowance gone;
2. it closes the ledger and finishes in prose again, tables still untabled → shape gate qualifies, finds nothing, emits `loop_answer_shape_exhausted`, and the untabled prose goes to the user.

**2 of 4 live runs** of the three-part question went exactly that way. Telemetry signature: `loop_finalization_refused=1` **+** `loop_answer_shape_exhausted=1` **+** `loop_answer_shape_refused=0` — traces `900a85a4` and `16f090db` (Phoenix `data-agent-runtime`). The gate was starved on precisely the question it was built for, and the "conservative" bound is what starved it: a single allowance can only ever serve whichever complaint arrives **first**.

**Now: two independent allowances**, `intents` and `answer_shape`, selected by a required `kind` on `claim_finalization_block` and spelled into the persisted key (`"0:1:intents"`, `"0:1:answer_shape"` — `models.finalization_block_key`).

- **New bound: TWO extra round-trips per window**, one per kind, still multiplied only by `max_budget_windows`. That is the honest price and it is what the sequence above costs.
- **Precedence is unchanged** — the gate stays an `elif` on the pending-intents branch, so at most one refusal happens per round-trip and the intents refusal is byte-identical when it fires. Precedence is about *which complaint this round*, and was never what starved the gate.
- **`loop_answer_shape_exhausted` regains a single meaning**: this gate already refused once in this window. Under the shared grant it also meant "the other gate took it", which is exactly why the starvation was hard to see in the data — the counter fired for two unrelated reasons.
- **`already_refused_this_round` stays one flag**, not one per kind: `answer_shape` lives only at exit #1 and only in the `elif`, while the batched exit-#2 refusals the flag exists for require tool calls, so a round-trip has at most one refusing kind.

**The Protocol has four implementations** — `session/couchbase_store.py`, `session/memory_store.py`, and the two hand-written proxies in `scripts/run_ui_runtime.py` / `scripts/run_ui_runtime_real.py`. `kind` is **required, with no default**, because a defaulted parameter is precisely what a hand-written proxy forwards silently and wrongly; `tests/runtime/test_launcher_session_store_proxies.py` compares signatures against the Protocol and fails for any implementation that drifts.

### J.4 What the nudge says

Ephemeral `user`-role injection, one round-trip, never persisted, draft carried back, `last_assistant_text` cleared — **§B.2/§B.3 unchanged**, for the same reasons. Three things it must say, in this order, because the failure was a belief and not a preference:

1. **The turn is not over and `answerWithTable` is available** — *"you can and must call it in your NEXT response."*
2. **One table per part answered** — `blueprint_id` for a blueprint's result, `sql` otherwise.
3. **The escape hatch**: if the answer really is a single figure or an empty result, **re-send your full answer** with no tool call and it will be accepted.

(3) is not decoration. The gate reads row counts, not meaning: a turn can legitimately run a multi-row query and answer one figure from it. The hatch keeps that turn correct at a cost of one round-trip instead of forcing a table nobody asked for — and it is why the draft must be carried back.

**The echoed draft marks its own truncation** (` …[truncated]` past `_MAX_NUDGE_DRAFT_CHARS`), and the hatch asks for the model's *full* answer rather than for the echo verbatim. The echo is the model's ONLY surviving copy — exit #1 persists nothing and D22 discards free text around tool calls — and the slice is invisible from the inside, so an unmarked cut beside "re-send this unchanged" silently loses the tail of a long answer.

### J.5 A second bare-text finish passes

Grant spent ⇒ record `loop_answer_shape_exhausted` and finalize. **The runtime never hard-locks a turn**, exactly as `ENFORCEMENT_EXHAUSTED` does not — minus the ledger write, because answer shape has no ledger and the user's answer is in hand.

**"Unchanged" in the nudge is an instruction, not a check.** ANY second bare-text finish passes — rephrased, expanded, or a different answer entirely. Nothing compares it to the draft, and nothing should: models rarely re-send verbatim, and a runtime that required it would hard-lock precisely the model that took the escape hatch in good faith and reworded on the way.

**"Refuse once" is per WINDOW and per KIND, not per turn.** The claim key is `(turn_index, window, kind)` (§C.1, §J.3), so a budget-cap `"continue"` starts a new window with a fresh allowance of each kind and the gate can refuse again — at most once more per window, bounded overall by `max_budget_windows`. That is the same arithmetic the pending-intents refusal has always had; it is called out here because "one forced re-round" reads as per-turn and is not.

### J.6 Not gated

`askUser` / budget-cap / blueprint pauses, the hard ceiling, and the `"stop"` resume. The gate lives **only** on the ordinary no-tool-calls exit; a pause is not a finish, and the ceiling path never reaches the exit at all.

### J.7 Tests

`tests/runtime/loop/test_answer_shape_gate.py`:

| Case | Expect |
|---|---|
| Multi-row query, bare-text finish | Refused once; nudge injected (user role, one round-trip, not persisted); `answerWithTable` next round accepted |
| **Budget-cap resume** after two multi-row calls | `multi_row_calls: 2` — **the seeding proof**, and the only test that has it (see below) |
| Draft longer than `_MAX_NUDGE_DRAFT_CHARS` | Echo carries ` …[truncated]`; a short draft carries no marker |
| Same, prose again | Passes; `loop_answer_shape_exhausted`; **two claims of `answer_shape`, one allowance** |
| Two multi-row calls | `multi_row_calls: 2` |
| **Zero rows only** (live q6) | Untouched — no refusal, no event, **no store round-trip** |
| Single row | Untouched |
| `sampleRows` with 10 rows | Untouched |
| Turn that answered via `answerWithTable` | Untouched |
| `askUser` pause / hard ceiling holding multi-row results | Not gated |
| Later turn of the same session | Not gated; no claim |
| Pending intent **and** untabled rows | Intent nudge fires (precedence); on the handed-back round the ledger closes and the shape gate fires **from its own allowance** — two claims, `["intents", "answer_shape"]`, two nudges, then the table. **This is the starvation regression** (§J.3) |
| Second turn of the session, multi-row prose | Refused on its own merits — `{"0:1:answer_shape": 1, "1:1:answer_shape": 1}`; cross-turn replay safety for the new claim |
| `finalization_block_key` / store | Kinds are independent allowances; an unknown kind RAISES rather than minting an unbounded one |
| Refused round reaching the budget cap | Charged (§C.3); the cap's force-block finds nothing pending, so **no ledger write and no `loop_intent_force_blocked`** |
| `tests/runtime/observability/test_tool_span_wiring_e2e.py` | Both events survive the **real** `guardrail_observer`, and `multi_row_calls` actually exports |

**The seeding proof is `test_a_budget_cap_resume_reseeds_the_count_from_the_persisted_trail`, and it has to be a dedicated test.** Window 1 runs both multi-row queries and caps; window 2 does nothing but finish in prose, and must still be refused with `multi_row_calls: 2`. An earlier draft of this section claimed the blueprint resume tests proved it — **they do not**. They resume a two-row blueprint and finish in prose, so they exercise the seed, but `ScriptedModelClient` does not raise on leftover turns: a seed regression there just leaves the helper's second turn unconsumed and the suite stays green. Those tests now assert `resume_model.calls_made == 2` so the exercise is at least *observed*; the proof lives in the dedicated test, which fails on a seeded-count regression and is the only thing that does.

---

## I. Review corrections

| Was | Now | Why |
|---|---|---|
| No turn gate | **§A** | A stale state from an abandoned turn refused an unrelated turn and corrupted the old one's record |
| Counter local to `_run_loop_body`, "fresh per window" | **§C.1 persisted, keyed by window** | `_run_loop_body` re-enters on every resume while `window_count` stands still ⇒ unbounded re-rounds |
| Cap counted per refused call | **§C.2 per round-trip** | Two `answerWithTable` in one batch burned both chances with no re-round given |
| Exit #1 `continue`d without charging | **§C.3 `record_iteration` first** | Refused rounds were free; only the 60s wall clock stopped them, and every resume restarts it |
| "Would fabricate a tool_call" as the reason to reject a synthetic entry | **§B.2 — three real reasons** | Discovery emulation fabricates exactly that; the decision stands, the reason didn't |
| Nudge threaded "like `discovery_canonical`" | **§D one round-trip** | That lifetime repeats the nudge forever and migrates it past work it predates |
| Draft answer discarded | **§B.3 carried back; `last_assistant_text` cleared** | The model had to regenerate blind, while the refused prose could still reach the user on the ceiling path |
| ~~Exit-#2 in-scope rationale = status gate~~ → "`frozenset()` provenance is binding" | **§B.1 — the STATUS GATE is binding in-turn; a turn-scoped drop bounds it cross-turn** | **The correction was itself inverted, and shipped a defect.** `filter_trail`'s current-turn exemption is status-gated and provenance-blind, so `frozenset()` is load-bearing only where no `current_turn_index` is passed — i.e. exactly the later turns where the entry must NOT survive. It replayed the pending-intent descriptions, dead intent ids and refused draft prose into every later turn of the session, under any since-narrowed scope. Fixed by matching the error code in `_is_stale_model_text_entry`; `frozenset()` stays, for the union |
| Claim failure propagates | **§B.1 — caught, `loop_finalization_block_claim_failed`, treated as `False`** | A CAS/connection failure aborted `_run_loop_body` with the answer in hand; the sibling `_force_block_pending_intents` already took the opposite posture |
| Counter keyed by window | **§C.1 keyed by `(turn_index, window)`** | `window_count` restarts at 1 every turn while the map persists for the session, so turn N+1's window 1 collided with turn N's — the forced re-round was silently dead from a session's second block-spending turn onward |
| "Read at the exit" vs "byte-identical" | **§E load once, refresh from tool results** | The two were contradictory; a per-exit store read is not free |
| Three escapes | **§F four**, incl. budget cap during a refused round | C.3 creates a new path to the cap |
| Refused-round cap ⇒ `BUDGET_EXHAUSTED` | **§F.0 `ENFORCEMENT_EXHAUSTED`**, capacity kept as `budget_cap_reached` on `loop_intent_force_blocked` | **This reverses a decision that passed two review rounds, on measurement.** Live: answer computed at 25s, turn capped at 61.6s on the **wall clock**, tokens +385 across the final three rounds — 36 seconds spent on two rejected `updateAnalysisState` calls and one refused `answerWithTable` (`s412e8424614e465bbd26d7a2a1400ebe` / `647416592aff2225d1903ae7c82b8396`). **More budget would have changed nothing**, so the label sent 07 §E.2's reader to raise a ceiling that was not the problem. The reversal is one branch wide: the hard ceiling and the `"stop"` resume are untouched, and `RUNTIME_REASON_CODES` stays at three |
| "No intent ever ends `pending`" | **§F.1 scoped to terminated turns** | False for abandoned pauses and CAS races; would fail 07's assertion against a real store |
| Splice order unspecified | **§D.1 — this doc owns it** | The nudge would otherwise steal `_last_user_index` from the state block |
| Check placed "in the same block as" the blueprint-not-run nudge | **§B.1 — before `_resolve_answer_sql`** | Otherwise it fires the answer-table hooks for a refused designation and clobbers the better message |
