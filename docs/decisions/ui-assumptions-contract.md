# UI Assumptions — `recordAssumptions` + the `assumptions` result field

**Status:** Contract-locked. Additive amendment to
`docs/decisions/ui-slice1-enriched-result-contract.md` (the enriched `/turn`
`result` event). Backend + frontend build against *this file*.

**Scope:** Surface the agent's own plain-English assumptions as a first-class,
additive field on the final turn result (SSE `result` event + `GET
/session/history` turns), alongside `sql` / `result_table` / `provenance`.

---

## 0. Decisions (settled, not relitigated here)

- Assumptions are **MODEL-DECLARED**. The runtime never infers them.
- Assumptions are **PLAIN ENGLISH**, stated in the user's own terms — **never**
  SQL, codes, column names, or raw stored values.
- They are surfaced via a dedicated runtime tool, **`recordAssumptions`**.
- The new field **mirrors the existing `sql` accumulator ("UI Slice 1") in every
  respect** — additive, nullable, `[] -> null` fork, accumulated across budget
  windows, populated best-effort at every `TurnOutcome` return site.

## 1. The field

`assumptions: string[] | null` — a new key on the `result` SSE event
(`_outcome_to_dict`) and on each `GET /session/history` turn.

- A **list of short plain-English sentences**, in the order the model recorded
  them, deduped preserving **first-occurrence** order.
- **`null`-not-`[]` fork (identical to `sql`):** a turn that recorded **no**
  assumptions serializes as `null`, not `[]`, so the UI treats "no assumptions
  panel" and "empty" identically. The internal accumulator is a `list[str]`; the
  `[] -> None` collapse happens at every return site.
- **Nullable on every status** (best-effort, never load-bearing): `done`,
  `paused_ask_user`, `paused_budget_cap`, `stopped_hard_ceiling` may each carry a
  populated list (whatever was recorded before the return) or `null`. The FE must
  tolerate `null` on any status.

## 2. The tool — `recordAssumptions`

- Function tool, name `recordAssumptions`. One required param `assumptions`
  (`array` of `string`). **No credential params** (D5) — no warehouse data, no
  backing stack.
- **Always advertised AND always wired** (`runtime_tools["recordAssumptions"]`
  registered unconditionally in `app.py`). It has no backing stack, so it never
  takes the advertised-but-unwired `RUNTIME_TOOL_UNAVAILABLE` path.
- A runtime tool (the `resolveValues` / `runBlueprint` shape): intercepted in the
  agent loop, never dispatched to the MCP, counts as exactly one
  `tool_calls_made`. It is stateless — it returns a tiny confirmation
  `ToolResult` (`result_preview` = the accepted count, `result_full = None`) and
  never raises on malformed args.
- **Prompt contract:** the model is instructed (tool description + system prompt)
  to call it **once, just before the final answer**, with each assumption a short
  plain-English sentence; and to **skip the call** when it made no assumptions.
  Good: `"'Active employees' was taken to mean currently-employed staff."` Bad:
  `"EmployeeStatus = 'A'"`.

## 3. Cleaning (`clean_assumptions` / `fold_assumptions`) — the plain-English rule is NOT runtime-enforced

`clean_assumptions(raw) -> list[str]` is the SINGLE normalizer, and
`fold_assumptions(target, raw)` is the SINGLE in-place fold, used by ALL three
sites (loop accumulation, loop blueprint-resume trail reconstruction, and
`session_history` history reconstruction), so they agree byte-for-byte. Cleaning:
drops non-strings and blank (`.strip()`) items; dedupes first-occurrence; applies
lenient safety caps (max items / max length).

It **deliberately does NOT** detect or strip SQL/codes. The plain-English / no-SQL
rule is enforced by the **tool description + system prompt**, not the runtime —
the runtime does not second-guess the model's own text (consistent with how the
loop treats `answer` / `pending_question` text).

## 4. Accumulation (loop) — mirrors `turn_sql`

- `AgentLoop._run_loop` holds a turn-window-local `turn_assumptions: list[str]`
  accumulator next to `turn_sql`, seeded from a `seed_assumptions` param.
- On each **successful** `recordAssumptions` call, `clean_assumptions(args
  ["assumptions"])` is folded in place (deduped, first-occurrence) — no-op for
  any other tool / non-`ok` result.
- `assumptions = turn_assumptions or None` is threaded into **every**
  `TurnOutcome(...)` return: `done`, the `askUser` `paused_ask_user`,
  `paused_budget_cap`, `stopped_hard_ceiling`, and `_pause_from_runtime_tool`.
- The blueprint approval-resume path seeds `seed_assumptions` by reconstructing
  the turn's prior `recordAssumptions` entries from the trail (parity with the
  `seed_sql` enrichment seed), so assumptions recorded before a mid-DAG pause
  survive the resumed window.

## 5. History scope posture (FLAGGED FOR REVIEW)

Assumptions are **model-authored plain English** and, by contract, carry **no
warehouse data** (no cell values, no column names). They are therefore **NOT
scope-gated per trail entry** the way `tool_calls` are. Instead they inherit the
**answer's** scope treatment exactly — the same posture as `answer` /
`provenance_union` / `pending_question` text.

Concretely, in `project_history`:
- assumptions are gathered from the **RAW (unfiltered) trail's**
  `recordAssumptions` entries (a `recordAssumptions` entry has `None` provenance
  and would otherwise be dropped by `filter_trail` — which is irrelevant here);
- a turn's assumptions are surfaced **only when that turn's assistant answer
  survives the message scope filter** (`assistant is not None`). If the answer is
  withheld out of scope, its assumptions are withheld with it.

**Reviewer:** please double-check this coupling — surfacing assumptions is tied
strictly to answer survival, never to per-entry trail scope. The premise is that
plain-English assumptions never name a column/value outside scope (enforced by
the prompt/schema, not the filter).

## 6. Provenance-union carve-out + replay posture (load-bearing)

A successful `recordAssumptions` trail entry is intentionally `ok` + **`None`
provenance** (it carries no warehouse data). Two consequences are handled
explicitly so the tool never damages a normal turn:

- **Union carve-out.** `_compute_turn_provenance_union` (agent_loop.py) is
  fail-closed: any `ok`+`None` entry would normally collapse the whole turn's
  provenance union to `None`, which would tag the assistant answer undetermined
  and drop BOTH the answer and its assumptions from history + future-turn replay
  — **even under allow-all scope**. So `recordAssumptions` `ok` entries are
  **excluded from the union**, exactly like the existing
  `IDEMPOTENT_READ_ALREADY_SERVED_CODE` carve-out. (We do NOT give the tool
  `frozenset()` provenance — see below.)
- **Not replayed under narrowed scope.** Because the entry keeps `None`
  provenance, `filter_trail` drops it from the replayed trail, so the raw
  assumption strings never re-enter model context when the scope narrows. It
  rides the D94 withheld-provenance sentinel path only to keep its `tool_call`
  paired (the model sees a generic data-free "proceed" tool-slot fill); the
  `loop_result_withheld_provenance` diagnostic is suppressed for it (routine-use
  noise), symmetric to the idempotent-read guard.

Net: on BOTH surfaces (live result event and `GET /session/history`) assumptions
inherit the **answer's** fate — surfaced when the answer is in scope, withheld
with it when not — and never leak assumption text into a narrowed-scope replay.
