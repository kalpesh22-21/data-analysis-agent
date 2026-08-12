# Release 1 — Build Documents

Implementation guides for [release-1-routing-and-intent-coverage.md](../release-1-routing-and-intent-coverage.md) (the spec). The spec says *what* and *why*; these say *where* and *how*, verified against the tree at commit `bb53061` on `docs/release-1-routing-and-intent-coverage`.

**All seven have been through code review.** Each ends with a corrections table; read it before editing, because several of the reverted ideas look like improvements on a cold read.

| Doc | Deliverable | Spec § | Size |
|---|---|---|---|
| [01](01-prompt-rewrite.md) | Blueprint-first prompt rewrite | §3 | S |
| [02](02-blueprint-card-enrichment.md) | Enriched blueprint search cards | §4 | S |
| [03](03-analysis-state.md) | `analysisState` — tool, storage, context | §5 | L |
| [04](04-evidence-validators.md) | Completion + blocking validators | §6 | M |
| [05](05-finalization-enforcement.md) | Finalization enforcement | §7 | M |
| [06](06-telemetry.md) | Raw routing telemetry | §8 | S |
| [07](07-evaluation.md) | Evaluation — scripted + **live-model** suites | §9 | L |

## Build order

03 → 04 → 05 are one coupled workstream and should land together; the validators are meaningless without the state, and enforcement is meaningless without the validators. **06 is downstream of both** — its emit sites live inside them (its own header says `Depends on: 03, 05`). Only 01 and 02 are truly independent.

```
01 ─┐
02 ─┴──────────────────────────────→ 07
    03 ─→ 04 ─→ 05 ─→ 06 ─────────→ 07
```

07 additionally needs a runtime change of its own — the `create_app(extra_observers=…)` seam — so it is not purely a test deliverable.

## Findings from the wiring check

Five things the spec assumes that the code either already provides or does not. Each is expanded in its own doc.

1. **Card enrichment is a Cypher `RETURN` change, not N extra fetches.** `_RECALL_QUERY` (vector_index.py:158) selects only `id, intent, slots_summary, uses, score`, but `resolves_json` / `slots_json` / `result_grain_json` are already stored on the `:Blueprint` node. Adding them to the projection costs nothing per call. → [02](02-blueprint-card-enrichment.md)
2. **`status` is redundant on a search card.** Recall already filters `coalesce(node.status,'validated') = 'validated'`, so every recalled card is validated by construction. The spec lists it as an enrichment field; it carries no information there. → [02](02-blueprint-card-enrichment.md)
3. **`SessionStore` is a `Protocol` with two implementations.** The new fields touch the protocol, `CouchbaseSessionStore`, `InMemorySessionStore`, `SessionDoc.to_doc`/`from_doc`, and `test_couchbase_connect_gate.py` (which auto-parametrises over every public coroutine) — five places, not one. The store API must take a **merge callback**, not a precomputed value: `_mutate_with_cas_retry` re-runs its callback on conflict, so a value computed from a stale read would clobber the winner. → [03](03-analysis-state.md)
4. **The finalization nudge on exit #1 cannot be a standalone tool message.** `_assembled_to_canonical` only emits a `tool` message by expanding a trail entry into an `assistant(tool_calls) + tool` pair. Fabricating such a pair is itself an established pattern (discovery emulation does it), so the reason to avoid it is narrower: the nudge answers no call, and a fabricated pair would need re-splicing, deduping and pinning every rebuild for something that should live one round-trip. Use ephemeral `user`-role injection — same splice site and never-persisted posture as `discovery_canonical`, but **one round-trip, not one window**. → [05](05-finalization-enforcement.md)
5. **The §5.1 late-init boundary is order-sensitive within one response.** Dispatch walks `capped_tool_calls` in array order, so the spec's own round-1 pattern (`updateAnalysisState` + 3 × `runBlueprint`) self-rejects if the model emits the blueprints first. Needs an intra-batch ordering rule — and the partition must run on the **raw** list, since capping silently discards the overflow. `askUser` short-circuits the whole batch before dispatch, so state calls must also be committed before a pause. → [03](03-analysis-state.md)

## Findings from the code review of 03 and 05

A review pass over the two coupled documents found the four defects with the widest blast radius. (03's and 05's own corrections tables record ten and twelve respectively; these are the ones that would each have shipped a silently-broken implementation.) All are fixed; recorded here because they are the kind of thing a reader re-derives.

