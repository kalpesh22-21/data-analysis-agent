# 01 — Blueprint-First Prompt Rewrite

**Spec:** [§3](../release-1-routing-and-intent-coverage.md) · **Size:** S · **Depends on:** nothing · **Blocks:** 07

## Current state (verified)

*Pre-rewrite baseline, recorded before this doc was executed. For the shipped text and size see [01a](01a-prompt-draft.md).*

`src/data_agent/runtime/prompts.py` — `AGENT_SYSTEM_PROMPT`, a module-level constant, **11,297 chars ≈ 2,824 tokens**, re-sent on every round-trip.

Nine sections in order: *Sizing the request · Planning a complicated request · Operating procedure · Understanding blueprints · Trust boundary · Scope and sensitive data · Asking vs. assuming · Presenting a table · Answering.*

Wiring:
- `RuntimeSettings.agent_system_prompt` defaults to this constant; `agent_system_prompt_enabled` (default `True`) gates it (`runtime/config.py:431-439`).
- `ContextAssembler` takes it as `base_system_prompt` and inserts it at index 0 as the **sole** `role:"system"` message, *after* retrieval insertion (`context/assembly.py:281`).
- Being a module constant is load-bearing for D45: every per-round-trip rebuild and every resume must re-derive byte-identical messages.
- `fit_request_to_budget` pins the leading `system` run as undroppable head.

**Why the routing is wrong today.** The Operating procedure leads with discovery and `getTableSchema`; the blueprint-preference bullet is the **fifth of eight** (`prompts.py:99`; the bullets start at :76, :86, :91, :96, **:99**, :101, :108, :112), after the model has been told to fetch a schema and run a query. Blueprint cards are already in context before round 1 (`retrieval_top_k_blueprints=3`, inserted immediately before the current question), so the ordering actively works against material the model already holds.

## Changes

**Remove** the *Sizing the request* and *Planning a complicated request* sections entirely (currently the first two, ~2,000 chars).

**Add** a routing section in their place expressing spec §3's ten steps. Behaviour only — do not name route classes; those live in telemetry and evaluation.

**Keep unchanged (whole sections):** Trust boundary, Scope and sensitive data, Asking vs. assuming, Presenting a table, Answering.

### ⚠ Four rules live *inside* the section being rewritten and must survive it

The "keep" list above names only sections *outside* the Operating procedure. Rewriting that section wholesale silently drops these:

| `prompts.py` | Rule | Why it must survive |
|---|---|---|
| :101-107 | *"Once a validated blueprint has RETURNED a result, treat it as authoritative … do NOT run additional runQuerys to re-derive"* | This is spec §3 step 9 **and the entire subject of 07's re-derivation case**. Dropping it while adding a test for it is the worst outcome available |
| :76-85 | Do not re-fetch a schema you can still see | Per the module docstring at :5-8, this is *why the prompt exists* — without it the model was observed re-fetching the same schema dozens of times |
| :86-90 | Batch independent reads in one turn | The mechanism behind spec §3 step 5 |
| :96-98 | `resolveValues` before filtering on a code column | The tenant-code wrong-answer class; also Phase 2's first advisory validator |

Extend the contract test (below) to assert the authoritative-result rule is still present — it is the one with a downstream eval case depending on it.

### The "semantic-correctness reminder" does not exist

Spec §3 says to retain it. **There is no such line in `prompts.py`** — the phrase originates in §17 of the original review, which was answered as an architecture principle, not a prompt line. The nearest existing text is *"Never fabricate numbers"* (`prompts.py:203`). So this is an **addition**, not a retention: either write one line saying successful execution is not proof of semantic correctness, or record that it was deliberately left out. Do not carry it as "keep unchanged", which hides the decision.

**Rewrite** the Operating procedure so blueprint routing precedes schema discovery, and so `searchBlueprints` is framed as **per-deliverable practice**, not a fallback. Today it reads *"If none of the blueprints offered to you fit, call searchBlueprints…"* — that framing is the reason per-intent search will not happen.

### Terminology

Spec §3 uses **deliverable**; §5/§7/§9 and the state field use **intent**. State once, in the prompt, that they denote the same thing, then use one consistently. "Deliverable" is the better word for step 1 because it is what stops a metadata ask being excluded from the tracked set — keep it there.

### Additions the state contract requires

- Multi-deliverable request → call `updateAnalysisState` **before any substantive tool call**. Name the boundary concretely: *before any `runQuery`, `runBlueprint`, `sampleRows` or `resolveValues`*. The model cannot recover from crossing it (03 §E).
- **An empty result set is an answer, not an absence.** Complete the intent and say "none found"; do not mark it blocked. Without this the model takes the cheaper exit and coverage under-reports on exactly the questions users distrust most (04 §B.4).
- Single-deliverable request → do **not** call it.
- Emit `updateAnalysisState` **first in the batch** when combining it with substantive calls. The runtime also enforces this (03), but the prompt should not rely on the safety net.
- Completion evidence must be a `runQuery`, an authoritative `runBlueprint`, or a `getTableSchema` — the model chooses which call it cites, so it needs to know what counts.
- Do not attempt to finalize while a tracked intent is unresolved.

