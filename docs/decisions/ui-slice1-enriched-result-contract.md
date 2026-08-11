# UI Slice 1 — Enriched `/turn` `result` event contract

**Status:** Contract-locked (no minted Dxx). Seam for parallel backend + frontend fan-out.
**Scope:** Enrich the SSE `result` event so the UI can render SQL, the result table,
blueprint-use, provenance/lineage, and a verification badge. Backend + frontend build against
*this file only*; neither needs to read the other's code.

Makes buildable the target table in `docs/08-ui.md` § "The enriched turn-result contract".

---

## 0. Architectural decision — **Option A (enrich `TurnOutcome`)**

**Recommendation: A. The agent loop populates new fields on `TurnOutcome`; `_outcome_to_dict`
just serializes them.** No session-store read-back, no `result_full_ref` de-ref, no scope
re-derivation.

Why A wins decisively — every datum is already in-process at outcome assembly:

| Field | In-process source at the `for tool_call` loop | Store read-back (B) needs |
|---|---|---|
| `provenance` | `_compute_turn_provenance_union(session_id, turn_index)` — already called at `agent_loop.py:831`, fail-closed, correct | same helper anyway |
| `sql` (runQuery) | `tool_call.arguments["sql"]` | `TrailEntry.args["sql"]` |
| `sql` (runBlueprint) | `tool_result.result_full["sql"]` (list) — `executor.py:407` | de-ref `result_full_ref` from KV (extra fetch) |
| `result_table` | `tool_result.result_preview` (a `ResultPreview`) | `TrailEntry.result_preview` |
| `blueprint_use.blueprint_id` | `tool_result.result_full["blueprint_id"]` — `executor.py:401` | de-ref `result_full_ref` |
| `blueprint_use.slots` | `tool_call.arguments["slot_bindings"]` (raw model args) | `TrailEntry.args["slot_bindings"]` |
| `verification` | `tool_result.result_full["verify"]` + `["status"]=="verified"` — `executor.py:402,408` | de-ref `result_full_ref` |

Option B is strictly more expensive: `verify`, `blueprint_id`, and blueprint `sql` live *inside*
`result_full`, which the trail only stores behind a KV pointer (`result_full_ref`) — so B pays an
extra per-entry fetch to recover exactly the object the loop already holds. B is never chosen.

**Key finding that shrinks the slice:** the audit brief expected `blueprint_id` + slots to be
"dropped on the COMPLETED path" and require threading onto `ExecCompleted`→`ToolResult`. Reading
the code, `blueprint_id`, `sql`, and the `verify` block are **already present inside
`ExecCompleted.result_full`** (`executor.py:400-418`) and survive verbatim onto
`ToolResult.result_full`. The **only** genuinely-absent datum is the *bound slot values*, and the
loop already holds the **raw** `slot_bindings` from `tool_call.arguments`. So **Slice 1 needs no
`ExecCompleted` / `ToolResult` signature change at all** — it reads what is already there. (Resolved
vs. raw slots is the one deferred nicety; see fork 3 and Feasibility.)

Mechanism: add turn-window-local accumulators next to the existing `retrieval_memo` /
`withheld_call_ids` (`agent_loop.py:805-809`) — fresh per turn, not persisted — and populate them
inside the `for tool_call` loop whenever a **successful** `runQuery` / `runBlueprint` entry is
built. Read them at **every** `TurnOutcome(...)` return site.

---

## 1. The locked schema — `result` event, 9 fields

4 existing (unchanged) + 5 new (all **additive, nullable**; an old client ignores unknown keys —
backward compatible).

