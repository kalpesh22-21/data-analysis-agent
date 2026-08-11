# 05 — Finalization Enforcement

**Spec:** [§7](../release-1-routing-and-intent-coverage.md) · **Size:** M · **Depends on:** 03, 04 · **Blocks:** 07

This is where `analysisState` gets teeth. All of it lives in `runtime/loop/agent_loop.py`.

**Invariant:** no intent ever ends `pending`.

---

## The two terminal exits

`_run_loop_body` has exactly two paths to `status="done"`, and they need different mechanisms because only one has an error channel.

### Exit #2 — `answerWithTable` (easy; precedent exists)

`agent_loop.py:1992` — a successful `answerWithTable` sets `designated_answer_text`, and the turn ends after the batch drains.

Refuse by returning a retryable error `ToolResult` **before** the trail entry is written, exactly as `_answer_table_blueprint_not_run` (agent_loop.py:355) already does for its case. Copy that function's shape:

- `status="error"`, `error_code=FINALIZATION_BLOCKED_PENDING_INTENTS`, `retryable=True`
- **`denial_detail` names the pending intents.** This is the only channel that reaches the model: `TrailEntry` has no `user_message` field, and `context/budget.py::_render_entry` regenerates model-facing text from `error_code` alone via `classify_denial`. A message set only on `user_message` is silently dropped.
- Register the code in `dispatch/denial_mapping.py` or the model gets *"Something went wrong processing that request."*

A non-`ok` status also stops the terminal exit firing and — because `scope_filter.filter_trail`'s current-turn exemption is status-gated to `status != "ok"` — the entry reaches the model this same turn. That is exactly why the existing nudge works; the docstring at agent_loop.py:355 explains it.

### Exit #1 — a model turn with no tool calls (needs a mechanism decision)

`agent_loop.py:1719` — `if not result.tool_calls:` persists the assistant `TurnMessage` and returns `done`. **There is no error channel here.** The model produced prose, not a call.

