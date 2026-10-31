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
| Answer-table paging | `src/data_agent/runtime/export_proxy.py` |
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

### 4.1 Progress

Progress events contain `{turn_id, sequence, phase, message}`. Render the supplied
business-language message. Do not derive tool execution or SQL from progress.
Heartbeat SSE comments keep idle connections open; they are not user messages.

### 4.2 Terminal answer

The current result carries `status`, `pending_question`, `tool_calls_made`,
`answer_document` and `review`. The [schema-v2 answer contract](ui-answer-document-spec.md)
is authoritative for field shapes and each presentation kind. Data sources contain
executed SQL and column formats. Data presentations contain selectors, not rows,
values or chart points. Text uses Markdown. Paused turns have no sealed document.

The UI must offer table and CSV-part options for every data result except figures.
Facts, bars and lines use their same source for these controls; tables expose CSV
alongside the grid. No agent-controlled supporting/fallback flags are required.
Figures fetch their scalar but expose neither table nor CSV controls. Text needs
no query. Never require old assistant_text/answer_tables/layout/blocks fields.

### 4.3 Paging and downloads

`POST /query/page {sql, limit, offset}` returns the first saved page and snapshot ID.
Lane5 calls existing clickhouse-api snapshot-create/page APIs. Subsequent requests
send `{snapshot_id, limit, offset}` without reexecution. No formatting metadata is
sent: the UI retains formats from the answer source. Refresh sends SQL and
`refresh_of` at offset zero; replace rows when `reset_table` is true.

`GET /query/snapshots/{id}/download-urls` returns ordered CSV parts. S3 presigned
links download directly; authenticated `/download?part=N` bytes pass through Lane5.
There is no concatenated full-file stream. The answer contract specifies bounded
409 replacement recovery, explicit recreation on 404/410, and 403 handling.

### 4.4 Errors

Continue reading SSE until a terminal result, pause, or error. Preserve HTTP status,
error detail, `Retry-After`, replacement snapshot and `reset_table` through adapters.
Do not interpret a successful initial HTTP response as successful answer delivery.

## 5. History and refresh

`GET /session/history` projects saved messages under current access scope. Render
its `answer_document` with the same adapter used for live answers. Other historical
fields are not a second presentation contract. Ember may retain its own saved
documents/snapshot IDs and need not fetch the whole transcript on every reload.

`POST /answer/refresh {answer_id, expected_revision}` reviews a replacement answer
and atomically publishes the next revision. Refreshing rows alone does not rewrite
the takeaway. Do not treat existing v1 documents as v2; explicitly refresh/migrate.
Scope denial clears inaccessible data; saved SQL does not grant authorization.

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
| `GET /inbox?status=` | list; `status ∈ {in_review (default), rejected, validated, needs_parameterization, promoted}` |
| `GET /inbox/health` | write-plane mode |
| `POST /inbox/{id}/approve` | `in_review → validated` (lands into the corpus) |
| `POST /inbox/{id}/reject` | `in_review → rejected` (archived as negative signal, not deleted) |
| `POST /inbox/{id}/retract` | `validated → retired` (pull from index) |
| `POST /inbox/{id}/verify` | human vouches for an auto-landed node |
| `POST /inbox/{id}/promote` | emit MCP-format YAML for a manual PR; **idempotent re-emit** when the candidate is already `promoted` (no status move) |
| `POST /inbox/{id}/revise_knowledge` | ask the assistant for a `global_knowledge` payload (§8.3). **Writes nothing** |
| `POST /inbox/{id}/apply_knowledge` | the ONE write path into a knowledge candidate's payload (§8.3) |
| `GET /inbox/user_knowledge?user_id=` | one named user's private facts (§8.4). `user_id` is REQUIRED |
| `POST /inbox/user_knowledge/promote` | promote one private fact into a `global_knowledge` candidate (§8.4) |

⚠ This table is not exhaustive: `complete`, `revise`, `apply_revision`, `attest_scan` and the
`/inbox/mint/*` surface also exist and are documented in their own design docs
(`learning-blueprint-review-rework-design.md`, `learning-loop-*`). Pre-existing drift, recorded
rather than fixed here. `POST /inbox/{id}/trial_run` is documented in §8.5.

Ordering: the terminal archives `rejected` and `promoted` list **newest-first** (they are unbounded, so the limit caps old history — and a just-promoted row is the one whose YAML you are most likely to want back); `in_review`, `validated` and `needs_parameterization` list **oldest-first** (FIFO drain). Limit is 100, not configurable, **no pagination**.

### 8.1 Objects

