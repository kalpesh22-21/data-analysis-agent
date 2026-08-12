# Release 1 — Blueprint-First Routing and Intent Coverage

**Status:** Approved for build (Lead, 2026-08-11); revised the same day after Lead review — §3 step 1, §5.1, §5.4, §6.2, §7, §8, §9.2
**Supersedes for build purposes:** the full programme in [prompt-routing-review-qa.md](prompt-routing-review-qa.md), which remains the decision record and audit trail. Where the two disagree, this document governs Release 1; that one records why.

---

## 1. The two problems

Everything here serves exactly two defects. Anything that does not is out of scope (§10).

**P1 — The prompt routes badly.** Blueprint preference appears five bullets into the operating procedure, after the discovery-and-schema instructions, even though the three most relevant blueprint cards are already pre-injected into context before the model's first round-trip. The model is therefore invited to design fresh SQL against a schema it just fetched, when a validated blueprint for the same intent was already in front of it.

**P2 — Multi-part requests can lose an intent.** `_tool_trail_entry_to_canonical` synthesizes `assistant(content=None, tool_calls=[…])`, so all model free text around a tool call is discarded and never replayed. A decomposition made in round 1 does not exist in round 2. A three-part request can be answered in two parts with nothing detecting it.

**Non-goal:** general execution planning. Release 1 tracks what the *user asked for*, not how the model intends to get it.

---

## 2. What ships

| # | Item | §
|---|---|---|
| 1 | Blueprint-first prompt rewrite | §3 |
| 2 | Enriched blueprint search cards | §4 |
| 3 | Minimal `analysisState` (original intents only) | §5 |
| 4 | Evidence-backed completion and blocking | §6 |
| 5 | Finalization enforcement | §7 |
| 6 | Raw routing telemetry (events only, no projector) | §8 |
| 7 | Minimal Layer-4 evaluation: 6 cases, 2 metrics | §9 |

---

## 3. Prompt rewrite

**File:** `src/data_agent/runtime/prompts.py` — `AGENT_SYSTEM_PROMPT` (currently 11,297 chars ≈ 2,824 tokens, re-sent every round-trip).

**Remove** the "Sizing the request" and "Planning a complicated request" sections entirely. Semantic complexity is the wrong routing criterion: a linguistically complex question may map to one validated blueprint.

**Replace with behaviour-only routing.** Do not name internal route classes in the prompt — those live in telemetry (§8) and evaluation, not in model-facing text.

The hierarchy the prompt must express:

1. Identify the distinct **user-requested deliverables** — every part of the request the user expects an answer to, analytical *or* metadata/schema. Not "analytical intents": Release 1 explicitly supports the mixed request (§9.1 case 5), and a purely analytical framing invites the model to leave the metadata ask out of `analysisState` entirely, where nothing protects it.
2. For each **analytical** deliverable, check the **offered** blueprint cards first. A metadata or schema deliverable is grounded with the discovery tools instead — blueprint search does not apply to it.
3. For each unresolved analytical deliverable, call `searchBlueprints` **for that deliverable** — not once for the whole question.
4. One blueprint covers a deliverable → run it.
5. Several blueprints cover independent deliverables → issue them together in one response.
6. No blueprint fits → ground the deliverable in the catalog and institutional knowledge.
7. One grounded query suffices → run it; do not plan around obvious steps.
8. Multi-deliverable request → initialize `analysisState` **before any substantive execution**; §5.1 defines that boundary mechanically, and initialization is refused after it.
9. Never re-derive an authoritative blueprint result.
10. Do not finalize while a tracked intent is unresolved.

**Retain unchanged:** the trust boundary, scope/sensitive-data, asking-vs-assuming, table-presentation and answering sections, and the reminder that successful execution is not proof of semantic correctness. That reminder stays prompt-side in Release 1; Phase 2 makes parts of it mechanical.

**Known limitation, accepted.** Retrieval still embeds the **whole question as one string** and returns `retrieval_top_k_blueprints=3` cards — insufficient for a four-intent request, and a weak query vector for any single clause. Step 3 above is the mitigation. Layer-4 cases 1–4 (§9) are the test of whether it holds; if blueprint miss-rate is high, firing retrieval per declared intent is the Phase-2 fix.

