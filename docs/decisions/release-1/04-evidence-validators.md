# 04 — Evidence Validators

**Spec:** [§6](../release-1-routing-and-intent-coverage.md) · **Size:** M · **Depends on:** 03 · **Blocks:** 05, 07

**Two validators.** The spec says reusing one for both is the most likely implementation error here, and the reason is concrete: the completion validator requires `status == "ok"`, while `NO_ACCESS` evidence is a **denied** call. One validator would reject exactly the evidence blocking needs.

Put both in `composite/analysis_state.py` beside the tool, as pure functions over `list[TrailEntry]` — no I/O, so they unit-test without a store.

Both take `turn_index` and compare it against `entry.turn_index`. Same scoping rule as [03 §A.1](03-analysis-state.md)'s `live_analysis_state`: a validator must never accept evidence from a turn other than the one being enforced.

```python
def validate_completion_evidence(
    tool_call_id: str, trail: Sequence[TrailEntry], turn_index: int
) -> str | None:  # None = valid, else a reason string for denial_detail

def validate_block_evidence(
    tool_call_id: str, reason_code: str, trail: Sequence[TrailEntry], turn_index: int
) -> str | None:
```

Returning a reason string rather than a bool is what lets `denial_detail` tell the model *why* — the generic `classify_denial` text cannot.

---

## Fields available on `TrailEntry`

Everything the predicates need, and nothing more:

`turn_index` · `tool_call_id` · `tool_name` · `args` · `status` (`ok` | `denied` | `error`) · `error_code` · `provenance` · `result_preview` (`ResultPreview | None`) · `result_full_ref` · `ts` · `authoritative` · `denial_detail`

`ResultPreview` carries `columns`, `row_count`, `truncated`, `preview_rows`.

---

## A. Completion validator

The cited entry must satisfy **all** of:

| # | Condition | Notes |
|---|---|---|
| 1 | `entry.turn_index == turn_index` | Current logical turn — spans budget windows and pauses, which is what `turn_index` already means |
| 2 | `entry.status == "ok"` | |
| 3 | `entry.tool_name ∈ {runQuery, runBlueprint, getTableSchema}` | `resolveValues`, `sampleRows`, `searchBlueprints`, `searchKnowledge` are insufficient |
| 4 | If `runBlueprint`: `entry.authoritative is True` | A blueprint can return `ok` with an unclean verify block — `_is_verified_blueprint_result` gates the marker, so this catches what condition 2 does not |
| 5 | `entry.error_code != IDEMPOTENT_READ_ALREADY_SERVED` | See below |

### Condition 5 is not paranoia

`getTableSchema` is in `IDEMPOTENT_READ_TOOLS` (`loop/read_guard.py:30`). When the model re-issues an identical call, the loop **does not re-dispatch**: it persists a data-free `TrailEntry` with `status="ok"`, `error_code=IDEMPOTENT_READ_ALREADY_SERVED`, `provenance=None`, `result_preview=None` (`agent_loop.py:1818`). That entry passes conditions 1–4 while having fetched nothing. Without condition 5 an intent can be completed by citing a deduped no-op.

**Evidence reuse across intents is allowed.** One query can genuinely answer "headcount and average salary by department". Do not add a uniqueness check; flag reuse in telemetry (06) instead.

### The accepted trade

Admitting `getTableSchema` means any intent — including an analytical one — can be completed by citing a schema fetch. This is deliberate: it closes the metadata-intent gap without an `intent.kind` field. Mitigation is the `metadata_evidence_completion` counter plus Layer-4 case 5, **not** a structural rule.

A tightening was proposed and **rejected**: *reject `getTableSchema` evidence when any `runQuery`/`runBlueprint` succeeded in the turn.* The test is per-turn, the concern is per-intent, so it breaks the legitimate mixed request — the very case case 5 exists to protect. Do not reintroduce it.

---

## B. Block validator

**Runtime-forced codes are not model-declarable.** `BUDGET_EXHAUSTED`, `USER_STOPPED`, `ENFORCEMENT_EXHAUSTED` are set by the runtime (05), require no evidence, and must be **rejected** if they arrive from the model. Enforce via the `MODEL_REASON_CODES` / `RUNTIME_REASON_CODES` split from 03 §A.

Two model-declarable codes, both mechanically provable:

### `NO_ACCESS`

```
entry.status == "denied"
and entry.error_code in {COLUMN_SCOPE_VIOLATION, DATABASE_NOT_ALLOWED, SCRATCH_SESSION_VIOLATION}
```

All three exist in `dispatch/denial_mapping.py`. Do not widen to every denial: `CLICKHOUSE_QUERY_ERROR` and `CARTESIAN_JOIN_FORBIDDEN` are the model's own mistakes, not access failures.

### `REQUIRED_DATA_UNAVAILABLE`

```
(entry.status == "ok"
 and entry.error_code != IDEMPOTENT_READ_ALREADY_SERVED
 and entry.result_preview is not None
 and entry.result_preview.row_count == 0)
or entry.error_code == TABLE_NOT_FOUND
```

