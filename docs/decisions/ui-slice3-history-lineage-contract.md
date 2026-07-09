# UI Slice 3 — provenance read endpoint + conversation transcript contract

**Status:** Contract-locked (no minted Dxx — reuses D44 scope, D5, D25). Seam for parallel
backend + frontend fan-out.
**Scope:** Add a scope-filtered `GET /session/history` read endpoint that projects the persisted
`SessionDoc` (all prior turns) into a `turns[]` transcript — each turn joining its `TurnMessage`
(question / answer / per-turn provenance union) to its `TrailEntry`s (per-SQL sql + result table +
per-SQL provenance) — then make `ui/static/index.html` a multi-turn transcript that rehydrates from
this endpoint on load and appends live turns thereafter. Backend, frontend, and BFF build against
*this file only*.

Makes buildable `docs/08-ui.md` § "Conversation transcript" and § "Provenance / lineage" (the
"provenance read endpoint" prerequisite, `docs/08-ui.md:214`).

---

## 0. Architectural decision — **pure read over the persisted `SessionDoc`, no KV de-ref**

**Recommendation: the endpoint reads the session doc that is already in Couchbase (`messages` +
`tool_trail`), runs the two existing D44 pure filters over it, and projects — it does NOT re-run the
loop and does NOT de-reference any `result_full_ref` KV pointer.** Every datum the transcript needs
is inline on the persisted doc:

| Transcript datum | Inline source on the persisted doc | Cite |
|---|---|---|
| turn question | `TurnMessage(role="user").content` | `session/models.py:88-91` |
| turn answer | `TurnMessage(role="assistant").content` | `session/models.py:88-91` |
| per-turn provenance union | `TurnMessage(role="assistant").provenance` | `session/models.py:92` |
| per-tool-call sql (**runQuery**) | `TrailEntry.args["sql"]` | `session/models.py:133` |
| per-tool-call result table | `TrailEntry.result_preview` (a `ResultPreview`, inline — **not** behind KV) | `session/models.py:137` |
| per-tool-call provenance | `TrailEntry.provenance` (per-SQL USES-set) | `session/models.py:136` |

The **one** datum *not* inline is a `runBlueprint` node's SQL text — it lives in `result_full["sql"]`
behind `TrailEntry.result_full_ref` (`session/models.py:138`), the KV pointer. This is the Slice-1
"Option-A vs read-back" tension (`ui-slice1-enriched-result-contract.md §0`) resurfacing on the read
path. **Decision: Slice 3 stays inline-only (no KV read-back).** A `runBlueprint` tool-call in the
history therefore carries its per-node provenance + result table + `blueprint_id` (from
`TrailEntry.args`) but `sql: null`. Recovering blueprint node SQL via `SessionStore.read_full_result`
(`session/store.py:75`) is a deferred, additive enhancement — it does not block the slice and is
FE-invisible (`sql` is nullable per tool-call either way). See Feasibility (YELLOW-1).

Why a pure read wins: no loop re-entry, no model call, no scope re-derivation, no KV fan-out — the
endpoint is O(doc size), stateless, and side-effect-free (a genuine GET). It reuses `_extract_credentials`,
one store read, and the two pure D44 filters verbatim.

---

## 1. The endpoint

| Property | Value |
|---|---|
| Method + path | **`GET /session/history`** |
| Auth | Reuse `_extract_credentials(authorization, session_id, settings)` (`app.py:109-132`) verbatim — requires `Authorization: Bearer <jwt>` + `X-Session-Id`, yields this request's `column_scope`. Same 401/400 semantics as `/turn`. |
| Store read | `SessionStore.get_or_create_session(session_id)` (`session/store.py:45`) → the full `SessionDoc` (`messages` + `tool_trail` + `pause_checkpoint`). An unknown session creates an empty doc → `turns: []` (never 404). |
| Side effects | **None.** Read-only; no CAS, no writes, no loop, no KV de-ref. |
| Response | `application/json` (not SSE — it is a one-shot read, unlike `/turn`). |

The route sits beside `/turn` (`app.py:541`) and `/turn/resume` (`app.py:570`) inside `create_app`,
sharing `_extract_credentials` and the injected `session_store`.

### 1.1 Response wire shape

