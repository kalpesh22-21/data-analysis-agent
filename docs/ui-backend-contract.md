# Backend ↔ UI Integration Contract

**Audience:** an external UI developer building a client against this backend.
**Status:** describes what the code does *today* (verified against source, not aspirational docs).
**Scope:** every HTTP endpoint a UI can call, and **every object type the backend sends back**.

Source of truth for each section is cited so you can diff this doc against the code:

| Surface | Code |
|---|---|
| Runtime (chat) | `src/data_agent/runtime/app.py` |
| Turn result fields | `src/data_agent/runtime/loop/agent_loop.py` (`TurnOutcome`) |
| Result table / trail types | `src/data_agent/runtime/session/models.py` |
| Answer-table paging | `src/data_agent/runtime/query_page.py` |
| History projection | `src/data_agent/runtime/session_history.py` |
| Progress events | `src/data_agent/runtime/observability/progress.py` |
| Review inbox service | `src/data_agent/learning/inbox/service.py`, `.../inbox/models.py` |
| Upload (scratch) | `clickhouse-api/app/mcp_server.py` (`/scratch/v1/*`) |
| Reference BFF | `ui/server.py` |
| Token minting | `clickhouse-api/app/token_service.py` |

---

## 1. Topology — which process serves what

There is **no single backend origin**. Four independent services exist; a UI normally talks to *one* of them (your own BFF) which fans out server-side.

```
                       ┌────────────────────────────────────────────┐
  browser ── HTTP ───► │  YOUR BFF  (reference impl: ui/server.py)  │
                       └──┬──────────────┬──────────────┬───────────┘
                          │              │              │
              JWT+X-Session-Id      X-Reviewer-Token   JWT+X-Session-Id
                          ▼              ▼              ▼
                 ┌──────────────┐ ┌─────────────┐ ┌──────────────────┐
                 │   runtime    │ │ inbox svc   │ │ clickhouse-api   │
                 │  (chat/SSE)  │ │ (learning   │ │ (MCP + scratch   │
                 │  :8000       │ │  review)    │ │  upload) :18090  │
                 └──────────────┘ │  :8100      │ └──────────────────┘
                                  └─────────────┘
                                                   ┌──────────────────┐
                                                   │ token service    │
                                                   │ POST /token :19000│
                                                   └──────────────────┘
```

### 1.1 Hard constraints you must design around

1. **No CORS headers anywhere.** Neither the runtime, the inbox service, nor clickhouse-api installs `CORSMiddleware` (verified: no `add_middleware` call in `src/` or `ui/`). A browser **cannot** call them cross-origin. You must proxy through a same-origin backend of your own. Everything below assumes that proxy exists.
2. **The browser must never hold the JWT.** The reference BFF mints a JWT server-side and hands the browser only an opaque `session_id` (`ui/server.py:154-181`). The JWT carries the caller's `column_scope` — the entire access-control model. Treat it as a server-side secret.
3. **Two headers authenticate every runtime call:** `Authorization: Bearer <jwt>` and `X-Session-Id: <session_id>`. Both are required; the JWT is cryptographically bound to the session id (`sid_hash` claim).
4. **`X-Session-Id` format is load-bearing.** Must match `^[A-Za-z0-9_-]{1,128}$` (`app.py:98`). The reference BFF mints `"s" + uuid4().hex` — **underscore-free deliberately**, because scratch upload tables are named `s_<session_id>_bp_<hex>` and the read gate splits on `_`. If you mint your own session ids, keep them underscore-free.

---

## 2. Session lifecycle

```
POST /api/session                 → { session_id }        (your BFF mints JWT server-side)
POST /api/turn        (SSE)       → progress* → result | error
   ↳ if result.status == "paused_ask_user" | "paused_budget_cap":
POST /api/turn/resume (SSE)       → progress* → result | error
GET  /api/history                 → full transcript (for reload / rehydrate)
```

**Minting a token** (your BFF → token service, never the browser):

`POST {TOKEN_SERVICE_URL}/token` with `Authorization: Bearer <TOKEN_ISSUER_API_KEY>`

```jsonc
// request
{ "user_name": "alice", "column_scope": [], "session_id": "s4f2c…" }
// column_scope: [] == allow-all. Non-empty == allowlist of "database.table.column".
```

```ts
// response — TokenResponse (clickhouse-api/app/token_service.py:274)
interface TokenResponse {
  access_token: string;
  token_type: "Bearer";
  expires_in: number;   // seconds
  user_name: string;
  kid: string;          // signing key id
}
```

---