```ts
interface InboxListResponse { items: InboxItem[]; count: number }

interface InboxItem {
  candidate_id: string;                 // e.g. "candidate::<hash>::0" — contains "::", URL-encode it in paths
  type: "blueprint" | "global_knowledge" | "user_knowledge" | "schema_edit";
  status: "in_review" | "rejected" | "validated" | "needs_parameterization" | "promoted";
  reason: InboxReason;                  // re-derived, never stored — cannot drift
  summary: string;                      // entity-free one-liner, already redacted
  payload_view: Record<string, unknown>;// REDACTED payload — render verbatim
  evidence_refs: string[];              // KV keys only — NEVER quotes
  entity_scan: LeakageVerdict;
  dedup: DedupVerdict | null;
  created_at: string;                   // ISO-8601
  verified: boolean;                    // human-vouched (Phase-3); false for auto-landed
}

type InboxReason =                       // mirrors learning/inbox/models.py::InboxReason
  | "knowledge_pre_gate" | "schema_edit" | "leakage_near_miss"
  | "blueprint_sampled" | "dedup_conflict" | "suppressed_duplicate"
  | "fail_to_review" | "needs_parameterization" | "hand_authored";

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

**Two dedup reasons, and they are not interchangeable.** `dedup_conflict` is the SOFT near-miss
(`dedup.action` is `conflict` or `merge`) — something similar exists and the reviewer decides which
one wins. `suppressed_duplicate` is the DETERMINISTIC match (`action: "increment"`) against a **live**
learning-corpus artifact: the row is byte-equivalent to an artifact that already exists, the matched
artifact's `hit_count` was incremented, and the candidate is kept in review only so a human can look
at the match and revise a genuine delta out of it. Approving one as-is adds nothing to the corpus.
A duplicate of a **terminal** (rejected / retired) artifact, and a candidate structurally identical to
an MCP canon blueprint, never reach this surface at all — S6 drops them. A `suppressed_duplicate` card should render the matched
artifact (`dedup.matched_id`) alongside `dedup.canonical_key` — without it the reviewer cannot tell
what the row collided with. **Today's reference card renders only `canonical_key`**; treat
`matched_id` as an open UI item, not as already-shipped.

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

`promote` optionally takes `{ "doc_id"?: string, "title"?: string }`. The service **never touches git** — a human opens the PR with the returned metadata: write `yaml` to `target_path + filename` on `suggested_branch`, commit with `commit_message`, and do what `note` says (regenerate the corpus SHA sidecars with `tools/check_corpus_parity.py --write`). The MCP image must then be rebuilt before the promoted node is served as canon.

**`promote` does not answer the action shape.** It is the one action whose success body is a `PromotionEmit`, so a proxy or client that projects `{candidate_id, status, reason}` onto every action response will drop the YAML — which is the whole payload of the hop. Keep the pass-through body-agnostic. `verify` does answer the action shape, plus `node_stamped`: `false` means the neo4j write did not land (the service fails open) and the reviewer must re-verify — surface it, do not swallow it.

### 8.2 Inbox error codes

| Status | Meaning |
|---|---|
| `404` | feature flag off, **or** unknown candidate id — indistinguishable by design |
| `401` | missing `X-Reviewer-Token` |
| `403` | wrong token |
| `409` | illegal transition, or an approve that **held** — `detail` carries the reason verbatim |
| `400` | a required query field is blank (`user_knowledge` listing without a `user_id`) |
| `422` | candidate cannot be serialized to MCP YAML (`promote`); a payload the intake reader declines, or a model field this system has no contract for (`apply_knowledge` / `revise_knowledge`) — `detail` names the fix verbatim |
| `503` | reviewer token unconfigured, **or** landing plane unavailable |

**Offline mode is real:** when `GET /inbox/health` returns `"offline"`, list/reject/retract work but any approve that would land returns `503`. Pre-disable approve and show a read-only banner rather than letting the reviewer click into a 503. The service never fakes a `validated`.

### 8.3 Editing a knowledge candidate

Design: `knowledge-edit-and-user-promotion-design.md` §C. Both routes require the candidate to be
`in_review` **and** `type === "global_knowledge"` (409 otherwise).

```ts
// POST /inbox/{id}/revise_knowledge   body { feedback: string }   — WRITES NOTHING
interface KnowledgeProposal {
  payload: KnowledgePayload | {};       // {} + a reason ⇒ "the assistant had no suggestion"
  rationale: string;
  reason: string;                       // render inline, never as an error banner
  diff: { field: string; kind: "unchanged"|"changed"|"added"|"removed";
          before: string; after: string }[];   // always five rows; `before` may be "[withheld]"
}

// POST /inbox/{id}/apply_knowledge    body { payload: KnowledgePayload }
interface KnowledgeEditResult {
  candidate_id: string; type: string; status: "in_review"; reason: null;
  outcome: "edited";
  entity_scan: { result: string; hits: { field: string; kind: string }[] };  // NEVER a span
}