```jsonc
{
  "session_id": "s0a1b2…",
  "turns": [                          // ordered by turn_index ascending
    {
      "turn_index": 0,
      "question": "headcount by department?",   // user msg content — ALWAYS present
      "answer": "Engineering 3, Sales 3, Ops 3.", // assistant content — null if withheld/paused/none
      "provenance_union": ["hr.employees.department", "hr.employees.id"], // per-turn union, or null
      "tool_calls": [
        {
          "tool_name": "runQuery",
          "sql": "SELECT department, count(*) FROM hr.employees GROUP BY department",  // null for runBlueprint (§0)
          "result_table": {            // ResultPreview.to_doc(), or null
            "columns": ["department", "headcount"],
            "preview_rows": [["Engineering", 3], ["Sales", 3], ["Ops", 3]],
            "row_count": 3,
            "truncated": false
          },
          "provenance": ["hr.employees.department", "hr.employees.id"]  // per-SQL, sorted "db.table.column"
        }
      ]
    }
  ],
  "pending_question": null            // optional: mirrors an unconsumed pause checkpoint (§1.4)
}
```

Field encodings mirror Slice 1 exactly (no new shapes):
- `provenance_union` / per-call `provenance`: `sorted(f"{db}.{col}" for db, col in prov)` when the
  `frozenset[(db.table, column)]` is not `None`, else `null`, else `[]` when determined-empty —
  **identical projection to `_outcome_to_dict`** (`app.py:154-158`).
- `result_table`: `ResultPreview.to_doc()` (`session/models.py:44`) or `null` — verbatim Slice-1
  field 6 shape.

### 1.2 Which `TrailEntry`s become `tool_calls`

Only entries surviving `filter_trail` (§2) — which, because denied/errored entries carry `provenance
== None` (`context/scope_filter.py:14-17`; the dispatcher never sets a preview on a non-`"ok"`
result), means **only successful, in-scope tool calls** appear. History shows what the answer read,
not the model's dead ends. (Contrast the LIVE loop, which exposes current-turn denials to the *model*
via the `current_turn_index` exemption — that exemption is **not** used here, §2.)

### 1.3 A turn with no tool calls (pure chat)

`tool_calls: []`, `provenance_union: []` (determined-empty — a pure clarification/chat turn yields
`frozenset()`, `agent_loop.py:568-569`), `answer` present. FE renders question + answer, no
SQL/table/lineage panels.

### 1.4 A paused turn (awaiting resume)

A paused turn wrote its **user** message but **no assistant** message yet (`agent_loop.py:996-1001`
returns before the assistant append at `958`); the pending question lives on
`SessionDoc.pause_checkpoint`, not in `messages`. Rendering: the turn appears with `answer: null`
(indistinguishable on the wire from a scope-withheld answer — intentional, fail-closed: history never
signals that a withheld answer *existed*). To let a **reloaded** page re-open the ask-user prompt and
resume, the response carries a top-level `pending_question` mirroring the unconsumed checkpoint
(`doc.pause_checkpoint.pending_question` when `pause_checkpoint and not consumed`, `app.py:585`),
`null` otherwise. `pending_question` is model-authored text (not warehouse-derived), so it is not
scope-gated. This top-level field is a small **nice-to-have** for resume-after-reload; a first cut
may ship it `null` always and defer resume-rehydration.

---

## 2. D44 scope-filter — load-bearing, runs BEFORE any serialization

The endpoint MUST filter the persisted doc against **this request's** `column_scope` *before*
projecting. A past turn's answer or tool-call whose provenance is not ⊆ the caller's *current* scope
— or is `None` (undetermined) — is fail-closed dropped. This is the same guarantee the live replay
path enforces at `agent_loop.py:550` (`filter_messages(doc.messages, column_scope)` before the model
sees prior turns); the history endpoint is the **read-surface** sibling of that gate. Rationale:
`session/models.py:66-86` — a prior turn's answer ("Jane Doe's salary is $85,000…") must never be
replayed once `column_scope` narrows past what that answer was derived from. A read endpoint that
skipped this would be a scope-bypass channel around the very filter the loop applies.

Two pure filters, applied independently (both order-preserving, no I/O):

| Filter | Cite | Drops |
|---|---|---|
| `filter_messages(doc.messages, column_scope)` | `context/scope_filter.py:132` | assistant `TurnMessage`s whose `provenance ⊄ scope` or is `None`. **User messages always kept** (no warehouse data, `scope_filter.py:127-128`). |
| `filter_trail(doc.tool_trail, column_scope)` | `context/scope_filter.py:86` | `TrailEntry`s whose `provenance ⊄ scope` or is `None`. **`current_turn_index` is NOT passed** (`None`) — there is no in-progress turn in a read; every entry gets the strict `is_entry_in_scope` check, `scope_filter.py:109-118`. |

Core subset/`None` semantics (`is_provenance_in_scope`, `scope_filter.py:58-78`): `column_scope`
empty (`[]`) = allow-all → everything determined is kept; non-empty = allowlist (scratch pairs
exempt, session-gated); `provenance is None` → **always dropped**, even under allow-all (fail-closed).