## 3. Runtime API (chat) — `POST /turn`, `POST /turn/resume`

### 3.1 Request

| | |
|---|---|
| Method/path | `POST /turn` &nbsp;/&nbsp; `POST /turn/resume` |
| Headers | `Authorization: Bearer <jwt>`, `X-Session-Id: <id>`, `Content-Type: application/json` |
| Body (`/turn`) | `{ "message": string }` |
| Body (`/turn/resume`) | `{ "answer": string }` |
| Response | `200 text/event-stream` (SSE) on success; a **JSON error body with a non-2xx status** if rejected before the stream opens |

**Pre-stream rejections** (plain JSON, not SSE — check `response.ok` before attaching an SSE parser):

| Status | Body | Cause |
|---|---|---|
| `401` | `{"detail":"Missing or malformed Authorization header."}` | no/short `Authorization` |
| `401` | `{"detail":"<jwt verification message>"}` | expired/invalid/wrong-audience JWT |
| `400` | `{"detail":"Missing X-Session-Id header."}` | header absent |
| `400` | `{"detail":"Malformed X-Session-Id header."}` | fails the charset/length regex |
| `409` | `{"detail":"No pending checkpoint for this session."}` | `/turn/resume` with nothing paused (or already consumed) |

### 3.2 The SSE stream

Three event names. Exactly one terminal event (`result` **or** `error`) closes the stream.

```
event: progress
data: {"step":"running runQuery…","shape":{"tool_name":"runQuery"}}

event: progress
data: {"step":"thinking…","shape":{}}

event: result
data: { …TurnResult… }
```

Framing is `event: <name>\ndata: <json>\n\n` (`app.py:147`). The reference BFF forwards these bytes verbatim; do not re-frame.

---

## 4. Object catalog — everything the backend sends

TypeScript declarations below are normative. **Every field marked `| null` really is null in practice** — guard all of them.

### 4.1 `ProgressEvent` — SSE `progress`

```ts
interface ProgressEvent {
  step: string;                       // human-readable, already localized to English
  shape: Record<string, unknown>;     // machine-readable, allowlisted keys only
}
```

`shape` keys are drawn from a **closed allowlist** (`progress.py:29-50`) — never anything else:

`tool_name`, `error_code`, `window`, `tool_calls_made`, `question`, `blueprints`, `knowledge`, `rule_id`, `selected_count`, `dropped_count`, `top_score`, `cut_gap`.

**Canonical `step` labels** (`progress.py:55-69`) — stable strings you may key UI off, though prefer `shape.tool_name`:

| Event | `step` |
|---|---|
| retrieval start | `searching for a matching blueprint…` |
| retrieval done | `found matching context` (`shape.blueprints`, `shape.knowledge` = counts) |
| rule resolved | `resolved the filter set` |
| tool start | `running {tool_name}…` |
| tool ok | `step complete: {tool_name}` |
| tool denied | `step denied: {tool_name}` |
| model call | `thinking…` |
| turn done | `done` |
| paused (askUser) | `waiting for your answer…` |
| paused (budget) | `this is taking a while — continue, refine, or stop?` |
| hard stop | `stopping — budget exhausted` |

> **One exception to the label table:** when `progress_summary_enabled` is on server-side, an *additional* progress event may arrive whose `step` is a free-form LLM-authored sentence ("Querying overtime pay by department for January"). It is **not** from the table and **may contain concrete parameter values**. Its `shape` is still allowlist-governed. Render `step` as untrusted text.

**PII rule (D25):** `progress` never carries cell values, SQL text, row data, the JWT, or the column scope — *except* the opt-in summary channel above, which may carry tool-argument values (never results).

### 4.2 `TurnResult` — SSE `result` (the main payload)

```ts
type TurnStatus =
  | "done"
  | "paused_ask_user"
  | "paused_budget_cap"
  | "stopped_hard_ceiling";

interface TurnResult {
  // --- always present ---
  status: TurnStatus;
  assistant_text: string | null;
  pending_question: PendingQuestion | null;
  tool_calls_made: number;

  // --- additive, best-effort, nullable on EVERY status ---
  sql_executed: string[] | null;      // every query the turn RAN (audit list)
  answer_sql: string | null;          // the ONE query whose rows ARE the answer

  blueprint_use: BlueprintUse | null;
  verification: Verification | null;
  provenance: string[] | null;        // "database.table.column", sorted + deduped
  assumptions: string[] | null;
}
```