## Token budget

Removing two sections and adding one shorter routing block plus ~6 state lines should land **net neutral to slightly smaller**. Measure it: the prompt is re-sent every round-trip and is charged to the loop's per-window token SPEND ceiling (`max_window_token_spend`) every time, so growth is multiplied by round count.

```
uv run python -c "from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT as P; print(len(P), len(P)//4)"
```

Record before/after in the PR, and add a hard assertion rather than relying on "net neutral to slightly smaller".

**As shipped the assertion is `len(AGENT_SYSTEM_PROMPT) <= 15_000`**, not the `<= 11_297` this section originally specified. Three live findings landed after the rewrite (01a §5 finding 19, §6 the citation→tagging fix, §7 the direct-use routing path), each an instruction the runtime cannot enforce and the prompt is the only carrier of, taking it to 12,084 chars; the getBlueprint-before-runBlueprint rule (01a §8) then took it to 13,308, and the re-fetch-escape rewording (01a §9) to **13,383**, and that one the runtime DOES enforce — the prompt buys the model getting it right first time rather than learning it from a refusal. The ceiling was ratified at 15,000 by the user — deliberately above the current size, so the remaining routing/tracking work has room to land instead of being paid for by deleting working instructions. See [01a §7](01a-prompt-draft.md) for the ratification and the full history of the number.

## Edge cases

- **Duplicated slot-type glosses.** The "Understanding blueprints" section duplicates `SLOT_TYPE_GLOSS` from `runtime/blueprint/models.py:32` with no parity test — `tests/runtime/blueprint/test_models.py:28` pins only `SLOT_TYPES` ↔ `SLOT_TYPE_GLOSS`. If the rewrite touches that section, add the parity assertion.
- **⚠ `prompts.py:120-121` tells the model to read slots via `getBlueprint`.** Doc 02 makes the search card carry slots directly, and amends the *tool schema* accordingly — but the system prompt leads the message list and is re-sent every round-trip, so a stale instruction there defeats 02's entire round-trip saving. **01 owns this fix**; coordinate wording with 02.
- **`agent_system_prompt_enabled=False`** must still produce a working loop. Do not make any runtime behaviour conditional on prompt text.
- **Byte-stability.** No timestamps, no interpolation, no environment reads. A D45 rebuild must be byte-identical.

## Tests

| Test | Asserts |
|---|---|
| `tests/runtime/test_config.py` (extend) | Prompt constant is non-empty; `agent_system_prompt` default is the constant |
| `tests/runtime/context/test_assembly.py` (extend) | Prompt is still the sole `role:"system"` message and sits at index 0 after retrieval insertion |
| `tests/runtime/loop/test_base_prompt_survives_budget_cap_and_compaction.py` | Existing — must stay green; the head-pin behaviour is unchanged |
| New: `test_prompt_routing_contract.py` | No "SIMPLE"/"COMPLICATED" vocabulary; mentions `searchBlueprints`, `updateAnalysisState`, and the four substantive tool names; **the authoritative-result rule is still present**; length within budget |

That last test is deliberately crude — a keyword scan cannot tell a well-ordered routing section from a badly-ordered one containing the right words. It guards only against an edit silently *dropping* an instruction the runtime contract depends on. **Ordering and emphasis are proven by 07's live-model suite, not here.**

## ⚠ This document does not contain the deliverable

Every other doc in this set specifies to the line. This one specifies to the section: it says what to remove and what the replacement must express, but the replacement text does not exist. 01 is the only deliverable whose artifact is prose, and it is the fix for P1 — the release's primary defect. Two builders will produce materially different prompts, and the only acceptance signal is 07's live suite, which is expensive and late.

**Write the replacement text into a companion `01a-prompt-draft.md` before building**, so P1's fix is reviewed as an artifact rather than as an intention.

## Done when

- [ ] **Replacement prompt text drafted and reviewed** (`01a-prompt-draft.md`).
- [ ] Sizing + Planning sections gone; routing section present; no route-class names in model-facing text.
- [ ] The four in-section rules survived; the authoritative-result rule asserted by test.
- [ ] `prompts.py:120` slot instruction updated to match 02.
- [ ] Semantic-correctness line either added or its absence recorded.
- [ ] `searchBlueprints` framed as per-deliverable, not fallback.
- [ ] Late-init boundary named in terms of the four substantive tools.
- [ ] Before/after token count recorded.
- [ ] Existing prompt/assembly tests green.