| # | Field | Type | Nullable | Source of truth | UI renders into |
|---|---|---|---|---|---|
| 1 | `status` | `"done" \| "paused_ask_user" \| "paused_budget_cap" \| "stopped_hard_ceiling"` | no | `TurnOutcome.status` (`agent_loop.py:146-148, 235`) | turn state / spinner-off |
| 2 | `assistant_text` | `string \| null` | yes | `TurnOutcome.assistant_text` | the answer bubble |
| 3 | `pending_question` | `{question: string, options: string[] \| null} \| null` | yes | `TurnOutcome.pending_question` (`agent_loop.py:860`) | clarify chip / prompt |
| 4 | `tool_calls_made` | `int` | no | `TurnOutcome.tool_calls_made` | (debug / count) |
| 5 | `sql_executed` | `string[] \| null` | yes | ordered successful `runQuery`.args["sql"] + `runBlueprint`.result_full["sql"] | "show the query" panel |
| 6 | `answer_sql` | `string \| null` | yes | the model's `presentTable(sql=…)` argument (`composite/present_table.py`) | the paginated answer table, via `POST /query/page` |
| 7 | `blueprint_use` | `{blueprint_id: string, slots: {[name: string]: any}} \| null` | yes | `result_full["blueprint_id"]` + `tool_call.arguments["slot_bindings"]` | `Using blueprint …` chip |
| 8 | `verification` | `{passed: bool, method: "blueprint_gate", grain_checked: bool} \| null` | yes | `result_full["verify"]` + `["status"]` (`executor.py:402-417`) | passive **verified ✓** badge |
| 9 | `provenance` | `string[] \| null` (sorted, deduped `"database.table.column"`) | yes | `_compute_turn_provenance_union` (`agent_loop.py:533`) | lineage panel |

### Field rules

> **2026-08 revision.** `sql` was renamed **`sql_executed`** and `result_table` was **removed**
> and replaced by **`answer_sql`**. The two changes are one idea: separate *what the turn ran*
> from *what the answer IS*.
>
> `result_table` shipped the last successful query's `ResultPreview` — a fixed ~20-row window the
> user could not page past, chosen by the RUNTIME ("last successful query"), which is wrong exactly
> when a turn resolves values or probes a code space before answering. It also pushed the model
> toward transcribing rows into its prose, since the preview was the only table the user got.
>
> Now the model designates ONE query as the answer via the `presentTable` runtime tool, and the UI
> executes that itself against `POST /query/page` (`runtime/query_page.py`) with real paging. The
> endpoint adds no authority: it dispatches through the same scope-enforced `runQuery` path under
> the caller's own JWT. The designation is **advisory** — a model that forgets leaves `answer_sql`
> null, which is the right default for a scalar answer and a degradation for a table one.
>
> Migration: `result_table` is **gone, not nulled**, so a client keying on it fails loudly instead
> of silently rendering an empty table. `GET /session/history` is unchanged and still carries a
> per-tool-call `result_table`.

- **`sql_executed`** (fork 1): a **list** of SQL strings executed this turn, **in execution order**, from
  successful (`status=="ok"`) `runQuery` and `runBlueprint` trail entries only. Deduped
  **preserving first-occurrence order** (a turn re-running the identical string shows it once).
  Empty handling: a turn that ran **no** successful query → `null` (not `[]`), so the UI can treat
  "no SQL panel" and "empty SQL" identically. `runBlueprint` contributes its `result_full["sql"]`
  list (already a list; Slice B = one node → one string).
- **`answer_sql`** (fork 2): the **single** query the MODEL designated as the answer, via
  `presentTable`. **Last designation wins** (a turn has one answer table; a second call means the
  model changed its mind), and a call whose `sql` cleans to `None` leaves the previous designation
  intact rather than clearing it. `null` when the answer is a scalar/single row — or when the model
  simply did not call the tool. It is seeded across both resume paths from the trail
  (`_compute_turn_answer_sql`), so a designation made before an askUser / blueprint-approval pause
  survives it. The rows themselves are **no longer on the result event** — the UI fetches them a
  page at a time.
- **`blueprint_use`** (fork 3): **null unless a blueprint produced the answer** (a successful
  `runBlueprint`). Shape `{blueprint_id, slots}`. `slots` = the model-supplied
  `slot_bindings` map (raw). **No `version` field** — no version concept exists in the code.
- **`verification`** (fork 4): **null unless a blueprint produced the answer.** `method` is the
  constant `"blueprint_gate"` (the only verification path that exists — see Feasibility RED note).
  `passed` reflects `result_full["status"]=="verified"`; in practice it is **always `true` when
  present** (a failed gate never returns a blueprint answer — it falls back to the raw loop, so no
  `blueprint_use`/`verification` is emitted). `grain_checked` (from `verify.grain_checked`) lets the
  UI distinguish a *grain-verified* answer from one where the grain probe was vacuously skipped.
  **No LLM reasoning text** — the Slice-B gate is code-computed only (`verify.py`;
  `signature_checked` is `False`).