---

## 4. Enriched blueprint search cards

**File:** `src/data_agent/runtime/retrieval/tools.py` (`SearchBlueprintsTool`), `retrieval/models.py` (`ThinCard`).

`searchBlueprints` currently returns `{id, intent, slots_summary, score}`. Per-intent search is becoming the default path (§3 step 3), so each thin candidate costs a `getBlueprint` round-trip to evaluate.

**Enrich the card from fields that already exist** — `resolves`, `slots` (name / type / required), `result_grain`, `status`. No corpus schema change, no re-seed, no learning-loop change, no new authored fields.

---

## 5. `analysisState` v1

### 5.1 When it exists

**Multi-intent requests only.** A single-intent request creates no state. Dependent-but-single-intent work creates no state either — the model simply does the prerequisite work; only what the *user* asked for is tracked.

**No late initialization — and the boundary is mechanical, not advisory.** Late init was cut, but "before substantive execution" is not enforceable as prose, so it is defined against the current turn's trail:

| | Tools | Effect on first-time initialization |
|---|---|---|
| **Discovery** | `searchBlueprints`, `getBlueprint`, `getTableSchema`, `searchKnowledge` | Permitted first. The model is *expected* to look before it decomposes. |
| **Substantive** | `runQuery`, `runBlueprint`, `sampleRows`, `resolveValues` | Once any of these has an entry in this turn, first-time initialization is **rejected**. |

Rejection is `ANALYSIS_STATE_LATE_INIT` — non-retryable, registered in `dispatch/denial_mapping.py`, with the proposed descriptions carried in `denial_detail` (the same rendering trap as §7: `context/budget.py::_render_entry` builds it from the persisted entry — `denial_detail` if set, else `classify_denial(error_code)` — never from `ToolResult.user_message`, which has no `TrailEntry` field). Telemetry: `analysis_state_late_init_rejected`.

The turn then runs unprotected — there is no state, so §7 has nothing to enforce. That is the intended consequence: the alternative is late init surviving under another name.

**Accepted limitation — clarification cannot broaden the protected set.** `analysisState` survives an `askUser` pause and the resume appends the answer at the **same `turn_index`** (`couchbase_store.py:274`), so a post-clarification round is still inside the initialized turn. Combined with §5.4's immutability rule, this means:

> After `analysisState` initialization, clarification may refine the interpretation of existing intents but cannot add new tracked user deliverables. If the user's clarification broadens the request, the additional ask is best-effort and is **not** protected by Release-1 intent-coverage enforcement.

This is preferable to reopening either escape hatch: allowing the set to grow reopens mutation (and with it the evasion §5.4 closes), and allowing a second initialization reopens late init.

### 5.2 Shape

```jsonc
{
  "turn_index": 7,
  "intents": [
    {
      "intent_id": "i1",                         // RUNTIME-ASSIGNED; the model never supplies ids
      "description": "headcount by department",  // an ORIGINAL user deliverable
      "status": "pending",                       // pending | completed | blocked
      "evidence_tool_call_id": "call_a1",        // see §6
      "reason_code": null                        // closed enum; blocked only
      // `intent_id` and `description` are FROZEN after initialization (§5.4);
      // only the last three fields are ever updated.
    }
  ]
}
```

**Deliberately absent** — each of these was specified at some point and cut: `route`, `depends_on`, derived intents and their depth/count rules, withdrawal, creation-evidence, late initialization, free-form `note`, automatic blueprint-state flipping.

Two of those cuts cost nothing. **`route` is derivable** from the evidence call's tool name (`runBlueprint` ⇒ blueprint, `runQuery` ⇒ ad-hoc, `getTableSchema` ⇒ metadata), so routing telemetry survives without the field. **Prerequisite work needs no managed entity.**

### 5.3 Storage and lifecycle

**File:** `src/data_agent/runtime/session/models.py`, plus `couchbase_store.py` / `memory_store.py`.

