# 07 — Layer-4 Evaluation

**Spec:** [§9](../release-1-routing-and-intent-coverage.md) · **Size:** M · **Depends on:** all · **Blocks:** release

## Current state (verified)

`docs/11-testing.md` §Layer 4 specifies golden Q&A with result-set grading and LLM-as-judge, run as canaries. **Nothing is built** — no fixture, no harness, no runner. Layers 1–3 exist (`tests/runtime/`, `tests/integration/`, `tests/e2e/`).

This is the first Layer-4 code. Keep it small: six cases, two metrics.

## Split the suite by what it needs

Spec §9's six cases are **routing** assertions — they check which tools ran, in what order, with what state transitions. They do **not** need answer grading or an LLM judge.

| Layer | What | Where | Model |
|---|---|---|---|
| **Routing conformance** | The six cases | `tests/eval/` | `ScriptedModelClient` — deterministic, CI-safe |
| **Answer grading** | Correctness of the numbers | deferred | Live model + judge |

Build the first. The second is the canary programme `11-testing.md` describes and is not gated on this release. Say so explicitly in the harness README so nobody reads six green routing tests as "answers are correct".

`ScriptedModelClient` (`runtime/model/scripted_client.py`) already exists and is used across the loop tests — a scripted turn sequence, no API key, no flake.

## Fixture shape

`tests/eval/fixtures/routing/*.yaml`, one per case:

```yaml
id: case-04-three-intents
question: "Give me headcount by department, average salary by department, and hires over the last 6 months."
ground_truth:
  multi_intent: true          # the detection-rate denominator
  intent_count: 3
expect:
  intents_tracked: 3
  all_terminal: true
  tools_used_at_least: [runBlueprint]
  tools_forbidden: []
model_script:                  # scripted tool calls per round
  - tool_calls: [{name: updateAnalysisState, args: {...}}, {name: runBlueprint, args: {...}}]
  - ...
```

`ground_truth.multi_intent` is the field the detection metric depends on — see below. Every fixture carries it, including single-intent ones (`false`), or the denominator is wrong.

## The six cases

| # | Case | Corpus anchor | Passes when |
|---|---|---|---|
| 1 | Complex wording, one blueprint | `bp-compare-employee-check-detail-two-periods` — linguistically complex, one blueprint | Blueprint runs; no `runQuery` for that intent |
| 2 | Multiple independent blueprint intents | `bp-active-headcount-by-department` + `bp-average-salary-by-department` | Both run; both intents terminal |
| 3 | Blueprint + ad-hoc residual | headcount blueprint + a grouping no blueprint covers | Blueprint used for its part; ad-hoc only for the residual |
| 4 | Three intents, one previously forgotten | three independent asks | All three reach a terminal status |
| 5 | Mixed metadata + analytical | "what fields do we track for employees, and headcount by department" | Both complete; metadata intent completes on `getTableSchema` evidence |
| 6 | Authoritative result not re-derived | any validated blueprint | **No** `runQuery` re-deriving the same intent after `authoritative=True` |

Use the committed corpus (`tests/fixtures/corpus/blueprints.yaml`, 11 blueprints) and catalog (`tests/fixtures/catalog_export.json`, 11 tables) so the harness needs no live neo4j or ClickHouse.

**Case 5 is the one that justifies admitting `getTableSchema` as completion evidence** (04 §A). If it fails, the trade in spec §6.1 is not paying for itself.

**Case 6 is the regression test for the oldest behaviour here** — the prompt has told the model not to re-derive since Session 24; nothing has ever asserted it.

## The two metrics

### Dropped-intent rate — an assertion, not a metric

§7 guarantees no intent ends `pending`, so this is mechanically zero. Measuring it reports the enforcement working, never the system working.

Ship it as a **Layer-1/2 assertion**: after every eval run, assert no intent in any turn's final state has `status == "pending"`. A non-zero value is a runtime bug, not a quality signal. It belongs in the harness's teardown, not its report.

### Blocked / unfulfilled-intent rate

Tracked intents reaching a terminal status other than `completed`, **broken down by `reason_code`**. The breakdown is the diagnosis:

- `NO_ACCESS` → an entitlement story
- `REQUIRED_DATA_UNAVAILABLE` → a data story
- `ENFORCEMENT_EXHAUSTED` → the agent failing to finish an ask it accepted; **this is the one that should trend to zero**

**Caveat to carry in the report** (spec finding 4, open with the Lead): `ENFORCEMENT_EXHAUSTED` currently also absorbs *user withdrawals* — the user says "skip that" to a clarification, the model has no declarable exit, and the intent lands here. Until that is resolved, the bucket is not purely agent failure and the report must say so rather than letting a reader infer otherwise.

### Multi-intent detection rate

> Of requests known to be multi-intent, what percentage created an `analysisState`?

**The denominator requires ground truth.** In the harness it is `ground_truth.multi_intent` in the fixture. In production it is an offline classifier over logged questions — never self-reported, because the failure being measured *is* the model's own misjudgement.

**It cannot be replaced by `loop_analysis_state_late_init_rejected`.** That event catches only the model that missed the decomposition, *later realised it*, and was refused. The failure this metric exists for is the model that never realises at all, never attempts initialization, and answers part of the request — invisible to late-init telemetry, and invisible to dropped-intent rate too, because there is no state to drop from. Late-init rejections are a useful **submetric under** this one.

## Reading the telemetry

The harness consumes the 06 events through a recording observer, the way `tests/runtime/observability/` fixtures already do. Two derivations belong here, not in the loop:

- **Re-derivation detection** (case 6): a `runQuery` dispatched after a `runBlueprint` with `authoritative=True` in the same turn.
- **Corpus-gap inference**: a turn with a successful `searchBlueprints` where an intent completes on `runQuery` evidence.

Both need to know which intent a call serves, which the harness knows from the fixture and the loop does not.

## Harness layout

```
tests/eval/
  README.md              what this proves, and what it does not
  conftest.py            recording observer, scripted-client builder, fixture loader
  fixtures/routing/*.yaml
  test_routing_cases.py  the six cases
  metrics.py             the two metrics + the pending assertion
```

Gate on an env flag if runtime is a concern — `tests/e2e/` uses `RUN_E2E=1` — but with a scripted client these should be fast enough for CI.

## Tests for the harness itself

The metric code is code. `tests/eval/test_metrics.py`: detection rate with a known mix of multi/single fixtures; blocked-rate breakdown by reason; the pending assertion fires on a synthetic state containing a `pending` intent.

## Done when

- [ ] Six routing cases green against the committed corpus and catalog, no live infra.
- [ ] Both metrics computed and reported; `ENFORCEMENT_EXHAUSTED` caveat in the report text.
- [ ] Pending-intent assertion runs in teardown and fails loudly.
- [ ] Every fixture carries `ground_truth.multi_intent`.
- [ ] README states plainly that this proves routing and coverage, **not** answer correctness.
- [ ] `docs/11-testing.md` §Layer 4 updated: routing conformance built, answer grading still outstanding.