- **`provenance`** (fork 5): the USES-set as a **sorted, deduped list of `"database.table.column"`
  strings**, projected from the fail-closed per-turn union. Already scope-filtered upstream (each
  `TrailEntry.provenance` is the scope-enforced extractor output, `sqlparse/provenance.py:528` +
  `provenance/capture.py:59`). **JSON key = `provenance`** (matches `docs/08-ui.md` and the stored
  field name; "lineage" is the panel/UX label, not the wire key). Encoding: the union is
  `frozenset[(database.table, column)]`; join each pair as `f"{db_table}.{column}"`, then `sorted()`.
  `null` when the union is **undetermined** (any tool result had `None` provenance — fail-closed);
  `[]` when determined-empty (e.g. a pure-chat turn, or `SELECT 1`).

### Nullability by `status` (fork 6)

| `status` | 5,6,7,8,9 expected? |
|---|---|
| `done` (blueprint answered) | `sql` list, `result_table`, `blueprint_use`, `verification`, `provenance` all populated |
| `done` (raw-loop answered) | `sql` list + `result_table` + `provenance` populated; `blueprint_use`=`null`, `verification`=`null` |
| `done` (pure chat, no tools) | all five may be `null` (or `provenance`=`[]`) |
| `paused_ask_user` | best-effort: usually all `null` (paused before the query ran); if a query ran in an earlier window they may be populated |
| `paused_budget_cap` | best-effort partial; whatever succeeded before the cap |
| `stopped_hard_ceiling` | best-effort partial |

All five are **populated best-effort** — never load-bearing for correctness. A partial/errored turn
may carry none. The FE must treat every one of the five as possibly-`null` on every `status`.

---

## 2. Example payloads

### 2a. `status: done` — blueprint turn (all fields)

```json
{
  "status": "done",
  "assistant_text": "Headcount by department: Engineering 3, Sales 3, Ops 3.",
  "pending_question": null,
  "tool_calls_made": 1,
  "sql": ["SELECT department, count(*) FROM hr.employees GROUP BY department"],
  "result_table": {
    "columns": ["department", "headcount"],
    "preview_rows": [["Engineering", 3], ["Sales", 3], ["Ops", 3]],
    "row_count": 3,
    "truncated": false
  },
  "blueprint_use": {
    "blueprint_id": "headcount_by_dept",
    "slots": { "period": "2026-05", "department": "all" }
  },
  "verification": { "passed": true, "method": "blueprint_gate", "grain_checked": true },
  "provenance": ["hr.employees.department", "hr.employees.id"]
}
```

### 2b. `status: done` — raw-loop turn (no blueprint)

```json
{
  "status": "done",
  "assistant_text": "The average salary in Sales is $60,000.",
  "pending_question": null,
  "tool_calls_made": 2,
  "sql": ["SELECT avg(base_salary) FROM hr.employees WHERE department = 'Sales'"],
  "result_table": {
    "columns": ["avg_base_salary"],
    "preview_rows": [[60000]],
    "row_count": 1,
    "truncated": false
  },
  "blueprint_use": null,
  "verification": null,
  "provenance": ["hr.employees.base_salary", "hr.employees.department"]
}
```

---

## 3. Backend change list (ordered, file-level)

1. **`runtime/loop/agent_loop.py` — accumulators.** Beside `retrieval_memo`/`withheld_call_ids`
   (`~805-809`), add turn-window-local locals: `turn_sql: list[str] = []`,
   `primary_preview: ResultPreview | None = None`,
   `blueprint_use: dict[str, Any] | None = None`,
   `verification: dict[str, Any] | None = None`. (These persist across `while` windows within the
   one `run`/`resume` call, same as the existing memos.)
2. **`agent_loop.py` — populate inside the `for tool_call` loop** (right after the `TrailEntry` is
   built, `~929-941`), gated on `tool_result.status == "ok"`:
   - `runQuery`: append `tool_call.arguments.get("sql")` to `turn_sql` (skip falsy/dupes);
     set `primary_preview = tool_result.result_preview`.
   - `runBlueprint`: `rf = tool_result.result_full or {}`; extend `turn_sql` with `rf.get("sql", [])`;
     set `primary_preview = tool_result.result_preview`;
     set `blueprint_use = {"blueprint_id": rf.get("blueprint_id"), "slots":
     dict(tool_call.arguments.get("slot_bindings") or {})}`;
     if `rf.get("status") == "verified"`: set `verification = {"passed": True,
     "method": "blueprint_gate", "grain_checked": bool(rf.get("verify", {}).get("grain_checked"))}`.
   - Dedup `turn_sql` preserving first-occurrence order before it leaves the loop.
