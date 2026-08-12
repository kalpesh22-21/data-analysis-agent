# 05 — Finalization Enforcement

**Spec:** [§7](../release-1-routing-and-intent-coverage.md) · **Size:** M · **Depends on:** 03, 04 · **Blocks:** 07
**Revised** 2026-08-11 after review — see [§I](#i-review-corrections). **Supersedes spec §7's exit-#1 mechanism** (the spec says "synthetic tool result"); the invariant is unchanged, only the delivery.

This is where `analysisState` gets teeth. All of it lives in `runtime/loop/agent_loop.py`. Nothing is in production, so schema and signature changes are unconstrained.

**Invariant (scoped — see §F):** no intent ends `pending` on any turn that reaches a terminal outcome.

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
- `provenance=frozenset()` — **this is what keeps the entry in scope.** `is_provenance_in_scope(frozenset(), scope)` is unconditionally `True`, so the entry reaches the model on its own merits. (`filter_trail`'s current-turn exemption for non-`ok` entries is belt-and-braces, not the binding reason. The docstring at agent_loop.py:363 has the same imprecision.)
- **`denial_detail` names the pending intents.** `_render_entry` (budget.py:280) builds model-facing text as `entry.denial_detail or classify_denial(entry.error_code).user_message` — never from `ToolResult.user_message`, which has no `TrailEntry` field. Register the code in `denial_mapping.py` or the fallback string says nothing actionable.

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

An exit-#1 refusal leaves **no** persisted artifact by design, so it cannot be reconstructed from the trail. **Persist it:** `SessionDoc.finalization_blocks: dict[str, int] | None` keyed by window — `{"2": 1}` — so "one per budget window" is literal. A field on `SessionDoc`, **not** on `AnalysisState`, which is model-writable and unknown-key-rejecting. Added to 03 §B's table.

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

**`ENFORCEMENT_EXHAUSTED` means "enforcement could not establish a disposition"** — **not** that the system proved the intent impossible (Lead, 2026-08-11). Word it that way in `denial_mapping.py`, in telemetry, and in 07's metric report. Claiming proof would overstate what the runtime knows: zero rows is often a correct answer, and some denial probes cost one metadata call (04 §B.4). A user who withdraws an ask mid-clarification also lands here, and does so legitimately under that reading.
| Budget cap reached *during* a refused round | via C.3 | `BUDGET_EXHAUSTED` |

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

### F.2 Forward reference

If the Lead approves forcing `USER_DECLINED_CLARIFICATION` when the turn contains a consumed `askUser` pause and the intent is still pending (spec finding 4, open), it lands here as a **fourth runtime-forced code** — never model-declarable, so 04's block validator is unaffected. Additive.

---

## G. Interactions

**Pause paths are not finalization.** `askUser`, budget-cap and `runBlueprint` `ToolPause` all return non-`done` statuses with intents legitimately `pending`. Not gated.

**`_resume_blueprint`** re-enters `_run_loop`, so its eventual exit is gated by the same code.

**Batch with both.** 03 §E.2's partition commits `updateAnalysisState` before `answerWithTable` in the same response, so the model can close its last intent and finalize in one round rather than being refused into an extra one.

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
| **Refused round** | `record_iteration` charged; a refused round can trip the cap |
| Second attempt, still pending | `ENFORCEMENT_EXHAUSTED`, finalization proceeds |
| Counter across an `askUser` resume | **Not reset** — still one per window |
| Hard ceiling / `"stop"` resume with pending | Forced `BUDGET_EXHAUSTED` / `USER_STOPPED` |
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
| Exit-#2 in-scope rationale = status gate | **§B.1 — `frozenset()` provenance** | The status gate is belt-and-braces; provenance is binding |
| "Read at the exit" vs "byte-identical" | **§E load once, refresh from tool results** | The two were contradictory; a per-exit store read is not free |
| Three escapes | **§F four**, incl. budget cap during a refused round | C.3 creates a new path to the cap |
| "No intent ever ends `pending`" | **§F.1 scoped to terminated turns** | False for abandoned pauses and CAS races; would fail 07's assertion against a real store |
| Splice order unspecified | **§D.1 — this doc owns it** | The nudge would otherwise steal `_last_user_index` from the state block |
| Check placed "in the same block as" the blueprint-not-run nudge | **§B.1 — before `_resolve_answer_sql`** | Otherwise it fires the answer-table hooks for a refused designation and clobbers the better message |
