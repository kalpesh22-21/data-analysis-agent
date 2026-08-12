# Layer 4 — evaluation

Implements [`docs/decisions/release-1/07-evaluation.md`](../../docs/decisions/release-1/07-evaluation.md).
Read §H (the review-corrections table) before "improving" anything here; several
of the reverted ideas look like improvements on a cold read.

## The one thing to understand before reading a green run

> **The scripted suite cannot fail on a bad prompt.**
>
> `ScriptedModelClient.send_turn` records `messages` and returns the next
> scripted result. **The prompt is never read.** Every tool name and every
> argument in `test_runtime_mechanics.py` comes from a YAML fixture, so that
> suite passes identically against today's prompt, a badly-rewritten one, or one
> that says *"never use blueprints"*.
>
> **Green in `test_runtime_mechanics.py` means the runtime works. It does not
> mean the agent routes.**

Deliverable 01 changes nothing but a string, so a suite that discards the string
tests none of it. The suite that can go red on a bad prompt is
`test_routing_live.py`, and it is the release gate.

## The three suites

| Suite | File | Model | Proves | Gate |
|---|---|---|---|---|
| **A1 — runtime mechanics** | `test_runtime_mechanics.py` | `ScriptedModelClient` | Given a routing decision, the runtime records, validates, enforces and terminates correctly | Per-commit CI |
| **A2 — routing decisions** | `test_routing_live.py` | Live model, real prompt, real pre-injected cards | The prompt actually routes — **can go red on a bad prompt** | **Release gate**, nightly / pre-ship |
| **A3 — answer grading** | *(not built)* | Live + judge | The numbers are right | **Deferred** — see below |

### A3 is deferred, deliberately

`docs/11-testing.md` §Layer 4 specifies golden Q&A with result-set grading and
LLM-as-judge, run as canaries. **No Q&A harness exists, and Release 1 does not
build one.** A1 and A2 grade the *route* — which blueprint ran, which intents
reached a terminal disposition, whether an authoritative result was re-derived.
Neither grades the *numbers*. A3 is that canary programme, and it needs a judge,
a golden set and a seeded warehouse none of which are in scope here.

The *golden-replay* half of Layer 4 already exists and predates this work:
`learning/promotion/replay.py` (D36), a structure oracle on the promotion path,
tested under `tests/learning/promotion/`. It is a blueprint-regression tool, not
a question runner.

## Running them

```bash
uv run pytest tests/eval -q                       # A1 + the metric tests; A2 skips
RUN_LIVE_EVAL=1 uv run pytest tests/eval/test_routing_live.py -q -s
```

`RUN_LIVE_EVAL` follows `tests/e2e/`'s `RUN_E2E=1` idiom: without it the whole A2
module skips, so an ordinary `uv run pytest` costs no live model calls. Tuning:

| Variable | Default | Meaning |
|---|---|---|
| `RUN_LIVE_EVAL` | unset | Required. A2 skips without it. |
| `LIVE_EVAL_RUNS` | `3` | Runs per case. |
| `LIVE_EVAL_MIN_PASS_RATE` | `0.67` | The gate. |

**A2 is reported as a pass-rate over N runs, not a boolean.** The model is
non-deterministic and a single red run is noise; a boolean gate would flap.

## Layout

```
tests/eval/
  conftest.py                 recording observer, fixture loader, app builder
  fixtures/routing/*.yaml      the ten A1 cases
  test_runtime_mechanics.py    A1
  test_routing_live.py         A2
  metrics.py                   the metrics + the scoped pending sweep
  test_metrics.py              the metric code is code
```

## What the harness stands up

`TestClient(create_app(...))` — the **shipped** composition. Only four edges are
doubled: the MCP transport, the session store, the embedder/vector index, and (in
A1 only) the model. The `AgentLoop`, the `ContextAssembler` **carrying the real
base system prompt**, the retrieval pipeline, the evidence validators and
finalization enforcement are all the real ones.

That required a runtime change, `create_app(extra_observers=…)` (07 §B.1). Without
it the harness would have to hand-assemble an `AgentLoop`, duplicating
`_build_agent_loop` — and because `ContextAssembler.__init__` takes
`base_system_prompt: str | None = None`, such a harness can run **with no system
prompt at all** and nothing signals it. For a suite whose purpose is testing the
prompt, that is disqualifying.

### Two gates that cost an afternoon if you rediscover them

* `searchBlueprints` / `getBlueprint` / `runBlueprint` are wired **only** when a
  retrieval pipeline is injected. Without one, every blueprint case gets
  `RETRIEVAL_TOOL_UNAVAILABLE` — a wiring failure that reads like a routing
  result.
* Blueprint recall pre-filters on `uses ⊆ column_scope`, so an **empty
  `column_scope` drops every card**. Empty means *allow-all* for query execution
  and *match nothing* for blueprint recall. They are not the same switch.

### Column scope is enforced in two different places

Case 7 narrows `column_scope`, and the narrowing does two different jobs:

1. **In-process** — recall's `uses ⊆ scope` pre-filter drops the out-of-scope
   card from the pre-injected block. Real, and asserted.
2. **At the MCP** — query-time column-scope enforcement lives in `clickhouse-api`,
   not in the runtime. So the `COLUMN_SCOPE_VIOLATION` denial is **scripted** as
   the `MCPToolError` the real server raises. There is no in-process path that
   turns a narrow scope into a denied `runQuery` on its own.

## Fixture format

