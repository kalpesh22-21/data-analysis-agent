# 08 — Multi-Table `answerWithTable`

**Spec:** none — this is the follow-up 01a §10 named as *"the actual fix"* · **Size:** M · **Depends on:** 03 (intent ids), 05 (terminal exit) · **Blocks:** nothing in Release 1
**Written** 2026-08-12, verified against the working tree on `docs/release-1-routing-and-intent-coverage`.

`answerWithTable` carries ONE query. A multi-intent turn produces several result sets and can designate only one of them, so the model answers the rest in prose. Measured across 17 live sessions, checking each trail entry's **status**: **6/6** on single-deliverable turns, **1/9** on multi-intent ones (01a §10). The prompt lever is spent — two merge rules were written, both measured, both reverted, and 01a §10's revert notice proves why a third wording cannot work.

**This document removes premise (1) of that unsatisfiable triple.** Nothing here instructs a merge, because after this there is nothing to merge.

**Scope discipline, stated once:** this makes a multi-intent answer *renderable*. It does not make it *correct* — see [§J](#j-what-this-does-not-fix), which is not a formality.

---

## Current state (verified)

The single-table chain, end to end. Every hop below carries exactly one query, and only one of them is actually a constraint.

| Hop | Site | Carries |
|---|---|---|
| Tool schema | `mcp/tool_schema.py:328-371` | `{answer, sql?, blueprint_id?}`, `required: ["answer"]` (`:369`) |
| Tool | `composite/answer_with_table.py:147` | Stateless; `provenance=frozenset()` (`:187`), a two-cell confirmation, no rows |
| Resolution | `composite/answer_with_table.py:106` `resolve_designation(args, terminal_by_id)` | ONE string or `None`; `sql=` wins when both are given (`:139`) |
| Loop | `loop/agent_loop.py:2122` `_resolve_answer_sql` | Resolves once, fires both `hooks/answer_table.py` seams (`:2169`, `:2174`) |
| Fold | `loop/agent_loop.py:2181` `_accumulate_answer_sql` | LAST designation wins (`:2200-2205`) |
| Turn result | `loop/agent_loop.py:479-481` | `answer_sql`, `blueprint_use`, `verification` — all singular |
| Wire | `runtime/app.py:194-196` | Same three keys on the SSE `result` frame |
| Reload | `session_history.py:138-143`, surfaced at `:167-169` | `answer_sql_by_turn[turn_index]`, one per turn |
| Reload de-ref | `runtime/app.py:862-891` | `blueprint_id -> terminal_sql` for the WHOLE session |
| Resume seed | `loop/agent_loop.py:1508` `_compute_turn_answer_sql` | One string, both designation forms |
| UI | `ui/static/index.html:966` `renderAnswerTable(root, answerSql)` | One `.result-table` panel per turn block (`:697-711`) |

**Four things the tree already gives us for free.**

1. **`/query/page` is not a blocker.** It takes one `sql` per request (`runtime/app.py:901-939`), wraps it (`query_page.py:95` `build_page_sql`) and dispatches through the caller's own `runQuery` (`runtime/app.py:938-939`). N tables = N independent calls, each with its own offset. No paging change, no BFF change (`ui/server.py:361`).
2. **`resolve_designation` already takes exactly the item shape we need.** It reads `args["sql"]` and `args["blueprint_id"]` off any mapping. An element of a `tables` array IS that mapping, so multi-table adds **no new resolution logic** — the same function, called in a loop.
3. **`blueprint_terminal_sql` is already a MAP.** `_capture_terminal_sql` (`loop/agent_loop.py:831-853`) records every successful blueprint of the window, and `runtime/app.py:862-891` rebuilds it for the whole session on reload. Three blueprints in a turn are already all resolvable; only the payload could not name them.
4. **The intent → producing-call binding already exists.** Every terminal intent ends with a bound `evidence_tool_call_id` (`composite/analysis_state.py:961-1013`; `session/models.py:285`), resolved from the call-time `serves_intent` tag or an explicit citation. §B uses this — as a check, not as the source.

**One thing that is worse than the brief assumed.** `verification` is not bound to the answer table *today*. `_accumulate_enrichment` (`loop/agent_loop.py:2072-2119`) sets `{passed: True, method: "blueprint_gate", grain_checked: …}` when ANY `runBlueprint` returns `status == "verified"`, and never resets it — `"passed": True` is the only occurrence of that key in the runtime (`:2114`); there is no `passed: False` anywhere. So a turn that ran a verified blueprint for i1 and then answered with a hand-written `runQuery` for i2 already renders "verified ✓" over an unverified grid (`ui/static/index.html:1115-1131` badges on `passed === true` alone). Multi-table does not create that over-claim; it makes it unignorable. See [§C](#c-verification).

---

## A. The shape of the change, in one line

```
answerWithTable(answer, tables: [{sql?, blueprint_id?, caption?}])
```

Everything else in this document is the consequences.

> **Amended 2026-08-13 — §A originally read `answerWithTable(answer, sql?, blueprint_id?, tables?)`.** The transition posture of §B.3 is **retired**: the top-level pair is gone from the model-facing schema and `tables` is required. [§O](#o-amendment-2026-08-13--tables-only) is the amendment and its evidence; §B.3 is left standing as the reasoning that was correct when it was written and the measurement that overturned it.

---

## B. Decision 1 — an explicit `tables` array, singular kept as shorthand

**Chosen:** `tables: [{sql | blueprint_id, caption}]`, with top-level `sql`/`blueprint_id` unchanged and still honoured. *(The second clause is retired — [§O](#o-amendment-2026-08-13--tables-only). `tables` is now the only carrier the model sees, and the top-level pair is read-path only.)*

### B.1 Why not derived from completed intents

The mapping exists (§Current state, point 4), and deriving would be strictly less for the model to get wrong. It is still wrong, for four reasons, the first of which is decisive:

1. **The evidence query is the wrong query.** `answer_with_table.py:26-30` and `query_page.py:9-12` both state it: a designated `sql` is deliberately **not** required to be one the agent ran, *because the executed query usually carries a `LIMIT` the agent chose for its own reading and paging needs the un-capped shape*. Deriving the table from the evidence call would page the agent's own capped query and silently truncate the grid — a new silent failure, introduced to avoid a payload field.
2. **`getTableSchema` is intent-taggable** (`mcp/tool_schema.py:229`, `composite/analysis_state.py:170`). A metadata intent's evidence is a schema fetch, which has no pageable SQL. Derivation would need a per-tool exclusion list — a hand-enumerated set, which is the defect class README findings 6, 9, 15 and 18 all belong to.
3. **A `blocked` intent also carries evidence** (`analysis_state.py:999-1004`), and that evidence is a *denied or empty* call. Derivation would have to exclude it, or render the denial as a table.
4. **The counts legitimately differ.** 04 §A permits one call answering several intents; the model may also answer three intents with two tables, or compose the presentation query at answer time.

**Derivation is kept as a check, not a source.** After designation, count completed intents whose evidence entry is a `runQuery`/`runBlueprint` and whose result is not represented among the designated tables, and emit `loop_answer_table_intent_uncovered {intent_count}` ([§L](#l-telemetry)). That is a signal an intent's result went untabled — it is **not** a refusal, because a scalar part of a multi-part answer correctly belongs in the prose.

### B.2 Why the array is the safe place for a flat schema, not the risky one

03 §C.3.1 measured the failure this decision has to survive: the model **cannot omit keys**. It emits every declared property and fills the unused ones with placeholders — `""` for a string, the first enum member for an enum. Six live `updateAnalysisState` calls were rejected for exactly that.

An item of `tables` is `{sql, blueprint_id, caption}` — three strings, no enum. The placeholder serialisation is `{"sql": "", "blueprint_id": "bp-x", "caption": ""}`, and **that exact shape is already handled**: `clean_answer_sql` (`answer_with_table.py:74-85`) and `clean_blueprint_id` (`:97-103`) map empty/whitespace to `None`, and `resolve_designation`'s docstring records that `sql=""` beside `blueprint_id` **is the form the live model actually emits** (`:122-124`). The normalisers were written against the observed serialisation and are already load-bearing on this exact payload.

The residual risk is a wholly-placeholder ITEM (`{"": "", "": "", "": ""}` → resolves to nothing, names no blueprint). Rule: **drop it, count it** (`loop_answer_table_item_dropped {reason: "unresolvable"}`). Bounded by the cap ([§F](#f-decision-5--the-cap)).

### B.3 Why the singular stays

> **RETIRED 2026-08-13 by [§O](#o-amendment-2026-08-13--tables-only).** The conclusion below was overturned by a live measurement it did not have. It is kept verbatim rather than rewritten because the argument is still the right *shape* — do not break a working path for tidiness — and what killed it was evidence, not taste. The precedence rules in this section are unchanged and still describe the code: they now govern a read-path fold rather than a model-facing shorthand.

`sql=`/`blueprint_id=` at the top level is the shape that succeeds **6/6**. Removing it to force one canonical path would put the working case through an untested one for tidiness. It stays, and it is not deprecated.

**Precedence, mirroring the rule already in the code** (`answer_with_table.py:139`, "`sql=` wins when both are given: it is the more specific instruction"):

> `tables` wins **when it yields at least one resolvable item**. Otherwise the top-level `sql`/`blueprint_id` pair is used as the single item.

Never both: a model that fills `tables: [{blueprint_id: X}]` **and** `sql: <X's SQL>` gets one table, not two. Fallback rather than union also means a model that emits `tables: []` (an empty placeholder array — the array analogue of `""`) still gets its top-level designation, instead of silently losing its table.

### B.4 Normalisation order (one function, `resolve_answer_tables`)

Runs once per `answerWithTable` call, in `loop/agent_loop.py` where `_resolve_answer_sql` sits today:

1. Choose the source list per B.3.
2. Per item: `resolve_designation(item, blueprint_terminal_sql)` — the existing function, unchanged.
3. An item that names a `blueprint_id` and resolves to `None` **refuses the whole call** with the existing `_answer_table_blueprint_not_run` nudge (`agent_loop.py:522-545`), exactly as today. Dropping it instead would silently lose a deliverable's table, which is the failure this document exists to fix.
4. Dedupe on resolved SQL, first-occurrence order — the `turn_sql` discipline (`agent_loop.py:2099`).
5. Cap ([§F](#f-decision-5--the-cap)).
6. `caption`: `sanitize_text(caption, MAX_FIELD_CHARS)` (`runtime/sanitize.py:36,42`) — it is model-authored text re-entering the UI, the same posture 03 §D takes for `description`. Empty → `None`; the UI then falls back to the panel's static "Results" summary.

**The hooks fire per item, not per call.** `AnswerTableEvent` (`hooks/answer_table.py:71-88`) already carries a single `blueprint_id`/`sql` pair, so one event per designated table is the natural reading and needs no field change. Both seams stay dormant (`:40-42`).

**`_accumulate_answer_sql`'s last-wins rule survives verbatim**, promoted to the list: a second successful `answerWithTable` **replaces** the whole set rather than appending to it. The recorded rationale — *"a second `answerWithTable` means the model changed its mind about which query is the answer"* (`agent_loop.py:2200-2205`) — is exactly as true of a set as of a string, and appending would make "changed its mind" unexpressible.

---

## C. Decision 2 — `verification`

This is the decision most likely to be got wrong by assumption, so the reasoning is written out rather than the conclusion alone.

### C.1 What the field means today, precisely

`verification: {passed, method, grain_checked} | null`, one block per turn, set by `_accumulate_enrichment` (`agent_loop.py:2107-2119`) on **any** blueprint returning `status == "verified"`, never reset, never `passed: false`. The UI badges on `passed === true` and treats `null` as *no claim* — never as a warning: *"absence is information — never a warning. No 'failed' state exists"* (`ui/static/index.html:1112-1114`).

Two things follow that must be held apart:

- **(a) It is not bound to the answer table even at N=1.** Verified blueprint for i1 + hand-written answer query for i2 ⇒ green badge over an unverified grid. Pre-existing.
- **(b) At N>1, one flag over a mixed set is an over-claim** — the one the brief names.

A design that fixes only (b) leaves the field meaning "some blueprint was verified somewhere in this turn", which is not a statement about anything the user is looking at.

### C.2 The options

| | Behaviour | Verdict |
|---|---|---|
| 1. Status quo, one turn-level block | Green if any blueprint verified | **No.** Leaves (a) and (b) |
| 2. Per-table only, envelope field removed | Fully-verified single-table turns report `null` | **No.** `null` reads as *no claim*, so every existing verified turn silently downgrades — a break that loses information rather than adding it |
| 3. Per-table + **OR** roll-up | Green if any table verified | **No.** This is (b), restated |
| 4. Per-table + **conservative AND** roll-up over designated tables | Green only if every designated table is verified | **Chosen** |

### C.3 The rule

**Per table, derived, never carried over.**

- Each designated table carries `verification: {passed: true, method: "blueprint_gate", grain_checked: bool} | null`.
- It is derived at designation from the `runBlueprint` result that produced *that table's* `terminal_sql`. A `sql=` table is **always** `null` — there is nothing that verified it.
- **Mechanism:** `_capture_terminal_sql` (`agent_loop.py:831-853`) already reads `result_full` at dispatch, with `blueprint_id`, `terminal_sql` and `verify` all in hand. Widen its map from `blueprint_id -> terminal_sql` to `blueprint_id -> (terminal_sql, verification | None)`, captured at that one site. The table's SQL and its verification then come from the same run by construction and cannot be paired up wrongly later. The reload path (`runtime/app.py:862-891`) reads the same two keys out of the same `result_full`.
- **A hook-substituted query loses its verification.** If `resolve_unresolved` or `resolve_ephemeral` (`hooks/answer_table.py:151-157`) replaced the SQL, that table's block becomes `null`: the D56 gate verified a query that is no longer the one being paged. Inert today (both dormant), stated so a future hook cannot silently inherit a badge.

**Envelope roll-up: AND, over the designated tables only.**

- `verification` = `{passed: true, method: "blueprint_gate", grain_checked: <AND over the set>}` iff **every** designated table is verified and there is at least one; otherwise `null`.
- **Never `passed: false`.** Absence stays the only negative signal. A `false` would render as a red "verification failed" badge whose actual meaning is "one of these is a hand-written query" — which is not a failure at all, and would be read as one.
- Computing it over the **designated tables** rather than over "any blueprint that ran" is what fixes (a). **It is a behaviour change for existing single-table turns**: a turn that ran a verified blueprint and then answered with raw `sql=` **loses its badge**. That is the correction landing, and it will show up as a test diff — flagged here so it is not read as a regression.

**UI:** with ≥2 tables the block-level badge is suppressed and badges render per panel; with ≤1 it renders exactly as today. A roll-up badge sitting above per-table badges is redundant when they agree and contradictory when they do not.

`blueprint_use` (the chip, `ui/static/index.html:1096-1110`) follows the same shape: per table, plus the envelope's primary ([§E](#e-decision-4--envelope-back-compat)).

### C.4 What per-table verification still does not claim

It inherits README finding 22 exactly: `blueprint_gate` verifies the result **shape** against the blueprint's own declared `result_grain` — that the blueprint did what it claims, never that what it claims is what was asked. Three green badges are three shape checks, not three correct answers.

---

## D. Decision 3 — D44 provenance on reload

### D.1 What is actually there today

The reload path builds `answer_sql_by_turn` from the **raw** trail (`session_history.py:138-143`) and surfaces it only when that turn's assistant message survived `filter_messages` (`:167-169`, `:84`). The assistant message's provenance is the fail-closed turn union (`agent_loop.py:1445-1487`). So the granularity of the drop is **the turn**, and the `answerWithTable` entry's own `frozenset()` provenance (`answer_with_table.py:187`) is trivially in scope and does no gating at all.

### D.2 The mechanism

**Per-table provenance, computed at designation, persisted, read by both paths.**

- At designation, per resolved table: `capture_provenance("runQuery", {"sql": resolved}, catalog, session_id=…)` (`provenance/capture.py:90-117`). Same extractor, same fail-closed posture — a parse failure yields `None`, and `None` drops that table.
- Persist as one additive optional field on the `answerWithTable` `TrailEntry` — `answer_table_provenance: tuple[frozenset[tuple[str, str]] | None, ...] | None`, positionally parallel to the resolved tables, serialised in `to_doc`/`from_doc` (`session/models.py:182-197`). Additive-with-`None`-default is the established pattern (`authoritative` `:146`, `denial_detail` `:162`, `serves_intent` `:180`); legacy docs load unchanged.
- **The entry's own `provenance` stays `frozenset()`.** This new field is not the entry's provenance and must **not** enter `_compute_turn_provenance_union` (`agent_loop.py:1445`). That union is fail-closed: one unparseable designated SQL would collapse it to `None` and drop the turn's whole answer from every later replay — README finding 14's class, one field over.
- Both the live path and `project_history` filter through the same pure predicate, `is_provenance_in_scope(table_prov, column_scope)` (`context/scope_filter.py:66`). Persisting one value that both read is deliberate: `resolve_designation` exists because reading `args["sql"]` in one place and both forms in the other silently dropped every blueprint designation, and *"reload is the only place that regression shows"* (`ui/static/index.html:1264-1268`).

Result: a scope narrowing drops the out-of-scope tables and keeps the rest.

### D.3 What this honestly buys, and what it does not

**It does not make the common case granular.** The turn union already contains every column every tool call in the turn read — including all N tables' columns. A narrowing that excludes table 2's columns drops the assistant message, and all N tables go with it, because the answer-survival gate is turn-wide and deliberately so. Loosening that gate is a D44 message-filter change and is **out of scope here**.

**What it does close is a fail-open on the read path.** A designated `sql=` need not have been executed this turn — that is the documented contract (`answer_with_table.py:26-30`). Its columns therefore appear in **no** trail entry's provenance, so the turn union does not cover them, and today `/session/history` will hand back a table that will simply 403 when the browser tries to page it. This is a **consistency** defect, not an entitlement hole: `/query/page` re-enforces scope at execution under the caller's own credentials (`runtime/app.py:914-919`), so nothing out-of-scope was ever readable. The gain is that the transcript stops offering a table it knows cannot be loaded — and that the per-table claim becomes *checked* rather than *assumed*, which is the posture the rest of D44 is written in.

Emit `history_answer_table_scope_dropped {table_count}` when it bites, so the gap between "message survived" and "tables survived" is visible rather than inferred.

---

## E. Decision 4 — envelope back-compat

**Additive `answer_tables`, primary still populated.** A break is permissible (nothing is in production) and is not taken, because it buys nothing.

```jsonc
{
  "answer_sql": "<answer_tables[0].sql>",          // unchanged meaning at N<=1
  "blueprint_use": { … },                          // = answer_tables[0].blueprint_use
  "verification": { … } | null,                    // AND roll-up, §C.3
  "answer_tables": [
    {"sql": "…", "caption": "Headcount by department",
     "blueprint_use": {"blueprint_id": "bp-…", "slots": {…}},
     "verification": {"passed": true, "method": "blueprint_gate", "grain_checked": true}}
  ]
}
```

- **The primary is the FIRST item, not the last.** Within one call the first item is the model's lead table. (Between calls, last-wins still applies to the whole set — §B.4.)
- **`answer_sql` is a derived projection of `answer_tables[0]`, computed in one place**, never accumulated independently. Two fields that can disagree is the real cost of going additive; deriving one from the other is what pays it.
- Four consumers keep working untouched at N≤1: the UI's single-panel route (`index.html:1225-1229`), `/session/history` (`session_history.py:167`), the resume seed (`agent_loop.py:1508`) and every pause path that carries `answer_sql` forward (`agent_loop.py:3169`, `:3328`).
- `/session/history` gains `turns[].answer_tables` alongside `turns[].answer_sql`, same additive shape.
- **UI:** `.result-table` (`index.html:697-711`) becomes a `<template>` cloned per table, with `renderAnswerTable` taking the list. Each clone keeps its own `offset` closure, so each grid pages independently — which is the whole reason `/query/page` needed no change.
- **Lazy fetch, and it is a net reduction.** The panel is already a collapsed `<details>` (`:697`, no `open` attribute) while `renderAnswerTable` fetches immediately (`:994`). Move the first fetch to the panel's first open, and expand the first table by default. N=1 goes from one eager query to zero-until-opened; N=8 goes from 8 concurrent warehouse queries at render to one.

---

## F. Decision 5 — the cap

**`MAX_ANSWER_TABLES = MAX_INTENTS` (8), imported from `composite/analysis_state.py:114`, not re-declared.**

**Why equal, not lower.** A cap below `MAX_INTENTS` re-creates the conflict this document exists to dissolve: a 5-intent turn under a 4-table cap is told *"you cannot show a table for every part"*, and the only remaining move is to merge or to drop one into prose — premise (1) of the unsatisfiable triple, returning through the payload instead of the prompt. Tying the two constants together means the ceiling can never be the thing that forces a merge, and importing rather than re-declaring means they cannot drift.

**Why 8 is affordable.** With lazy fetch (§E) N tables cost one query at render, not N. The wire cost is N SQL strings on one SSE frame, alongside `sql_executed`, which is already a list of every query the turn ran. The designation cost is N sqlglot parses for per-table provenance (§D), bounded and off the hot path.

**Overflow is truncated, not refused** — and this is the one place 03 §A.4's *"REJECT, never truncate"* precedent deliberately does not transfer. There, truncating rewrote a frozen `description` that enforcement depended on. Here, with the cap equal to `MAX_INTENTS`, exceeding it means the model designated more tables than it can possibly have intents — duplication or nonsense, not a legitimate request being clipped. Refusing costs the user a finished answer over the model's bookkeeping. Keep the first 8 after dedupe, emit `loop_answer_table_item_dropped {reason: "over_cap"}`.

---

## G. Decision 6 — prompt and tool-description wording

### G.1 The sketch

`## Presenting a table`, inserted **after** the UNCONDITIONAL bullet and **before** the `sql`/`blueprint_id` mechanics:

> - One table per part. If you answered three parts, send three tables: `tables: [{blueprint_id: "…"}, {blueprint_id: "…"}, {sql: "…"}]`, in the order you answered them, each with a short `caption` naming its part. Send the result you ALREADY produced for each part — a blueprint result goes in as its `blueprint_id`, unchanged (see Operating procedure). If one query you ran already covered two parts, that is one table, not two. A part whose answer is a single number still belongs in your prose, not in a grid of its own.

~480 chars. Prompt is **13,383** today against a ratified ceiling of 15,000 (`tests/runtime/test_prompt_routing_contract.py:435`); headroom 1,617.

`ANSWER_WITH_TABLE_TOOL_SCHEMA["description"]` (`mcp/tool_schema.py:331-348`, currently 1,186 chars) is amended identically — it is re-sent every round-trip beside the prompt, and a contradiction between them is live on every turn (01a §8's lesson; the same text is why README finding 24 had two edit sites).

### G.2 Why this cannot re-create the merge conflict — mechanically, not by tone

1. **Premise (1) is gone from the payload.** 01a §10's triple was: *one table per answer* + *never re-derive a blueprint result* + *two blueprints answer two same-grain parts*. With `tables`, (1) is false, so (2) and (3) coexist and there is nothing left to reconcile. The text never has to ask for a merge because the payload never forces one.
2. **No imperative mentions writing a query.** Every verb is *send* / *goes in as*. The one merge sentence — *"if one query you ran already covered two parts, that is one table, not two"* — is **descriptive of work already done**, past tense, and cannot route the model back to the SQL editor. Both reverted rules were imperative (*"write ONE query returning them side by side"*, *"MERGE ONLY PARTS…"*).
3. **It lands inside the existing rule, not beside it.** *"unchanged (see Operating procedure)"* cites the authoritative-result rule it must not outrank — README finding 24's stated general lesson, and the thing both reverted versions failed at.
4. **No worked example.** §10's example was the sharpest part of the defect: both halves were blueprint-covered in the seeded corpus, so the illustration taught the error. The payload sketch is the only illustration, and it names no domain.
5. **The scalar clause is carried forward.** 01a §10 identified it independently (zero-row answers are correctly 0/2 on this tool) and it is the one part of that section with no failure attached.

### G.3 The acceptance check, and the gap it does not cover

`test_the_prompt_never_instructs_re_deriving_a_blueprint_result_into_a_table` (`tests/runtime/test_prompt_routing_contract.py:221-257`) asserts the absence of seven phrases in `## Presenting a table`. **None of them appears above, so it stays green unchanged** — and its own docstring already anticipated this design: *"If a future multi-table `answerWithTable` makes merging legitimate, it must do so WITHOUT telling the model to re-query a blueprint result, and this test is the line"* (`:240-242`). That is the acceptance criterion; do not weaken it.

**The gap: that test reads `AGENT_SYSTEM_PROMPT` only.** It never inspects `ANSWER_WITH_TABLE_TOOL_SCHEMA["description"]`, which carries the same instructions to the same model on every round-trip. During this design's verification pass the two were momentarily out of step — the prompt reverted, the tool description not yet — and no test could have seen it. **Extend the same banned-phrase check over the tool description** as part of this deliverable. It is one assertion, and it closes the exact seam README finding 24 was fixed at twice.

---

## H. What changes in `tests/eval/`

**Less than the brief assumes, and the difference is worth recording.** No A1 test asserts `answer_sql` or the designation shape at all — `answer_sql` appears nowhere in `tests/eval/*.py`. The fixtures *encode* a single-table answer (`case-02-two-blueprints.yaml:82-87`, `case-04-three-intents.yaml:101-105`) but nothing reads it, so no existing assertion breaks.

**The harness needs no change.** `_parse_call` (`tests/eval/conftest.py:176-185`) validates only the four top-level keys `name`/`id`/`args`/`serves_intent` and passes `args` through opaquely, so a `tables:` array works as-is.

The work:

| Change | Where |
|---|---|
| Keep case-02/04 on the top-level `sql:` shorthand, unchanged | it is the 6/6 path and now has a regression test by accident — keep it that way |
| **New case-11 — two blueprints, two tables** | `tests/eval/fixtures/routing/case-11-two-tables.yaml`; asserts `len(answer_tables) == 2`, each with its own `blueprint_use`, and the AND roll-up |
| **New case-12 — mixed verification** | one blueprint table + one `sql=` table; asserts per-table blocks are `{passed: true}` and `null`, and that the envelope roll-up is **`null`** — the honest-reporting assertion, and the one that would have caught an OR roll-up |
| **New case-13 — narrowed-scope reload** | drives a turn, re-reads `/session/history` under a scope excluding table 2's columns; asserts per-table filtering, and asserts the turn-wide answer gate's dominance explicitly (§D.3) so a later reader does not mistake the limit for a bug |
| `expect.answer_tables: <n>` | `tests/eval/conftest.py` `RoutingCase.expect` is a free-form dict (`:163`, `:216`) — no loader change |
| **A2 gains L7** | *"Three deliverables ⇒ three tables"*, reported as a pass-rate. This is the only thing that can prove the fix: 07 §A's whole point is that a scripted suite cannot fail on a bad prompt, and every claim in §G is a claim about a live model |
| 07 §C fixture shape, §D case table, §G Done-when | `docs/decisions/release-1/07-evaluation.md` |

**The headline number to re-measure is 1/9.** Not "does `answer_tables` populate" — that is A1 and is definitionally true.

---

## I. Migration and rollout

Nothing is in production, so most of this is a sentence.

- **`SessionDoc` / `TrailEntry`:** one additive optional field with a `None` default (§D.2). Legacy documents load byte-identically — the third time this pattern is used (`authoritative`, `denial_detail`, `serves_intent`).
- **No data migration.** An old session replays with `answer_table_provenance = None` and no `tables` in `args`; `resolve_designation` handles it as it does today, `answer_tables` is a one-element list, and the UI takes the N=1 route. Identical rendering.
- **Wire:** additive. An old client ignores `answer_tables`, the same posture `assumptions` shipped with (`runtime/app.py:202-206`).
- **`SessionStore` Protocol: unchanged** — no new store method, so README finding 18's four-implementation trap does not apply here. Said explicitly because it is the first thing to check on any Release-1 change, and the answer being *no* is itself worth recording.
- **The one ordering constraint:** the prompt/tool-description change (§G) must land **with or after** the payload change, never before. A prompt that says `tables:` against a schema that has no such property produces a rejected argument on every multi-intent turn.

---

## J. What this does NOT fix

- **It does not make a multi-intent answer correct.** It makes it *renderable*. Whether the three tables hold the right numbers is 07 §A's **A3** (live + judge), explicitly deferred to the `11-testing.md` canary programme.
- **It does not check that the N tables are the N deliverables.** A table captioned "average salary by department" that pages a headcount query is accepted. §B.1's intent-coverage telemetry counts *un*tabled intents; it cannot tell a mislabelled table from a correct one.
- **It does not raise the multi-intent routing rate.** That is 01a §7's territory. This changes what happens *after* the work is done.
- **It does not make `verification` mean "the number is right"** — README finding 22, restated per table in §C.4. N green badges are N shape checks.
- **It does not fix the scratch-TTL limit; it multiplies it by N.** A composed blueprint's terminal SQL reads a session-scoped `scratch.*` table with a TTL (`answer_with_table.py:46-49`). With N tables, N grids can go stale independently, and a partial failure — three panels, one erroring — is a UI state that does not exist today. `references_scratch` (`hooks/answer_table.py:110-120`) fires per item, so the dormant seam is in the right place; nothing is done with it here.
- **It does not make the reload drop granular in the common case** — §D.3, stated at length because the brief expected otherwise.
- **It does not raise `MAX_INTENTS`.** Eight deliverables remains the ceiling on everything.

---

## K. Rejected alternatives

### K.1 A merge rule in the prompt — tried twice, measured, reverted twice

01a §§10-11, both marked REVERTED. The four live runs of one three-part question (*"Active headcount by department, average salary by department, and hires in the last 6 months"*):

| Run | Blueprints used | Outcome |
|---|---|---|
| pre-merge-rule | `runBlueprint ×3` | **3/3 intents completed** |
| pre-merge-rule, after the §8 `getBlueprint` gate | `runBlueprint ×2` + `runQuery ×1` | **3/3 completed** |
| merge rule v1 (§10, unqualified) | `runQuery ×3` | **timed out at 73s, no answer, 3/3 `pending`** |
| merge rule v2 (§11, narrowed) | `runQuery ×2`, **zero `runBlueprint`** | **timed out, no answer** |

v2 was written on the theory that v1 failed because it was unqualified. It made the turn **worse**: v1 still ran one blueprint, v2 ran none — on that run the model expanded all three blueprints via `getBlueprint` and ran none of them, hand-writing a merged `SELECT` twice, which is precisely the re-derivation v2's own first clause forbids. The `getBlueprint` gate was cleared by isolation on the same build: a single-deliverable question ran `getBlueprint > runBlueprint > answerWithTable`, `done`.

**No third wording exists**, because the instruction set is unsatisfiable, not badly worded (01a §10). Three individually-correct rules cannot all hold: *one table per answer*; *never re-derive a blueprint result*; *two blueprints each produce a table for two same-grain parts*. The model resolves it the only way that makes all three formally satisfiable — it drops the blueprints, so rule 2 has no object. That is a coherent reading of the prompt, not a misreading.

### K.2 Multiple `answerWithTable` calls per turn

Superficially free — no schema change at all. Rejected on three mechanics:

1. **The first successful call ends the turn.** `designated_answer_text` is set (`agent_loop.py:3246-3254`) and the turn returns once the batch drains. N tables would require N calls in ONE batch and a rewrite of the terminal exit.
2. **It would have to invert a documented semantics.** `_accumulate_answer_sql` is last-wins *by design*: *"a second `answerWithTable` means the model changed its mind about which query is the answer"* (`:2200-2205`). Making N calls mean N tables leaves "changed its mind" unexpressible.
3. **Two calls in one batch is already a defended-against misbehaviour.** 05 §C.2 exists solely because `[answerWithTable, answerWithTable]` would otherwise burn both finalization chances in one round-trip; there is an adversarial test for it. Making that shape the normal path collides head-on with enforcement.

And each call carries a **required** `answer` (`mcp/tool_schema.py:369`), so N calls means N final prose answers with no defined winner.

### K.3 Doing nothing

The cost is measured and specific: **1/9**. The prompt lever is spent (K.1), and the fallback shape — answering in prose — is the same shape that produced README finding 19's truncated markdown tables, where the user lost the grid *and* got a clipped table.

The honest counter is that the release is already large and this is new surface. The answer: it is one payload field, one resolver loop, one UI loop and one additive trail field — no new store method, no Protocol change, no migration, and one existing contract test that already stays green. The alternative on the table for the same problem (A3 answer grading) is an order of magnitude larger and does not fix this.

---

## L. Telemetry

Shape-only (D25). **Emitting an event does not publish its payload** — `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` (`observability/tracing.py:615-687`) is a strict key allowlist, and README finding 10 is what happens when that is forgotten.

| Event | Payload | Allowlist |
|---|---|---|
| `loop_answer_tables_designated` | `table_count`, `blueprint_table_count`, `verified_table_count` | **all three are new keys — add them** |
| `loop_answer_table_item_dropped` | `reason` ∈ `{unresolvable, duplicate, over_cap}` | `reason` already present (`:646`) |
| `loop_answer_table_intent_uncovered` | `intent_count` | already present (`:627`) |
| `history_answer_table_scope_dropped` | `table_count` | shares the new key above |

Captions, SQL and cell values never appear. `blueprint_id` is already allowlisted as corpus-authored (`:652`) if a per-item event ever needs it.

---

## M. Done when

- [x] `tables: [{sql?, blueprint_id?, caption?}]` on `ANSWER_WITH_TABLE_TOOL_SCHEMA`; top-level `sql`/`blueprint_id` unchanged and still honoured. **Superseded 2026-08-13 by [§O](#o-amendment-2026-08-13--tables-only):** `required: ["answer", "tables"]`, `minItems: 1`, and the top-level pair removed from the schema and kept only as a read-path fold, with a regression test loading a pre-§O document and asserting replay, the resume seed, cross-turn context and pageability all still work.
- [x] `resolve_designations` + `finalize_designations` in `composite/answer_with_table.py`, built on the **existing** `resolve_designation` — no second resolution path. (Placed beside `resolve_designation` rather than in the loop, as §B.4 sketched, because the resume seed and `project_history` must read the list the same way the loop wrote it — the exact divergence `resolve_designation` itself exists to prevent.)
- [x] `tables`-wins-when-it-carries-a-designation precedence; empty/placeholder array falls back to the top-level pair. See §N: the doc said "yields at least one *resolvable* item", which contradicts §B.4 step 3 for an unrun blueprint named inside `tables`; the implemented test is *carries a designation*.
- [x] Dedupe on resolved SQL; cap at `MAX_ANSWER_TABLES = MAX_INTENTS`, **imported** from `analysis_state.py`; overflow truncated with telemetry.
- [x] A `blueprint_id` item that did not run this turn still refuses the call via `_answer_table_blueprint_not_run` — never silently dropped.
- [x] Both `hooks/answer_table.py` seams fire **once per designated table**, still dormant; a hook-substituted query loses its chip and its badge.
- [x] Per-table `verification`, derived from the widened `_capture_terminal_sql` map (`blueprint_id -> BlueprintRun(terminal_sql, verification, slots)`, captured at ONE site); `null` for `sql=` tables and for hook-substituted queries.
- [x] Envelope `verification` is the **AND** roll-up **over designated tables only**; never `passed: false`. The single-table badge-loss case (verified blueprint + raw-`sql` answer) is asserted, not discovered. **Scoped to turns that designated a table** — see §N.
- [x] `answer_table_provenance` persisted additively; the entry's own `provenance` stays `frozenset()` and the new field is **excluded** from `_compute_turn_provenance_union` — asserted directly, driven with an unparseable designated query.
- [x] Live and reload filter tables through the same `is_answer_table_in_scope`; a per-table drop is asserted, and §D.3's turn-wide gate limit is asserted too. **Undetermined provenance is KEPT, not dropped** — see §N.
- [x] `answer_tables` additive on the SSE `result` frame and on `/session/history`; `answer_sql` derived from `answer_tables[0]` in exactly one place (`_answer_envelope`), and both projections share one serialisation (`AnswerTable.to_doc`).
- [x] Resume seed (`_compute_turn_answer_tables`) and every pause path reconstruct the whole list, not the first element. **This uncovered a live defect** — `_run_loop` accepted `seed_blueprint_terminal_sql` and never forwarded it, so a blueprint completing before an approval pause was not designatable after it. Fixed, with a regression test that fails without the forward.
- [x] Prompt bullet + identical tool-description amendment; prompt 14,227 → **14,750** against the 15,000 ceiling. **§O re-phrased both to the entries-only shape:** prompt 15,736 → **15,875** against the 16,000 ceiling (125 chars of headroom — thin, and the next addition should re-argue the ceiling rather than shave working instructions to fit under it); tool description 1,867 → 2,018.
- [x] `test_the_prompt_never_instructs_re_deriving_a_blueprint_result_into_a_table` green **unchanged**, and **extended to cover `ANSWER_WITH_TABLE_TOOL_SCHEMA["description"]`**.
- [x] eval cases 11-13 added; A2 gains L7; 07 §§C/D/G updated. The harness needed no change, as predicted.
- [x] Three new telemetry keys added to `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST`, with a test that reads them back off the span.
- [ ] UI: `.result-table` cloned per table, independent paging closures, first table expanded, the rest fetched on first open. *(Delivered separately; the backend change does not touch `ui/`.)*
- [x] **§O:** a zero-table `answerWithTable` is nudged (`ANSWER_TABLE_NO_TABLE_DESIGNATED`) instead of silently terminating the turn, and `answer_table_succeeded` is set from a resolved table rather than from a successful call — the two ends of one defect, found on two live-loop probes. See §O.5.
- [ ] **The 1/9 measurement is re-run on live multi-intent turns and reported.** Nothing else closes this — A2's L7 is the harness for it.

---

## N. Corrections

Seeded with the places the code contradicted the brief this document was written from; review corrections append here.

| Was | Now | Why |
|---|---|---|
| "Consider whether tables should be derived from the `serves_intent` binding rather than listed by the model" | **§B.1 — listed, with derivation kept as a check only** | The evidence call is the wrong query: a designated `sql` is deliberately not required to have been run, *because the agent's own query carries a `LIMIT` it chose for its own reading and paging needs the un-capped shape* (`answer_with_table.py:26-30`, `query_page.py:9-12`). Deriving would page the capped query and silently truncate every grid. `getTableSchema` evidence and `blocked`-intent evidence would each need a hand-enumerated exclusion — the defect class of findings 6, 9, 15 and 18 |
| "`verification` is currently one block per turn — with three blueprints one may verify cleanly and another not" | **§C.1 — true, and it is already not bound to the answer table at N=1** | `_accumulate_enrichment` (`agent_loop.py:2107-2119`) sets the block from ANY verified blueprint and never resets it; `"passed": True` is the only occurrence in the runtime (`:2114`). A verified blueprint for i1 plus a hand-written answer for i2 already renders a green badge over an unverified grid. Multi-table exposes an existing over-claim rather than creating one — so the roll-up is computed over **designated tables**, which is the actual fix |
| "Each table needs its own provenance, or a scope narrowing drops all tables or none, when it should drop only the out-of-scope ones" | **§D.3 — per-table provenance is necessary but not sufficient; the common case stays all-or-nothing** | The turn union (`agent_loop.py:1445`) already contains every table's columns, and the reload gate is the assistant message's survival (`session_history.py:167-169`). Narrowing to exclude one table's columns drops the message and therefore all tables. What per-table provenance genuinely closes is the read-path fail-open for a designated `sql=` that was never executed — a **consistency** defect, not an entitlement hole, since `/query/page` re-enforces scope at execution |
| "07's A1 fixtures assert a single-table answer shape" | **§H — they encode one; nothing asserts it** | `answer_sql` appears in no `tests/eval/*.py`. No existing assertion breaks, and `_parse_call` (`conftest.py:176-185`) passes `args` through opaquely, so the harness needs no change at all. The work is three new cases, not a migration |
| "`/query/page` is not a blocker — the UI can call it once per table" | **Confirmed, and the UI is the real work** | `/query/page` (`app.py:901`) and `build_page_sql` (`query_page.py:95`) need nothing. But `renderAnswerTable` (`index.html:966`) is written against ONE `.result-table` subtree found by `querySelector` (`:697-711`), so the panel must become a cloned template with a per-clone `offset` closure. Noted because "no paging change" reads as "no UI change", and it is not |
| Cap left open | **§F — equal to `MAX_INTENTS`, imported** | A cap below `MAX_INTENTS` re-creates premise (1) through the payload: a 5-intent turn under a 4-table cap is told it cannot show a table per part, and the only move left is to merge. Also: 03 §A.4's *"reject, never truncate"* precedent does **not** transfer, and §F says why |
| Prompt revert assumed complete in both places | **§G.3 — it is complete now, and no test could have caught it if it were not** | `test_the_prompt_never_instructs_re_deriving_a_blueprint_result_into_a_table` (`test_prompt_routing_contract.py:221`) reads `AGENT_SYSTEM_PROMPT` only, never `ANSWER_WITH_TABLE_TOOL_SCHEMA["description"]` — the second carrier README finding 24 had to fix by hand. Extending it is part of this deliverable |

### Corrections found while implementing

| Was | Now | Why |
|---|---|---|
| §B.3: "`tables` wins **when it yields at least one resolvable item**" | **`tables` wins when at least one item CARRIES A DESIGNATION** (a cleanable `sql` or `blueprint_id`, resolved or not) | The two rules disagree on one case, and it is not a corner: `tables: [{blueprint_id: X}]` where X never ran RESOLVES to nothing. Under the resolves-only test the source falls back to an empty top-level pair, the item vanishes and the model silently loses its table — which §B.4 step 3 exists to forbid. Under the implemented test it stays the source, survives as a refusal, and the model gets the retryable nudge it can act on. The two cases §B.3 argued about (`tables: []`, an all-placeholder array) behave identically either way |
| §C.3: envelope `verification` is `null` whenever fewer than one designated table is verified | **The roll-up governs turns that DESIGNATED a table; a turn with no answer table keeps its turn-level enrichment** | §C.2 rejected option 2 (per-table only) because *"`null` reads as no claim, so every existing verified turn silently downgrades — a break that loses information rather than adding it"*. That objection applies with full force to a turn that ran a verified blueprint and answered in PROSE: there is no grid to over-claim on, and downgrading it would delete the enrichment the blueprint approval-resume seed exists to carry across a pause (`test_approval_resume_final_outcome_carries_enrichment` asserts it by name). Both over-claims §C.1 names are still fixed, because as soon as there IS a designated table the derived value wins outright — including when it is `None` |
| §D.2: "a parse failure yields `None`, and `None` drops that table" | **Undetermined per-table provenance is KEPT; only a table PROVEN out of scope is dropped** | `is_provenance_in_scope(None, scope)` is `False` for every scope including the empty allow-all one, so dropping on `None` would delete every grid whose query the runtime's own extractor cannot parse — an uncatalogued table reference is enough — for queries `POST /query/page` executes perfectly well today. It would buy no access control doing it: §D.3 states the posture itself, that this is a **consistency** defect and not an entitlement hole, precisely because `/query/page` re-enforces scope at execution under the caller's own credentials. Fail-closed here would be a new silent failure introduced to satisfy a posture rather than a read |
| §B.4: `resolve_answer_tables` lives "in `loop/agent_loop.py` where `_resolve_answer_sql` sits today" | **The pure resolver lives beside `resolve_designation` in `composite/answer_with_table.py`; the loop method wraps it with hooks, provenance and telemetry** | The resume seed and `project_history` must read the designation back exactly as the loop wrote it. A resolver private to the loop would give them a second reading — which is the divergence `resolve_designation` was extracted to prevent, and *"reload is the only place that regression shows"* |
| — | **`_run_loop` never forwarded `seed_blueprint_terminal_sql` to `_run_loop_body`** | Found while widening the map. The blueprint approval-resume built the seed and `_run_loop` accepted it as a keyword, then dropped it — so `answerWithTable(blueprint_id=X)` naming the blueprint that completed BEFORE the pause resolved to nothing, and the model got "you have not run it this turn" for a blueprint it had run. Silent in both directions: `_run_loop`'s docstring argues the explicit signature mirror catches a typo'd kwarg at any CALL site, which it does — and which is exactly why an omission at the FORWARDING site went unseen |

---

## O. Amendment 2026-08-13 — tables-only

**§B.3 is retired. The top-level `sql`/`blueprint_id` pair is removed from the model-facing schema; `tables` is the single carrier and is REQUIRED. A single-table answer is a one-entry list.**

This reverses a decision this document argued for at length. What overturned it is not an argument — §B.3's argument is still sound on its own terms — but a live call that the argument did not have.

### O.1 The evidence

R7 q1's actual `answerWithTable` invocation:

```jsonc
{"answer": "…", "sql": "", "blueprint_id": "bp-…", "tables": []}
```

Four declared properties carrying **one field's worth of information**: two placeholder strings and an empty array beside the one value that meant anything. The model did not choose to send three designation fields; it could not do otherwise. 03 §C.3.1 measured exactly this and it is the premise §B.2 was written on — *the model emits every declared property and fills the unused ones with placeholders* — and §B.2 used it to argue the array was **safe**. It is; the normalisers handle it; nothing was refused. That was never the question.

The question §B.3 got wrong is what a second carrier COSTS when keys cannot be omitted. A model with two ways to name one table must fill both on every call, and must decide which one to mean. §B.3 treated the top-level pair as a free, already-working path kept beside the new one. It was not free: it was a permanent instruction to serialise a decision the model had already made, twice, in two places, on every terminal call of every turn.

**This is the same hazard class as 01a §14's `updateAnalysisState` slim-down, and the same fix.** There, `evidence_tool_call_id` and `reason_code` were retired because a model that cannot omit keys was emitting placeholders into fields enforcement then had to normalise away — six live calls rejected for it. Here, `sql` and `blueprint_id` at the top level are the same defect one tool over. That the two were found independently, five days apart, on the same measured behaviour, is the argument for reading the rule generally rather than patching each instance: **a declared property is a cost paid on every call, not an option offered.**

### O.2 What §B.3's argument was right about, and what it missed

> *"Removing it to force one canonical path would put the working case through an untested one for tidiness."*

Right in shape, wrong in fact, for two reasons:

1. **The single-table case is not going through an untested path.** `tables: [{sql}]` resolves through `resolve_designation` — the *same function*, on the *same mapping* — as the top-level pair did. §Current state point 2 and §B's load-bearing claim say so: an element of `tables` IS the mapping that function reads. There is one resolver, and it was already carrying both. The 6/6 path is one list index away from where it was, not on new code.
2. **It was not tidiness.** Tidiness would be the only motive if the second carrier were inert. It is not inert; it is emitted, with a placeholder, on every call.

### O.3 The folding rule

The top-level pair does not disappear from the RUNTIME, only from the schema. `resolve_designations` keeps reading it, under the rule §B.3 already specified — unchanged, and now describing a fold rather than a shorthand:

> `tables` wins when at least one of its entries **carries a designation**. Otherwise the legacy top-level `sql`/`blueprint_id` pair is folded in as **one entry**.

with the corollary already stated in §B.2, now doing more work:

> **A key carrying no information is ABSENT.** `sql: ""` and `blueprint_id: ""` clean to `None`, so R7 q1's payload above resolves through its blueprint id and is not refused, and placeholder legacy fields beside a real `tables` array are not a conflict — there is nothing there to conflict.

**Non-empty legacy fields with an empty `tables` are FOLDED, never refused.** A stale-context model must not lose its answer over a payload detail the runtime reads perfectly well; and `tables` being required makes `tables: []` *more* likely than before, not less, since a model with nothing to put there must still emit the key. Refusing would cost the user a finished answer to enforce a shape the runtime does not need enforced.

**Never a union**, unchanged from §B.3: a call carrying both gets one table.

### O.4 Why the read path can never drop the fold

There is **no migration and no expiry date**, and the reason is not caution:

- Every `answerWithTable` trail entry persisted before this change carries the top-level shape and no `tables` key.
- Those entries are **successful**, and a successful `answerWithTable` entry is not a fire-and-forget record. It **replays cross-turn** into model context, it **seeds a resumed window** (`_compute_turn_answer_tables`), and it **rebuilds the transcript** a reloaded browser renders (`session_history.project_history`).
- A read path that understood only `tables` would fail **silently in all three at once**: the grid vanishes from the reload, the resume returns `answer_sql=None`, and nothing reports it. That is the defect `resolve_designation` was extracted to prevent, and *"reload is the only place that regression shows"*.

Verified read-path consumers, all of which resolve through `resolve_designations` and therefore handle both shapes: `_compute_turn_answer_tables` / `_compute_turn_answer_sql` (resume seed), `session_history.project_history` (reload), `_resolve_answer_tables` (live), and `AnswerWithTableTool.run`'s own confirmation flag. `_capture_terminal_sql` reads `runBlueprint` results and never the answer args; `filter_trail`'s status-gated exemption and `POST /query/page` are both shape-blind — the first keys on status, the second takes SQL from the client.

**The answer-shape gate is shape-agnostic too**, and this was checked rather than assumed: both sites that set `answer_table_succeeded` — the in-batch one and the trail-walk seed — read the tool NAME and the STATUS, never `args`. A gate that stopped recognising a legacy designation would charge a finished turn an extra round-trip and tell the model to table what it already tabled, which is the false-positive half 05 §J is most exposed to.

### O.5 The defect the slim-down surfaced — a zero-table `answerWithTable`

Found in review, on two live-loop probes, and fixed as part of this amendment. It PRE-DATES §O — an empty `tables` beside an empty top-level pair was always possible — but §O makes it likelier, because `tables` is now required and a model that cannot omit a declared key sends `[]`.

| Probe | Call | Before |
|---|---|---|
| **A** | `{answer: <prose>, tables: []}` on a turn holding a 6-row result | `status=done`, no table, **zero events, zero logs**. The call succeeded, carried non-blank prose, and TERMINATED the turn through exit #2 — which the 05 §J answer-shape gate does not watch, since that gate lives on exit #1 (a model turn with no tool calls) |
| **B** | mid-turn `{answer: "", tables: []}` | Set `answer_table_succeeded = True` with **zero designations**, disarming the gate; the model's later bare-prose finish then passed unrefused |

One flag, two silent paths, and probe A is the worse one: the user asked for a breakdown, the runtime held six rows of it, and the answer was prose — with nothing anywhere reporting that a table had been lost.

**Both ends are fixed, and neither half is sufficient alone.**

1. **The terminal site nudges.** A successful `answerWithTable` that carries no designation at all, on a turn with `multi_row_answer_calls > 0`, becomes the retryable `ANSWER_TABLE_NO_TABLE_DESIGNATED` — modelled exactly on `_answer_table_blueprint_not_run`, which is the same failure one step later (there the model named a table that could not be resolved; here it named none).
2. **The flag is set from substance.** `answer_table_succeeded` requires a resolved table, not merely a successful call. It reads the ACCUMULATOR rather than this call's resolution, so a later designation-free retry cannot re-arm the gate on a turn that already has its table.

**Three scopes, each of which is the false-positive protection:**

- `multi_row_answer_calls > 0` — a turn holding nothing multi-row has no table being withheld, and a zero-row *"none found"* answered in prose is CORRECT (live q6). Nudging it would charge a right answer a round-trip and tell the model to grid a number.
- **Non-blank `answer`**, mirroring the terminal condition — *a call that would not have ended the turn is not a finalization and must not be refused as one*, the rule the pending-intents refusal already follows at this exit. A blank-`answer` empty call terminates nothing; refusing it would spend the window's allowance on a habit call and leave the real prose finish unrefusable. Probe B is covered by the FLAG, not by the nudge.
- **`carried_designation`, not "zero tables resolved"** — an empty resolved list has two causes needing opposite handling. The model named nothing (nudge; it can fix that), or it named something the runtime then dropped for a reason the model cannot act on — out of scope, duplicate, over cap. Nudging the second would tell the model to fix a payload that was already correct.

**It shares the shape gate's allowance** (`kind="answer_shape"`), because it is the same complaint arriving through the other exit: without sharing, one mistake could be refused twice per window, once per exit. Grant spent ⇒ **the prose passes and the turn ends** — never a hard lock, the posture `ENFORCEMENT_EXHAUSTED` takes for intents. `loop_answer_table_empty_designation` is emitted where the fact is established rather than at the nudge site, so the behaviour stays visible after the allowance is gone; counting it at the nudge would under-report it precisely once it starts repeating.

**The trail-walk seed keeps its name+status test, deliberately.** Re-resolving persisted `args` there would be a third reading of the designation (the divergence `resolve_designation` exists to prevent) and would need a D46 KV de-reference per blueprint to answer correctly. The asymmetry errs false-NEGATIVE, which is the safe direction: it can only leave the gate disarmed on a turn whose `answerWithTable` succeeded in an earlier window, and such an entry can no longer be designation-free.

**And it is the FOURTH instance of the cross-turn model-text class** (`context/assembly.py`, README findings 9 and 11). Its `args` carry the refused draft answer, so the code joins `_STALE_CROSS_TURN_ERROR_CODES` — the set was extended rather than a third predicate added, exactly as that module instructs. It survives its own turn, which is the whole mechanism, and no later one.

### O.6 What this does not change

- **Not the multi-table feature.** Everything from §B.4 down — precedence, dedupe, the cap, per-table verification, per-table provenance, the envelope, the resume seed — is untouched. This is a payload slim-down inside a shape that already existed.
- **Not the 1/9 measurement.** §M's last open box is still open; a narrower payload is not evidence the model fills it.
- **Not the `answer` requirement**, and `tables` becoming required is the same argument: this tool exists to deliver a table, and an `answerWithTable` with no table is a terminal call that shows the user nothing.
- **Not `minItems: 1` into an enforcement.** These schemas are non-strict, so nothing rejects `tables: []` at the boundary. It is declared because it is the honest contract and the model reads it; the runtime's answer to an empty array is §O.3's fold, not a rejection.
