# 09 — The answer judge

**Spec:** this document · **Size:** M · **Depends on:** [05](05-finalization-enforcement.md) (the gate chain, the allowance), [08](08-multi-table-answer.md) (exit #2) · **Blocks:** nothing — it is additive

> **The one-line claim.** Every check that stands at a terminal exit today reads the SHAPE of the finish. §B asks *did you do the work?*, §J *did you deliver it in the required form?*, §K *did you say anything at all?*, §L *does what you said stand on anything?* — and all four are regexes and counters over the loop's own bookkeeping. **None of them can read the answer.** This one can, at the price of a model call, and it is bounded by the same allowance machinery so that price is paid at most once per window per turn.

---

## A. What it is

An LLM check that runs at a terminal exit, receives a scope-filtered brief of the turn, and returns a structured verdict. On rejection the loop hands the model one round back with the judge's feedback; on a spent allowance it ships the answer.

**It is a fifth complaint in the §L family, not a new species.** Everything structural about it is inherited: it is guarded on `not refused_finalization`, it charges to a named allowance kind claimed through `FinalizationGate.may_refuse`, the exhausted path ships the answer, and the events carry a runtime-authored slug and nothing else.

**Three things make it different from an `AnswerRule`, and all three are why it is not simply a fourth entry in `ANSWER_RULES`:**

| | `AnswerRule` | The judge |
|---|---|---|
| predicate | sync, pure, `str` in / `bool` out | async, one model round-trip |
| cost of running | free | a model call, on the terminal path |
| what it reads | `prose` + `turn_sql` | a brief of the whole turn |

`first_match` must stay sync and pure — it is called on every prose finish and its purity is what makes the registry cheap to extend. So the judge is a separate, explicitly LAST check, and it never runs when a rule already matched.

---

## B. Why an LLM at all

The judge earns its keep only on questions with no deterministic form. Four criteria were proposed; **two of them already have one and must NOT be given to the judge.**

| Proposed criterion | Verdict |
|---|---|
| Was the question answered? | **Partly deterministic.** For a multi-deliverable turn `AnalysisState.intents` already tracks per-part disposition and §B already refuses a finish with any `pending`. The judge's residue is: *the ledger says completed, the prose does not mention it* — real, and unreachable from the ledger alone. |
| Partial answer, reason not stated? | **Judge.** `reason_code` lives on the intent; whether the ANSWER TEXT says so is semantic. |
| Assumption made, not recorded? | **Judge, and this is the core one.** `accum.assumptions` says what WAS recorded; nothing can say what SHOULD have been. |
| Markdown / schema / SQL leakage | **Already enforced** — `markdown_table`, `sql_in_answer` (§L), `scrub_answer_prose`. Do not duplicate; two enforcers for one rule disagree, and the judge's allowance would be spent on a complaint a regex already made for free. |
| Non-contextual follow-up question | **Judge, at a DIFFERENT site** — see §C.3. |

**One criterion is added that was not proposed, and it is the strongest.** *Does the prose contradict the rows the turn actually produced?* — the answer claiming a rise where the table shows a fall, naming a leader that is not the top row. No counter can see it, and unlike "was the question answered" it is checkable against evidence the judge is holding.

⚠ **`ungrounded_quantity` (§L) is NOT superseded.** It fires when `turn_sql` is empty — the judge is not needed to know that nothing ran, and a regex that costs nothing must keep taking that case. The judge's grounding value is the opposite case: a query DID run and the prose still does not follow from it.

---

## C. Three sites

### C.1 Exit #1 — the prose finish (`agent_loop.py:2687`)

After §L's registry, guarded on `not result.tool_calls and not refused_finalization` and a non-blank draft.

**Order against §K is free**, for §L.5's reason: the judge requires non-blank prose and §K fires only on blank, so the two conditions are disjoint by construction. It is placed AFTER §K anyway, so a silent finish never pays for a model call.

### C.2 Exit #2 — the designated answer (`agent_loop.py:3096`)

**This is the site that matters most and the one §L.9 explicitly left open:** *"Exit #2 is out of scope: a designated `sql` the model never executed is the same class of hole with a table around it, and needs its own check."*

Today an `answerWithTable` finish is checked by nothing. No answer rule runs there. A multi-part answer that closes both intents, designates one table and discusses one subject ends `done` with no event and no log line. **That is the population where "was every part answered" has teeth**, and a judge wired only to exit #1 will never see a single member of it.

⚠ **A judge at exit #1 alone is not a smaller version of this design; it is the wrong half.** §L.5 records the escape route: the `markdown_table` nudge tells the model *"call answerWithTable instead"*, the model complies, and it leaves through the exit nothing inspects.

### C.2.1 ⚠ The "designated SQL that never ran" check was BUILT AND REVERTED

The obvious deterministic companion to the judge here — a designated `sql` absent from `accum.sql_executed` is a table with nothing behind it — **is wrong, and 05 §L.9's framing of it as "the same class of hole" is wrong with it.**

`query_page.py` states the contract outright:

> The SQL is whatever the model designated; **it is NOT required to have been executed during the turn.** It adds NO authority: it runs through the SAME `ToolDispatcher.dispatch("runQuery", ...)` with the CALLER's own credentials, so column scope, read-only enforcement, row caps, denial mapping and provenance capture are the identical code path.

The grid is populated by executing the designated query **at render time**, per page. A designation the turn never ran is therefore a *presentation query composed at answer time* — a designed-for shape, not a fabrication. `tests/eval/fixtures/routing/case-13-narrowed-scope-reload.yaml` documents exactly that, in a comment, as the thing it exists to exercise.

The check was implemented (sqlglot-normalised, LIMIT-stripped, casefolded, fail-open on any parse failure), and it refused **14 existing tests plus 8 eval cases** before the contract was found. It is removed entirely. **Do not rebuild it.** What remains of the underlying worry — prose describing rows the model never saw — is a GROUNDING question the judge is already holding the evidence for (`sql_executed` versus the results versus the draft), and it belongs to `contradicts_result`, not to a refusal.

The general lesson, and the reason this section exists rather than a deleted branch: **the fixture that "fails" may be asserting a contract the new check has not read.** Six of the seven surviving checks in this family were validated against live traces; this one was validated against an argument.

### C.3 The askUser pause (`agent_loop.py:3471`)

The proposed criterion *"a follow-up question that asks which COLUMN instead of which THING"* cannot be reached from either answer exit: `askUser` is intercepted in the loop, never dispatched, and terminates the turn as `paused_ask_user`. §L.9 leaves pauses ungated.

**The scrub is already there and is the argument FOR the judge, not against it.** `scrub_answer_prose` runs on the question at line 3485 — so *"which AnnualSalary did you mean?"* reaches the user as *"which [schema detail withheld] did you mean?"*. That is §L.3's trap exactly: the redaction produces something that is neither a usable question nor an honest one. **The judge's job here is to produce a question that never needed redacting.**

**This is the cheapest site to reject at and should ship first.** The user has seen nothing, no answer is at risk, and the cost of a rejection is one round-trip that is invisible to them.

---

## D. What the judge sees

### D.1 The ceiling

`RuntimeSettings.request_token_budget()` = `(model_context_window − response_token_reserve) × 0.8` = **89,600 tokens**. The judge is a SEPARATE request, so it gets its own; it does not shrink the agent's context.

The `0.8` is not spare margin to spend. Its docstring says why it exists: `_estimate_tokens` is chars/4 and JSON- and SQL-dense content runs ~3 chars/token in practice. **The judge brief is the densest payload this system builds** — SQL verbatim plus JSON previews. Use `fit_request_to_budget` and `_estimate_tokens`; do not write a second estimator.

### D.2 The brief

Everything below is a window-local or an accumulator at exit time. **No store reads on the common path.**

| Field | Source | Typical |
|---|---|---|
| question + date anchor | turn's first `user` message, `_turn_date_anchor` | ~100 tok |
| intents (id, description, status, reason_code) | `AnalysisState.intents` | ~200 tok |
| recorded assumptions | `accum.assumptions` | ~150 tok |
| SQL executed, verbatim | `accum.sql_executed` | 100–400 tok each |
| result previews | `_render_entry(entry, preview_row_count)` | ≤4,000 tok each |
| the draft answer | `result.assistant_text` / `args.answer` | <1k tok |
| designated tables (exit #2) | `accum.answer_tables` | ~200 tok |
| `figure_corroborated` | §D.4 | 1 field |

Single-deliverable exit #1: **~2–4k tokens.** Five-result exit #2: **~20–25k.** For scale, measured agent requests run 7.1k–31.5k.

### D.3 The judge sees exactly what the model saw — no more

**Render every result through `context/budget.py::_render_entry(entry, preview_row_count)`**, the same single producer that builds the model's tool messages. Same bytes, no possible divergence.

`preview_row_count` is 20 and `max_tool_result_tokens` is 4,000, so the model itself never sees more than 20 rows or ~4k tokens of any result. The full result IS reachable — `TrailEntry.result_full_ref` + `SessionStore.read_full_result` — and reading it here would be a mistake:

⚠ **A judge that reads `result_full` rejects answers the model could not have produced.** *"The answer says Engineering leads at 412 but row 847 disagrees"* — when row 847 was never in the model's context. That punishes the model for the preview cap, and it manufactures false rejections, which §L.6 establishes as the expensive direction: a false negative costs the status quo, a false positive burns a round-trip telling a correct model it was wrong, and §J.2's measured response to being wrongly refused is an apology asserting the turn is over.

**The rule: the judge may only fault the model for what the model could see.**

### D.4 Corroboration is a code pass, not a criterion

*"Does the figure in the prose appear anywhere in what this turn returned?"* is §L.7's designed-for-but-unbuilt check, it genuinely needs the full result, and it is **not a judge criterion — it is a scan.** Run it in code against `read_full_result`, hand the judge `figure_corroborated: true | false | unchecked`.

This converts the judge's weakest criterion into its strongest and keeps §D.3 intact: the judge still never READS the full result, it reads a boolean derived from it. `unchecked` is a first-class value (no figure in the prose, no ref, a failed read) and must never read as `false`.

### D.5 What the judge must not see

- **Anything the scope filter dropped.** `context/budget.py`'s ordering contract — *"this module must only ever be handed an already scope-filtered trail — it has no scope information of its own and performs no filtering"* — binds the judge identically. Feed it `filter_trail` output. A judge over an unfiltered trail is a D44 hole with a model call in the middle.
- **`AGENT_SYSTEM_PROMPT`, wholesale.** 11,297 chars ≈ 2,824 tokens per call, and it turns the judge into a conformance checker for every rule in it — after which a prompt edit silently changes judge behaviour with nothing testing it. Restate in the judge's own prompt only the specific rules its criteria test.
- **Retrieval cards / blueprint definitions.** Only needed for *"did the blueprint measure the right thing"*, which is out of scope (§J).
- **More than the current turn**, plus the immediately preceding user/assistant pair when the question is short or deictic. Full history is what overflows the budget and what the scope filter has most likely narrowed (`TurnMessage.provenance` drops prior assistant text once `column_scope` narrows).

### D.6 Trimming, and the failure it causes if done silently

If the brief overflows, the drop priority is NOT `fit_request_to_budget`'s. **Pinned:** question, anchor, draft, assumptions, intents, executed SQL — these are the SUBJECT of the judgement and dropping one inverts the verdict. **Droppable:** result previews, largest first.

⚠ **Mark what was trimmed.** The rendered preview already carries `truncated`, and `_render_entry` sets it when rows are cut — reuse the flag and make the judge prompt say what it means. **A judge that silently loses a result reports `unexplained_gap` for a part it simply could not see.** That is the most dangerous failure in this design, it is a trimming bug wearing a model's clothes, and it is invisible in telemetry because the verdict looks like every other rejection.

---

## E. The verdict

One forced tool call, no retries, **fail-open** — the discipline `learning/judge/schema.py` already states for a judge whose output is an optimization rather than a product. An unreachable provider, a timeout, a malformed response, an unknown slug all APPROVE, exactly as a deployment with the judge disabled would behave.

```
{ approved: bool, violation: <closed enum> | "", feedback: str }
```

**`violation` is a closed, runtime-authored enum**, because it is the only field that can go on a span (§K) and because an open string becomes its own bucket in every distribution query:

| slug | site | means |
|---|---|---|
| `unrecorded_assumption` | answer | a choice shaped the answer and `recordAssumptions` does not carry it |
| `unexplained_gap` | answer | part of the ask is unanswered and the prose does not say so |
| `contradicts_result` | answer | the prose disagrees with the rows the turn produced |
| `non_contextual_question` | askUser | the question is posed in schema terms, not the user's |

**`feedback` is untrusted model text re-entering model context** and goes through `sanitize_text(…, MAX_FIELD_CHARS)` before use — the treatment `finalization.py::_describe_pending` gives intent descriptions, and for a stronger reason: the judge reads tool results, which the Trust boundary section of the prompt treats as data that may attempt to instruct. **A judge that reads results and emits free text into the loop is a laundering path for injected content unless this sanitisation is unconditional.**

`approved: false` with an empty or unknown `violation` is a MALFORMED response and fails open. A rejection the runtime cannot name is a rejection it cannot report.

---

## F. The allowance

**Two kinds, not three** — added to `FINALIZATION_BLOCK_KINDS`:

| Kind | Sites | Why |
|---|---|---|
| `answer_judge` | exit #1 **and** exit #2, SHARED | One complaint about one answer. A model pushed from #1 to #2 by an earlier nudge must not be judged twice for the same finish. |
| `ask_user_judge` | the askUser pause | A different complaint at a different moment, and rejecting costs the user nothing — they have not seen the question. |

Worst case per window goes from four extra round-trips to **five**, plus one if the turn also asks. Still bounded by `max_budget_windows` (3).

**Both follow §J.5/§L.8 exactly: the exhausted path SHIPS.** The judge reads meaning, not truth, so a second refusal would be the runtime destroying an answer it cannot prove wrong.

### F.1 The peek problem — decide it here

Once the allowance is spent, a rejection cannot act. Calling the judge anyway costs a model call for no control effect; skipping it loses the exhausted-rate, which §L.6 names as the signal a check is tuned against.

`FinalizationGate` exposes only `refused_this_round`; `may_refuse` CONSUMES on grant. **Decision: add a non-consuming `has_spent(kind) -> bool` read and SKIP the judge when the allowance is already gone**, emitting `loop_answer_judge_skipped{reason:"allowance_spent"}`. The skip event carries the same operational signal at zero token cost — how often a turn reached a second finish after a judge rejection — and the tuning question ("did the fix land?") is answered by the *refused* rate falling, not by grading an answer nothing can act on.

**`has_spent` is WINDOW-LOCAL, not a store read**, and the consequence is worth stating because it was found by a test rather than by design. `_run_loop_body` builds a fresh gate on every entry, including every resume, while `SessionDoc.finalization_blocks` persists. So:

| Path | What happens |
|---|---|
| Refuse, model retries **inside the same window** | `has_spent` is `True` → **skipped**, no model call. The common case, and exactly the one §F.1 exists for. |
| Refuse, pause, **askUser resume** (same `window_count`, fresh gate) | `has_spent` is `False` → the judge runs, `may_refuse` is denied by the persisted claim → `..._exhausted`, answer ships. One judge call that could have been skipped. |

⚠ **The `_exhausted` events are therefore reachable only by the second row.** The ordinary refuse-then-retry sequence emits `_skipped`, never `_exhausted` — do not read a zero `_exhausted` rate as "the judge never gets overruled". Both branches are kept deliberately: the peek is the cheap common case, and the exhausted branch is the honest handler for the case the peek cannot see, which would otherwise drop a judged rejection with no event at all. Closing the gap would mean a new `SessionStore` Protocol method and a Couchbase round-trip on every terminal exit, to save a rare model call.

---

## G. The refusal, per site

### G.1 Exit #1 — ephemeral, draft echoed

Identical mechanics to §J/§K/§L: a `user`-role injection that is **persisted nowhere**, `last_assistant_text = None`, and the draft echoed through `_draft_echo` — exit #1 preserves nothing and D22 discards free text around tool calls, so the echo is the model's only surviving copy.

The nudge must open by saying **the turn is not over**. §J.4/§K.5 establish that the measured failure mode of a refusal is a belief about turn mechanics, not about content.

### G.2 Exit #2 — a persisted `ToolResult`

Exit #2 has a real error channel, so the refusal is the shape `finalization_blocked()` already uses:

```python
ToolResult(status="error", tool_name=ANSWER_TABLE_TOOL_NAME,
           error_code=ANSWER_JUDGE_REJECTED_CODE, retryable=True,
           provenance=frozenset(), result_preview=None, result_full=None,
           user_message=detail, denial_detail=detail)
```

Four consequences, each a lesson already paid for elsewhere in this file:

1. **`status != "ok"`** so the terminal exit does not fire and `filter_trail`'s status-gated current-turn exemption keeps the entry visible this same turn.
2. **`denial_detail`**, because `context/budget.py::_render_entry` builds the model-facing text from `entry.denial_detail or classify_denial(...).user_message` and never from `ToolResult.user_message`, which has no `TrailEntry` field.
3. **`provenance=frozenset()`, never `None`.** `None` means UNDETERMINED and `_compute_turn_provenance_union` is fail-closed — it would collapse the turn's union and drop the user's own answer from every later replay.
4. **No draft echo needed** — the model's prose is in `TrailEntry.args` and `_render_entry` replays it.

Two registrations ride along, and omitting either is a silent defect:

- `ANSWER_JUDGE_REJECTED_CODE` in `dispatch/denial_mapping.py::_DENIAL_TABLE` with `kind=DenialKind.GATE` and `enforcement=True` — GATE because the turn's WORK may be perfect; what was refused is the finish. Without the entry `classify_denial` degrades to a generic message; without `enforcement` the learning plane counts it as a substantive failure (H1).
- a match in `context/assembly.py::_is_stale_model_text_entry`, so judge feedback and the refused draft do not replay in later turns under a since-narrowed scope. It is persisted under `answerWithTable`, whose successful entries must keep replaying, so it must be matched by ERROR CODE and not by tool name — exactly as `FINALIZATION_BLOCKED_PENDING_INTENTS_CODE` is.

⚠ **The per-round flag is load-bearing here.** Exit #2 refusals happen inside the per-tool-call loop, which processes up to 8 calls from one response. `FinalizationGate.begin_round` / `_refused_this_round` is what stops `[answerWithTable, answerWithTable]` from spending two judge calls and two claims in one round-trip.

### G.3 askUser — ephemeral, and the turn continues

The judge runs BEFORE the `PauseCheckpoint` is built, on the raw `question` argument (**pre-scrub** — the point is to catch what the scrub would have to redact). A rejection injects a nudge and the turn continues instead of pausing. Nothing is persisted and the user never sees the rejected question.

---

## H. Wall-clock headroom — the skip that keeps this from costing answers

Live multi-intent turns were measured ending on the WALL CLOCK, not on rounds or tokens (`config.py::max_wall_clock_seconds`, raised 60 → 180 for that reason). A rejection issued near the cap is a rejection the model cannot act on:

```
R9  at 168s → prose finish, judge rejects, last_assistant_text = None
R10 regeneration begins → guard.exceeded trips at 180s
    → paused_budget_cap, assistant_text = None
```

The user is asked *continue / refine / stop* and shown nothing, having had a serviceable answer at 168s. **This is the judge converting a good-enough answer into an empty pause, and it is the failure mode most likely to make the feature net-negative.**

`BudgetGuard.usage()` already exposes what is needed — no new API:

```python
u = guard.usage()
if u.max_wall_clock_seconds - u.elapsed_seconds < settings.answer_judge_min_headroom_seconds:
    observer("loop_answer_judge_skipped", {"reason": "wall_clock"})   # ship the answer
```

Default **25s**: one model round-trip plus the judge call itself. A rejection is only worth making when there is room to act on it.

---

## I. Token accounting — NOT budgeted (decided)

**Judge tokens do NOT go through `guard.record_iteration` and do NOT count against `max_window_token_spend`.**

The argument for counting them was that the 1M ceiling is documented as a runaway backstop and an unmetered model call on the terminal path of every turn is a runaway path. **It is recorded here as a known, accepted residual risk rather than as an open question**, because three independent bounds already stand between the judge and a runaway, and the ceiling would only be the fourth:

1. the per-kind, per-window allowance — at most one judge REFUSAL per window per kind;
2. the `spent(kind)` skip (§F.1) — at most one judge CALL per window per kind;
3. the wall-clock headroom skip (§H).

⚠ **What this genuinely gives up:** judge spend is invisible to the pause the rest of the loop uses. A provider that starts returning slowly, or a judge prompt that grows, shows up as latency and cost with no in-loop signal. **Mitigation: `loop_answer_judge_called` carries the call's token count as telemetry only.** The number is observable even though nothing acts on it, so the decision can be revisited on data rather than on argument.

---

## J. What this does NOT do

- **It does not verify the numbers.** `figure_corroborated` asks whether a figure appears in the turn's own results; nothing asks whether the query measured the right thing. That is `semantic_catalog`/D56 territory (issues D2/D3) and stays there.
- **It does not judge routing.** A turn that hand-wrote SQL where a blueprint existed is judged on its ANSWER, like any other. §L.8's deferral of routing enforcement stands unchanged.
- **It does not close §L.8's hole, it narrows it.** The exhausted path still ships.
- **It does not fix a data-anchored window.** A blueprint whose window ends at the latest data on record and a user who meant the calendar year is issue **J7**, and the model is FOLLOWING `prompts.py` when it reports the data window. The judge's contribution is `unrecorded_assumption` — forcing the choice into the open where the user can see it. **Do not tune the judge to "fix" J7; it will start rejecting compliant answers.**
- **It does not replace an offline eval.** 07 §A3 (answer grading, live + judge) is a different instrument with a different bar, and this is not it.

---

## K. Telemetry

Every event takes the `loop_` prefix, without which `observability/tracing.py::guardrail_observer` drops it SILENTLY — the near-miss `loop_analysis_state_auto_bound` shipped that way for a review round.

| Event | Payload |
|---|---|
| `loop_answer_judge_refused` | `violation`, `site` |
| `loop_answer_judge_exhausted` | `violation`, `site` |
| `loop_answer_judge_skipped` | `reason` (`wall_clock` \| `allowance_spent` \| `disabled`) |
| `loop_answer_judge_failed` | `reason` (`timeout` \| `provider_error` \| `malformed`) |
| `loop_answer_judge_called` | `site`, `tokens` (§I) |
| `loop_ask_user_judge_refused` / `_exhausted` | `violation` |

`violation` and `site` must be added to `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST`; `reason` and `tokens`-shaped ints are already there. **`feedback` is NEVER an attribute** — it is model-composed text about the user's question and the turn's figures, and §L's span test asserts that neither the question nor a refused figure reaches a span.

### K.1 The judge's own span

The OpenAI SDK is auto-instrumented, so the judge's round-trip emits an `LLM` span whether or not anything asks for one — and that is the problem: **in the turn trace it is indistinguishable from the agent's own round-trips.** A turn that made five model calls and a turn that made four plus a judgement look identical, which is precisely what an operator watching judge cost or judge latency needs to tell apart. It matters more here than it would elsewhere because §I puts judge spend outside `max_window_token_spend`, making telemetry the only place it is observable at all.

`tracing.answer_judge_span` opens a `CHAIN` span AROUND the call, so the auto `LLM` span becomes its child through ambient context and the whole thing lands under `agent.turn` with no propagation wiring. Verified live:

```
agent.turn
├─ Response [llm] × 4              the agent's own round-trips
├─ answer_judge [chain]            site=exit_prose approved=true tokens=1101
│   ├─ Response [llm]              the judge's call, now attributable
│   └─ loop_answer_judge_called [guardrail]
└─ tool.runQuery / tool.getTableSchema / …
```

⚠ **A span attribute bypasses `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` entirely.** The events are filtered default-deny; this span is not. `site`, `approved`, `violation`, `tokens` and `outcome` are set by hand and are all closed vocabularies or counts — the brief and `feedback` must never join them. `tests/runtime/observability/test_answer_judge_span_e2e.py` asserts the absence by dumping every attribute of every exported span and grepping for the question, the draft and the figure.

The span wraps the timeout and provider-error exits too (`outcome=`), because that is where a latency question is actually answered.

---

## L. Determinism and the scripted suite

`AGENT_SYSTEM_PROMPT` is a module constant precisely so every rebuild and resume re-derives byte-identical messages (D45). **A live judge on the terminal path is a non-deterministic gate**, so:

- `answer_judge_enabled: bool = False` in `RuntimeSettings`. Off by default; a deployment opts in.
- A `ScriptedAnswerJudge` double for the A1 scripted-mechanics suite (07 §A), which must be able to drive refuse/approve/exhaust paths without a provider.
- The judge is constructed in `_build_agent_loop` from the same `ModelClient` seam, through `begin_turn_client` — one per-turn handle, like every other model call in the turn.

---

## M. The learning plane

`summary/loader.py::_infer_accepted_signal` counts a data turn with ANY assistant text as successfully answered. A judge-rejected-then-shipped answer must not enter the corpus as accepted, and judge feedback must never reach `TurnSummary.assistant_text` — the precedent is `EMPTY_ANSWER_FALLBACK_TEXT`, dropped at the single point the field is populated (05 §K).

**The verdict is a genuinely good learning signal** — a turn the judge faulted is a turn worth not learning from — but plumbing it is a separate slice and is NOT in scope here. This slice's obligation is only that the judge does not CORRUPT the existing signal.

---

## N. Build order

Smallest first, each independently shippable:

1. ~~**`loop/answer_judge.py`**~~ — **BUILT.** Brief builder, forced-tool schema, `parse_verdict` guard, fail-open, `AnswerJudge.review`. Plus `context/budget.py::render_entry`, the public parity seam for §D.3. 37 unit tests.
2. ~~**`FINALIZATION_BLOCK_KINDS` + `FinalizationGate.has_spent(kind)`**~~ — **BUILT.** Both allowance kinds and the non-consuming peek; 10 tests in `test_finalization_unit.py`.
3. ~~**The askUser site (§C.3)**~~ — **BUILT.** Loop wiring (`AgentLoop(answer_judge=…)`, `_judge`, `_judge_brief`), the four config fields, `app.py` construction, the two span attributes. 12 tests in `test_ask_user_judge.py`. Off by default; the whole runtime suite is unchanged with `answer_judge=None`.
4. ~~**Exit #1 (§C.1)**~~ — **BUILT.** After §L, before nothing; `answer_judge_nudge_text` with the marked draft echo, the draft clear, the shared allowance.
5. ~~**The deterministic exit-#2 check**~~ — **BUILT, THEN REVERTED. See [§C.2.1](#c21--the-designated-sql-that-never-ran-check-was-built-and-reverted).** The premise was wrong: `query_page.py` executes the designated query at render time, so a designation the turn never ran is a designed-for shape, not a fabrication. Do not rebuild it.
6. ~~**Exit #2 (§C.2)**~~ — **BUILT.** The persisted `ToolResult`, `ANSWER_JUDGE_REJECTED` in the denial table (`kind=GATE`), the `_is_stale_model_text_entry` match, and a round-local refusal (see §P.1 — the per-round flag alone was NOT enough).
7. ~~**`figure_corroborated` (§D.4)**~~ — **BUILT**, and narrowed on 05 §L.7's own argument: it emits `True` or "not checked" and **never `False`** — a non-match is not evidence, and reporting it as one would push the judge toward `contradicts_result` on every derived or rounded figure. Capped at `_MAX_CORROBORATION_READS` (4) full-result reads, short-circuiting on the first match, and skipped entirely when the prose reports no figure.

---

## O. Tests

| Case | Expect |
|---|---|
| Judge rejects at exit #1 | Refused once; draft echoed; a corrected answer next round accepted verbatim |
| Judge rejects twice | `loop_answer_judge_exhausted`; **the prose SHIPS** |
| Allowance already spent when the judge would run | `loop_answer_judge_skipped{allowance_spent}`; **no model call** |
| A §L rule matched this round | Judge never runs; no model call; `store.claims` unchanged |
| Judge rejects at exit #2 | Persisted `ToolResult`; turn does NOT end; `denial_detail` reaches the model via `_render_entry` |
| Designated `sql` the turn never ran | **NOT checked** — `query_page.py` executes it at render time (§C.2.1) |
| A figure present in the full result | `figure_corroborated: True` |
| A figure absent from the results | **`None`, never `False`** (05 §L.7) |
| Prose with no figure | Not checked; no `read_full_result` |
| A 50-row result, exit #2 | Brief preview is capped at `preview_row_count`, not the full 50 |
| Blank `answer` on `answerWithTable` | Not a finalization — not judged, allowance untouched |
| `[answerWithTable, answerWithTable]` both rejected | One claim, one judge call, two refusals |
| Exit #1 rejection then exit #2 finish | `answer_judge` is SHARED — the second meets a spent allowance and ships |
| askUser rejection | Nudge injected; turn continues; **no `PauseCheckpoint` persisted**; user never sees the question |
| Provider timeout / error / malformed / unknown slug | **Approves.** Fail-open, `loop_answer_judge_failed{reason}` |
| `approved: false` with empty `violation` | Malformed → approves |
| Wall clock inside the headroom | `loop_answer_judge_skipped{wall_clock}`; answer ships |
| Brief overflows the budget | Previews trimmed and MARKED; question/draft/assumptions/intents/SQL all survive |
| Trail out of `column_scope` | Never reaches the brief |
| `feedback` containing a newline + an instruction line | Sanitised before it enters model context |
| Span e2e | `violation`/`site` survive the real `guardrail_observer`; **`feedback`, the question and every figure do NOT** |
| `answer_judge_enabled=False` | Byte-identical loop behaviour to before this slice |
| Judge-rejected-then-shipped turn | `_infer_accepted_signal` unchanged; no judge text in `TurnSummary` |

---

## P. Post-review corrections

Two defects the first cut shipped, both found by review, both in the exit-site wiring rather than in the judge. Recorded because each is a trap the next check in this family can walk into unchanged.

### P.1 The cost-avoidance peek cannot reach the free-refusal path

**A second `answerWithTable` in one batch shipped the answer the judge had just refused.** Sequence: call A judged → refused → `may_refuse` grants, which RECORDS the grant. Call B reaches `_judge`; `has_spent` reports the allowance gone; the judge is SKIPPED — **and a skip returns `APPROVED`**, so B stayed `ok` and terminated the turn in the very round the judge refused it. The user received a near-copy of the refused answer and the feedback reached the model never.

§G.2 claimed the per-round flag covered this, and it does for every OTHER gate: they call `may_refuse` unconditionally and get the free `True` from `_refused_this_round`. **The judge cannot, because its cost-avoidance peek runs first** — the very optimisation §F.1 added to keep it affordable is what routes it around the mechanism.

The fix is a round-local `judge_refusal_this_round`, reset beside `designated_answer_text`: a later `answerWithTable` in the same batch is re-issued the same refusal, with no second model call and no second claim. Precedence at the site is now (1) reuse the round's refusal, (2) skip if another gate already refused this round, (3) judge.

⚠ **The generalisable lesson:** any check that skips itself to save money must ask whether the thing it is skipping had a side effect the rest of the chain depends on. `test_two_answer_calls_in_one_batch_are_both_refused_once` asserts the OUTCOME now; the version that shipped asserted `claims.count == 1` and passed over the whole defect.

### P.2 The brief was built before the decision to use it

Both exit sites awaited `_judge_results` (a session load) and `_corroborated_figures` (up to four KV reads) **before** `_judge` performed its `None` / `has_spent` / headroom checks. So every terminal exit of every turn paid for a brief that was usually thrown away — **including with the feature switched off, which is the shipped default**, contradicting §D.2's "no store reads on the common path" and §O's "byte-identical when disabled".

Worse, `_judge_results` had no exception guard and `run()` has no catch around `_run_loop_body`: a transient store error there aborted a turn whose answer was already in hand — the fail-open violation this design exists to avoid, arriving through the call site rather than through `review()`.

Fixed by splitting `_judge_would_run` (sync, free, holds all three skips) out of `_judge`, and passing the brief as an **async factory** so its I/O happens behind that gate and inside a `try` that degrades to `APPROVED`.

### P.3 Smaller corrections from the same pass

- **Corroboration read the RAW trail.** The rendered results were correctly scope-filtered; the `result_full_ref`s were not, so an entry `filter_trail` had dropped could still produce `figure_corroborated=True`. Approval-only in its failure direction, but the one D44 inconsistency in the slice. Now derived from the same `in_scope` list.
- **`asserts_quantity` re-implemented `reported_figures`'s scan** while its docstring claimed extraction made drift impossible. It now delegates.
- **`messages_for` sat outside `review`'s `try`**, making "never raises" true only by `json.dumps(default=str)` happening to tolerate every brief.

### P.4 A misbound `else` published "exhausted" on every clean tabled answer

Found by reading a live Phoenix trace, by nothing offline. At exit #2 the `else` emitting `loop_answer_judge_exhausted` was bound to `if not table_verdict.approved:` rather than to the inner `if await may_refuse(...)`, so an **approval** took it:

```
answer_judge [chain]          site=exit_table approved=True tokens=1566
loop_answer_judge_exhausted   site=exit_table violation=''      ← on an APPROVED answer
```

⚠ **The failure is silent in every direction that a test usually looks.** The verdict is right, the answer ships, no allowance is spent, no user sees anything. The only casualty is the metric — `..._exhausted` is supposed to mean *the judge was overruled and the answer shipped anyway*, and it was firing on every clean `answerWithTable` finish. Tuning this feature on that number would have read the healthiest possible population as the sickest.

The empty `violation` is the tell: `APPROVED` carries `violation=""`, so any exhausted event without a slug is this bug.

`test_an_approval_emits_no_refusal_or_exhausted_event_at_either_exit` asserts the WHOLE judge event set is empty on an approval, at both exits — verified to fail against the misbound version. **An assertion about one event could not have caught this**; only an assertion about the absence of all of them.

### P.5 Live measurement

Run against the real stack, 20 turns: 14 in-process (l2-mcp + ClickHouse, gpt-5.5) and 6 through the FULL surface — BFF :3000 → runtime :8000 → l2-mcp → ClickHouse, retrieval live on Neo4j, Couchbase sessions, Phoenix tracing. The full-stack runs are what exercised blueprint routing (`searchBlueprints` → `getBlueprint` → `runBlueprint`, verified badge) and multi-part `analysisState` turns, neither of which the in-process probe could reach.

| Population named in the question | Judge outcome |
|---|---|
| yes (6 turns, incl. one `exit_table`) | **0 rejections** |
| no (4 turns) | 3 rejections, all `unrecorded_assumption` |

Every rejection was the same latent agent behaviour: a `status != not-hired` filter applied and not disclosed. In each case the model responded to the nudge by calling `recordAssumptions` and the answer shipped with the assumption visible — the loop delivering exactly its intended value. **The 0-for-6 on clean answers is the number that matters**, because §L.6 makes the false positive the expensive direction.

One measured **false negative**: *"What is the average pay in Sales?"* was approved with the annual-vs-hourly choice undisclosed. That is the cheap direction and is left alone.

⚠ One measured **false positive** before a prompt fix: an answer reading *"…based on 2 active employees"* was faulted for not recording the population it had just stated. The judge prompt now says disclosure in the ANSWER TEXT satisfies the criterion exactly as `recordAssumptions` does — the test is whether the user can SEE the choice, not which channel carried it.
