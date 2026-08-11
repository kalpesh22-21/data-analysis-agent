# 01 — Blueprint-First Prompt Rewrite

**Spec:** [§3](../release-1-routing-and-intent-coverage.md) · **Size:** S · **Depends on:** nothing · **Blocks:** 07

## Current state (verified)

`src/data_agent/runtime/prompts.py` — `AGENT_SYSTEM_PROMPT`, a module-level constant, **11,297 chars ≈ 2,824 tokens**, re-sent on every round-trip.

Nine sections in order: *Sizing the request · Planning a complicated request · Operating procedure · Understanding blueprints · Trust boundary · Scope and sensitive data · Asking vs. assuming · Presenting a table · Answering.*

Wiring:
- `RuntimeSettings.agent_system_prompt` defaults to this constant; `agent_system_prompt_enabled` (default `True`) gates it (`runtime/config.py:431-439`).
- `ContextAssembler` takes it as `base_system_prompt` and inserts it at index 0 as the **sole** `role:"system"` message, *after* retrieval insertion (`context/assembly.py:281`).
- Being a module constant is load-bearing for D45: every per-round-trip rebuild and every resume must re-derive byte-identical messages.
- `fit_request_to_budget` pins the leading `system` run as undroppable head.

**Why the routing is wrong today.** The Operating procedure leads with discovery and `getTableSchema`; the blueprint-preference bullet is the *sixth* bullet, after the model has been told to fetch a schema and run a query. Blueprint cards are already in context before round 1 (`retrieval_top_k_blueprints=3`, inserted immediately before the current question), so the ordering actively works against material the model already holds.

## Changes

**Remove** the *Sizing the request* and *Planning a complicated request* sections entirely (currently the first two, ~2,000 chars).

**Add** a routing section in their place expressing spec §3's ten steps. Behaviour only — do not name route classes; those live in telemetry and evaluation.

**Keep unchanged:** Trust boundary, Scope and sensitive data, Asking vs. assuming, Presenting a table, Answering, and the semantic-correctness reminder.

**Rewrite** the Operating procedure so blueprint routing precedes schema discovery, and so `searchBlueprints` is framed as **per-deliverable practice**, not a fallback. Today it reads *"If none of the blueprints offered to you fit, call searchBlueprints…"* — that framing is the reason per-intent search will not happen.

### Terminology

Spec §3 uses **deliverable**; §5/§7/§9 and the state field use **intent**. State once, in the prompt, that they denote the same thing, then use one consistently. "Deliverable" is the better word for step 1 because it is what stops a metadata ask being excluded from the tracked set — keep it there.

### Additions the state contract requires

- Multi-deliverable request → call `updateAnalysisState` **before any substantive tool call**. Name the boundary concretely: *before any `runQuery`, `runBlueprint`, `sampleRows` or `resolveValues`*. The model cannot recover from crossing it (03 §late-init).
- Single-deliverable request → do **not** call it.
- Emit `updateAnalysisState` **first in the batch** when combining it with substantive calls. The runtime also enforces this (03), but the prompt should not rely on the safety net.
- Completion evidence must be a `runQuery`, an authoritative `runBlueprint`, or a `getTableSchema` — the model chooses which call it cites, so it needs to know what counts.
- Do not attempt to finalize while a tracked intent is unresolved.

## Token budget

Removing two sections and adding one shorter routing block plus ~6 state lines should land **net neutral to slightly smaller**. Measure it: the prompt is re-sent every round-trip and the loop's per-window token ceiling is `model_context_window`, so growth is multiplied by round count.

```
uv run python -c "from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT as P; print(len(P), len(P)//4)"
```

Record before/after in the PR.

## Edge cases

- **Duplicated slot-type glosses.** The "Understanding blueprints" section duplicates `SLOT_TYPE_GLOSS` from `runtime/blueprint/models.py` with no parity test — the existing test only pins `SLOT_TYPES` ↔ `SLOT_TYPE_GLOSS`. If the rewrite touches that section, add the parity assertion; otherwise leave it and note it.
- **`agent_system_prompt_enabled=False`** must still produce a working loop. Do not make any runtime behaviour conditional on prompt text.
- **Byte-stability.** No timestamps, no interpolation, no environment reads. A D45 rebuild must be byte-identical.

## Tests

| Test | Asserts |
|---|---|
| `tests/runtime/test_config.py` (extend) | Prompt constant is non-empty; `agent_system_prompt` default is the constant |
| `tests/runtime/context/test_assembly.py` (extend) | Prompt is still the sole `role:"system"` message and sits at index 0 after retrieval insertion |
| `tests/runtime/loop/test_base_prompt_survives_budget_cap_and_compaction.py` | Existing — must stay green; the head-pin behaviour is unchanged |
| New: `test_prompt_routing_contract.py` | The prompt contains no "SIMPLE"/"COMPLICATED" vocabulary; mentions `searchBlueprints`, `updateAnalysisState`, and the four substantive tool names by which the late-init boundary is defined |

That last test is deliberately crude — it guards against a future edit silently dropping an instruction the runtime contract depends on.

## Done when

- [ ] Sizing + Planning sections gone; routing section present; no route-class names in model-facing text.
- [ ] `searchBlueprints` framed as per-deliverable, not fallback.
- [ ] Late-init boundary named in terms of the four substantive tools.
- [ ] Before/after token count recorded.
- [ ] Existing prompt/assembly tests green.