```ts
interface PendingQuestion {
  question: string;
  options: string[] | null;   // null == free-text answer expected
}

// HISTORY ONLY. `GET /session/history` still returns this per tool call; the live
// `result` event no longer carries it (see §4.2.2).
interface ResultTable {                  // ResultPreview.to_doc(), session/models.py:38
  columns: string[];
  row_count: number;                     // TOTAL rows, not preview length
  truncated: boolean;
  preview_rows: unknown[][];             // heterogeneous JSON scalars per cell
}

interface BlueprintUse {
  blueprint_id: string;
  slots: Record<string, unknown>;        // the model-supplied slot_bindings (raw, pre-resolution)
}

interface Verification {
  passed: boolean;                       // effectively always true when present — see §4.2.4
  method: "blueprint_gate";              // the only value that exists
  grain_checked: boolean;
}
```

#### 4.2.1 `sql_executed` — null vs `[]`
*(was `sql` before 2026-08 — renamed so it can never be read as "the answer".)*

Every query the turn **actually ran**, in execution order, deduped preserving first occurrence — the audit/explain list, including intermediate probes and sanity checks. A turn that ran no successful query serializes as **`null`, not `[]`** — treat "no SQL panel" and "empty" identically. `runBlueprint` contributes its node SQL list.

Not the same thing as `answer_sql`: this is *what ran*, that is *what the answer is*. A turn commonly has several entries here and one (or zero) there. Queries issued **inside** `resolveValues` do not appear — the list is per model-issued tool call.

#### 4.2.2 `answer_sql` — the answer table
*(replaced `result_table` in 2026-08. `result_table` is **gone from the live result**, not nulled — a client keying on it will get `undefined`, deliberately, so the change fails loudly rather than silently rendering an empty grid. `GET /session/history` still returns it per tool call; see §7.2.)*

**The rows are no longer on the result event.** `answer_sql` is a single query string; you fetch its rows yourself, a page at a time, from `POST /query/page` (§4.4). That is the whole rendering contract for the answer table.

Why it changed: `result_table` was a fixed ~20-row preview the user could not page past, and the *runtime* picked it ("the last successful query"), which is wrong exactly when a turn resolves values or probes a code space before answering. Now the **model** designates which query is the answer, and the UI renders the full result with real paging.

`answer_sql` is `null` when:
- the answer is a **scalar or single row** — correct, render prose only, a one-cell grid helps nobody; or
- the model simply did not designate one. The designation is **advisory** (the model calls an `answerWithTable` tool); a table-shaped answer with `answer_sql: null` is possible and must degrade to prose, not to an error.

Rows returned by §4.4 **may contain real cell values** — the caller's own scope-filtered answer data over an authenticated session. This is the deliberate asymmetry with `progress` (§4.1). Render every cell via `textContent` / auto-escaping — warehouse text, never trusted markup.

#### 4.2.3 `blueprint_use`
`null` unless a **blueprint** produced the answer. `slots` are the *raw* model-proposed bindings, which can differ from post-resolution bound values (a `resolve_via` expansion, a defaulted slot). There is **no version field** — no blueprint version concept exists.

#### 4.2.4 `verification` — read this before designing the badge
- Non-null **only** for a successful blueprint answer.
- `passed` is therefore **effectively always `true` when present**: a *failed* gate never returns a blueprint answer, it silently degrades to the raw loop, which emits `verification: null`.
- So: **`null` means "not verifiably verified", not "verification failed".** Render nothing on null. Never render a warning, never pause, never prompt.
- `grain_checked: false` means the grain probe was vacuously skipped — optionally a subtler tick.

#### 4.2.5 `provenance` — three-valued
| Wire | Meaning | UI |
|---|---|---|
| `["hr.employees.department", …]` | determined USES-set | lineage chips (already sorted/deduped) |
| `[]` | determined-empty (pure chat, `SELECT 1`) | optional "no tables read" |
| `null` | **undetermined** — at least one tool result had unknown provenance (fail-closed) | hide the panel |

Never inside-scope-leaks: the set is produced by the scope-enforced extractor, so it can never name a column outside the caller's `column_scope`.

#### 4.2.6 `assumptions`
Model-declared, **plain-English** sentences ("'Active employees' was taken to mean currently-employed staff."). Never SQL, codes, or column names — enforced by prompt, not by the runtime, so treat as untrusted text. Same `[] → null` fork as `sql_executed`. Dedupe/order/caps are applied server-side.

#### 4.2.7 Nullability by `status`

