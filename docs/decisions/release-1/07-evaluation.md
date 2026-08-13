# 07 — Evaluation

**Spec:** [§9](../release-1-routing-and-intent-coverage.md) · **Size:** L · **Depends on:** all · **Blocks:** release
**Revised** 2026-08-11 after review — see [§H](#h-review-corrections). The scripted-only design was replaced with a three-suite split after review showed it could not fail on a bad prompt.

## Current state (verified)

`docs/11-testing.md` §Layer 4 specifies golden Q&A with result-set grading and LLM-as-judge, run as canaries. **No Q&A harness exists.** The *golden-replay* half of Layer 4 (D36) is built — `learning/promotion/replay.py`, a structure oracle on the promotion path, tested under `tests/learning/promotion/` — but it is a blueprint-regression tool, not a question runner, and is out of scope here.

---

## A. Three suites, split by what varies

The first draft put all six cases on `ScriptedModelClient` and called them "routing conformance". They were not.

`ScriptedModelClient.send_turn` (`model/scripted_client.py:36-47`) records `messages` and then returns `self._script[self._cursor]` — **the prompt is never read**. Every tool name and argument comes from the fixture. So "case 1 routes to the blueprint and writes no fresh SQL" restates the YAML, and passes identically against today's prompt, a badly-rewritten one, or one that says *"never use blueprints"*. Since deliverable 01 changes nothing but a string, a suite that discards the string tests none of it — and spec §3's stated trigger (*"Layer-4 cases 1–4 are the test of whether per-intent search holds"*) could never fire.

The real seam is **model choice vs. runtime mechanics**:

| Suite | Model | Proves | Gate |
|---|---|---|---|
| **A1 — Runtime mechanics** | `ScriptedModelClient` | Given a routing decision, the runtime records, validates, enforces and terminates correctly | Per-commit CI |
| **A2 — Routing decisions** | Live model, real prompt, real pre-injected cards | The prompt actually routes — **can go red on a bad prompt** | **Release gate**, nightly / pre-ship |
| **A3 — Answer grading** | Live + judge | The numbers are right | Deferred (the `11-testing.md` canary programme) |

A2 is what P1 buys evidence with. Report it as a **pass-rate over N runs**, not a boolean — the model is non-deterministic and a single red run is noise.

`tests/eval/README.md` must say plainly: the scripted suite **cannot fail on a bad prompt**, and green there means the runtime works, not that the agent routes.

---

## B. Harness wiring

### B.1 `create_app` needs an observer seam — this is a runtime change

The harness must read the §06 telemetry, and there is no way in.

- `app.py:736` builds `combine_observers(emitter.observe, _tracing_observer)` **inside** the request handler. No parameter accepts a recorder.
- SSE is not a fallback: `observability/progress.py:126` drops any event `to_progress_event` does not recognise, which is most of §06's set.
- There is no "recording observer fixture" today; the idiom is an inline `lambda e, p: events.append((e, p))` passed to a **directly-constructed** component (`tests/runtime/retrieval/test_observability.py:55`), never through `create_app`.

Without the seam the harness must hand-assemble `AgentLoop` (as `tests/runtime/loop/test_agent_loop.py:62` does), duplicating ~120 lines of `_build_agent_loop` — so Layer 4 would test a runtime that is not the shipped one, and drift silently. **Worse: `ContextAssembler.__init__` takes `base_system_prompt: str | None = None` and Layer-1 tests run promptless by default, so a hand-assembled harness can run with no system prompt at all and nothing signals it.** For a suite whose purpose is testing the prompt, that is disqualifying.

Add the parameter:

```python
def create_app(*, ..., extra_observers: Sequence[ToolObserver] = ()) -> FastAPI:
    ...
    agent_loop = _build_agent_loop(
        combine_observers(emitter.observe, _tracing_observer, *extra_observers)
    )   # app.py:737 and :775
```

### B.2 What A1 stands up

`TestClient(create_app(...))` with `FakeMCPClient`, `InMemorySessionStore`, an injected `RetrievalPipeline(FakeEmbeddingClient, FakeVectorIndex(corpus))`, `FakeScratchClient`, and a recording observer — the pattern at `tests/runtime/test_app.py:366`.

Two gates to know about:
- **`searchBlueprints`, `getBlueprint` and `runBlueprint` are wired only `if active_retrieval is not None`** (`app.py:584-641`). Without an injected pipeline every blueprint case gets `RETRIEVAL_TOOL_UNAVAILABLE`.
- Load the corpus through `retrieval/corpus_loader.py::load_seed_fixtures` so fixtures cannot drift from the seeded corpus — and so `ref:` nodes get inlined (see C.1).

### B.3 What A2 stands up

The **shipped** composition: real prompt, real `create_app`, live model. Warehouse and corpus can still be the committed fixtures — what must be real is the model and the prompt.

---

## C. Fixture format

The first draft specified only `model_script`. Four more things are required or the first implementer finds them by trial.

`FakeMCPClient` (`mcp/fake_client.py:47`) is scripted as `{tool_name: [responses…]}`, consumed **in order per tool name**, and raises `AssertionError` when a tool is called more often than scripted. It also serves the tool catalogue from its `tools=[MCPToolSpec…]` list — a `getTableSchema` missing from that list is an unknown tool.

```yaml
id: case-04-three-intents
question: "Give me headcount by department, average salary by department, and hires over the last 6 months."
ground_truth:
  multi_intent: true          # A2's denominator; also the false-positive check (see E.3)
  intent_count: 3
column_scope: [dbpcm_warehouse.employee.department_name, ...]   # recall pre-filters on uses ⊆ scope
corpus: [bp-active-headcount-by-department, bp-average-salary-by-department, bp-hires-per-month]
embedding: {"<question text>": [1.0, 0.0]}                      # FakeEmbeddingClient map
mcp_tools:  [getTableSchema, runQuery]                          # FakeMCPClient catalogue
mcp_script:
  runQuery: [{columns: [...], rows: [...]}]                     # ordered, per tool
model_script:                                                   # A1 only; A2 has none
  - tool_calls:
      - {name: updateAnalysisState, id: s1, args: {...}}
      - {name: runBlueprint, id: b1, serves_intent: i1, args: {...}}
expect:
  intents_terminal: 3
  re_derivation: false
```

**08 added a `tables:` array to `answerWithTable`'s arguments and the harness needed NO change** — `_parse_call` (`conftest.py`) validates only the four top-level keys `name`/`id`/`args`/`serves_intent` and passes `args` through opaquely, and `RoutingCase.expect` is a free-form dict. So a multi-table case is a fixture, not a loader change:

```yaml
      - name: answerWithTable
        id: a1
        args:
          answer: "…"
          sql: ""                # the placeholder the live model actually emits
          blueprint_id: ""       # (03 §C.3.1 — it cannot omit a declared key)
          tables:
            - {sql: "", blueprint_id: bp-…, caption: "…"}
            - {sql: "SELECT …", blueprint_id: "", caption: "…"}
expect:
  answer_tables: 2
  envelope_verification_passed: null   # the AND roll-up; `null` is a real expectation
```

**No existing assertion broke.** The fixtures ENCODED a single-table answer but nothing read it — `answer_sql` appears in no `tests/eval/*.py` — so cases 2 and 4 stay on the top-level `sql:` shorthand deliberately: it is the 6/6 path, and it now has a regression test by accident.

**`serves_intent` is load-bearing** — see C.2. **It is now a REAL runtime argument** (README finding 20), not a fixture-only label: the harness passes it into the scripted call's `arguments`, the runtime strips it before dispatch and persists it on `TrailEntry.serves_intent`, and §C.2's mapping is derived from that field unioned with the explicit `evidence_tool_call_id` bindings — so A2 keeps working now that a live model closes intents by tag and sends no evidence id at all. An empty `column_scope` drops every card, since blueprint recall pre-filters on `uses ⊆ scope`.

### C.1 Case 1's anchor was wrong

The draft named `bp-compare-employee-check-detail-two-periods`. Verified at `tests/fixtures/corpus/blueprints.yaml:339` — three nodes, of which two are unresolved `ref:` entries inlined only by `corpus_loader.resolve_blueprint_references` at load, emitting `table` outputs consumed via `scratch.detail_a`/`scratch.detail_b` (the D93 materialize-and-join path, needing a scratch client or the executor degrades to UNSUPPORTED), three required slots, and `result_grain: [register_type, type_code]` — so the scripted rows must satisfy the D56 grain gate or `authoritative` stays `False` and 04's condition 4 rejects the evidence. Cases 1 and 6 would both fail for fixture-authoring reasons.

**Use `bp-hires-projection`** (`blueprints.yaml:277`): genuinely complex wording — *"Projected number of hires over the next N months at the current hiring pace"* — single node, no scratch, and `result_grain: []` so the grain teeth are vacuously skipped and `authoritative` is earned legitimately.

### C.2 Re-derivation is intent-scoped, not turn-scoped

The draft's predicate — *"a `runQuery` after a `runBlueprint` with `authoritative=True` in the same turn"* — flags **case 3**, which is legal. `prompts.py:104` says explicitly: *"You MAY run further queries only for a DISTINCT part of the user's question that the blueprint did not answer."* Case 3 is exactly that shape.

```
re_derivation(turn) =
  ∃ q ∈ runQuery calls, ∃ b ∈ runBlueprint calls:
      trail_entry(b).authoritative is True
      and q.serves_intent == b.serves_intent
      and q.ts > b.ts
```

Case 3's assertion is then `re_derivation == False` **despite** a post-blueprint `runQuery` — the interesting half, and unwritten in the draft.

In A2 there is no fixture to declare `serves_intent`, so derive it from the persisted trail (`metrics.serves_intent_from_trail`): `TrailEntry.serves_intent` — the call-time tag, and the only source for a tag-closed intent — unioned with the `{intent_id, evidence_tool_call_id}` bindings in the `updateAnalysisState` entries' own `args`. Not from `loop_intent_completed`, which carries a tool NAME and cannot tell two `runQuery` calls apart.

---

## D. Cases

**A1 (scripted, per-commit).** Assertions must be things a script cannot fake — runtime behaviour, not tool choice.

| # | Case | Asserts |
|---|---|---|
| 1 | Pre-injection | `client.calls[0].messages` carries the three cards **with 02's enrichment**, before round 1 |
| 2 | Two blueprints complete | Both intents terminal; evidence accepted; finalization allowed |
| 3 | Blueprint + ad-hoc residual | `re_derivation == False` despite a post-blueprint `runQuery` |
| 4 | Three intents | All three terminal; none silently dropped |
| 5 | Metadata + analytical | Metadata intent completes on `getTableSchema`; **script only one schema fetch per table** — a second identical call yields an `IDEMPOTENT_READ_ALREADY_SERVED` entry that 04 condition 5 rejects |
| 6 | Pending intent blocks finalization | `answerWithTable` refused, one forced re-round, then `ENFORCEMENT_EXHAUSTED` |
| 7 | `NO_ACCESS` block | Narrow `column_scope` so a `runQuery` is denied; block accepted; bucket counted |
| 8 | `REQUIRED_DATA_UNAVAILABLE` block | Scripted zero-row result; block accepted |
| 9 | **Multi-intent across an `askUser` pause** | Round 1 emits `updateAnalysisState` + `askUser` (03 §E.1's exact shape — state must commit **before** the pause); resume; all intents terminal; **the block counter is not reset by the resume** (05 §C.1) |
| 10 | **Abandoned pause, then a new turn** | Turn N pauses with pending intents, never resumed; turn N+1 is single-intent and finalizes; turn N's intents untouched and the teardown sweep does not fire on it (05 §A) |
| 11 | **Two blueprints, two tables** (08) | `len(answer_tables) == 2`, each with its OWN `blueprint_use`, both badges EARNED by `blueprint_gate`, and the AND roll-up green |
| 12 | **Mixed verification** (08) | One blueprint table + one `sql=` table; per-table blocks are `{passed: true}` and `null`, and the envelope roll-up is **`null`** — the honest-reporting assertion, and the one an OR roll-up would fail |
| 13 | **Narrowed-scope reload** (08) | Re-reads `/session/history` under a scope excluding a column that appears ONLY on a never-executed designated `sql=`: that table alone drops, `history_answer_table_scope_dropped` fires. Then narrows a column the turn actually READ and asserts the turn-wide answer gate's dominance explicitly (08 §D.3), so a later reader does not mistake the limit for a bug |

Cases 7–10 did not exist in the draft; 11–13 arrived with 08. **Cases 11–13 cannot measure the thing 08 exists to fix**: `answer_tables` populating in A1 is definitionally true, because the fixture writes the array. What they prove is that the runtime carries every designated table honestly once the model sends them. 7 and 8 are the only end-to-end coverage of 04's block validator; 9 and 10 are the only tests of P2's actual shape — an intent lost *between rounds* — and of the scoping in F.

**A2 (live, release gate).** The routing decisions, N runs each, reported as a rate:

| # | Question shape | Passes when |
|---|---|---|
| L1 | Complex wording, one blueprint (`bp-hires-projection`) | Routes to the blueprint; no fresh SQL for that intent |
| L2 | Two independent blueprint intents | Each routed to its own blueprint |
| L3 | Blueprint + ad-hoc residual | Blueprint for its part; ad-hoc only for the residual |
| L4 | Three intents | `analysisState` initialized; all three terminal |
| L5 | Mixed metadata + analytical | Both complete |
| L6 | Authoritative result | No `runQuery` re-deriving the same intent |
| L7 | **Three deliverables ⇒ more than one table** (08) | The answer designates a table per part instead of describing the rest in prose |

**L7 is the only thing that can prove 08.** The headline number to re-measure is **1/9** — `answerWithTable` succeeded on 6/6 single-deliverable turns and 1/9 multi-intent ones — not "does `answer_tables` populate". It reuses L4's question verbatim on purpose: L4 asks whether the three intents were TRACKED and reached a terminal disposition, L7 whether the three RESULTS reached the user as grids. The same turn failed the second while passing the first, eight times out of nine. The predicate is *more than one table*, not *exactly three*: the scalar clause survives (a part answered by a single number belongs in the prose), and one query legitimately covering two parts is one table.

L1–L4 are spec §3's stated test of whether per-intent `searchBlueprints` holds. If blueprint miss-rate is high here, the Phase-2 fix is firing retrieval per declared intent.

---

## E. Metrics

### E.1 Dropped-intent rate — a contract assertion, not a metric

§7 guarantees no intent ends `pending` **on a turn that reaches a terminal outcome**, so on those turns the rate is mechanically zero and measuring it reports the enforcement, never the system.

The scoping is required, not pedantry. Three paths legitimately leave `pending` state: an abandoned `askUser` pause, an abandoned budget-cap pause, and a resume that loses a CAS race (`resume()` raises before reaching any escape). Those are *non-terminated* turns. An unscoped assertion fails against any real store — 05 §I records that the unscoped form nearly shipped.

Enforcement is *proven* at Layer 1 (05 §H). The eval teardown is a cheap redundant sweep over whatever turns the cases produced, scoped to `status in {"done", "stopped_hard_ceiling"}` (the real `TurnStatus` values; the others are `paused_ask_user`, `paused_budget_cap`).

### E.2 Blocked / unfulfilled-intent rate

Tracked intents reaching a terminal status other than `completed`, **bucketed by `reason_code`**.

**Derive the buckets from `REASON_CODES`** (03 §A.3), not a hard-coded list — the draft named three of five, omitting `BUDGET_EXHAUSTED` and `USER_STOPPED`. The enum is final for Release 1 at five, but deriving costs nothing and means a later addition cannot silently go uncounted.

**Fold per `intent_id` final state, not per event.** A single forced block emits *both* `loop_analysis_state_transition` and `loop_intent_force_blocked` (06), so counting events double-counts.

Reading: `NO_ACCESS` is an entitlement story, `REQUIRED_DATA_UNAVAILABLE` a data story, and `ENFORCEMENT_EXHAUSTED` means **enforcement could not establish a disposition** — not that the agent failed, and not that the intent was proved impossible. It should trend down, but it measures *undetermined* outcomes, and a user withdrawing an ask mid-clarification lands there legitimately.

**Reading `BUDGET_EXHAUSTED` versus `ENFORCEMENT_EXHAUSTED` (changed 2026-08-12).** These two are the buckets most likely to be misread, because only one of them is a capacity story:

| Bucket | What actually ran out | What to do about it |
|---|---|---|
| `BUDGET_EXHAUSTED` | Capacity. The turn burned **every window it was allowed** and stopped at the hard ceiling. | A ceiling is a real candidate: `max_budget_windows`, the token budget, the wall clock. |
| `ENFORCEMENT_EXHAUSTED` | **Enforcement.** Either the forced re-round was spent with intents still pending, or the budget cap arrived *during a refused round*. | Raising a ceiling is usually the wrong move. Look at the refusal loop: what the model kept failing to close, and why. |

Until 2026-08-12 the cap-during-a-refused-round path wrote `BUDGET_EXHAUSTED`, which put a **pure enforcement failure into the capacity bucket** — [05 §F.0](05-finalization-enforcement.md#f0-the-fourth-path-is-enforcement_exhausted-reversed-2026-08-12-on-measurement) has the measurement: answer computed at 25s, turn capped at 61.6s on the **wall clock**, tokens moving +385 across the final three rounds, all 36 remaining seconds spent on two rejected `updateAnalysisState` calls and one refused `answerWithTable`. More budget would have changed nothing, and the bucket said otherwise.

**So `ENFORCEMENT_EXHAUSTED` will read higher than it used to, and `BUDGET_EXHAUSTED` lower — that is the correction landing, not a regression.** Any trend line crossing that date must be read as two series, not one.

**To split the enforcement bucket by whether the cap was also hit**, read `loop_intent_force_blocked.budget_cap_reached` (06) — present as `True` only on the refused-round path. It is the right way to see budget pressure on these intents; the reason code is not, and no longer pretends to be.

**One caveat the report must carry.** `REQUIRED_DATA_UNAVAILABLE` fires on any correct query whose answer is legitimately empty (04 §B.4), so track `zero_row_block` against `zero_row_completion` — the ratio is the health signal, not the count.

### E.3 Multi-intent detection rate

> Of requests known to be multi-intent, what percentage created an `analysisState`?

**In A1 this is definitionally 1.0 and carries no information** — the numerator fires only because the fixture scripts `updateAnalysisState`. Ship the *definition* plus a computation tested against synthetic input (`test_metrics.py`); do not print it beside the A1 results as though measured.

**Its first real reading is A2**, where the model decides whether to initialize.

**The denominator needs ground truth** — `ground_truth.multi_intent` in the harness, an offline classifier in production. Never self-reported: the failure being measured *is* the model's own misjudgement. And it cannot be replaced by `loop_analysis_state_late_init_rejected`, which catches only the model that missed the decomposition, *later realised*, and was refused. The failure this metric exists for is the model that never realises at all.

`ground_truth.multi_intent: false` on single-intent fixtures buys a **false-positive** check — an `analysisState` initialized for a single-intent request burns 03 §E's boundary and adds CAS writes for nothing.

---

## F. Harness layout

```
tests/eval/
  __init__.py            required — every test package here has one
  README.md              what each suite proves, and what it cannot
  conftest.py            recording observer, fixture loader, app builder
  fixtures/routing/*.yaml
  test_runtime_mechanics.py    A1 — scripted, per-commit
  test_routing_live.py         A2 — live model, RUN_LIVE_EVAL=1
  metrics.py             the metrics + the scoped pending sweep
  test_metrics.py        the metric code is code
```

A2 gates on an env flag, following `tests/e2e/`'s `RUN_E2E=1`.

---

## G. Done when

- [ ] `create_app(extra_observers=…)` seam added; harness drives the **shipped** composition.
- [ ] A1 green per-commit; its assertions are runtime behaviour, not fixture echo.
- [ ] A2 built, gating the release, reported as a pass-rate over N runs.
- [ ] `tests/eval/README.md` states the scripted suite cannot fail on a bad prompt.
- [ ] Re-derivation predicate is intent-scoped; case 3 passes *with* a post-blueprint `runQuery`.
- [ ] Cases 7–10 present (two block reasons, one pause, one abandoned pause).
- [x] Cases 11–13 present (two tables, mixed verification, narrowed-scope reload) and A2 gained **L7**; the harness needed no change (08 §H).
- [ ] Buckets derived from `REASON_CODES`; folded per intent, not per event.
- [ ] Detection rate reported only for A2; A1's value documented as definitionally 1.0.
- [ ] Pending sweep scoped to terminal outcomes.
- [ ] `docs/11-testing.md` §Layer 4 updated: routing suites built, answer grading outstanding, golden replay noted as pre-existing.

---

## H. Review corrections

| Was | Now | Why |
|---|---|---|
| Six cases, all scripted, called "routing conformance" | **§A three suites**; live A2 gates the release | `send_turn` never reads `messages`; the scripted assertions restated the fixture and would pass against a hostile prompt |
| Harness "needs no live infra", observer "the way existing fixtures do" | **§B.1 `create_app` observer seam** | No seam exists; the alternative is hand-assembling `AgentLoop`, which also inherits the promptless Layer-1 default |
| Re-derivation = `runQuery` after authoritative `runBlueprint` in the turn | **§C.2 intent-scoped, `serves_intent`** | The turn-scoped form flags case 3, which is legal per `prompts.py:104` |
| Case 1 anchored on the two-period comparison | **§C.1 `bp-hires-projection`** | Unresolved `ref:` nodes, scratch materialization, three slots and a real grain gate — heavy scaffolding for a vacuous assertion |
| Fixture = `model_script` only | **§C full shape** | `FakeMCPClient` needs a catalogue and ordered per-tool queues; recall needs `column_scope`; `FakeEmbeddingClient` needs a vector map |
| Six cases, all expecting completion | **§D cases 7–10** | No case blocked, paused, or crossed a resume — so 04's block validator and P2's actual shape were untested |
| Three reason buckets | **§E.2 derived from `REASON_CODES`**, folded per intent | Omitted `BUDGET_EXHAUSTED`/`USER_STOPPED`; per-event counting double-counts forced blocks |
| Detection rate reported from the harness | **§E.3 A2 only** | Definitionally 1.0 over n=4 in A1; would read as a green quality signal |
| "§7 guarantees no intent ends `pending`" | **§E.1 scoped in the lead sentence** | The unscoped restatement is the exact claim 05 §I exists to correct |
| "Nothing is built" | **§Current state** | `learning/promotion/replay.py` is Layer 4's golden-replay half (D36) |