### 2.1 What is withheld vs. what drops the whole turn

The two filters run **independently**, then the projection joins the survivors by `turn_index`. The
resulting granularity:

| Situation | Effect on the turn in `turns[]` |
|---|---|
| Assistant msg dropped (answer union ⊄ scope, or `None`) | `answer: null`, `provenance_union: null`. **Question still renders** (user msg always kept). |
| A tool-call's provenance ⊄ scope, or `None` | that entry is **omitted** from `tool_calls[]` — its `sql`, `result_table`, and `provenance` all vanish together. |
| Pure-chat turn's assistant msg dropped | question-only turn (answer null, tool_calls []). |
| **Whole turn dropped** | **Never** as long as the turn recorded a user question — the user message always survives, so the turn always renders at least its question. Field-level withholding (answer + individual tool-calls) is the mechanism; there is no separate "drop the whole turn" path. |

Rationale for question-only turns (not whole-turn drop): the user's own question carries no
warehouse-derived data (`scope_filter.py:127-128` keeps it unconditionally), so showing "you asked X"
under a since-narrowed scope leaks nothing while preserving conversational continuity — this is
exactly the asymmetry `filter_messages` already encodes. The withheld answer's *content* and its
provenance union are gone; only the caller's own prior input remains.

### 2.2 PII / scope safety (D25)

`result_table.preview_rows` and both provenance lists carry the caller's **own** scope-filtered answer
data — allowed on a `result`/read surface, exactly as Slice 1 §5 established, and **barred on
`progress`/spans** (D25). History is returned once, over the authenticated session, restricted to
this request's `column_scope` by the filters above — a *narrower* surface than the live `/turn`
result (it re-applies the *current* scope to *past* turns). Nothing here widens the trust boundary.

---

## 3. Per-SQL lineage breakdown — decision

**The history endpoint carries per-tool-call (per-SQL) lineage; the LIVE Slice-1 lineage panel stays
union-only.**

- **History (this slice):** each `tool_calls[]` entry pairs its `sql` (the query) with its
  `provenance` (the columns *that* query read) — a genuine per-SQL breakdown, sourced from the inline
  per-`TrailEntry` `provenance` + `args["sql"]`. A turn's `provenance_union` (the assistant message's
  union) is *also* returned, so the UI can show both "everything this turn read" and "which query read
  which columns."
- **Live panel (Slice 1) — UNCHANGED.** The `/turn` `result` event keeps emitting only the per-turn
  `provenance` union (`app.py:154-158`), rendered by `renderProvenance` (`index.html:626-648`). We do
  **not** add per-SQL to the live event. Reason: doing so would reopen and re-version the Slice-1
  wire contract for marginal live value, and the per-SQL join (sql ↔ its provenance) is already
  available for any turn via `GET /session/history` immediately after it completes. Keeping the live
  event frozen preserves the parallel-fan-out boundary Slice 1 locked.

Net: per-SQL lives on the read endpoint; the live panel is untouched.

---

## 4. BFF proxy — `GET /api/history`

Add a JSON (non-streaming) proxy to `ui/server.py`, mirroring the existing `_proxy_inbox` pattern
(`ui/server.py`, the JSON sibling of `_proxy_stream`):

```python
@app.get("/api/history")
async def history(session_id: str) -> JSONResponse:
    jwt = _jwt_for_session(session_id)          # reuse existing helper
    headers = {"Authorization": f"Bearer {jwt}", "X-Session-Id": session_id}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(f"{RUNTIME_URL}/session/history", headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Runtime unreachable: {exc}") from exc
    try:
        content = r.json()
    except ValueError:
        content = {"detail": r.text}
    return JSONResponse(status_code=r.status_code, content=content)
```

Notes: `session_id` arrives as a query param (GET) — the browser already holds `session_id` from the
session-mint flow; the **JWT stays server-side** (D82/D5), attached on the proxy hop exactly like
`/api/turn`. The BFF adds nothing new to the trust model — same `_jwt_for_session` lookup, same
byte-forwarding-of-status posture. Non-2xx runtime bodies propagate as-is so the browser's error
branch renders them.

---

## 5. Transcript UI — `ui/static/index.html`

The page becomes multi-turn: a `#transcript` container of **turn blocks**, one per turn, each holding
what today's single "Answer" section holds (question, answer, blueprint chip, verified badge, SQL
panel, result table, lineage panel). Two population paths, one render function.

### 5.1 DOM restructure