| `status` | What to expect |
|---|---|
| `done` (blueprint path) | all six enrichment fields populated |
| `done` (raw loop) | `sql_executed` + `provenance` populated; `blueprint_use` and `verification` **null** |
| `done` (table answer) | `answer_sql` non-null → page it via §4.4 |
| `done` (scalar answer) | `answer_sql` **null** — expected, not an error |
| `done` (pure chat) | all may be null; `provenance` may be `[]` |
| `paused_ask_user` | usually all null; `pending_question` non-null → **render the prompt and call `/turn/resume`** |
| `paused_budget_cap` | partial; `pending_question` = `{question, options:["continue","refine","stop"]}` |
| `stopped_hard_ceiling` | partial; terminal — resume is not offered |

`paused_*` statuses are the **only** ones where `pending_question` is meaningful. Send the user's reply as `{"answer": "<text>"}` to `/turn/resume`. Resume is **exactly-once**: a second resume against a consumed checkpoint yields a `409` (pre-stream) or an `error` event (racing).

### 4.3 `TurnError` — SSE `error`

```ts
interface TurnError {
  code: "AlreadyConsumedError" | "CASMismatchError" | "INTERNAL_ERROR";
  message: string;
}
```

- `AlreadyConsumedError` — the pause checkpoint was already consumed (duplicate/stale resume). Refresh via `GET /session/history`.
- `CASMismatchError` — concurrent writes to the same session. Safe to retry the turn.
- `INTERNAL_ERROR` — `message` is always the canned string `"Something went wrong processing this turn. Please try again."`. Raw exception text is **never** streamed (info-disclosure guard, `app.py:110`); the real error is server-side only. Do not try to parse it.

---

### 4.4 `POST /query/page` — fetching the answer table

Runs `answer_sql` and returns one page of rows. This is what replaced the inline
`result_table` preview.

| | |
|---|---|
| Path | `POST /query/page` |
| Headers | same as `/turn` (`Authorization` + `X-Session-Id`) |
| Body | `{ sql: string, limit?: number, offset?: number }` |

```ts
interface QueryPageRequest {
  sql: string;              // echo back TurnResult.answer_sql verbatim
  limit?: number | null;    // default 100, clamped to 1..1000
  offset?: number | null;   // default 0, clamped to >= 0
}

interface QueryPageResponse {
  columns: string[];
  rows: unknown[][];        // heterogeneous JSON scalars per cell
  limit: number;            // the limit ACTUALLY applied after clamping
  offset: number;
  has_more: boolean;        // hint: this page came back full. NOT a total count.
}
```

**`has_more` is a hint, not a count.** It is `rows.length >= limit`. There is no total —
counting every row would mean a second aggregate query per page. Drive a "Next"
control off `has_more`; do not render "page 3 of 12".

**Paging bounds are the server's.** The SQL is parsed and re-emitted as
`SELECT * FROM (<your sql>) AS page_src LIMIT n OFFSET m`. A `LIMIT` inside
`answer_sql` still bounds the inner result, but can never let a page exceed `limit`.
Bad paging params are clamped, never rejected — a malformed `limit` is UI plumbing,
not a reason to fail a user's scroll.

**It grants no extra access.** The query runs through the same scope-enforced
`runQuery` path the agent uses, under this caller's own credentials. Echoing
`answer_sql` back from the browser therefore confers nothing: a tampered query can
only reach what the same session's `column_scope` already allows.

Errors — check `response.ok` and render the `error` string in place of the grid:

| Status | Body | When |
|---|---|---|
| `400` | `{error}` | not a single read-only `SELECT` (writes, multi-statement, unparseable). The message is **static** — it never echoes your SQL back |
| `403` | `{error, error_code}` | column-scope or scratch-session denial; `error` is the canned, PII-safe string |
| `502` | `{error, error_code}` | the query failed downstream |

**A scratch-backed answer can expire.** If the turn was answered by a composed
blueprint that materialises into `scratch.*`, `answer_sql` reads a session-scoped
table with a TTL (1h by default). Paging it from a **different session** is a `403
SCRATCH_SESSION_VIOLATION`, and after the TTL it fails downstream. Treat a
previously-working table that starts erroring as expected, not as a bug — re-ask the
question.

---

## 5. `GET /session/history` — transcript rehydration

| | |
|---|---|
| Path | `GET /session/history` |
| Headers | same as `/turn` (`Authorization` + `X-Session-Id`) |
| Response | `200 application/json` — **never 404**; an unknown session returns `turns: []` |
| Side effects | none (pure read; no KV de-ref, no loop, no model call) |