6. **`analysisState` had no turn gate.** A state persists after its turn ends, so without `state.turn_index == turn_index` a stale `pending` intent from an abandoned turn refuses an unrelated later turn, burns its nudge, and writes `ENFORCEMENT_EXHAUSTED` onto the old turn's record. The predicate `live_analysis_state` is now defined once in [03 §A.1](03-analysis-state.md) and used by 03, 04 and 05. The original justification for keeping the field across turns — "`session_history` reads it" — was simply false.
7. **The finalization nudge counter reset on every resume.** `_run_loop_body` re-enters on `askUser` and blueprint resumes while `window_count` stays frozen, so a counter local to that function makes forced re-rounds unbounded. It is now persisted on `SessionDoc`, keyed by window, and consumed **per round-trip** rather than per refused call — two `answerWithTable` calls in one batch would otherwise burn both chances without ever granting a re-round. → [05 §C](05-finalization-enforcement.md)
8. **A forced re-round was free.** Exit #1 sits before `guard.record_iteration`, so looping back without charging the window leaves the 60-second wall clock as the only backstop — and every resume restarts it. → [05 §C.3](05-finalization-enforcement.md)
9. **The cross-turn drop closed the rendered block but not the trail entry.** `_is_stale_assumptions_entry` is hard-coded to one tool name, while every `updateAnalysisState` call leaves a `TrailEntry` whose `args` carry the descriptions with `frozenset()` provenance — kept under any `column_scope`, in every later turn. The predicate is generalised to a tool set. → [03 §D.1](03-analysis-state.md)

## Findings from the review of 01, 02, 04, 06 and 07

A second pass over the remaining five found four more of the same class.

10. **Emitting an event does not publish its payload.** `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` (`observability/tracing.py:598`) is a strict attribute allowlist — deliberately not a type filter, because a bare `isinstance` check once leaked `loop_paused_ask_user`'s `question`. Of every payload key doc 06 proposed, **only `window` is on it**; the rest would reach Phoenix as correctly-named spans carrying nothing. Different mechanism from `OTLP_DROP_SPAN_NAMES`, which filters names one layer later. → [06](06-telemetry.md)
11. **Enrichment changes the search card's D44 posture.** `searchBlueprints` carries safe-empty `frozenset()` provenance *because* its cards hold no column identifiers; `resolves` and `result_grain` are column names, so enriched cards would be kept in replay forever under any scope. Resolved by setting the entry's provenance to the union of the returned cards' `uses`. → [02](02-blueprint-card-enrichment.md)
12. **The evidence trail cannot be handed in from the loop.** `_run_loop_body`'s only trail load sits *above* the round-trip loop and is reduced to signatures — a snapshot from there holds nothing from the current window, so every citation would fail as "unknown id" while looking correctly wired. The tool reads the trail itself. → [04](04-evidence-validators.md), [03 §C.1](03-analysis-state.md)
13. **A scripted model cannot prove routing.** `ScriptedModelClient.send_turn` never reads `messages`, so a suite built on it passes identically against a hostile prompt — and deliverable 01 changes nothing but a string. Resolved by splitting into a scripted runtime-mechanics suite (CI) and a **live-model routing suite that gates the release**. → [07](07-evaluation.md)

## Findings from the code review of the implementation

Four more, found reviewing the shipped code rather than the docs. The first is finding 9's class for the third time.

14. **The finalization refusal was the missed third carrier of finding 9's leak.** It is persisted as an `answerWithTable` entry — `denial_detail` naming every pending intent, `args` holding the refused draft prose, `frozenset()` provenance — so it replayed in every later turn of the session under any since-narrowed scope, telling a later turn to resolve **dead intent ids** with `updateAnalysisState`. `_is_stale_model_text_entry` now matches on error code as well as tool name. **05 §I's recorded rationale was itself inverted** ("the status gate is belt-and-braces; provenance is binding"): the status-gated current-turn exemption is provenance-blind, so `frozenset()` was load-bearing only in the later-turn contexts where the entry should not survive. → [03 §D.1](03-analysis-state.md), [05 §B.1](05-finalization-enforcement.md)
15. **`finalization_blocks` was keyed by budget window alone**, while `window_count` restarts at 1 on every external turn and the map is never cleared — so the forced re-round was silently dead from a session's second block-spending turn onward, writing `ENFORCEMENT_EXHAUSTED` for intents never asked twice and inflating 07's headline rate. Keyed by `(turn_index, window)`. Finding 6's class, one field over. → [05 §C.1](05-finalization-enforcement.md)
16. **An `askUser` past the per-iteration cap stopped pausing the turn** — a Release-1 regression: the scan moved onto the *capped* list when the state partition was introduced, and capping silently discards the overflow. The model's clarifying question was swallowed and the turn finished `done`. → [03 §E.2](03-analysis-state.md)
17. **Two unbounded/uncontained paths.** `claim_finalization_block` could raise out of `_run_loop_body` with the answer in hand (now caught, reported on `loop_finalization_block_claim_failed`, treated as "no re-round"), and surplus `updateAnalysisState` calls each cost a store write with no bound on the list (now bounded at two rejections, then dropped). → [05 §B.1](05-finalization-enforcement.md), [03 §E.2](03-analysis-state.md)

**One finding escalated past the docs, and the Lead has now ruled on it.** Evidence-backed blocking stops the model *asserting* an unfalsifiable reason; it does not stop it *producing* a falsifiable one. `SELECT … WHERE 1=0` yields `REQUIRED_DATA_UNAVAILABLE` — and `NO_ACCESS` costs **one metadata call**: `getTableSchema(<scratch_db>, <anything>)` fails closed with `SCRATCH_SESSION_VIOLATION` without touching data or locking the late-init boundary. Separately, zero rows is *also* how a correct query answers "nobody", so the block predicate fires on honest work.