- Replace the singleton `<section aria-labelledby="answer-heading">` (`index.html:385-416`) with a
  `<section id="transcript" data-testid="transcript">` and a `<template id="turn-template">` cloned
  per turn. The template contains the same nodes that exist today (`#answer`, `#blueprint-chip`,
  `#verified-badge`, `#sql-panel`/`#sql-blocks`, `#result-table` + head/body/caption,
  `#provenance-panel`/`#provenance-chips`) **plus** a new `.turn-question` node, all keyed by
  `class`/`data-testid` within the block (ids become classes — one instance per turn now).
- Add a per-block `data-testid="turn-block"` and a per-tool-call `data-testid="turn-tool-call"` for
  the optional per-SQL breakdown list.

### 5.2 Refactor the five Slice-1 renderers to take a block root

`renderSql`, `renderResultTable`, `renderBlueprintChip`, `renderVerification`, `renderProvenance`
(`index.html:519-648`) currently close over the singleton `els.*`. Refactor each to accept a `root`
(the turn block) and query its own nodes via `root.querySelector(...)`. Behavior per field is
**unchanged** — this is a scoping refactor, not a logic change. Add a small `renderPerSqlBreakdown(root,
toolCalls)` that lists each `{tool_name, sql, provenance}` (SQL in a `<pre>`, its provenance as chips),
sourced only from `/api/history` turns (live turns have no per-SQL data → the breakdown is hidden).

### 5.3 Relax the per-turn reset (`index.html:659-667`, called at `:814`)

Today `sendMessage` calls `resetEnrichedResult()` to clear the singleton panels each submission, so the
UI keeps **no** prior-turn state. **Change:** on submit, create a **fresh turn block** (clone the
template), render the question into it immediately, append it to `#transcript`, and hold a reference as
"the in-progress block." The live `result` event populates **that** block via
`renderTurnBlock(inProgressBlock, resultData)` (which calls the five renderers scoped to the block) —
it no longer clears anything. Prior blocks stay in the DOM. `resetEnrichedResult` is **removed**; the
progress list may still clear per turn (unchanged UX).

### 5.4 Rehydrate on load

After the session mint (`index.html:850-862`), `fetch("/api/history?session_id=" + sessionId)` and, for
each `turn` in `turns[]`, clone a block and render it (question, answer, and the five fields — mapping
`provenance_union → provenance`, `tool_calls → sql/result_table/per-SQL breakdown`). This restores the
transcript on **page reload** and on a **resumed session** (same `session_id`, new page load). If the
response carries a top-level `pending_question`, re-open the ask-user prompt (§1.4) so resume works after
reload. History fetch failure is non-fatal: log to the error banner, start with an empty transcript.

### 5.5 Live vs. history reconciliation

A turn the user runs live is appended from the `result` event (§5.3); the same turn, after reload,
comes from `/api/history`. To avoid double-render on reload the page simply **rebuilds from history on
load** (clear `#transcript`, then render `turns[]`) — live appends only accrue within a single page
session. The two paths never run concurrently for the same turn.

### 5.6 Constraints (unchanged from Slice 1)

Vanilla JS, `data-testid` hooks, **XSS-safe: `textContent` only** (the existing `cellText`/`clearNode`
helpers, `index.html:505-515`, carry over verbatim into the scoped renderers). Cells are the caller's
own scope-filtered data but still never `innerHTML`. Empty / first-turn state: fresh session →
`turns: []` → empty `#transcript` (optionally a subtle "Ask a question to begin" placeholder).

---

## 6. Feasibility (GREEN / YELLOW / RED) + fan-out

| Area | Rating | Reason |
|---|---|---|
| Endpoint (route + `_extract_credentials` + one store read) | **GREEN** | Pure read; every reused piece exists (`app.py:109-132`, `store.py:45`). No new infra. |
| Projection: question / answer / union / per-call provenance / result_table | **GREEN** | All inline on the persisted doc (`session/models.py:88-92, 133, 136-137`); `ResultPreview.to_doc()` + the Slice-1 provenance projection reused verbatim. |
| Per-call `sql` for **runQuery** | **GREEN** | `TrailEntry.args["sql"]` inline (`session/models.py:133`). |
| Per-call `sql` for **runBlueprint** | **YELLOW-1 (scoped, non-blocking)** | Node SQL lives in `result_full["sql"]` behind `result_full_ref` (`session/models.py:138`) — a KV pointer, not inline. Slice 3 ships `sql: null` for `runBlueprint` calls (provenance + result_table + `blueprint_id` from `args` still present). KV read-back via `read_full_result` (`store.py:75`) is a deferred additive; FE-invisible (`sql` nullable per call either way). Same Option-A-vs-read-back call Slice 1 made. |
| Scope-filter join (messages ↔ trail by `turn_index`) | **YELLOW-2 (design, non-blocking)** | The two filters run **independently** — a turn's answer can be withheld while a tool-call survives (or vice versa). The projection must handle each independently (do **not** assume answer-present ⟺ tools-present) and join survivors by `turn_index`. §2.1 specifies the exact matrix. Correct fail-closed behavior; the risk is only a projection that wrongly couples the two. |
| Frontend transcript restructure | **GREEN (largest surface)** | Singleton→per-turn refactor of five existing renderers + template clone + reset relaxation. No new rendering logic, but it touches the most lines and is the one place Slice-1 UI code is reshaped. |
| BFF `GET /api/history` | **GREEN** | Trivial JSON proxy mirroring `_proxy_inbox`; JWT stays server-side. |
| No RED. | — | Nothing blocks parallel work. |