```ts
interface HistoryResponse {
  session_id: string;
  turns: HistoryTurn[];              // ordered by turn_index ascending
  pending_question: PendingQuestion | null;   // an UNCONSUMED pause — re-open the prompt on reload
}

interface HistoryTurn {
  turn_index: number;
  question: string;                  // ALWAYS present (user text is never withheld)
  answer: string | null;             // null == withheld-by-scope OR paused OR none yet
  provenance_union: string[] | null; // same 3-valued encoding as TurnResult.provenance
  assumptions: string[] | null;
  tool_calls: HistoryToolCall[];
}

interface HistoryToolCall {
  tool_name: string;                 // "runQuery" | "runBlueprint" | "resolveValues" | …
  sql: string | null;                // ALWAYS null for runBlueprint (node SQL is behind a KV ref)
  result_table: ResultTable | null;
  provenance: string[] | null;       // per-SQL USES-set
}
```

### 5.1 Scope filtering — behavior you must not misread

History is re-filtered against **this request's** `column_scope`, not the scope the turn originally ran under. Consequences:

| Situation | Wire effect |
|---|---|
| Past answer no longer in scope | `answer: null`, `provenance_union: null`, `assumptions: null` — **question still renders** |
| A tool call no longer in scope | that entry is simply **absent** from `tool_calls[]` |
| Whole turn dropped | **never**, as long as the turn recorded a question |

An `answer: null` is **deliberately indistinguishable** from "paused" or "never answered". History does not signal that a withheld answer existed. Do not build UI that infers withholding.

Only **successful, in-scope** tool calls appear — denials and errors carry undetermined provenance and are filtered out. History shows what the answer read, not the model's dead ends.

`tool_calls[]` is per-SQL; the live `result` event is union-only. That asymmetry is intentional and frozen.

**History and the live result now describe the answer table differently.** A live turn
gives you `answer_sql` (paged via §4.4); history gives you a per-tool-call
`result_table` preview and **no** `answer_sql`. So a table answer looks different on
reload than it did live — a ~20-row static preview instead of a paged grid — and a
`runBlueprint` turn's history `sql` is `null` besides. Rebuilding a live-quality
table from history is not currently possible; render the preview and accept the
downgrade, or re-ask.

### 5.2 Live vs history reconciliation
The reference client rebuilds the whole transcript from `/session/history` on page load, then appends live turns from `result` events within that page session. Running both for the same turn double-renders — pick one path per turn.

**No pagination.** The endpoint returns the whole session. Long sessions return large payloads; budget for it client-side.

---

## 6. `GET /ready` — readiness probe

Unauthenticated (a k8s probe carries no JWT).

```ts
interface ReadyResponse { ready: boolean }   // 200 when ready, 503 when not
```

`ready:false` means the corpus graph has not been seeded by the hydrator yet — retrieval-backed answers will be degraded. Not a UI-facing endpoint, but useful for a "backend warming up" state.

---

## 7. Upload → join (`/scratch/v1/*` on clickhouse-api)

Two-step: **analyze** (preview, stateless) → **upload** (re-parse + rename + materialize). The file is sent **twice** by design — there is no server-side parse cache.

Both are `multipart/form-data`, both require the same `Authorization` + `X-Session-Id` pair, and both derive the target table **only** from the bound session id (a body-supplied session is ignored).

### 7.1 `POST /scratch/v1/analyze`

Request: multipart with a single `file` part (`.csv` or `.xlsx`).

```ts
interface UploadAnalyzeResponse {
  columns: UploadColumn[];
  row_count: number;          // total data rows (post-header)
  sample_rows: unknown[][];   // ≤10 rows, for showing examples next to each column
}

interface UploadColumn {
  name: string;               // SANITIZED identifier — this is the mapping key
  type: string;               // inferred ClickHouse type, e.g. "Int64", "Nullable(Float64)"
}
```

### 7.2 `POST /scratch/v1/upload`

Request: multipart with `file` (the same file) **and** `mapping` (a JSON **string** form field).

```jsonc
// mapping — keys are sanitized column names from analyze; values are roles
{ "emp_id": "EmployeeCode", "dept": "DepartmentCode", "fte": "none" }
```

| Role | Renamed to |
|---|---|
| `EmployeeCode` | `employee_code` |
| `DepartmentCode` | `department_code` |
| `DepartmentName` | `department_name` |
| `none` / absent | unchanged |

Each non-`none` role may be used **at most once**, and a rename must not collide with a kept column — both are hard `400 UPLOAD_MAPPING_INVALID`. Unknown mapping keys are ignored (the file is authoritative). All roles are optional.

```ts
interface UploadResponse {
  table: string;      // e.g. "scratch.s_s4f2c…_bp_9a3c…" — show this to the user
  row_count: number;
}
```