// The payload's key set is CLOSED — exactly these five, enforced by the same reader the
// extractor uses at intake. Any other key is a 422 naming it.
interface KnowledgePayload {
  statement: string;                    // required, non-empty
  knowledge_type?: string; scope?: string;
  related_terms?: string[]; structured?: Record<string, string>;
}
```

`apply_knowledge` REPLACES the payload, so an optional field is cleared by omitting it. A 422
carries the intake reader's own sentence — show it verbatim, it names the fix. A draft the
assistant produced that still scans dirty is **withheld**: `payload` comes back empty and `reason`
names `field (kind)`, never the span.

### 8.4 The per-user knowledge surface

Design: `knowledge-edit-and-user-promotion-design.md` §D. This is a **deliberate D17 exception** —
a reviewer token can read one named user's private, entity-bearing facts. `user_id` is required;
blank is a 400, never "all users". There is no cross-user listing.

```ts
// GET /inbox/user_knowledge?user_id=<id>&limit=100
interface UserKnowledgeListResponse {
  records: {
    record_id: string; user_id: string; statement: string;
    fact_type: string | null; scope: string; structured: Record<string, unknown> | null;
    committed_at: string;
    provenance: { source_session: string; source_trace: string };
    promotion: { candidate_id: string; status: string } | null;   // already promoted?
  }[];
  count: number; user_id: string;
}