A new nullable field on `SessionDoc`, sibling to `pause_checkpoint` — the existing precedent for a structured, non-message, non-trail field.

- **Single mutable field, latest-wins.** Never append-only trail entries: N rounds would put N copies in the pinned region of `fit_request_to_budget` and reproduce unbounded context growth in structured form.
- **`frozenset()` provenance** — determined-empty, the `recordAssumptions` posture.
- **Dropped cross-turn**, via the `_is_stale_assumptions_entry` pattern in `context/assembly.py`.
- **Re-rendered into context from the store on every round-trip** (D45 statelessness — never carried in memory across a pause).
- Survives `askUser` pause/resume and process restart, because it lives on the session doc.

### 5.4 The tool

**New file:** `src/data_agent/runtime/composite/analysis_state.py`, mirroring `composite/record_assumptions.py`.

`updateAnalysisState` — always wired, no backing stack, so it can never return `UNAVAILABLE`. Registered in `agent_loop.py`'s `runtime_tools` and in `mcp/tool_schema.py::_LOCAL_TOOL_SCHEMAS` (**tool count 14 → 15**).

- **The model proposes descriptions only.** The runtime validates, assigns stable ids, and returns them in the tool result — which is how the model learns them.
- **Initialization is a one-time full declaration; all subsequent calls are batched updates**, several intents per call. Batching is load-bearing: with automatic blueprint-flipping cut, per-intent update calls would reintroduce the bookkeeping overhead this trim exists to remove.
- **Originals are immutable after initialization.** Once the state exists for a turn, the set of intents, their `intent_id`s and their `description`s are frozen. An update may change **only** `status`, `evidence_tool_call_id` and `reason_code`. No addition, no deletion, no rewrite. Without this, "full-replace" is a free exit from §7: the model drops the hard intent from the array and finalizes cleanly, having answered two of three asks. A call that attempts any of the three is rejected as `ANALYSIS_STATE_INVALID`, like any other malformed state.
- **Exempt from `max_tool_calls_per_iteration`** (default 8). Calls beyond that cap are silently dropped and never dispatched, with no error to the model — so without the exemption, a state call plus eight substantive calls loses the eighth.
- **Validation is derived from downstream reads, not spot-patched per field:** closed enum on `status` and `reason_code`, bounded intent count, **unknown keys rejected rather than passed through**. A validation failure returns a retryable error (`ANALYSIS_STATE_INVALID`) the model can correct — never a silently-stored malformed state.

---

## 6. Evidence rules

Two validators. **They are not the same validator**, and reusing one for both is the most likely implementation error here.

### 6.1 Completion

`status → completed` requires `evidence_tool_call_id` referencing a call in the **current turn** that:

- has `status == "ok"`;
- has `tool_name ∈ {runQuery, runBlueprint, getTableSchema}`;
- if `runBlueprint`, additionally has `authoritative == true` — a blueprint *can* return `ok` with an unclean verify block, so this catches a case "completed successfully" does not;
- does **not** carry `error_code == IDEMPOTENT_READ_ALREADY_SERVED`. That marker is the repeated-read guard's data-free nudge: it satisfies "exists, current turn, ok" while having fetched nothing.

`resolveValues`, `sampleRows`, `searchBlueprints` and `searchKnowledge` are **not** sufficient to complete an intent.

**Evidence reuse across intents is allowed** — one query can genuinely answer "headcount and average salary by department" — and is telemetry-flagged only.

> **Accepted trade.** Admitting `getTableSchema` means any intent can be completed by citing a schema fetch, including an analytical one. This is how the metadata-intent gap is closed without an `intent.kind` field. Mitigation is `metadata_evidence_completion` telemetry (§8) plus Layer-4 case 5, **not** a structural rule. A previously-proposed tightening — reject `getTableSchema` evidence when any `runQuery`/`runBlueprint` succeeded in the turn — was **rejected**: the test is per-turn but the concern is per-intent, so it breaks the legitimate mixed request.

### 6.2 Blocking

**Runtime-forced** — `BUDGET_EXHAUSTED`, `USER_STOPPED`, `ENFORCEMENT_EXHAUSTED` (§7). Set by the runtime; no evidence required, and the model cannot declare them.