**`result_preview is not None` is required, not defensive.** A guard-marker entry has `result_preview=None`, and so does a denied entry — dereferencing `.row_count` would raise `AttributeError` inside the validator. The guard-marker exclusion is stated separately because that entry is `ok`, so the marker check must come before the preview check.

> **What this validator does not close.** Both predicates are cheaply *manufacturable*: a query with an impossible predicate returns zero rows, and a query naming an out-of-scope column is denied on request. Evidence-backed blocking stops the model asserting an unfalsifiable reason; it does not stop it producing a falsifiable one. The guarantee to state is **"the model cannot silently drop an ask"**, not "cannot evade". Escalated to the Lead; until decided, the adversarial suite asserts the current behaviour so the hole is measured rather than assumed away.

### The three cut reasons

`NO_GROUNDED_SEMANTICS`, `NO_APPLICABLE_TOOL` and `USER_DECLINED_CLARIFICATION` were specified and cut at Lead review: each proved an *attempt* rather than an outcome, which defeats the point of evidence-backed blocking. **Do not reintroduce them** — spec §10 records the rejection.

Consequence, and it is intended: an intent that is genuinely unanswerable but not *provably* so has **no model-declared exit**. It stays `pending` and 05's `ENFORCEMENT_EXHAUSTED` path terminates it. The runtime records the failure rather than accepting the model's word for it.

`NO_APPLICABLE_TOOL` survives as an **inferred** signal — a turn that ran `searchBlueprints` and then completed the intent on `runQuery` evidence searched the corpus and fell through to ad-hoc. Reconstructed from telemetry (06), asserted by nobody.

---

## Open spec item

One question is with the Lead and affects this file:

> At enforcement exhaustion, if the turn contains a consumed `askUser` pause and the intent is still pending, should the runtime force `USER_DECLINED_CLARIFICATION` rather than `ENFORCEMENT_EXHAUSTED`?

If approved it adds a **runtime-forced** code — never model-declarable, so the block validator above is unaffected. Only 05's forcing logic and `RUNTIME_REASON_CODES` change. Build as specified; the amendment is additive.

---

## Tests

`tests/runtime/composite/test_evidence_validators.py`:

| Case | Expect |
|---|---|
| `runQuery` ok, current turn | valid |
| `runBlueprint` ok + `authoritative=True` | valid |
| `runBlueprint` ok + `authoritative=False` | **invalid** |
| `getTableSchema` ok | valid |
| `getTableSchema` ok + `IDEMPOTENT_READ_ALREADY_SERVED` | **invalid** |
| `resolveValues` / `sampleRows` / `searchBlueprints` ok | **invalid** |
| Entry from a prior `turn_index` | **invalid** |
| Unknown `tool_call_id` | **invalid** |
| Same id cited by two intents | **valid** (reuse allowed) |

`tests/runtime/composite/test_evidence_validators_adversarial.py`:

| Case | Expect |
|---|---|
| `NO_ACCESS` citing `COLUMN_SCOPE_VIOLATION` denial | valid |
| `NO_ACCESS` citing an `ok` call | **invalid** |
| `NO_ACCESS` citing `CLICKHOUSE_QUERY_ERROR` | **invalid** |
| `REQUIRED_DATA_UNAVAILABLE` citing ok + `row_count == 0` | valid |
| `REQUIRED_DATA_UNAVAILABLE` citing ok + `row_count > 0` | **invalid** |
| `REQUIRED_DATA_UNAVAILABLE` citing `TABLE_NOT_FOUND` | valid |
| `REQUIRED_DATA_UNAVAILABLE` citing a guard-marker entry | **invalid, no raise** |
| `REQUIRED_DATA_UNAVAILABLE` citing a denied entry (`result_preview is None`) | **invalid, no raise** |
| Model declares `BUDGET_EXHAUSTED` / `USER_STOPPED` / `ENFORCEMENT_EXHAUSTED` | **invalid** |
| Model declares a cut reason (`NO_APPLICABLE_TOOL`) | **invalid** — not in the enum |
| **Manufactured** `REQUIRED_DATA_UNAVAILABLE` citing `SELECT … WHERE 1=0` | **valid today** — assert it passes *and* that the transition is recorded in telemetry, so the known hole is measured |
| **Manufactured** `NO_ACCESS` citing a deliberately out-of-scope column | **valid today** — same treatment |

The two "no raise" rows are the ones a hand-written validator gets wrong.

## Done when

- [ ] Two separate pure functions; neither reused for the other's job.
- [ ] Completion requires `ok` + three-tool set + `authoritative` for blueprints + guard-marker exclusion.
- [ ] Blocking accepts non-`ok` entries; both predicates exact; `result_preview` null-safe.
- [ ] Runtime-only reason codes rejected from the model.
- [ ] Failures return a reason string that reaches the model via `denial_detail`.
- [ ] Adversarial suite green.
