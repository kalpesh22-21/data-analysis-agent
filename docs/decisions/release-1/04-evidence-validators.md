# 04 — Evidence Validators

**Spec:** [§6](../release-1-routing-and-intent-coverage.md) · **Size:** M · **Depends on:** 03 · **Blocks:** 05, 07
**Revised** 2026-08-11 after review; **both flagged changes approved by the Lead the same day** — see [§F](#f-review-corrections).

Everything the enforcement mechanism guarantees rests here. If these accept weak evidence, the model closes intents it did not answer and the ledger records it as done.

> **AMENDED after the live run (README finding 20) — how the binding is MADE, not what it accepts.** Completion by citing a `tool_call_id` scored **0 successes in 9 live attempts**. The model now tags the work when it dispatches it — `runQuery(sql=…, serves_intent="i2")` — and closes the intent with `{intent_id, status}` alone; `composite/analysis_state.py::resolve_tagged_evidence` finds the tagged entry and runs it through **the two validators below, unchanged**. Every condition in this document still binds, through the same code. Two consequences to keep straight:
> - ~~**The citation path stays and is not legacy.**~~ **Superseded 2026-08-12 (see the amendment below).** The reuse it existed to express is now the auto-bind backstop's rule 2; `loop_evidence_reused` still fires for it.
> - **§B.3's distinctness rule is unaffected.** It is checked over the merged state after the bindings have resolved, so it holds whichever way each was made. (A pure-tag collision is not even reachable: one trail entry carries one tag. The BACKSTOP can collide, and is refused there.)
> The presence rule in §B.2 moved rather than relaxed — "completed ⇒ a non-empty evidence string" was a payload check; it is now "completed ⇒ some qualifying call is tagged for this intent", checked against the trail, and a terminal update that resolves to nothing is refused as `unresolved_evidence`.

> **AMENDED 2026-08-12 — the model supplies NEITHER field now, and §A/§B still say what they said.** The `updateAnalysisState` item schema is `{description}` on the first call and `{intent_id, status}` on every later one. This document is the reason both removals are safe, so read the change against it:
> - **`evidence_tool_call_id` is gone.** Counting the same measurement again: 9 attempts, 9 invented ids, 0 successes, ever. It was kept above for the one shape a single-valued tag cannot express (one call, several intents); that shape is now handled by the AUTO-BIND BACKSTOP in [03 §C.3.2](03-analysis-state.md#c32-the-auto-bind-backstop--added-2026-08-12-with-the-schema-trim), whose candidate pool is built from **these validators** — so nothing is ever bound that §A or §B would refuse.
> - **`reason_code` is gone**, and §B.7 below defines what replaced it.
> - **§B.2's presence table is retired outright**, not relaxed: there is no field left to omit, and the obligation it encoded ("neither terminal status is reachable with no binding at all") is now enforced against the trail, where it is strictly stronger.
> - The two validators are **unchanged, line for line**. `_missing_entry_reason`'s "belongs to an earlier turn" / "not dispatched yet" branches are no longer reachable through the tool — every id they now see was selected by the runtime from this turn's trail — and are kept because these are pure functions and that branch is what makes "evidence must come from the turn being enforced" true on the function's own terms.

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

**Condition 5 is load-bearing.** `getTableSchema` is in `IDEMPOTENT_READ_TOOLS` (`loop/read_guard.py`); an identical repeat is not re-dispatched but persisted as a data-free entry with `status="ok"` and that marker. It passes 1–4 having fetched nothing.

> **The guard's contract changed after this was written, and condition 5 is UNAFFECTED — which is the point worth stating.** The guard no longer dedups unconditionally: since [README finding 23](README.md) it dedups an identical repeat *unless the first result is no longer readable*, in which case the repeat is re-dispatched for real (capped). So marker entries are now **rarer**, but nothing about them changed — a marker still means *nothing was fetched*, and it is still not evidence.
>
> Note this points the OPPOSITE way to the blueprint-definition gate ([02](02-blueprint-card-enrichment.md#the-partial-reversal-getblueprint-before-runblueprint)), where a dedup-guarded `getBlueprint` **does** satisfy the gate. The two are not in conflict because they ask different questions: this validator asks *"did work happen?"* (a dedup proves it did not), while the gate asks *"does the model have the definition?"* (a dedup proves it does). Anyone tempted to make them consistent should change neither. The constant is `IDEMPOTENT_READ_ALREADY_SERVED_CODE` and lives at `context/assembly.py:100` — import it, never re-type the literal. Better: move it beside `IDEMPOTENT_READ_TOOLS` in `loop/read_guard.py`, which is where the concept belongs.

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

Blocklisting the three runtime codes would let any *future* runtime code become model-declarable the day it lands. The allowlist also rejects `None`, `""` and unknown strings for free. Nothing is queued to be added — the one code that was under consideration is now closed (§C) — which is precisely when a blocklist looks safe and quietly stops being so.

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

### B.2 Presence is a rule, not an assumption — **retired 2026-08-12, and why that is not a weakening**

Nothing in the draft required `evidence_tool_call_id` to *exist*. Omit the key and no rule fires — the whole mechanism bypassed by leaving a field out. This is the repo's own recorded lesson: derive the guard from what downstream reads require, not from the fields that happen to be sent. The rule as written was:

```
status == "completed"  ⇒ evidence_tool_call_id is a non-empty str
status == "blocked"    ⇒ reason_code ∈ MODEL_REASON_CODES
                         AND evidence_tool_call_id is a non-empty str
status == "pending"    ⇒ both absent/None
```

**Neither field is in the schema any more, so there is nothing left to check the presence of.** What the rule was PROTECTING survives and moved:

```
status ∈ {completed, blocked}  ⇒ some call on THIS turn validates as
                                 evidence for that status, found by tag or
                                 by the auto-bind backstop — else refused
status == "pending"            ⇒ nothing is recorded, whatever arrived
```

That is strictly stronger. The old form could only assert that a non-empty *string* was present; the new one has to find a call the validators accept. The five bypass shapes it was tested against (omitted, `null`, `""`, whitespace, and a string naming nothing) collapse into one shape with no bypass — and the *lesson* is unchanged, since the new guard is likewise derived from what downstream reads require rather than from what the model happens to send.

### B.3 ✅ Block evidence must be distinct per intent — approved

Reuse is right for completion and wrong for blocking. A denial arises from one specific SQL and column set; it asserts nothing about a *different* deliverable.

Cost of exploiting the draft's global reuse rule: one `getTableSchema(<scratch_db>, "x")` ⇒ `SCRATCH_SESSION_VIOLATION` ⇒ mark all eight intents `blocked` ⇒ finalization proceeds on a fully "evidenced" record. *(Since 2026-08-12 the model no longer cites that id — the auto-bind backstop's rule 1 hands every untagged blocked intent the same lone denial, which is the same attack with less typing. The rule is unchanged and still catches it, because it is checked over the merged state after the bindings resolve.)*

Require distinctness within a state — purely mechanical, no semantics — and emit `block_evidence_reused`. It does not stop manufacture; it raises the cost from O(1) to O(n) calls and makes bulk-blocking visible instead of hiding it behind a shared id.

Per the Lead: *"a single denial should not be reusable to close several unrelated intents. Completion evidence may still be reusable where one result genuinely answers multiple asks, but model-declared blocking evidence is one-intent/one-evidence."* So the asymmetry is deliberate — **completion reuse stays allowed** (§A), blocking reuse does not.

### B.4 Two holes that remain open

**Manufacture is cheaper than the spec implies.** The escalation to the Lead said "a query naming an out-of-scope column is denied on request", implying the model must know a column it lacks. It does not. Verified, cheapest first, none needing warehouse access or any knowledge of the user's scope:

- `getTableSchema(<scratch_db>, <anything>)` ⇒ `SCRATCH_SESSION_VIOLATION` (`clickhouse-api/app/service.py:317`, fails closed on any foreign or session-less name). A **metadata call** — and it does not lock 03 §E's late-init boundary.
- Any table in a database outside the allowlist ⇒ `DATABASE_NOT_ALLOWED` (`service.py:73`).
- `explainQuery` runs the same guardrails without executing (`service.py:791`), so a scope violation is producible without touching data.

**Zero rows is an answer, not an absence.** `REQUIRED_DATA_UNAVAILABLE` fires on every *correct* query whose result is legitimately empty — "who left last month" when nobody did. That same entry is simultaneously valid *completion* evidence, so the model chooses, and `blocked` is cheaper: no prose, no table, no `answerWithTable`. This is not manufacture — it fires on honest work, and it makes coverage under-report on exactly the questions whose answer is "none", which users already distrust.

Mechanically indistinguishable from manufacture, so the mitigation is prompt plus measurement: **01** should say an empty result set is `completed` with an explicit "none found" answer, never `blocked`; **06** should count `zero_row_block` against `zero_row_completion`; **07** should include a genuinely-empty case.

**A ZERO-ROW CLAIM MUST COME FROM A QUERY (rule added 2026-08-12, found on review of the auto-bind backstop).** `_build_preview`'s BARE-LIST branch (`dispatch/tool_dispatcher.py`) gives `listDatabases`/`listTables` a real `row_count`, so an EMPTY listing is `status="ok"` + `row_count == 0` and this validator accepts it as `REQUIRED_DATA_UNAVAILABLE`. Under the old contract that was unreachable in practice — the model had to CITE the id, and citation had 0 successes ever — but the backstop would have bound it for free, turning *"this database has no tables"* into *"this deliverable cannot be done"* with no model claim at all. That is not this hole being re-priced; it is a NEW one the release would have opened.

So `auto_bind_candidates`' blocked pool is narrowed **per derived code**:

| Derived code | Auto-bind pool | Why |
|---|---|---|
| `NO_ACCESS` | **unrestricted** (as this validator is) | A denial is a denial whichever tool hit it, and the cheapest route above is already a metadata probe — excluding metadata tools would not close it, and would break the case where a schema fetch is genuinely what was refused. |
| `REQUIRED_DATA_UNAVAILABLE` | **`SUBSTANTIVE_TOOLS` only** | "The data is not there" is a claim about a QUERY. A listing is discovery, not evidence of absence. |

The rule is on the BACKSTOP, not on the validator, and the **tagged path is deliberately not narrowed** — same reasoning as a tagged `getTableSchema` completing an intent while an auto-bound one cannot: the trade is the model's to claim, not the runtime's to make on its behalf.

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
>
> **This warning came true on a DIFFERENT tool, and the fix is §B.4's per-code narrowing.** `listDatabases`/`listTables` already take the bare-list branch, so they already reach `row_count == 0` — the validator accepts an empty listing today and the review caught it as the auto-bind backstop was about to make that reachable. Note what the narrowing does and does not do: it keeps the BACKSTOP from binding a non-substantive zero-row call, and it leaves the VALIDATOR unrestricted, so this paragraph's warning still stands for any future citation-shaped path. `searchBlueprints` becoming a bare list would be caught by the same clause, since it is not in `SUBSTANTIVE_TOOLS` either.

### B.7 The reason code is DERIVED, not declared — 2026-08-12

The model used to send `reason_code` and this document used to check it. Read §B.1 again and the redundancy is plain: `NO_ACCESS` was accepted only for a call refused with a code in `NO_ACCESS_ERROR_CODES`, and `REQUIRED_DATA_UNAVAILABLE` only for a successful call with `row_count == 0`. The validator *recomputed the code from the trail* and refused any disagreement — so the model's value carried no information the runtime lacked, and the only thing it could contribute was a mismatch.

The runtime now asks the question backwards:

```python
def classify_block_evidence(tool_call_id, trail, turn_index) -> str | None:
    for code in DERIVABLE_REASON_CODES:            # == sorted(MODEL_REASON_CODES)
        if validate_block_evidence(tool_call_id, code, trail, turn_index) is None:
            return code
    return None                                    # proves neither — refuse the block
```

Three properties worth stating explicitly:

- **There is no second implementation of §B.1 to drift.** The derivation *is* the validator, run over the closed set of codes. The two are mutually exclusive by construction (`NO_ACCESS` needs `status != "ok"`, `REQUIRED_DATA_UNAVAILABLE` needs `ok`), so at most one can match and the iteration order is irrelevant.
- **The MODEL/RUNTIME split became structural.** `DERIVABLE_REASON_CODES` is derived from `MODEL_REASON_CODES`, so a runtime-only code (`ENFORCEMENT_EXHAUSTED`, `BUDGET_EXHAUSTED`, `USER_STOPPED`) is now *unreachable* from this path rather than rejected by an allowlist on it — and a future addition to the model-declarable enum becomes derivable without a second edit.
- **Blocking did not get easier.** A call that merely failed — a retryable SQL error — classifies as neither, so there is nothing honest to write and the block is refused, exactly as a mislabelled `reason_code` was refused before. What changed is that the model no longer has to state a fact it was never the authority on.

---

## C. Closed — no fourth runtime code

Whether enforcement exhaustion should force `USER_DECLINED_CLARIFICATION` when the turn contains a consumed `askUser` pause was open through two review rounds. **Closed 2026-08-11: it is not added.**

The concern it existed for was that a user withdrawing an ask mid-clarification would be recorded as agent failure. The Lead's rewording of `ENFORCEMENT_EXHAUSTED` to *"enforcement could not establish a disposition"* removes the mislabel at the source — for a withdrawal, that description is simply **correct**. A distinct code would add a reason to the enum, a forcing branch to 05, and a bucket to 07's report, to express something the existing code already expresses accurately.

`RUNTIME_REASON_CODES` therefore stays at three, `MODEL_REASON_CODES` at two, and this validator is final for Release 1.

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
| Same id bound to two intents (completion) | **valid** |
| `tool_call_id` omitted / `null` / `""` / whitespace | **invalid** (the pure-function guard; unreachable through the tool since 2026-08-12) |

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
| **One denial blocking three intents** | **invalid** under B.3 — reached by the backstop now, not by a citation |
| `classify_block_evidence` on a denied / zero-row / row-bearing / merely-failed call | `NO_ACCESS` / `REQUIRED_DATA_UNAVAILABLE` / `None` / `None` — B.7's derivation, asserted against the same entries the validators are |
| Manufactured `NO_ACCESS` via `getTableSchema(<scratch_db>, …)` | **valid today** — assert it, and assert the telemetry records it |

The two "no raise" rows and the manufactured-evidence rows are the ones a hand-written validator gets wrong.

## F. Review corrections

| Was | Now | Why |
|---|---|---|
| Trail handed in from the loop | **Read at call time** | The loop's only load is above the round-trip loop and is reduced to signatures — a snapshot has nothing from this window, so every citation failed |
| `NO_ACCESS` needs `status == "denied"` | **`status != "ok"`** | Blueprint denials surface as `status="error"` with the inner code — the primary route could not produce access evidence |
| Four model-declarable codes | **Two** ✅ *(Lead-approved)* | `DATABASE_NOT_ALLOWED`/`TABLE_NOT_FOUND` are `retryable=True` — the codebase's own "model got the name wrong" bucket — and `TABLE_NOT_FOUND` is also how a scope denial surfaces from `sampleRows` |
| Runtime codes "must be rejected" | **Allowlist** | Blocklisting makes any future runtime code model-declarable the day it lands |
| Presence of evidence assumed | **B.2 explicit rule**, then **retired 2026-08-12** | Omitting the key bypassed the mechanism entirely; there is no key left to omit, and the obligation moved to the trail where it is stronger |
| Model declares `reason_code`, validator checks it | **B.7: runtime derives it** | The validator already recomputed the code from the trail, so the model's value could only ever disagree |
| Citation is the escape hatch for evidence reuse | **03 §C.3.2 auto-bind rule 2** | 0 successes in 9 live attempts; the runtime can find the one call itself |
| Reuse allowed everywhere | **Distinct for blocking** ✅ *(Lead-approved)* | One `SCRATCH_SESSION_VIOLATION` could block every intent at once |
| Marker check "must precede" the preview check | **Null test precedes the dereference** | Ordering in an `and` chain is irrelevant; the null test is the safety property, and the draft pointed at the wrong clause |
| Manufacture = "name an out-of-scope column" | **B.4 three metadata-only routes** | The true price is one `getTableSchema` against the scratch db |
| Zero rows unremarked | **B.4 named as a distinct hole** | Fires on honest work, not just manufacture; skews coverage on "none found" answers |
| "Cannot silently drop an ask" | **B.5 two-clause statement** | Tracking is opt-in and `completed` is not bound to the intent |