**Model-declared** — requires a closed-enum `reason_code` **and** `evidence_tool_call_id`, validated as appropriate to the reason. Without this, `blocked` is a free exit from any hard intent and §7's guarantee degrades to *"cannot silently drop an ask unless it declares the ask blocked."*

**Only two reasons are model-declarable in Release 1**, and both are mechanically provable from a cited trail entry:

| `reason_code` | Predicate on the cited entry |
|---|---|
| `NO_ACCESS` | `status == "denied"` **and** `error_code ∈ {COLUMN_SCOPE_VIOLATION, DATABASE_NOT_ALLOWED, SCRATCH_SESSION_VIOLATION}` |
| `REQUIRED_DATA_UNAVAILABLE` | (`status == "ok"` **and** `result_preview.row_count == 0`) **or** `error_code == TABLE_NOT_FOUND` |

**Three softer reasons were specified and cut**, because each proved an *attempt* rather than an outcome — which is the whole point of evidence-backed blocking:

- `NO_GROUNDED_SEMANTICS` — a successful `getTableSchema`/`searchKnowledge` proves grounding was attempted, never that it failed. No second condition exists that would prove the negative.
- `NO_APPLICABLE_TOOL` — proves the model searched, not that the search came up short. Even with the extra condition (empty result, or a `getBlueprint` expansion) it remains a proof of diligence, not of absence. It survives as an **inferred** signal instead: a turn containing `searchBlueprints` whose intent then completes on `runQuery` evidence is a corpus gap, derivable from §8 telemetry without the model asserting it.
- `USER_DECLINED_CLARIFICATION` — the ≥2-user-message test proves another message exists, not that it declined. (`askUser` is intercepted in the loop and never reaches the dispatcher, so it leaves a `PauseCheckpoint`, not a `TrailEntry`; the durable record is the appended `TurnMessage(role="user")` at the same `turn_index`. That mechanism is sound — what was missing is a predicate that distinguishes a refusal from an answer.)

An intent that is genuinely unanswerable but not provably so therefore has **no model-declared exit**. It stays `pending`, and §7's `ENFORCEMENT_EXHAUSTED` path terminates it. That is the intended flow, not a gap: the runtime records the failure rather than accepting the model's word for it.

Two consequences for the implementation:

1. **This validator must accept non-`ok` entries.** `NO_ACCESS` cites a *denied* call; half of `REQUIRED_DATA_UNAVAILABLE` cites `TABLE_NOT_FOUND`. The completion rule ("`ok`, from the three-tool set") would reject exactly the evidence these reasons need.
2. **The guard-marker exclusion applies here too.** An entry carrying `IDEMPOTENT_READ_ALREADY_SERVED` fetched nothing; it also carries no `result_preview`, so the `row_count == 0` half of `REQUIRED_DATA_UNAVAILABLE` must test for the marker rather than dereference a preview that is `None`.

---

## 7. Finalization enforcement

**File:** `src/data_agent/runtime/loop/agent_loop.py`.

While any tracked intent is `pending`, neither terminal exit may complete:

- **Exit #2 — `answerWithTable`.** Return a retryable error `ToolResult` with `FINALIZATION_BLOCKED_PENDING_INTENTS`, exactly as `_answer_table_blueprint_not_run` already does for its case. Use **`denial_detail`** to name the pending intents: `context/budget.py::_render_entry` builds it from the persisted entry — `denial_detail` if set, else `classify_denial(error_code)` — never from `ToolResult.user_message`, which has no `TrailEntry` field. Register the code in `dispatch/denial_mapping.py`, or the model is told only the generic fallback string.
- **Exit #1 — a model turn with no tool calls.** There is no error channel here. Do not persist the assistant answer; inject the nudge and re-enter the loop. **Capped at one forced re-round per budget window**, so enforcement cannot itself burn the window. *(Mechanism amended after review: the nudge is an ephemeral `user`-role injection, not a synthetic tool result — a standalone `tool` message is not expressible. The invariant is unchanged; see [release-1/05](release-1/05-finalization-enforcement.md) §B.2. The forced re-round must also be charged to the budget window, or it is free and only the wall clock stops it.)*