After a successful upload the agent can `JOIN` that table via a normal chat turn — **no new tool, no special turn parameter**. The read gate authorizes it automatically because the table name embeds the caller's session id.

### 7.3 Upload error object

All scratch routes use one shape (**note: `error`/`code`, not `detail`**):

```ts
interface ScratchError { error: string; code: ScratchErrorCode }

type ScratchErrorCode =
  | "SCRATCH_SESSION_MISSING"        // 400 — no bound X-Session-Id
  | "UPLOAD_PARSE_ERROR"             // 400 — not valid CSV/XLSX, empty, no header, non-multipart
  | "UPLOAD_MAPPING_INVALID"         // 400 — bad JSON, duplicate role, rename collision
  | "SCRATCH_MATERIALIZE_REJECTED"   // 400 — downstream write rejection
  | "UPLOAD_TOO_LARGE"               // 413 — file/body over the byte cap (default 8 MiB)
  | "SCRATCH_TOO_LARGE";             // 413 — parsed rows over scratch_max_rows (default 10 000)
```

Row-count rejection happens at **analyze**, before you render a mapping UI. Uploaded tables are TTL'd (default 3600s) and session-scoped; nothing outlives the session.

---

## 8. Review inbox (learning-loop moderation surface)

**Gated off by default.** Both the service and the reference BFF require `REVIEW_INBOX_ENABLED=1`; anything else ⇒ **every route 404s**. Auth is a shared secret header `X-Reviewer-Token`, held server-side (never in the browser). This is *not* a `column_scope` JWT — a reviewer has no warehouse scope.

| Route (service) | Purpose |
|---|---|
| `GET /inbox?status=` | list; `status ∈ {in_review (default), rejected, validated}` |
| `GET /inbox/health` | write-plane mode |
| `POST /inbox/{id}/approve` | `in_review → validated` (lands into the corpus) |
| `POST /inbox/{id}/reject` | `in_review → rejected` (archived as negative signal, not deleted) |
| `POST /inbox/{id}/retract` | `validated → retired` (pull from index) |
| `POST /inbox/{id}/verify` | human vouches for an auto-landed node |
| `POST /inbox/{id}/promote` | emit MCP-format YAML for a manual PR |

Ordering: `rejected` lists **newest-first** (the archive is unbounded, so the limit caps old history); `in_review` and `validated` list **oldest-first** (FIFO drain). Limit is 100, not configurable, **no pagination**.

### 8.1 Objects

```ts
interface InboxListResponse { items: InboxItem[]; count: number }

interface InboxItem {
  candidate_id: string;                 // e.g. "candidate::<hash>::0" — contains "::", URL-encode it in paths
  type: "blueprint" | "global_knowledge" | "user_knowledge" | "schema_edit";
  status: "in_review" | "rejected" | "validated";
  reason: InboxReason;                  // re-derived, never stored — cannot drift
  summary: string;                      // entity-free one-liner, already redacted
  payload_view: Record<string, unknown>;// REDACTED payload — render verbatim
  evidence_refs: string[];              // KV keys only — NEVER quotes
  entity_scan: LeakageVerdict;
  dedup: DedupVerdict | null;
  created_at: string;                   // ISO-8601
  verified: boolean;                    // human-vouched (Phase-3); false for auto-landed
}

type InboxReason =
  | "knowledge_pre_gate" | "schema_edit" | "leakage_near_miss"
  | "blueprint_sampled" | "dedup_conflict" | "fail_to_review";

interface LeakageVerdict {
  result: "pass" | "reroute" | "quarantine" | "reject";
  hits: EntityHit[];
  scanned_fields: string[];
  scanner: string;
}

interface EntityHit {
  field: string;   // where in the payload
  kind: string;    // employee_code | dept_code | person | date | region | …
  span: "";        // ALWAYS blank on this surface — the raw value never crosses it
}

interface DedupVerdict {
  canonical_key: string;
  matched_id: string;
  similarity: number;
  action: string;
  layer: string;
}
```

**Redaction rule the UI must honor:** `payload_view` has entity-bearing spans replaced with `"[redacted]"`, `summary` is run through the same strip, and `entity_scan.hits[].span` is blanked. The inbox surface **never** re-exposes a value the leakage gate flagged — do not build a "show raw value" affordance; the data is not on the wire.

```ts
interface ActionResult {                 // approve / reject / retract / verify
  candidate_id: string;
  type: string;
  status: string;                        // the NEW store status
  reason: string | null;                 // null on success; hold reason on 409
  node_stamped?: boolean;                // verify only — false ⇒ prompt a re-verify
}

interface InboxHealth { write_plane: "full" | "offline" }

interface PromotionEmit {                // POST /inbox/{id}/promote
  yaml: string;
  filename: string;
  target_path: string;
  suggested_branch: string;
  commit_message: string;
  note: string;
}
```