### Fan-out plan (disjoint files, one seam)

| Stream | Files | Builds against |
|---|---|---|
| **Backend** | `src/data_agent/runtime/app.py` (new `GET /session/history` route) + a pure projection helper `project_history(messages, trail, column_scope, pause_checkpoint) -> dict` (recommend a new module `src/data_agent/runtime/session_history.py` so it is unit-testable without HTTP — the pure-function test target, mirroring how `filter_trail` is the pure D44 target); tests in `tests/runtime/test_app.py` + a projection unit test. | §1, §2, §3 |
| **Frontend** | `ui/static/index.html` (transcript restructure) + `tests/ui/`. | §1.1, §5 |
| **BFF** | `ui/server.py` (`GET /api/history`) + `tests/ui/` server test. | §4 |

The **one shared seam** is the §1.1 wire shape (`turns[]` + per-call `{tool_name, sql, result_table,
provenance}` + optional top-level `pending_question`). BE, FE, and BFF build independently against it.
The BFF is a byte-pass-through, so it couples to nothing but the path.

---

## 7. Test hooks

**Unit — `project_history` (pure, the primary D44 read-surface target):**
- Narrowed scope drops an out-of-scope **past turn's answer** (`provenance_union ⊄ scope`) → `answer:
  null`, `provenance_union: null`, question still present.
- Narrowed scope drops an out-of-scope **tool-call** → that entry omitted from `tool_calls[]`; a
  sibling in-scope entry in the same turn survives (proves independent per-entry filtering, YELLOW-2).
- `None` provenance → dropped (fail-closed): a `None`-provenance assistant msg → `answer` withheld; a
  `None`-provenance trail entry (e.g. a denied/errored call) → omitted, even under allow-all `[]`.
- Allow-all scope (`[]`) → everything determined is kept; ordering by `turn_index` preserved.
- Pure-chat turn → `tool_calls: []`, `provenance_union: []`, answer present.
- `runQuery` entry → `sql` inline; `runBlueprint` entry → `sql: null`, provenance + result_table present.
- `result_table` equals `ResultPreview.to_doc()` for each in-scope entry.

**Endpoint — `tests/runtime/test_app.py`:**
- Auth: missing/malformed `Authorization` → 401; missing/malformed `X-Session-Id` → 400 (reuses
  `_extract_credentials`, `app.py:114-119`).
- Empty / unknown session → `200` with `turns: []` (never 404).
- Shape: a seeded multi-turn session (fake `SessionStore` with `messages` + `tool_trail`) returns the
  §1.1 structure; a paused-session fixture → last turn `answer: null` + top-level `pending_question`.
- Scope: same seeded session read under a **narrowed** `column_scope` token withholds the out-of-scope
  turn's answer / tool-call vs. the allow-all read (the end-to-end D44 read-surface assertion).

**Frontend — `tests/ui/`:**
- Load with a seeded `/api/history` (turns[]) → prior turns render as `data-testid="turn-block"`s with
  question/answer/SQL/table/lineage; per-SQL breakdown lists `turn-tool-call`s.
- A live `result` event **appends** a new block without clearing prior blocks (proves the §5.3 reset
  relaxation).
- XSS: a history payload with markup in a cell / SQL / question renders as `textContent` (no injection).
- Empty session → empty `#transcript`.

---

**Status:** Contract-locked. Endpoint: **`GET /session/history`** (BFF: `GET /api/history`). Per-SQL
lineage on the **history endpoint only**; live Slice-1 panel unchanged. YELLOW-1 (`runBlueprint` node
SQL behind a KV ref → `sql: null`, KV read-back deferred), YELLOW-2 (messages↔trail filtered
independently — project each separately, join by `turn_index`). No RED.