```yaml
id: case-04-three-intents
question: "…"
ground_truth:
  multi_intent: true          # A2's denominator; also the false-positive check
  intent_count: 3
column_scope: [dbpcm_warehouse.employee.department_name, …]   # uses ⊆ scope
corpus: [bp-…, bp-…]          # ids from tests/fixtures/corpus/blueprints.yaml
embedding: {"<question text>": [1.0, 0.0]}                    # FakeEmbeddingClient map
mcp_tools:  [getTableSchema, runQuery]                        # FakeMCPClient catalogue
mcp_script:
  runQuery:                                                   # ordered, PER TOOL
    - {columns: […], rows: […], row_count: 2, truncated: false}
    - {error: {code: COLUMN_SCOPE_VIOLATION, message: "…"}}   # -> MCPToolError
requests:                     # optional; defaults to one turn with `question`
  - {kind: turn, message: "…"}
  - {kind: resume, answer: "…"}
model_script:                 # A1 only; A2 has none
  - tool_calls:
      - {name: updateAnalysisState, id: s1, args: {…}}
      - {name: runBlueprint, id: b1, serves_intent: i1, args: {…}}
expect:
  intents_terminal: 3
  re_derivation: false
```

Things that will bite:

* `FakeMCPClient` consumes `{tool_name: [responses…]}` **in order per tool name**
  and raises `AssertionError` when a tool is called more often than scripted. It
  also serves the tool **catalogue** from `mcp_tools` — a `getTableSchema` missing
  from that list is an unknown tool.
* A blueprint with a non-empty `result_grain` costs **two** `runQuery` responses:
  the node SQL, then the D56 grain probe (`{__bp_n, __bp_d}`, which must agree or
  `authoritative` is never earned). A blueprint with `result_grain: []` costs one.
* `serves_intent` is **load-bearing**. Re-derivation is intent-scoped (below).
* The corpus is loaded through the real `load_seed_fixtures` +
  `resolve_blueprint_references`, so a fixture cannot drift from the seeded corpus
  and `ref:` nodes are inlined exactly as the hydrator inlines them.

### Why case 1 is anchored on `bp-hires-projection`

Not on `bp-compare-employee-check-detail-two-periods`, which the first draft
named: that one has unresolved `ref:` nodes, two scratch materializations, three
required slots and a real `result_grain` gate, so `authoritative` would stay
`False` and 04's condition 4 would reject the evidence. `bp-hires-projection` has
genuinely complex wording, one node, no scratch, and `result_grain: []` — so the
grain teeth are vacuously skipped and the marker is earned legitimately.

## Re-derivation is INTENT-scoped, not turn-scoped

```
re_derivation(turn) = ∃ q ∈ runQuery, ∃ b ∈ runBlueprint:
    trail_entry(b).authoritative is True
    and q.serves_intent == b.serves_intent
    and q.ts > b.ts
```

The turn-scoped form — *"a `runQuery` after an authoritative `runBlueprint` in the
same turn"* — flags **case 3, which is legal**: `prompts.py` says the model MAY
run further queries for a **distinct** part of the question the blueprint did not
answer. Case 3 therefore asserts `re_derivation == False` *despite* a
post-blueprint `runQuery`, and asserts that the rejected turn-scoped predicate
would have flagged it — so the two can never quietly collapse into one answer.

In A2 there is no fixture to declare `serves_intent`, so it is reconstructed from
the `updateAnalysisState` **trail entries**, whose `args` carry the model's own
`{intent_id, evidence_tool_call_id}` bindings. Not from `loop_intent_completed`
(06 gives it `evidence_tool_name`, and a tool *name* cannot tell two `runQuery`
calls apart) and not from the final `AnalysisState` (latest-wins, so a rebound
evidence id — precisely the re-derivation shape — has already been overwritten).

## Metrics (`metrics.py`)

**E.1 — dropped-intent is a contract assertion, not a metric.** Scoped to
`status in {"done", "stopped_hard_ceiling"}`. The other two `TurnStatus` values
are `paused_ask_user` and `paused_budget_cap`, and 05 §F.1 names three ways a turn
legitimately ends with `pending` intents on the doc: an abandoned `askUser` pause,
an abandoned budget-cap pause, and a resume that loses a CAS race. **An unscoped
assertion fails against any real store** — case 10 exists partly to make that
concrete.

**E.2 — buckets derived from `REASON_CODES`, folded per intent.** Not a hard-coded
list (the draft named three of five). Not per event: one forced block emits *both*
`loop_analysis_state_transition` and `loop_intent_force_blocked`, so per-event
counting double-counts. `zero_row_block` is tracked against `zero_row_completion`
— the **ratio** is the health signal, because `REQUIRED_DATA_UNAVAILABLE` fires on
any correct query whose answer is legitimately empty.

Read `ENFORCEMENT_EXHAUSTED` as **"enforcement could not establish a
disposition"** — not agent failure, and not proof the intent was impossible. A
user who withdraws an ask mid-clarification lands there legitimately.

**E.3 — multi-intent detection rate is reported for A2 only.** In A1 it is
**definitionally 1.0 and carries no information**: the numerator fires only
because the fixture scripts `updateAnalysisState`. The definition and its
computation live in `metrics.py` and are tested against synthetic input in
`test_metrics.py`; the number is **not** printed beside the A1 results. Its
denominator needs ground truth and can never be self-reported — the failure being
measured *is* the model's own misjudgement. `multi_intent: false` fixtures buy the
false-positive check, and that half *is* asserted in A1.

## The contract these suites test

> **For every intent the model chooses to track, Release 1 guarantees a recorded,
> falsifiable terminal disposition. It does not guarantee that every user intent
> was detected, nor that a model-declared disposition is semantically true.**

Both clauses are load-bearing. Tracking is opt-in, and `completed` means "cited an
`ok` call of the right kind", not "answered". Case 8 asserts one of the known-open
holes rather than pretending it is closed: a zero-row result is valid evidence for
*both* completing and blocking an intent, so the runtime accepts both and the
ratio is what gets watched.