`promote` optionally takes `{ "doc_id"?: string, "title"?: string }`. The service **never touches git** — a human opens the PR with the returned metadata.

### 8.2 Inbox error codes

| Status | Meaning |
|---|---|
| `404` | feature flag off, **or** unknown candidate id — indistinguishable by design |
| `401` | missing `X-Reviewer-Token` |
| `403` | wrong token |
| `409` | illegal transition, or an approve that **held** — `detail` carries the reason verbatim |
| `422` | candidate cannot be serialized to MCP YAML (promote only) |
| `503` | reviewer token unconfigured, **or** landing plane unavailable |

**Offline mode is real:** when `GET /inbox/health` returns `"offline"`, list/reject/retract work but any approve that would land returns `503`. Pre-disable approve and show a read-only banner rather than letting the reviewer click into a 503. The service never fakes a `validated`.

---

## 9. Reference BFF surface (`ui/server.py`)

If you build your own BFF, mirror these. Paths are what the shipped demo UI calls.

| BFF route | Proxies to | Notes |
|---|---|---|
| `POST /api/session` | token service `POST /token` | returns `{ session_id }` **only** |
| `POST /api/turn` | runtime `POST /turn` | body `{session_id, message}`; streams SSE back byte-for-byte |
| `POST /api/turn/resume` | runtime `POST /turn/resume` | body `{session_id, answer}` |
| `GET /api/history?session_id=` | runtime `GET /session/history` | JSON passthrough |
| `POST /api/query/page` | runtime `POST /query/page` | body `{session_id, sql, limit?, offset?}`; JSON passthrough. Status + body propagate, so a `400`/`403` reaches the browser unchanged |
| `GET /api/inbox?status=` | inbox `GET /inbox` | validates `status ∈ {in_review, rejected}` → else 400 |
| `GET /api/inbox/health` | inbox `GET /inbox/health` | |
| `POST /api/inbox/{id}/{action}` | inbox | `action ∈ {approve, reject, retract}`; id is URL-encoded on the hop |
| `POST /api/upload/analyze?session_id=` | `/scratch/v1/analyze` | **raw multipart passthrough** — do not parse the form |
| `POST /api/upload?session_id=` | `/scratch/v1/upload` | same |
| `POST /api/session/scope` | re-mints a narrower JWT | **test-only**; 404 unless `UI_TEST_AFFORDANCES=1`; narrows only, never widens |

BFF-specific errors: `404 {"detail":"Unknown session_id — call POST /api/session first."}`, `502 {"detail":"Runtime unreachable: …"}` / `"Inbox service unreachable: …"` / `"clickhouse-api unreachable: …"`, and `413 {"error":…,"code":"UPLOAD_TOO_LARGE"}` on the header pre-check.

Upstream status codes and bodies propagate **as-is** — a non-2xx from the runtime is not coerced into a 200 SSE frame. Always check `response.ok` before SSE-parsing.

---

## 10. Client implementation rules

1. **Detect pre-stream errors first.** `POST /turn` can return JSON with a 4xx. Check `response.ok`; only then attach an SSE parser.
2. **Guard every nullable field on every status.** A partial/errored/paused turn may carry all six enrichment fields as `null`.
3. **Ignore unknown keys.** The result event is versioned by *addition only* — new nullable fields will appear without notice; an old client ignoring them stays correct. Never validate with a closed schema that rejects extras.
4. **`null` ≠ `[]`.** For `sql_executed`, `assumptions`, and `provenance` the distinction is deliberate. `provenance: []` means "read nothing"; `provenance: null` means "we could not determine what was read" — the second must not render as the first.
5. **Escape everything.** Cell values, column names, SQL, questions, answers, assumptions, uploaded headers, and inbox payloads are all untrusted text. Use `textContent`/auto-escaping, never `innerHTML`.
6. **Never persist or expose the JWT client-side.** The browser holds only `session_id`.
7. **Resume is exactly-once.** Disable the resume control after firing; on `409`/`AlreadyConsumedError`, re-fetch history rather than retrying.
8. **Rebuild the transcript from history on load**, then append live turns — do not merge both sources for the same turn.
9. **Render the answer table from `answer_sql`, not from the result event.** The rows are not in the payload — fetch them from §4.4. `answer_sql: null` is the normal scalar case; degrade to prose rather than showing an error or an empty grid.
10. **Long turns are normal.** A turn does retrieval + multiple LLM round-trips + queries. Keep the SSE read timeout unbounded (the reference BFF sets `read=None`) and drive perceived latency from `progress` events.