3. **`agent_loop.py` — enrich `TurnOutcome`.** Add 5 fields to the frozen dataclass (`~231-238`):
   `sql: list[str] | None`, `result_table: ResultPreview | None`,
   `blueprint_use: dict[str, Any] | None`, `verification: dict[str, Any] | None`,
   `provenance: frozenset[tuple[str, str]] | None`. Give all `= None` defaults so the existing
   construction sites stay valid, then pass the accumulators at **every** `TurnOutcome(...)` return
   (`~845, 867, 955, 973`, and the resume-path returns). For `provenance`, reuse
   `await self._compute_turn_provenance_union(session_id, turn_index)` at the `done` return (it is
   the single fail-closed source of truth; compute once, do not re-derive in-loop).
4. **No `ExecCompleted` / `ToolResult` / `RunBlueprintTool` change required** for Slice 1
   (blueprint_id, sql, verify already ride `result_full`; raw slots ride `tool_call.arguments`).
   *Deferred (not this slice):* if resolved-slot fidelity is later wanted, add a
   `result_full["slots"]` field in `executor.py` and prefer it over the raw args — additive,
   reversible.
5. **`runtime/app.py` — `_outcome_to_dict`** (`139-145`): serialize the 5 new fields.
   `sql`/`blueprint_use`/`verification` pass through as-is; `result_table` →
   `outcome.result_table.to_doc() if outcome.result_table else None`; `provenance` →
   `sorted(f"{db}.{col}" for db, col in outcome.provenance)` when not `None`, else `null`.

Everything else on the SSE path (`_stream_turn`, `_format_sse`, error framing) is unchanged.

---

## 4. Frontend contract (parse + render, no backend read needed)

Parse the `result` SSE event's `data` JSON. All 5 new keys are **optional** — guard every one.

- **`sql: string[] | null`** → "Show the query" panel. Render each string in a `<pre>`/code block,
  in order. If `null` or empty, hide the panel.
- **`result_table: {columns, preview_rows, row_count, truncated} | null`** → expandable table.
  Header from `columns` (may be `[]` for non-tabular results — then render `preview_rows` as raw
  cells). Body from `preview_rows` (array of row-arrays, cells are heterogeneous JSON scalars).
  If `truncated` **or** `preview_rows.length < row_count`, show "showing N of `row_count` rows".
  Charting is out of scope (lib TBD — `docs/08-ui.md` open question).
- **`blueprint_use: {blueprint_id, slots} | null`** → the chip
  `Using blueprint {blueprint_id} · {k}={v} · …` iterating `slots`. Null → no chip.
- **`verification: {passed, method, grain_checked} | null`** → passive badge. `passed===true` →
  quiet **"verified ✓"**. Null → **render nothing** (absence is itself information — never a
  warning, never a pause, never a prompt). `grain_checked===false` may render a subtler tick if the
  design wants to distinguish grain-verified from grain-skipped; optional.
- **`provenance: string[] | null`** → lineage panel: a list of `database.table.column` chips
  (already sorted). Null → hide the panel (undetermined). `[]` → optional "no tables read" note.

FE must remain correct when a field is `null` on *any* status, and when the event carries only the
4 original keys (old backend / partial turn).

---

## 5. PII / scope safety note

- **Cell values are allowed on `result`, barred on `progress` (D25).** `result_table.preview_rows`
  and `provenance` carry the caller's **own** scope-filtered answer data — the very rows they asked
  for, already restricted to their `column_scope`. This is the deliverable, returned once, to the
  authenticated session. The D25 rule that strips cell values from **`progress`** events (and from
  Phoenix spans) exists because those are **telemetry/observability** surfaces with a wider audience
  and a longer retention; they show step/shape only. Distinct surfaces, distinct rules — stating it
  explicitly so a reader does not "fix" `result` to match `progress`.
- **Provenance stays within `column_scope`.** Each `TrailEntry.provenance` is the output of the
  scope-enforced extractor (`sqlparse/provenance.py:528`), captured only on successful, in-scope
  tool calls (`provenance/capture.py:59`); the union cannot name a column outside the caller's
  scope. `blueprint_use.slots` are the caller's own inputs. Nothing here widens the trust boundary.