## Findings from the first live run

18. **`analysisState` was completely non-functional in the real runtime, with the whole suite green.** `SessionStore` has **four** implementations, not two: besides `memory_store` and `couchbase_store`, both launchers wrap the real store in a hand-written `_LazyCouchbaseSessionStore` proxy that re-declares and forwards every method (`acouchbase` connects eagerly and needs a running loop; the launchers build their app at import). Release 1 added `apply_analysis_state` and `claim_finalization_block` to the Protocol; [03 §B](03-analysis-state.md#b-persistence) enumerated *five files* and *two implementations*, so neither proxy was touched. Every `updateAnalysisState` call on the real server raised `AttributeError` → `RUNTIME_TOOL_INTERNAL_ERROR`, and nothing in `tests/` could see it because nothing imports `scripts/`. **This is the same class as findings 6, 9 and 15**: a guard (here, a change list) derived from a hand-enumerated set rather than from what the contract requires. `read_full_result` had already been lost the same way once. Fixed by adding the methods *and* by `tests/runtime/test_launcher_session_store_proxies.py`, which derives the required surface from the Protocol and the proxy population from the source tree, so the next Protocol addition cannot be forgotten in either place. → [03 §B](03-analysis-state.md#b-persistence)

## The contract

> **For every intent the model chooses to track, Release 1 guarantees a recorded, falsifiable terminal disposition. It does not guarantee that every user intent was detected, nor that a model-declared disposition is semantically true.**

Approved by the Lead, 2026-08-11. Two separate problems with separate mechanisms: `analysisState` solves **state loss after detection**; the detection-rate metric (07 §E.3) measures whether the model created the state **in the first place**.

The same reasoning fixes how `ENFORCEMENT_EXHAUSTED` is described. It means **"enforcement could not establish a disposition"** — not that the system proved the intent impossible. Evidence that looks mechanical can be semantically ambiguous, so a code claiming proof would overstate what the runtime knows. Word it that way in `denial_mapping.py`, telemetry, and 07's report.

## Conventions these docs assume

**Test layout mirrors the module tree.** `tests/runtime/<package>/test_<module>.py`, with adversarial cases in a sibling `*_adversarial.py` or `*_qa.py` file — see `tests/runtime/loop/` for the established pattern.

**Degrade-not-fail, never silently.** Every new failure path emits an observer event and logs server-side; nothing reaches the model as `str(exc)`.

**The model-facing error channel is `denial_detail`, not `user_message`.** `context/budget.py::_render_entry` builds it from the persisted entry — `denial_detail` if set, else `classify_denial(error_code)` — never from `ToolResult.user_message`, which has no `TrailEntry` field. A specific message set only on `user_message` is silently dropped. Every new error code needs a `denial_mapping.py` entry *and*, where the text must name specifics, a `denial_detail`.

**Telemetry is shape-only** (D25): counts, enums, tool names. Never question text, SQL, cell values, or resolved code strings.

**Provenance for tools that read no warehouse data is `frozenset()`, not `None`.** `None` means *undetermined* and is dropped fail-closed from replay — see the long note in `composite/record_assumptions.py` for what that cost when it was got wrong.

## Done criteria for the release

- [ ] All seven deliverables merged, `uv run pytest` green, `uv run ruff check` clean.
- [x] `docs/02-tools-and-api.md` corrected: 12 → **15** tools, with `recordAssumptions`, `answerWithTable` and `updateAnalysisState` documented and `answerWithTable`'s terminal-exit behaviour described.
- [x] `docs/04-blueprints.md` F2 note corrected (table intermediates are no longer rejected pre-dispatch when a scratch client is wired).
- [ ] `create_app(extra_observers=…)` seam added so the harness drives the shipped composition.
- [ ] Scripted suite green per-commit; **live-model routing suite built and gating the release**, reported as a pass-rate.
- [ ] `tests/eval/README.md` states plainly that the scripted suite cannot fail on a bad prompt.
- [ ] No intent ends `pending` **on any turn that reaches a terminal outcome** — asserted at Layer 1/2, not measured. Turns abandoned at a pause, or whose resume loses a CAS race, legitimately leave `pending` state behind and must be excluded, or the assertion fails against any real store.
- [x] ~~Narrow the model-declarable block codes to two~~ — **approved** 2026-08-11 (04 §B.1).
- [x] ~~Require block evidence distinct per intent~~ — **approved** 2026-08-11; completion reuse stays allowed (04 §B.3).
- [x] ~~The guarantee sentence~~ — **approved** and now the contract above (04 §B.5, spec §6).
- [x] ~~Force `USER_DECLINED_CLARIFICATION` after a consumed `askUser` pause~~ — **rejected** 2026-08-11. `ENFORCEMENT_EXHAUSTED`, read as *"could not establish a disposition"*, already describes a user withdrawal accurately. `RUNTIME_REASON_CODES` final at three, `MODEL_REASON_CODES` at two.

**No open design questions remain.** Everything left is implementation.
- [ ] `01a-prompt-draft.md` written and reviewed — 01 is the only deliverable whose artifact is prose, and the text does not exist yet.