**Terminal escapes.** Three, all runtime-forced, all marking every surviving `pending` intent `blocked` before allowing finalization. Without them the turn cannot terminate: both exits are reachable with pending intents and must return something.

| Trigger | Forced code |
|---|---|
| `stopped_hard_ceiling` | `BUDGET_EXHAUSTED` |
| Budget-cap resume answered `"stop"` | `USER_STOPPED` |
| The forced re-round is spent and intents are still pending | `ENFORCEMENT_EXHAUSTED` |

**`ENFORCEMENT_EXHAUSTED`, precisely:**

```
model attempts finalization with pending intents
  → runtime refuses and forces one re-round

model attempts finalization again, intents still pending
  → runtime marks every surviving pending intent  blocked / ENFORCEMENT_EXHAUSTED
  → finalization proceeds
```

Runtime-forced, no evidence required, own telemetry counter (§8). It is what makes the invariant literal now that §6.2 admits only two model-declared reasons: an intent that is genuinely unanswerable but not provably so has no model-side exit, so without this path the turn either finalizes with a `pending` intent or grinds to the budget cap on an ordinary request. Enforcement stays capped at one forced re-round per budget window either way — the cap is what bounds the cost, and this is what happens when it is reached.

**Invariant:** no intent ever ends `pending`.

---

## 8. Telemetry (events only)

Emit now, project later — the projector is Phase 2, but the data must not be lost while behaviour stabilises. All shape-only: **no question text, no SQL, no cell values, no resolved code strings** (D25 posture).

- intents declared (count, turn)
- every status transition, with `reason_code`
- evidence `tool_name` per completion — this is what makes `route` derivable
- `metadata_evidence_completion` — completion whose evidence is a `getTableSchema`
- evidence reuse across intents
- finalization refusals and forced-nudge count
- forced-blocked events, by reason — including a dedicated `ENFORCEMENT_EXHAUSTED` counter (§7)
- `analysis_state_late_init_rejected` — first-time initialization refused past the §5.1 boundary
- `searchBlueprints` calls per turn
- blueprint runs vs. ad-hoc queries
- re-derivation attempts after an authoritative result

**The inferred corpus-gap signal.** The last three lines together reconstruct what `NO_APPLICABLE_TOOL` would have asserted (§6.2): a turn that ran `searchBlueprints` and then completed the intent on `runQuery` evidence searched the corpus and fell through to ad-hoc. Inferred from what happened rather than declared by the model, which is the stronger form of the same fact — and it needs no field.

---

## 9. Evaluation

### 9.1 Cases

| # | Case | Passes when |
|---|---|---|
| 1 | Complex wording, one blueprint | Routes to the blueprint; no fresh SQL for that intent |
| 2 | Multiple independent blueprint intents | Each intent routed to its own blueprint |
| 3 | One blueprint + one ad-hoc intent | Blueprint used for its part; ad-hoc only for the residual |
| 4 | Three intents, one previously forgotten | All three reach a terminal status |
| 5 | Mixed metadata + analytical request | Both complete; metadata intent completes on `getTableSchema` |
| 6 | Authoritative blueprint result | **No** `runQuery` re-deriving the same intent |

### 9.2 Metrics

**Dropped-intent rate is a contract assertion, not a metric.** §7 guarantees no intent ends `pending`, so the rate is mechanically zero; measuring it would report the enforcement working, never the system working. It ships as a Layer-1/2 assertion — a non-zero value is a bug in the runtime, not a quality signal.

The two metrics that carry information:

**Blocked / unfulfilled-intent rate** — tracked intents reaching a terminal status *other than* `completed`, broken down by `reason_code`. This is what dropped-intent rate was reaching for. The breakdown is the diagnosis: `NO_ACCESS` is an entitlement story, `REQUIRED_DATA_UNAVAILABLE` is a data story, and `ENFORCEMENT_EXHAUSTED` is the agent failing to finish an ask it accepted — the one that should trend toward zero.