A `tool` message cannot be injected standalone: `_assembled_to_canonical` only ever emits one by expanding a trail entry into an `assistant(tool_calls) + tool` pair. Delivering the nudge as a synthetic trail entry would fabricate a `tool_call` the model never made. (D94's sentinel looks similar but is not — it fills the slot of a call the model *did* make.)

**Use ephemeral `user`-role injection.**

- Do **not** persist the assistant answer, and do **not** persist the nudge. Both are within-turn control flow.
- Thread the nudge into `_build_canonical_messages` the way `discovery_canonical` is threaded: computed in `_run_loop_body`, passed as an argument, spliced at build time, never written to `doc.messages`.
- **Placement:** at the tail, after the current question — it is a response to what the model just tried. There is precedent for a mid-turn `user` message at the tail: the `askUser` resume answer is appended at the same `turn_index`.
- **Role `user`, not `system`:** the base prompt must remain the sole `role:"system"` message.
- Persisting it would surface in the `/session/history` transcript as something the user said. It must stay ephemeral.

**Turn-window state:** a `finalization_nudges_used` counter alongside `seen_read_calls` / `retrieval_memo` in `_run_loop_body` — fresh per window, not persisted.

---

## The forced-re-round cap

**One forced re-round per budget window.** Enforcement must not be able to burn the window it is protecting.

Per turn the ceiling is `max_budget_windows` (default 3), so at most three forced re-rounds across a turn — and only if the user answers "continue" at each cap.

```
attempt 1 → refuse, inject nudge, nudges_used = 1
attempt 2 → nudges_used exhausted → force-block and finalize
```

---

## Terminal escapes

Three, all runtime-forced, all marking every surviving `pending` intent `blocked` before finalization proceeds. Without them the turn cannot terminate — both exits are reachable with pending intents and must return something.

| Trigger | Site | Forced code |
|---|---|---|
| Hard ceiling | `window_count >= self._max_budget_windows` → `stopped_hard_ceiling` (agent_loop.py:2059) | `BUDGET_EXHAUSTED` |
| Budget-cap resume answered `"stop"` | `resume()`, `normalized.startswith("stop")` (agent_loop.py:695) | `USER_STOPPED` |
| Forced re-round spent, intents still pending | this file | `ENFORCEMENT_EXHAUSTED` |

Each writes the state once, then finalizes. Each has its own telemetry counter (06).

**The `"stop"` path is easy to miss** — it returns `done` from inside `resume()`, well before `_run_loop`, with `tool_calls_made=0` and a canned message. It needs its own force-block call; it will not inherit one from the loop body.

**`ENFORCEMENT_EXHAUSTED` is what makes the invariant literal** now that only two reasons are model-declarable. An intent that is genuinely unanswerable but not provably so has no model-side exit — so without this path the turn either finalizes with a `pending` intent (invariant broken) or grinds to the budget cap on an ordinary request (bad UX for a correctness feature).

---

## Where the check goes

`analysisState` is on the session doc, so the loop must read it before allowing either exit. It changes within the turn, so read it at the exit, not once per window.

- **Exit #2:** in the per-tool-call loop, in the same block that currently handles the `answerWithTable` blueprint-not-run nudge (agent_loop.py:1884) — before the trail entry is written.
- **Exit #1:** immediately after `if not result.tool_calls:`, before the assistant message is persisted.

**No state ⇒ no enforcement.** A single-intent turn has no `analysisState`, so both exits behave exactly as today. This must be the fast path and it must be byte-identical — most turns are single-intent.

---

## Interactions to get right

**Pause paths are not finalization.** `askUser`, a budget-cap pause, and a `runBlueprint` `ToolPause` all return non-`done` statuses with intents legitimately `pending`. Do not gate them. Only the two `done` exits are gated.

**`_resume_blueprint`** re-enters `_run_loop`, so its eventual exit is gated by the same code. No separate handling.

**Blueprint mid-DAG resume** rehydrates and continues; state is untouched by it.

**Interaction with the intra-batch ordering rule (03 §E):** the partition puts `updateAnalysisState` first, so a batch containing both a state update and `answerWithTable` commits the update before the finalization check runs. That ordering is what lets the model close its last intent and finalize in one response, rather than being refused and forced into an extra round.

---

## Tests

`tests/runtime/loop/test_finalization_enforcement.py`:

| Case | Expect |
|---|---|
| No `analysisState`, exit #1 | Finalizes — byte-identical to today |
| No `analysisState`, exit #2 | Finalizes |
| All intents `completed`, exit #1 / #2 | Finalizes |
| One `pending`, exit #2 | `FINALIZATION_BLOCKED_PENDING_INTENTS`, retryable, `denial_detail` names the intent, turn continues |
| One `pending`, exit #1 | Assistant answer not persisted; nudge injected as ephemeral `user` message; loop re-enters |
| Nudge injected | Not present in `doc.messages`; not in `/session/history` |
| Second attempt, still pending | Force-blocked `ENFORCEMENT_EXHAUSTED`, finalization proceeds |
| Nudge cap | At most one forced re-round per window |
| Hard ceiling with pending | All forced `BUDGET_EXHAUSTED`; `stopped_hard_ceiling` returned |
| Budget-cap resume `"stop"` with pending | All forced `USER_STOPPED`; `done` returned |
| `askUser` pause with pending | **Not** gated |
| Batch = update + `answerWithTable`, update completes the last intent | Finalizes in one response, no refusal |

`tests/runtime/loop/test_finalization_enforcement_adversarial.py`:

| Case | Expect |
|---|---|
| Model drops the pending intent from the array and finalizes | Rejected by 03's immutability rule, not by this file — assert the composed behaviour end-to-end |
| Model marks pending → `completed` citing a `searchBlueprints` call | Rejected by 04; intent stays pending; finalization still refused |
| Model repeats `answerWithTable` after refusal without resolving | Refused again until the cap, then force-blocked |
| Pending intent + hard ceiling + `answerWithTable` in the same batch | Terminates exactly once, no double-write of state |

## Done when

- [ ] Both exits gated; no-state path byte-identical to today.
- [ ] Exit #2 uses a retryable `ToolResult` with `denial_detail` naming the intents; code registered in `denial_mapping.py`.
- [ ] Exit #1 uses ephemeral `user`-role injection; nothing persisted; nothing in history.
- [ ] Nudge capped at one per budget window.
- [ ] Three forced-block paths, including the easily-missed `"stop"` branch in `resume()`.
- [ ] Pause paths not gated.
- [ ] Adversarial suite green.