---

## 11. Known gaps — plan around these

| Gap | Impact |
|---|---|
| **No CORS anywhere** | a browser cannot call the runtime/inbox/clickhouse-api directly; a same-origin proxy is mandatory |
| **No pagination** on `/session/history` or `GET /inbox` | whole-session and top-100 payloads; large sessions are large responses |
| **`runBlueprint` history `sql` is always `null`** | node SQL lives behind a KV pointer that the read path deliberately does not dereference; the live `result` event *does* carry it |
| **History has no `answer_sql`** | a table answer reloads as a static ~20-row `result_table` preview, not the paged grid it was live (§5.2) |
| **A scratch-backed `answer_sql` expires** | composed blueprints that materialise into `scratch.*` produce a session-scoped, TTL'd answer table; paging it later or from another session fails (§4.4) |
| **`answer_sql` is advisory** | the model may not designate one for a genuinely tabular answer; there is no server-side fallback, so plan for prose-only |
| **`verification` only exists on the blueprint path** | there is no raw-loop verification gate; absence is not failure |
| **`blueprint_use.slots` are raw model inputs** | may differ from post-resolution bound values |
| **In-memory session→JWT map** in the reference BFF | single-process only; a real deployment needs shared storage |
| **Inbox/upload/summary features are flag-gated and off by default** | probe `GET /inbox/health` and degrade rather than assuming availability |
| **`session_id` must be underscore-free** | scratch table naming and the read gate split on `_` |

---

## 12. Worked examples

**Blueprint-answered TABLE turn** — note the answer text DESCRIBES the table instead of
listing it, and the rows come from `POST /query/page`, not from this payload:
```json
{
  "status": "done",
  "assistant_text": "Headcount is split evenly across 3 departments.",
  "pending_question": null,
  "tool_calls_made": 1,
  "sql_executed": ["SELECT department, count(*) FROM hr.employees GROUP BY department"],
  "answer_sql": "SELECT department, count(*) AS headcount FROM hr.employees GROUP BY department",
  "blueprint_use": { "blueprint_id": "headcount_by_dept", "slots": { "period": "2026-05" } },
  "verification": { "passed": true, "method": "blueprint_gate", "grain_checked": true },
  "provenance": ["hr.employees.department", "hr.employees.id"],
  "assumptions": ["'Headcount' was taken to mean currently-employed staff."]
}
```

**Raw-loop SCALAR turn** — `blueprint_use`/`verification` null, and `answer_sql` null
because a single number needs no grid. This is the common case, not a degraded one:
```json
{
  "status": "done",
  "assistant_text": "The average salary in Sales is $60,000.",
  "pending_question": null,
  "tool_calls_made": 2,
  "sql_executed": ["SELECT avg(base_salary) FROM hr.employees WHERE department = 'Sales'"],
  "answer_sql": null,
  "blueprint_use": null,
  "verification": null,
  "provenance": ["hr.employees.base_salary", "hr.employees.department"],
  "assumptions": null
}
```

**Clarification pause** — render the prompt, then `POST /turn/resume`:
```json
{
  "status": "paused_ask_user",
  "assistant_text": null,
  "pending_question": { "question": "Did you mean base salary or gross pay?", "options": ["base salary", "gross pay"] },
  "tool_calls_made": 1,
  "sql_executed": null, "answer_sql": null, "blueprint_use": null,
  "verification": null, "provenance": null, "assumptions": null
}
```

**Paging that table** — `answer_sql` echoed back verbatim:
```
POST /api/query/page   {"session_id":"s…","sql":"SELECT department, count(*) AS headcount FROM hr.employees GROUP BY department","limit":2,"offset":0}
→ 200 {"columns":["department","headcount"],"rows":[["Engineering",3],["Sales",3]],"limit":2,"offset":0,"has_more":true}

POST /api/query/page   {… "offset":2}
→ 200 {"columns":["department","headcount"],"rows":[["Ops",3]],"limit":2,"offset":2,"has_more":false}
```

**Budget pause** — fixed three options:
```json
{
  "status": "paused_budget_cap",
  "assistant_text": null,
  "pending_question": { "question": "…", "options": ["continue", "refine", "stop"] },
  "tool_calls_made": 12,
  "sql_executed": ["…"], "answer_sql": null, "blueprint_use": null,
  "verification": null, "provenance": null, "assumptions": null
}
```