**Multi-intent detection rate** — *of requests known to be multi-intent, what percentage created an `analysisState`?* This catches what nothing else can: an undetected multi-intent request has no state, so it has nothing to drop from. **The denominator requires ground truth** — Layer 4 initially, an offline classifier in production. It cannot be self-reported, and it cannot be replaced by `analysis_state_late_init_rejected`: that event catches only the case where the model missed the decomposition, *later realised it*, and was refused. The failure this metric exists for is the model that never realises at all, never attempts initialization, and answers part of the request. Late-init rejections are a useful diagnostic submetric under this one, not a substitute for the classifier.

---

## 10. Explicitly out of scope

| Deferred to | Item |
|---|---|
| **Phase 2** | Advisory ad-hoc SQL validators (start with check 7, client-defined value resolution) · `coverage` as a first-class result field · routing-telemetry projector |
| **Phase 3** | Scratch chaining — auto-materialization, handles, upstream-provenance map, read-time subset check, truncation behaviour, paging restriction |
| **Separate projects** | 8-hour session refresh · bounded concurrent dispatch |

Findings for all of these are preserved in [prompt-routing-review-qa.md](prompt-routing-review-qa.md) §R3 — including the 1,000-row `max_response_rows` ceiling, the existing truncation guard at `executor.py:1057`, the scratch provenance carry-forward rule, and the D45 constraint on where a handle map may live. Do not rediscover them.

**Also rejected outright** (§D.1 of the base document): a model-facing `materializeResult` write tool; retaining mid-turn assistant narration; scope-hash stamping; new `supported_dimensions` / `semantic_contract` corpus fields; an `intent.kind` field.

**Rejected at Lead review (2026-08-11):** the three attempt-proof block reasons — `NO_GROUNDED_SEMANTICS`, `NO_APPLICABLE_TOOL`, `USER_DECLINED_CLARIFICATION` (§6.2); mutation or deletion of original intents after initialization (§5.4); late initialization in any form (§5.1).

---

## 11. Open implementation choices

Engineering discretion under Lead §47 — none blocks the build, but each needs deciding before the code that depends on it:

1. **Expressing a dependency at proposal time.** Ids are runtime-assigned, so a proposed intent cannot reference another by id. The candidate shape is a 0-based index into the submitted array (`depends_on_proposed: [2]`), rewritten by the runtime. *Note:* `depends_on` itself is cut from v1, so this only matters if a dependency field returns.
2. Exact `updateAnalysisState` JSON schema and error shapes.
3. Storage representation of `AnalysisState` on `SessionDoc` (`to_doc` / `from_doc`).
4. Layer-4 fixture format.
5. Advisory-validator message format (Phase 2).

**Any change that alters an invariant in §6, §7 or §9.2 comes back for review rather than being treated as an implementation detail.**

---

## 12. Build documents

Per-deliverable implementation guides, verified against the code, live in **[release-1/](release-1/)** — see its [README](release-1/README.md) for build order and the five wiring findings. This section is the summary index; those documents are the working detail.

---

## 13. Touchpoints

| Area | File |
|---|---|
| Prompt | `runtime/prompts.py` |
| Tool schema, count 14 → 15 | `runtime/mcp/tool_schema.py` |
| Tool implementation | `runtime/composite/analysis_state.py` *(new)* |
| Registry, finalization, forced-blocked, `ENFORCEMENT_EXHAUSTED` | `runtime/loop/agent_loop.py` |
| State persistence | `runtime/session/models.py`, `session/couchbase_store.py`, `session/memory_store.py` |
| Render into context, cross-turn drop | `runtime/context/assembly.py` |
| New error codes — `FINALIZATION_BLOCKED_PENDING_INTENTS`, `ANALYSIS_STATE_INVALID`, `ANALYSIS_STATE_LATE_INIT` | `runtime/dispatch/denial_mapping.py` |
| Enriched cards | `runtime/retrieval/tools.py`, `runtime/retrieval/models.py` |
| Docs to correct | `docs/02-tools-and-api.md` (says 12 tools; will be 15), `docs/04-blueprints.md` (stale F2 note) |