---

## 6. Feasibility (per field)

| Field | Rating | Reason |
|---|---|---|
| `provenance` | **GREEN** | Already computed at `agent_loop.py:831` via `_compute_turn_provenance_union`; fail-closed union in-hand. Only a projection (`frozenset` → sorted strings) is new. |
| `result_table` | **GREEN** | `tool_result.result_preview` (`ResultPreview`) is in-process each iteration; reuse `.to_doc()`. No new shape. |
| `sql` | **GREEN** | runQuery → `tool_call.arguments["sql"]`; runBlueprint → `result_full["sql"]` (`executor.py:407`). Both in-process. Only list-accumulation + dedup is new. |
| `blueprint_use` | **YELLOW (minor, fully scoped here)** | `blueprint_id` is in `result_full` (GREEN); **slots** ship as the *raw* model `slot_bindings` from `tool_call.arguments` — GREEN to read, but they are the model's inputs, which may differ from post-resolution *bound* values (e.g. a `resolve_via` expansion or a defaulted slot). Slice 1 ships **raw** slots (buildable now, no executor change). Resolved-slot fidelity is a deferred additive enhancement (`result_full["slots"]`). Does **not** block fan-out. |
| `verification` | **RED (conceptual — scope corrected, not blocked)** | The pass/fail datum **is** reachable (`result_full["verify"]`, `executor.py:408`) so the *plumbing* is GREEN. The RED is the brief's premise: the D56 gate runs **only inside the blueprint executor**. The **raw loop has no verification gate** — a raw-loop answer is simply unverified. And a **failed** blueprint gate never returns a blueprint answer; it degrades to the raw loop (`executor.py:389-396`). Consequences the FE/design must accept: (1) `verification` is **non-null only for a successful blueprint answer**; (2) `verification.passed` is therefore **effectively always `true` when present** — a `passed:false` badge never reaches a user; (3) there is **no `"raw_loop_fallback"` verdict** — that case is `verification: null`, i.e. "no badge." This corrects the `docs/08-ui.md` wording "the gate always runs." **It does not block the slice** — the badge is honest as "this specific answer came from a verified blueprint fast path; absence = not (verifiably) verified." If a broader "always verify every answer" guarantee is wanted, that is a **new raw-loop verification gate** — out of scope, flagged as a separate design item. |

**Fan-out safety:** nothing above blocks parallel backend/frontend work. The one shared contract
seam is this schema; the only judgment call inside it (raw vs resolved slots) is FE-invisible
(`slots` is `{name: value}` either way). BE and FE can build independently against §1/§2.

---

## 7. Test hooks

- **Both runtimes emit the new fields for free under Option A** — they share `create_app` →
  `_stream_turn` → `_outcome_to_dict`, and both run the *real* `AgentLoop`:
  - `scripts/run_ui_runtime.py` (scripted: `DemoMCPClient` + `DemoModelClient`) — its blueprint
    scenarios ("headcount by department", "bad headcount", "average tenure") already drive real
    `runBlueprint`/`runQuery` results, so `sql`/`result_table`/`blueprint_use`/`verification`/
    `provenance` populate from the **fake** tool results with **no manual field-faking needed**.
    Verify each scripted scenario's `result` event now carries the expected new keys (esp. the
    "bad headcount" verify-fail case → `verification: null`, raw-loop fallback).
  - `scripts/run_ui_runtime_real.py` (real OpenAI + MCP + ClickHouse) — populates from live tool
    results automatically; smoke-check one real blueprint answer and one raw-loop answer.
- **SSE contract tests to update** — `tests/runtime/test_app.py`: the `result`-event assertions at
  `~88-90, 184-191, 281, 306, 416, 449` currently assert only `status`/`assistant_text`/
  `pending_question`. Add assertions that (a) the 4 original keys are unchanged (backward compat),
  (b) the 5 new keys are present, (c) a blueprint scenario yields non-null `blueprint_use` +
  `verification.passed==true`, (d) a raw-loop scenario yields `blueprint_use==null` +
  `verification==null` + a non-null `sql`/`provenance`, (e) a paused turn tolerates all-`null`.
  Add one unit test for `_outcome_to_dict` projecting the `frozenset` provenance → sorted
  `"db.table.column"` strings and `ResultPreview` → `.to_doc()`.
```
