# 04 — Evidence Validators

**Spec:** [§6](../release-1-routing-and-intent-coverage.md) · **Size:** M · **Depends on:** 03 · **Blocks:** 05, 07
**Revised** 2026-08-11 after review; **both flagged changes approved by the Lead the same day** — see [§F](#f-review-corrections).

Everything the enforcement mechanism guarantees rests here. If these accept weak evidence, the model closes intents it did not answer and the ledger records it as done.

## Two validators, and they are not interchangeable

Reusing one for both is the most likely implementation error: the completion validator requires a successful call, while `NO_ACCESS` evidence is a **failed** one.

```python
def validate_completion_evidence(
    tool_call_id: str, trail: Sequence[TrailEntry], turn_index: int
) -> str | None:  # None = valid, else a reason string for denial_detail

def validate_block_evidence(
    tool_call_id: str, reason_code: str, trail: Sequence[TrailEntry], turn_index: int
) -> str | None:
```

Pure functions in `composite/analysis_state.py`, so they unit-test without a store. Returning a reason string rather than a bool is what lets `denial_detail` tell the model *why*; `classify_denial`'s canned text cannot.

Both compare `entry.turn_index == turn_index`. Same rule as [03 §A.1](03-analysis-state.md)'s `live_analysis_state`: never accept evidence from a turn other than the one being enforced. **Write that condition into both predicates below** — it is not implicit.

### The trail must be read at call time, not handed in

**The trail the loop already holds is unusable.** `_run_loop_body`'s only trail load is at `agent_loop.py:1664` — *above* the round-trip loop at `:1700` — and it is immediately reduced to signatures for `seen_read_calls`. No list survives, and every entry is appended later (`:1952`). A snapshot taken there contains **nothing from the current window**, so evidence written in round 1 and cited in round 2 fails as "unknown `tool_call_id`" — every completion and every block, on every turn, landing in `ENFORCEMENT_EXHAUSTED` while looking correctly wired.

`UpdateAnalysisStateTool` therefore calls `await session_store.load_trail(session_id)` at execution and filters to `turn_index` — what `_compute_turn_provenance_union` (`:960`), `_compute_turn_assumptions` (`:1001`) and the assembler all already do per round-trip. 03 §E.2 caps state calls at 2 per response, so that is ≤2 reads per round-trip and none on turns without a state call.

### Evidence must come from a *prior* round-trip

03 §E.2 dispatches `updateAnalysisState` **first** in every batch. So `[runQuery, updateAnalysisState(completed, evidence=<that call>)]` — the natural shape, and the one 05 §G encourages — can never validate: the evidence entry does not exist yet when the state call runs.

Say so in the tool description, and make the reason string distinguish *"that call has not been dispatched yet — cite it next round"* from *"unknown id"*. The correct model behaviour differs.

---

## A. Completion validator

All of:

| # | Condition | Notes |
|---|---|---|
| 1 | `entry.turn_index == turn_index` | Spans budget windows and pauses, which is what `turn_index` means |
| 2 | `entry.status == "ok"` | |
| 3 | `entry.tool_name ∈ {runQuery, runBlueprint, getTableSchema}` | `resolveValues`, `sampleRows`, `searchBlueprints`, `searchKnowledge` insufficient |
| 4 | If `runBlueprint`: `entry.authoritative is True` | A blueprint can return `ok` with an unclean verify block — `_is_verified_blueprint_result` gates the marker, so this catches what condition 2 does not |
| 5 | `entry.error_code != IDEMPOTENT_READ_ALREADY_SERVED` | Below |

**Condition 5 is load-bearing.** `getTableSchema` is in `IDEMPOTENT_READ_TOOLS` (`loop/read_guard.py:30`); an identical repeat is not re-dispatched but persisted as a data-free entry with `status="ok"` and that marker (`agent_loop.py:1818`). It passes 1–4 having fetched nothing. The constant is `IDEMPOTENT_READ_ALREADY_SERVED_CODE` and lives at `context/assembly.py:100` — import it, never re-type the literal. Better: move it beside `IDEMPOTENT_READ_TOOLS` in `loop/read_guard.py`, which is where the concept belongs.

Since the validator holds the whole trail, it can also make the rejection actionable — locate the original by `read_guard.idempotent_read_signature(tool_name, args)` and return *"that call was deduped; cite `<original id>`"*. The loop cannot do this: `seen_read_calls` is a set of signatures with no ids.

**Evidence reuse across intents is allowed** for completion — one query genuinely answers "headcount and average salary by department". Telemetry-flagged only. *(Blocking is different — see B.3.)*

### The accepted trade

Admitting `getTableSchema` means any intent, including an analytical one, can be completed by citing a schema fetch. Deliberate: it closes the metadata-intent gap without an `intent.kind` field. Mitigation is the `metadata_evidence_completion` counter plus 07 case 5, **not** a structural rule.

A tightening was proposed and **rejected**: *reject `getTableSchema` evidence when any `runQuery`/`runBlueprint` succeeded in the turn.* The test is per-turn, the concern is per-intent, so it breaks the legitimate mixed request. Do not reintroduce it.

---

## B. Block validator

**Runtime-forced codes are not model-declarable.** Enforce as an **allowlist**, not a blocklist:

```python
if reason_code not in MODEL_REASON_CODES:
    return "reason_code is not one the model may declare"
```

Blocklisting the three runtime codes would let any *future* runtime code become model-declarable the day it lands — and 05 §F.2 anticipates exactly that (`USER_DECLINED_CLARIFICATION`). The allowlist also rejects `None`, `""` and unknown strings for free.

**05's runtime force-block path bypasses this validator entirely.** It writes `BUDGET_EXHAUSTED` / `USER_STOPPED` / `ENFORCEMENT_EXHAUSTED` directly. An implementer routing those through `validate_block_evidence` gets them rejected by the rule above.

### B.1 The two model-declarable reasons

✅ **Narrowed on review; approved by the Lead.** The draft admitted four codes. Two are gone — *"they are too ambiguous. `DATABASE_NOT_ALLOWED` can mean the model simply chose the wrong database, and `TABLE_NOT_FOUND` can represent either a naming mistake or `sampleRows` obscuring a scope denial. Neither is strong enough to let the model terminate an intent as genuinely blocked."*

| `reason_code` | Predicate on the cited entry |
|---|---|
| `NO_ACCESS` | `entry.status != "ok"` **and** `entry.error_code ∈ {COLUMN_SCOPE_VIOLATION, SCRATCH_SESSION_VIOLATION}` |
| `REQUIRED_DATA_UNAVAILABLE` | `entry.status == "ok"` **and** `entry.error_code != IDEMPOTENT_READ_ALREADY_SERVED` **and** `entry.result_preview is not None` **and** `entry.result_preview.row_count == 0` |

**The error code establishes the denial, not the outer status.** Per the Lead: *"a qualifying access failure may arrive as `status="denied"` or `status="error"` when a blueprint propagates the underlying access error."* The non-`ok` gate is a sanity check; the code is what qualifies.

**Why that matters concretely.** The dispatcher sets `denied` for an `MCPToolError` (`tool_dispatcher.py:381`), but the blueprint tool does not: `ExecFailed` becomes `ToolResult(status="error", …)` (`blueprint/tool.py:135`) with the inner code passed through verbatim (`executor.py:394`, `:894`). So a `runBlueprint` that hit `COLUMN_SCOPE_VIOLATION` internally persists `status="error"` — and the draft rejected it. **In a blueprint-first release, the primary route could not produce access evidence.** Keying on the code is safe because the only `ok` entry carrying an `error_code` is the guard marker, which is not in the set.

**Why `DATABASE_NOT_ALLOWED` and `TABLE_NOT_FOUND` were dropped.** The draft excluded `CLICKHOUSE_QUERY_ERROR` and `CARTESIAN_JOIN_FORBIDDEN` as "the model's own mistakes" — but both dropped codes are `retryable=True` in `denial_mapping.py:77-86`, with messages *"Let me check what's accessible"* and *"Let me verify the table name"*. The codebase classifies them in the same bucket. A typo'd table name yielded a valid `REQUIRED_DATA_UNAVAILABLE`, indistinguishable in the ledger from genuine absence. Worse, `TABLE_NOT_FOUND` is also how a *scope* denial surfaces: `clickhouse-api/app/service.py:459` raises it when the caller's column scope hides every column of a `sampleRows` target — recording "the warehouse lacks this" for what is precisely "this user may not see this", inverting the one distinction governance reads.

The surviving two are the non-retryable ones, which is the line the draft's own principle drew.

**Ordering.** The null test must precede the `.row_count` dereference — denied entries and guard markers both carry `result_preview=None`, as do error entries from `retrieval/tools.py:171` and `composite/resolve_values.py:383`. The marker clause is defence in depth, not the ordering rule; an earlier draft had this backwards, which invited a "simplification" that would `AttributeError`.

### B.2 Presence is a rule, not an assumption

Nothing in the draft required `evidence_tool_call_id` to *exist*. Omit the key and no rule fires — the whole mechanism bypassed by leaving a field out. This is the repo's own recorded lesson: derive the guard from what downstream reads require, not from the fields that happen to be sent.

```
status == "completed"  ⇒ evidence_tool_call_id is a non-empty str
status == "blocked"    ⇒ reason_code ∈ MODEL_REASON_CODES
                         AND evidence_tool_call_id is a non-empty str
status == "pending"    ⇒ both absent/None
```

Test the omitted key, explicit `null`, `""`, and whitespace.

### B.3 ✅ Block evidence must be distinct per intent — approved

Reuse is right for completion and wrong for blocking. A denial arises from one specific SQL and column set; it asserts nothing about a *different* deliverable.

Cost of exploiting the draft's global reuse rule: one `getTableSchema(<scratch_db>, "x")` ⇒ `SCRATCH_SESSION_VIOLATION` ⇒ mark all eight intents `blocked`/`NO_ACCESS` citing that single id ⇒ finalization proceeds on a fully "evidenced" record.

Require distinctness within a state — purely mechanical, no semantics — and emit `block_evidence_reused`. It does not stop manufacture; it raises the cost from O(1) to O(n) calls and makes bulk-blocking visible instead of hiding it behind a shared id.

Per the Lead: *"a single denial should not be reusable to close several unrelated intents. Completion evidence may still be reusable where one result genuinely answers multiple asks, but model-declared blocking evidence is one-intent/one-evidence."* So the asymmetry is deliberate — **completion reuse stays allowed** (§A), blocking reuse does not.

### B.4 Two holes that remain open

**Manufacture is cheaper than the spec implies.** The escalation to the Lead said "a query naming an out-of-scope column is denied on request", implying the model must know a column it lacks. It does not. Verified, cheapest first, none needing warehouse access or any knowledge of the user's scope:

- `getTableSchema(<scratch_db>, <anything>)` ⇒ `SCRATCH_SESSION_VIOLATION` (`clickhouse-api/app/service.py:317`, fails closed on any foreign or session-less name). A **metadata call** — and it does not lock 03 §E's late-init boundary.
- Any table in a database outside the allowlist ⇒ `DATABASE_NOT_ALLOWED` (`service.py:73`).
- `explainQuery` runs the same guardrails without executing (`service.py:791`), so a scope violation is producible without touching data.

**Zero rows is an answer, not an absence.** `REQUIRED_DATA_UNAVAILABLE` fires on every *correct* query whose result is legitimately empty — "who left last month" when nobody did. That same entry is simultaneously valid *completion* evidence, so the model chooses, and `blocked` is cheaper: no prose, no table, no `answerWithTable`. This is not manufacture — it fires on honest work, and it makes coverage under-report on exactly the questions whose answer is "none", which users already distrust.

Mechanically indistinguishable from manufacture, so the mitigation is prompt plus measurement: **01** should say an empty result set is `completed` with an explicit "none found" answer, never `blocked`; **06** should count `zero_row_block` against `zero_row_completion`; **07** should include a genuinely-empty case.

### B.5 The guarantee

> **The Release-1 contract, as approved by the Lead (2026-08-11):**
>
> **For every intent the model chooses to track, Release 1 guarantees a recorded, falsifiable terminal disposition. It does not guarantee that every user intent was detected, nor that a model-declared disposition is semantically true.**
>
> The two halves are separate problems with separate mechanisms. `analysisState` solves **state loss after detection**; the multi-intent detection-rate metric (07 §E.3) measures whether the model created the state **in the first place**. Neither substitutes for the other.

Both clauses are load-bearing. Tracking is opt-in — no live state means no enforcement (05 §A), and a rejected late init leaves the turn *unprotected* by design (03 §E). And `completed` means "cited an `ok` call of the right kind", not "answered": nothing binds evidence to the intent, so an unrelated `runQuery` closes any intent and the user sees the same dropped ask, differing only in that a transition was recorded.

### B.6 The cut reasons

`NO_GROUNDED_SEMANTICS`, `NO_APPLICABLE_TOOL` and `USER_DECLINED_CLARIFICATION` were cut at Lead review: each proved an *attempt* rather than an outcome. **Do not reintroduce** — spec §10 records the rejection.

Consequence, intended: an intent genuinely unanswerable but not *provably* so has no model-declared exit. It stays `pending` and 05's `ENFORCEMENT_EXHAUSTED` terminates it.

**Read that code precisely.** Per the Lead: it means *"enforcement could not establish a disposition"* — **not** that the system proved the intent impossible. The escalation findings are why: evidence that looks mechanical can be semantically ambiguous (zero rows is often the correct answer), and some denial probes cost one metadata call. A code that claimed proof would be overstating what the runtime knows.

`NO_APPLICABLE_TOOL` survives as an **inferred** signal: a turn that ran `searchBlueprints` and then completed the intent on `runQuery` evidence fell through to ad-hoc. Derivable from `loop_intent_completed{intent_id, evidence_tool_name}` + `tool_dispatch_ok{tool_name}` — **no fixture knowledge needed, so it works in production too**, not just in the harness.

> **Watch this one.** `_build_preview`'s non-tabular branch hard-codes `row_count=1` (`tool_dispatcher.py:250`), and `searchBlueprints` returns `{count, degraded, blueprints}` — so a zero-hit search cannot currently reach `row_count == 0`. Change either shape to a bare list and the cut reason walks back in through `REQUIRED_DATA_UNAVAILABLE`, since the block validator does not restrict `tool_name`.

---

## C. Open spec item

> At enforcement exhaustion, if the turn contains a consumed `askUser` pause and the intent is still pending, should the runtime force `USER_DECLINED_CLARIFICATION` rather than `ENFORCEMENT_EXHAUSTED`?

If approved it is a **runtime-forced** code — never model-declarable — so B's allowlist keeps this file unaffected by construction. Only 05's forcing logic and `RUNTIME_REASON_CODES` change.

---

## D. Not a hazard — checked and cleared

- **Inner blueprint `runQuery` calls never appear in the trail.** Only the loop appends (`agent_loop.py:1312`, `:1834`, `:1952`); the executor calls `ToolDispatcher.dispatch` directly (`executor.py:391`, `:718`). They have no `tool_call_id` and are not citable — which is also the mechanism behind the `status="error"` finding in B.1.
- **A paused blueprint leaves nothing to cite.** `ExecPaused` returns `status="ok"` with `result_preview=None`, but the loop returns at `:1916` before persisting.
- **`explainQuery` is correctly outside the completion set.** `EXPLAIN` always returns rows, so it cannot reach `row_count == 0` either. Its only relevance is B.4.

---

## E. Tests

`tests/runtime/composite/test_evidence_validators.py`:

| Case | Expect |
|---|---|
| `runQuery` ok, current turn | valid |
| `runBlueprint` ok + `authoritative=True` | valid |
| `runBlueprint` ok + `authoritative=False` | **invalid** |
| `getTableSchema` ok | valid |
| `getTableSchema` ok + guard marker | **invalid**, reason names the original id |
| `resolveValues` / `sampleRows` / `searchBlueprints` ok | **invalid** |
| Prior `turn_index` | **invalid** |
| Unknown / not-yet-dispatched `tool_call_id` | **invalid**, distinct reasons |
| Same id cited by two intents (completion) | **valid** |
| `evidence_tool_call_id` omitted / `null` / `""` / whitespace | **invalid** |

`tests/runtime/composite/test_evidence_validators_adversarial.py`:

| Case | Expect |
|---|---|
| `NO_ACCESS` citing a `denied` `runQuery` | valid |
| **`NO_ACCESS` citing a `runBlueprint` with `status="error"` + `COLUMN_SCOPE_VIOLATION`** | **valid** — the B.1 fix |
| `NO_ACCESS` citing an `ok` call, or `CLICKHOUSE_QUERY_ERROR` | **invalid** |
| `NO_ACCESS` citing `DATABASE_NOT_ALLOWED` | **invalid** — dropped in B.1 |
| `REQUIRED_DATA_UNAVAILABLE` citing ok + `row_count == 0` | valid |
| …citing `TABLE_NOT_FOUND` | **invalid** — dropped in B.1 |
| …citing a guard-marker entry, or a denied entry | **invalid, no raise** — the `result_preview is None` paths |
| `runBlueprint` ok + `authoritative` + `row_count == 0` | valid for **both** validators — the B.4 ambiguity, asserted so it is measured |
| Model declares any `RUNTIME_REASON_CODE`, or a cut reason, or `None`/`""` | **invalid** |
| **One denial cited to block three intents** | **invalid** under B.3 |
| Manufactured `NO_ACCESS` via `getTableSchema(<scratch_db>, …)` | **valid today** — assert it, and assert the telemetry records it |

The two "no raise" rows and the manufactured-evidence rows are the ones a hand-written validator gets wrong.

## F. Review corrections

| Was | Now | Why |
|---|---|---|
| Trail handed in from the loop | **Read at call time** | The loop's only load is above the round-trip loop and is reduced to signatures — a snapshot has nothing from this window, so every citation failed |
| `NO_ACCESS` needs `status == "denied"` | **`status != "ok"`** | Blueprint denials surface as `status="error"` with the inner code — the primary route could not produce access evidence |
| Four model-declarable codes | **Two** ✅ *(Lead-approved)* | `DATABASE_NOT_ALLOWED`/`TABLE_NOT_FOUND` are `retryable=True` — the codebase's own "model got the name wrong" bucket — and `TABLE_NOT_FOUND` is also how a scope denial surfaces from `sampleRows` |
| Runtime codes "must be rejected" | **Allowlist** | Blocklisting makes any future runtime code model-declarable the day it lands |
| Presence of evidence assumed | **B.2 explicit rule** | Omitting the key bypassed the mechanism entirely |
| Reuse allowed everywhere | **Distinct for blocking** ✅ *(Lead-approved)* | One `SCRATCH_SESSION_VIOLATION` could block every intent at once |
| Marker check "must precede" the preview check | **Null test precedes the dereference** | Ordering in an `and` chain is irrelevant; the null test is the safety property, and the draft pointed at the wrong clause |
| Manufacture = "name an out-of-scope column" | **B.4 three metadata-only routes** | The true price is one `getTableSchema` against the scratch db |
| Zero rows unremarked | **B.4 named as a distinct hole** | Fires on honest work, not just manufacture; skews coverage on "none found" answers |
| "Cannot silently drop an ask" | **B.5 two-clause statement** | Tracking is opt-in and `completed` is not bound to the intent |