// POST /inbox/user_knowledge/promote   body { user_id, record_id }
interface PromotedUserKnowledge {
  candidate_id: string; status: string;
  already: boolean;                     // a second press finds the same row, moves nothing
  entity_scan: { result: string; hits: { field: string; kind: string }[] };
}
```

The promoted candidate is an ordinary `global_knowledge` row on the review queue, and it usually
arrives with a **non-passing** scan — the fact was a per-user one because it named an entity. That
is the expected state, not an error: the card withholds the flagged text and §8.3's assistant is
the next step. A `404` covers both an unknown record and one owned by a different user.

**Route order matters.** `/inbox/user_knowledge*` must be matched before `/inbox/{candidate_id}/…`
in both the service and any proxy, or a promote is read as a candidate named `user_knowledge`.

### 8.5 Trial run — `POST /inbox/{id}/trial_run`

Runs the candidate's SQL against the warehouse with reviewer-supplied slot values, under a token
**the reviewer pastes** (§"you need a warehouse token" in `learning-reviewer-guide.md`). Allowed on
`in_review` and `validated`. **Structure only — no rows ever cross this surface.**

A run that could not happen still answers **200** with `ok: false` and a machine `reason` — the
reviewer did nothing wrong, so render the reason as the next step, not as an error banner. Only a
wrong status (409) or a missing/unknown candidate (404/401/403/503 per §8.2) is an HTTP error.

```ts
// body { bindings?: Record<string, unknown>, token?: string }   — both default to empty
interface TrialRunResult {               // learning/inbox/inbox.py::TrialRunResult.to_wire
  ok: boolean;                           // false ⇒ it never reached the warehouse verdict
  reason: string;                        // "" when ok; a refusal code otherwise (below)
  detail: string;                        // human-readable, already scrubbed of the token and of
                                         //   any upstream cell value; may be ""
  missing: string[];                     // "missing_bindings" only — the slot names still needed
  columns: string[];                     // the result's column names
  row_count: number;                     // MEANINGLESS unless row_count_measured is true
  row_count_measured: boolean;           // see below
  distinct_grain_count: number | null;   // null when the grain was not probed
  verify_passed: boolean;                // the D56 grain + signature gate
  verify_reason: string | null;          // why it failed, when it did
  inconclusive: boolean;                 // ok && row_count === 0 && columns.length === 0
}
```

**`row_count_measured` is the honesty flag on `row_count`.** It is `true` only when the grain was
actually probed **and** a distinct-grain count came back. It is `false` everywhere else — on every
refusal path (`ok: false`), and on a blueprint that declares a grain the probe could not read. When
it is `false` the card must show **"row count: not checked"** rather than a number: `row_count`'s
default is `0`, and rendering that would tell a reviewer the query returned nothing when nobody
counted.

**`inconclusive` is not a pass and not a failure.** The D56 gate is satisfied *vacuously* by zero
rows (the grain teeth hold and the signature check is skipped when the candidate declares no
`result_signature.shape`), so `verify_passed: true` with `inconclusive: true` proves only that the
SQL parses and the scoped token may run it. Render it as its own third state — the dev warehouse's
row-level grant returns zero rows to a replay tenant, which looks exactly like this.

| `reason` (when `ok: false`) | Meaning |
|---|---|
| `missing_bindings` | `missing[]` names the slots with no value — re-prompt, do not retry |
| `bind_failed` | the values could not be bound into the template. A single-template run appends the binder's message to the reason itself (`bind_failed:<message>`); a **composite** returns a bare `bind_failed` and names only the step in `detail` — the offending value may be a warehouse cell read from an earlier step, and this surface returns structure, never values. Match this code with a prefix test, not equality |
| `no_uses_scope` | the candidate declares no column footprint, so no scoped token can be minted. **Not retryable by the reviewer** |
| `no_token` | no warehouse token was supplied |
| `warehouse_error` | the query failed at the warehouse; `detail` is the scrubbed message (or a withheld-whole notice) |
| `no_template` | nothing to run — a `needs_parameterization` row, or a payload with neither `sql_template` nor `node_templates` |
| `malformed_composite` | the DAG's SQL and its `composes` wiring disagree; `detail` names the step |
| `table_intermediate_unsupported` | a step passes a whole table downstream — outside what a trial can run today |
| `scalar_shape` | a consumed step did not return exactly one cell, or is consumed as several scalars |
| `no_scalar_probe` | this deployment's probe cannot read an intermediate value, so a multi-step blueprint cannot be trialled here |

A trial proves *"this SQL runs and returns this shape for this principal"*. It does **not** prove the
declared `uses` footprint is honest — that is re-checked statically at landing, where no pasted token
can influence it.

---

## 9. Reference BFF surface (`ui/server.py`)

If you build your own BFF, mirror these. Paths are what the shipped demo UI calls.

| BFF route | Proxies to | Notes |
|---|---|---|
| `POST /api/session` | token service `POST /token` | returns `{ session_id }` **only** |
| `POST /api/turn` | runtime `POST /turn` | body `{session_id, message}`; streams SSE back byte-for-byte |
| `POST /api/turn/resume` | runtime `POST /turn/resume` | body `{session_id, answer}` |
| `GET /api/history?session_id=` | runtime `GET /session/history` | JSON passthrough |
| `POST /api/query/page` | runtime `POST /query/page` | body `{session_id, sql?, snapshot_id?, refresh_of?, limit?, offset?}`; JSON passthrough. Status + body propagate, so a `400`/`403` reaches the browser unchanged |
| `GET /api/inbox?status=` | inbox `GET /inbox` | validates `status ∈ {in_review, rejected, validated, needs_parameterization, promoted}` → else 400 |
| `GET /api/inbox/health` | inbox `GET /inbox/health` | |
| `POST /api/inbox/{id}/{action}` | inbox | `action ∈ {approve, reject, retract, complete, verify, promote, revise, apply_revision, attest_scan, trial_run, revise_knowledge, apply_knowledge}`; id is URL-encoded on the hop; body-carrying actions forward a JSON body (capped by `INBOX_BODY_MAX_BYTES`, 413 over it) |
| `GET /api/inbox/user_knowledge?user_id=` | inbox `GET /inbox/user_knowledge` | `user_id` required — blank/absent ⇒ **400, never proxied**. Declared BEFORE the `{id}/{action}` catch-all |
| `POST /api/inbox/user_knowledge/promote` | inbox same path | bounded JSON body `{user_id, record_id}`; declared BEFORE the catch-all, so `user_knowledge` is never read as a candidate id |
| `POST /api/upload/analyze?session_id=` | `/scratch/v1/analyze` | **raw multipart passthrough** — do not parse the form |
| `POST /api/upload?session_id=` | `/scratch/v1/upload` | same |
| `POST /api/session/scope` | re-mints a narrower JWT | **test-only**; 404 unless `UI_TEST_AFFORDANCES=1`; narrows only, never widens |

BFF-specific errors: `404 {"detail":"Unknown session_id — call POST /api/session first."}`, `502 {"detail":"Runtime unreachable: …"}` / `"Inbox service unreachable: …"` / `"clickhouse-api unreachable: …"`, and `413 {"error":…,"code":"UPLOAD_TOO_LARGE"}` on the header pre-check.

Upstream status codes and bodies propagate **as-is** — a non-2xx from the runtime is not coerced into a 200 SSE frame. Always check `response.ok` before SSE-parsing.

---

## 10. Client acceptance

Use [the v2 acceptance checks](ui-answer-document-spec.md#acceptance-checks) for
live delivery, saved reload, all presentations, paging, snapshot replacement,
expiry, authorization and CSV parts. Apply source formats in the UI, preserve
raw values for arithmetic/export and render caveats once. Source freshness comes
only from code metadata; snapshot creation time is not data-as-of.
