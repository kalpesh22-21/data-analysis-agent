# UI Slice 4 — External dataset upload + column mapping (contract-lock)

**Status:** Contract-locked (no minted Dxx — reuses D92 session binding, D64 read gate, D93 scratch
write-plane). Seam for parallel fan-out across THREE disjoint builders.
**Scope:** Give the user a file-upload front door (CSV + XLSX) that parses → lets the browser map key
columns → materializes a session-scoped `scratch.s_<sid>_bp_<uuid>` table the agent can `JOIN`. Spans
`clickhouse-api` (endpoint + parser) + `data-analysis-agent` (BFF proxy + UI widget). Each builder
builds against *this file only*.

Makes buildable `docs/08-ui.md` § "Upload → join" and the last open prerequisite row in
`docs/08-ui.md` § "Backend surface prerequisites" (File-upload + column-mapping loader), and closes
`docs/07-external-data.md:66` (open question: ingestion side-channel formats/size caps).

---

## 0. Architectural decision — **the front door is analyze → map → upload, and `upload` re-parses the file**

The back half is already built and is reused verbatim: `scratch_ingest.materialize(session_id,
columns, rows, settings)` takes an already-parsed `{columns:[{name,type}], rows:[[...]]}` and returns
`{"table": "scratch.s_<sid>_bp_<uuid>", "row_count": N}` (`clickhouse-api/app/scratch_ingest.py:392-429`),
behind the D92-bound `/scratch/v1/*` custom routes (`clickhouse-api/app/mcp_server.py:660-702`). Slice 4
adds **only the parse + mapping front door** in front of it. Nothing on the write plane, the read gate,
or the runtime changes.

### The one genuine fork — where does the parsed data live between "map" and "materialize"?

The mapping step needs the parsed **column names** before it can run, so a single one-shot upload is
impossible — there must be a parse step whose output the browser maps, then a materialize step. The
question is what the browser holds between them.

| Option | Hand-off | Verdict |
|---|---|---|
| **A. Re-send parsed rows** | `analyze` returns full rows; browser re-POSTs `{columns, rows, mapping}` JSON to materialize | Rejected. Puts up-to-10k-row JSON back on the wire; two shapes (multipart in, JSON in); the mapping-rename must be trusted from the client against client-held rows. |
| **B. Re-send the FILE** ✅ | `analyze` is a pure preview; `upload` takes the **file again** (multipart) + mapping, re-parses, renames, materializes | **Chosen.** Both endpoints multipart + stateless; no big-rows round-trip; the authoritative endpoint parses server-side so the rename is applied to server-parsed columns, not client-supplied rows. |
| **C. Server-cache the parse** | `analyze` returns a token; browser sends `{token, mapping}`; server holds parsed rows keyed by token | Rejected. Introduces a server-side parsed-rows cache + TTL + eviction — a **persisted intermediate store**, which the locked scope forbids ("No persisted mapping store"). |

**Decision — Option B.** `analyze` is advisory/preview only; `POST /scratch/v1/upload` is the single
authoritative endpoint that **parse → rename → materialize** in one self-contained call. A client that
already knows the mapping can skip `analyze` entirely (independently testable). The re-parse is safe
because the parser is **deterministic**: same bytes → same `_sanitize_columns` output → the mapping
keys the browser computed against `analyze`'s columns still align on `upload`. Re-parse cost is trivial
at the few-MB byte cap. **No server state, no cache, no TTL to manage** beyond the scratch table's own
D93 TTL.

**Trade-off named:** `upload` parses the file twice per successful upload (once at `analyze` for the
preview, once at `upload` to materialize). Accepted: parsing a ≤ few-MB / ≤10k-row file is
microseconds-cheap next to the ClickHouse DDL + insert, and it buys statelessness + a single trust
boundary (rows are re-derived server-side, never accepted from the browser).

---

## 1. The two endpoints — `clickhouse-api`, on the MCP host

Both are **custom HTTP routes** (`@mcp.custom_route`, NOT `@mcp.tool` — the agent LLM can never see or
call them), mounted next to the existing scratch routes and inheriting the SAME `JWTAuthMiddleware`
session binding (D92). Both derive the session from `current_session_id.get()` ONLY — never the body
(`mcp_server.py:668, 712`). Both are `multipart/form-data`.

### 1a. `POST /scratch/v1/analyze` — parse + preview (no materialize)

Mirrors admin's `/analyze` vs `/ingest` split (`clickhouse-api/app/routers/admin.py:42`), minus the
warehouse-comparison logic (there is no target table to compare against — this is a fresh scratch load).

**Request (multipart):**

| Field | Type | Notes |
|---|---|---|
| `file` | file part | the CSV or XLSX; dispatched by filename extension + content-type |

**Response 200:**

```json
{
  "columns": [
    { "name": "emp_id",   "type": "Int64" },
    { "name": "dept",     "type": "String" },
    { "name": "fte",      "type": "Nullable(Float64)" }
  ],
  "row_count": 1284,
  "sample_rows": [[1001, "Engineering", 1.0], [1002, "Sales", 0.5]]
}
```

- `columns[i].name` is the **sanitized** identifier (`_sanitize_columns`, `admin_ingest.py:143`) — this
  is the key the browser's mapping is keyed on, and the exact name `upload` re-derives.
- `columns[i].type` is the inferred ClickHouse type (`infer_schema_streaming`, `admin_ingest.py:396`).
- `row_count` is the total data-row count (post-header). If it exceeds `scratch_max_rows` (10000,
  `config.py:205`), **fail here** with `413 SCRATCH_TOO_LARGE` (reuse the existing code) so the browser
  never renders a mapping UI for a file it can't materialize.
- `sample_rows` is a small preview (recommend ≤10 rows) so the browser can show cell examples next to
  each column while the user picks roles. Preview cells are the caller's own upload — no scope concern
  (they uploaded them), but still rendered via `textContent` in the UI (§5).

### 1b. `POST /scratch/v1/upload` — parse + rename + materialize (authoritative)

**Request (multipart):**

| Field | Type | Notes |
|---|---|---|
| `file` | file part | the SAME file (Option B) |
| `mapping` | form field, JSON string | `{ "<sanitized_col_name>": "<role>", … }`, role ∈ `EmployeeCode` \| `DepartmentCode` \| `DepartmentName` \| `none` |

**Mapping semantics (locked):**

| Role | Rename target (canonical join key) |
|---|---|
| `EmployeeCode` | `employee_code` |
| `DepartmentCode` | `department_code` |
| `DepartmentName` | `department_name` |
| `none` (or column absent from mapping) | keep the sanitized name unchanged |

- A role (other than `none`) may appear **at most once** — two columns mapped to `EmployeeCode` → reject
  `400 UPLOAD_MAPPING_INVALID`.
- A rename target must not collide with a **kept** column's name (e.g. an upload already has a column
  sanitized to `employee_code` AND another column mapped to `EmployeeCode`) → reject `400
  UPLOAD_MAPPING_INVALID`. Fail-closed; do not silently drop/merge.
- Mapping keys that name no parsed column are ignored (the file is authoritative). All roles optional —
  a user may map zero, one, or all three (an upload with only `department_name` is legal; the agent just
  joins on that key).

**Response 200:** exactly the existing materialize shape (pass through `scratch_ingest.materialize`'s
return verbatim):

```json
{ "table": "scratch.s_s1f2…_bp_9a3c…", "row_count": 1284 }
```

The browser shows `table` + `row_count` so the user knows what to ask against. The `s_<sid>_bp_…` name
is what makes the agent's later `JOIN` pass the D64 read gate (§6).

### 1c. Error codes (both endpoints)

| Code | HTTP | Trigger |
|---|---|---|
| `SCRATCH_SESSION_MISSING` | 400 | no bound `X-Session-Id` (fail-closed, reuse `mcp_server.py:670`) |
| `UPLOAD_TOO_LARGE` | 413 | file bytes over the front-door byte cap, **before** parse (§3) |
| `SCRATCH_TOO_LARGE` | 413 | parsed row_count over `scratch_max_rows` (reuse `ScratchTooLargeError`, `scratch_ingest.py:96`) |
| `UPLOAD_PARSE_ERROR` | 400 | not valid UTF-8 CSV / not a readable XLSX / empty file / no header |
| `UPLOAD_MAPPING_INVALID` | 400 | duplicate role, rename collision, or malformed `mapping` JSON |
| `SCRATCH_MATERIALIZE_REJECTED` | 400 | any downstream `ScratchWriteError` from materialize (bad identifier/type) |

Render via the existing `_scratch_error` helper (`mcp_server.py:655`) — `{"error", "code"}` JSON,
never echoing row data.

---

## 2. The parser — new `clickhouse-api/app/upload_ingest.py`

**Where it lives (decision):** a **new** `app/upload_ingest.py`, NOT an extension of `admin_ingest.py`.
Rationale — `admin_ingest` is the warehouse bulk-load path (request-supplied CH creds, 200 MB cap,
schema-diff against a target table); the upload front door is a session-scratch path (server creds,
few-MB cap, no target table). Different lifecycle, different trust model. But it **imports and reuses**
`admin_ingest`'s proven, side-effect-free parser primitives (same as `scratch_ingest` already reuses
`coerce`/`validate_identifier`/`validate_ch_type`, `scratch_ingest.py:56`):

- `infer_schema_streaming(content) -> (header, inferred_types, total)` (`admin_ingest.py:396`)
- `_sanitize_columns(header) -> list[str]` (`admin_ingest.py:143`)
- `infer_column_type(values) -> str` (`admin_ingest.py:332`) — for the XLSX per-column path

**Public surface (transport-agnostic, like `admin_ingest`):**

```python
def parse_upload(content: bytes, filename: str) -> tuple[list[dict[str, str]], list[list[Any]]]:
    """Dispatch CSV vs XLSX by extension; return ({name,type} columns, all data rows).

    Columns are sanitized identifiers with inferred ClickHouse types — the exact
    {columns, rows} shape scratch_ingest.materialize() consumes. Raises
    UploadParseError on undecodable / unreadable / header-less input."""

def apply_mapping(columns, rows, mapping) -> tuple[list[dict[str, str]], list[list[Any]]]:
    """Rename the role-tagged columns to the canonical join keys; validate no
    duplicate role / no target collision. Rows are untouched (rename is columns-only).
    Raises UploadMappingError."""
```

- **CSV path:** `infer_schema_streaming` for `(header, types, total)` + a full `csv.reader` pass for the
  data rows (cells stay **strings** — `scratch_ingest._coerce_row` runs each string through
  `admin_ingest.coerce` for its declared type at insert, `scratch_ingest.py:323-340`). Sanitize header
  via `_sanitize_columns`. Emit `columns=[{name,type}]`, `rows=[[str,…]]`.
- **XLSX path (new, `openpyxl`):** open with `openpyxl.load_workbook(io.BytesIO(content),
  read_only=True, data_only=True)`, take the active sheet's first row as the header, the rest as data.
  **Stringify every cell** (`"" if None else str(cell)`) so the XLSX path converges onto the **exact
  same** string-cell code as CSV: sanitize header with `_sanitize_columns`, infer each column's type
  with `infer_column_type(column_values)`, and let `scratch_ingest._coerce_row` coerce at insert. One
  coercion path for both formats; no XLSX-specific type handling in the write plane.
- Both paths produce a shape that materializes with **zero change** to `scratch_ingest.materialize`.

**Dependency addition:** add `openpyxl` to `clickhouse-api/requirements.txt` (no XLSX dep exists today;
`python-multipart==0.0.20` for `UploadFile` is already present, `requirements.txt:5`). Pin a current
release. See §7 for the XLSX-on-untrusted-input safety assessment (this is the slice's main YELLOW).

---

## 3. Front-door hardening (endpoint layer, before the parser)

- **Byte cap BEFORE parse** — read the upload, and if `len(content) > UPLOAD_MAX_BYTES` return `413
  UPLOAD_TOO_LARGE` before handing bytes to the parser. Recommend **8 MB** for phase-0 (a scratch
  upload capped at 10k rows is inherently small; this is far below admin's 200 MB `MAX_CSV_BYTES`,
  `routers/admin.py:24,54` — same *pattern*, session-appropriate *value*). Add as a settings field
  (mirror `scratch_max_rows` in `config.py:205`) so it's tunable. Guards against an XLSX
  decompression-bomb reaching openpyxl (§7).
- **Row cap reused** — `scratch_max_rows` (10000). Enforce at `analyze` (fail before the mapping UI) and
  again inside `materialize` (`scratch_ingest._validate_rows`, `scratch_ingest.py:304-313`). No new cap.
- **Session binding** — both routes require `current_session_id.get()`; None → `400
  SCRATCH_SESSION_MISSING` (fail-closed, identical to `mcp_server.py:669-674`). The materialized table's
  `s_<sid>_bp_…` prefix derives from that bound sid only (`scratch_ingest.scratch_table_name:150-198`),
  so cross-session writes are structurally impossible.
- **PII / learning isolation (confirm, don't change).** Uploaded data is often PII and **must not feed
  the entity-agnostic global stores**; upload sessions are **weak blueprint candidates** and, if learned
  at all, generalize to a "bring-your-own fact table with columns {employee_code, …}" slot — never the
  file or its values (`docs/07-external-data.md:57-60`). This slice adds **no learning-store write, no
  candidate extraction, no persistence** beyond the TTL'd session-scoped scratch table (D93 TTL 3600s,
  `config.py:196`). Nothing here changes that posture — stated so a builder does not add a "remember
  this upload" hook. The scratch table auto-GCs; no upload artifact outlives the session.

---

## 4. BFF proxy — `data-analysis-agent/ui/server.py`

Two new routes, `POST /api/upload/analyze` and `POST /api/upload`, that forward the browser's multipart
body to the clickhouse-api endpoints attaching the session's JWT + `X-Session-Id` — exactly the
credential pair `_proxy_stream` attaches for `/turn` (`ui/server.py:242`), sourced from the same
server-side `_SESSIONS` map (the browser never holds the JWT, D82/D5).

**Multipart forwarding differs from the JSON/SSE proxies — the key design note:** `_proxy_stream`
sends a JSON body and streams an SSE response; `_proxy_inbox` sends/receives JSON. Neither parses
multipart. The BFF must **NOT** parse the multipart form (that would require adding `python-multipart`
to the data-analysis-agent deps — it is absent from `pyproject.toml:26`, which lists only
`fastapi>=0.110`). Instead **pass the raw body through unparsed**: read `body = await request.body()`
and forward it to clickhouse-api with the browser's original `Content-Type` header (which carries the
multipart boundary). The BFF never inspects the file; it is a credential-attaching pipe. Response is
plain JSON (like `_proxy_inbox`, not SSE) — propagate the upstream status + JSON body verbatim so a
`413`/`400`/`401` reaches the browser's error branch unchanged.

```python
async def _proxy_upload(path: str, session_id: str, request: Request) -> JSONResponse:
    jwt = _jwt_for_session(session_id)                       # reuses ui/server.py:219
    body = await request.body()                              # bounded by the downstream byte cap
    headers = {
        "Authorization": f"Bearer {jwt}",
        "X-Session-Id": session_id,
        "Content-Type": request.headers.get("content-type", "application/octet-stream"),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{_scratch_base()}{path}", headers=headers, content=body)
    try:    content = resp.json()
    except ValueError:  content = {"detail": resp.text}
    return JSONResponse(status_code=resp.status_code, content=content)
```

- The `session_id` arrives as a query param (`/api/upload/analyze?session_id=…`) or a multipart field
  the BFF reads from the boundary — recommend **query param** so the BFF needs zero form parsing.
- **New env — the MCP/clickhouse-api base.** The scratch routes live on the MCP host, not `RUNTIME_URL`.
  Add `MCP_URL` (default `http://localhost:18090/mcp`, matching the runtime's own
  `runtime/config.py:35`) and derive the scratch base by swapping the path for `/scratch/v1` — the
  identical derivation the runtime already does in `runtime/config.py:362-374`. `_scratch_base()` =
  `urlsplit(MCP_URL)` → `(scheme, netloc, "/scratch/v1", "", "")`.
- **Trade-off:** `await request.body()` buffers the whole upload in BFF memory. Bounded by the few-MB
  cap (enforced downstream; the BFF may also short-circuit on `Content-Length` for a cheaper reject).
  Acceptable at phase-0; streaming (`content=request.stream()`) is the later optimization, noted not
  taken.

---

## 5. Upload UI — `data-analysis-agent/ui/static/index.html`

A widget added to the existing single-page UI, matching the vanilla-JS, `data-testid`, `textContent`
(XSS-safe) style already established for the enriched-result panels (the `els` map + `clearNode` +
`cellText` helpers at `index.html:604-635`, and the panel-render functions there). Uses the same
`sessionId` the page mints at load (`index.html:850`, `fetch("/api/session")`).

**Flow + elements (all `data-testid`'d):**

1. `upload-file-input` (file picker, `accept=".csv,.xlsx"`) + `upload-analyze-button`.
2. On analyze: `fetch("/api/upload/analyze?session_id=" + sessionId, {method, body: FormData(file)})`.
   Render `upload-column-list`: one row per `columns[i]` showing the sanitized name + inferred type + a
   sample value (from `sample_rows`, via `textContent`), each with a role `<select>`
   (`upload-role-select`, options EmployeeCode / DepartmentCode / DepartmentName / none, default none).
3. `upload-confirm-button` → build the `mapping` JSON from the selects, POST
   `/api/upload?session_id=…` with `FormData{file, mapping}`.
4. On success render `upload-result` = `scratch table ready: <table> (<row_count> rows)` via
   `textContent`, telling the user they can now ask questions that join it. Keep the table name visible.
5. **Error states** (reuse the existing `showError(code, message)` banner, `index.html:459`): `413`
   too-large (byte or row), `UPLOAD_PARSE_ERROR`, `UPLOAD_MAPPING_INVALID`, `SCRATCH_SESSION_MISSING`
   (no session — shouldn't happen post-init, surfaced fail-closed). Client-side, disable
   `upload-confirm-button` and flag duplicate-role selections before POST for a fast path, but the
   server rejection is authoritative.

All cell/column/table strings render through `textContent` (never `innerHTML`) — an uploaded column
header or cell value is untrusted text (§ house rule, mirrors `cellText`/`renderProvenance` at
`index.html:660-690`).

---

## 6. How the agent uses it — **NO runtime / agent change** (state it so builders add nothing)

Once the scratch table exists, the agent reaches it through the **unchanged read-only MCP**:

- discovers it via `listTables` / `getTableSchema` (the scratch db is in the allowlist,
  `docs/07-external-data.md:25-34`),
- `JOIN`s it via `runQuery` on the canonical keys (`employee_code` / `department_code` /
  `department_name`) the mapping renamed to,
- the **D64 read gate authorizes the JOIN automatically** because the table is
  `s_<injected session_id>_bp_…` and `_validate_scratch_name` (`clickhouse-api/app/sqlparse/provenance.py:249-302`,
  fail-closed on a None/mismatched session) extracts the owning session and matches it to the caller's
  bound sid.

**No join helper, no new tool, no executor change, no prompt change.** "Zero new data-plane tools"
(`docs/07-external-data.md:35`) holds. A builder must NOT add a `joinScratch`/`uploadTable` tool — the
front door is the two clickhouse-api REST routes + the BFF proxy, and the agent side is already
complete. (The `catalog/loader.py` / `is_scratch_table` names from earlier notes are **stale** — the
read gate lives entirely in `provenance.py:249`.)

---

## 7. Feasibility (GREEN / YELLOW / RED)

| Piece | Rating | Reason |
|---|---|---|
| Reuse `materialize` back-half | **GREEN** | `scratch_ingest.materialize` consumes `{columns, rows}` as-is (`scratch_ingest.py:392`); route + D92 binding + row cap + D64 read gate all built and unchanged. |
| CSV parse | **GREEN** | `infer_schema_streaming` + `_sanitize_columns` + `coerce` are in-repo, battle-tested, already reused by `scratch_ingest`. Only a full-row read is new. |
| `analyze` / `upload` routes | **GREEN** | Direct siblings of the existing `/scratch/v1/materialize` route + admin's analyze/ingest split. Multipart via the already-present `python-multipart`. |
| Byte cap + row cap + session bind | **GREEN** | Byte cap mirrors `MAX_CSV_BYTES`; row cap + session-missing reuse existing code paths verbatim. |
| BFF multipart proxy | **YELLOW** | Multipart differs from every existing proxy (`_proxy_stream`/`_proxy_inbox` are JSON/SSE). Mitigated by **raw-body passthrough** (no `python-multipart` dep added to data-analysis-agent) + a new `MCP_URL` env deriving `/scratch/v1` like the runtime does. Buffers body in memory — bounded by the cap. |
| analyze→materialize hand-off | **YELLOW → RESOLVED** | The state hand-off is the real fork; **resolved as Option B (re-send the file, `upload` re-parses)** — stateless, no cache, deterministic re-parse. Cost: parse twice. See §0. |
| **XLSX via openpyxl on untrusted input** | **YELLOW (the slice's main risk)** | New dep + new attack surface: XLSX is a zip → **decompression-bomb / zip-bomb** risk, plus openpyxl XML entity-expansion history. **Mitigations (all required):** (1) the pre-parse **byte cap** (§3) caps the compressed input; (2) `read_only=True` streams rows instead of loading the whole sheet; (3) `data_only=True` reads cached values, never evaluates formulas; (4) the **row cap** bounds materialized rows even if the sheet is huge; (5) **stringify all cells** so no XLSX-native type reaches the write plane. Residual: a small compressed file can still expand to many in-memory rows before the row cap trips — the row check must run **as rows are streamed**, capping early, not after a full materialization. Flag for the reviewer. |
| Agent JOIN path | **GREEN** | Zero change — D64 gate already authorizes `s_<sid>_*` (§6). |

**No RED.** Nothing blocks parallel fan-out.

### Fan-out plan (3 disjoint builders, one seam = this doc)

| Builder | Owns | Files | Depends on |
|---|---|---|---|
| **B1 — clickhouse-api** | `analyze` + `upload` routes, `upload_ingest.py` parser, `openpyxl` dep, byte-cap setting | `app/mcp_server.py`, new `app/upload_ingest.py`, `app/config.py`, `requirements.txt` | §1, §2, §3 |
| **B2 — BFF** | `/api/upload/analyze` + `/api/upload` raw-multipart proxy, `MCP_URL` env + `_scratch_base()` | `ui/server.py` | §4 (endpoint paths + error shape from §1c) |
| **B3 — UI** | upload widget, role dropdowns, result line, error states | `ui/static/index.html` | §1 (response shapes), §5 |

The seams: B2↔B1 is the §1 request/response + §1c error codes; B3↔B2 is the §1 JSON shapes surfaced
1:1 through the BFF; B3↔B1 never touch directly. The cost lands at those two seams, both pinned here.

---

## 8. Test hooks

| Test | Layer | Live stack or fake? |
|---|---|---|
| `parse_upload` CSV → columns/types/rows (incl. Nullable widening, sanitized headers, BOM) | unit | **fake** — pure bytes in, no CH |
| `parse_upload` XLSX → same shape as the equivalent CSV (round-trip parity) | unit | **fake** — bytes via a fixture .xlsx |
| `apply_mapping` — rename to canonical keys; duplicate role → `UPLOAD_MAPPING_INVALID`; target collision → reject | unit | **fake** |
| Byte cap → `413 UPLOAD_TOO_LARGE` (before parse); row cap → `413 SCRATCH_TOO_LARGE` | unit/endpoint | **fake** for the cap logic; over-cap row count needs no CH |
| `analyze` route: multipart in → `{columns,row_count,sample_rows}`; missing session → `400 SCRATCH_SESSION_MISSING` | endpoint | **fake** parser; **live CH not needed** (analyze never writes) |
| `upload` route: file+mapping → materialized table name + row_count; parse error → `400` | endpoint | **live ClickHouse** (materialize does real DDL+insert) — mark as an integration test, like existing scratch materialize tests |
| End-to-end: upload → agent `runQuery` JOIN on `employee_code` passes the D64 gate | integration | **live CH** — asserts the `s_<sid>_*` table is JOIN-able by the same session and rejected for another (reuse existing D64 read-gate tests) |
| BFF `_proxy_upload` attaches JWT + `X-Session-Id`, forwards raw body + content-type, propagates upstream status | BFF unit | **fake** upstream (httpx mock / `MockTransport`) — asserts the credential-injection boundary, no CH |
| UI trace: pick file → analyze renders columns + role selects → confirm → "scratch table ready …"; 413 + mapping-invalid render in the banner | UI (Playwright) | **fake/scripted** BFF+upstream — mirrors the existing `run_ui_runtime.py` scripted path; no live CH |

The parser + mapping + BFF proxy + UI trace all run on **fakes**; only the two `upload`/end-to-end
materialize assertions need the live ClickHouse stack (they exercise the already-tested write plane).

---

**Status:** Contract-locked. Reuses D92 (session binding), D64 (scratch read gate), D93 (scratch
write-plane). No Dxx minted.
**Open (deferred, non-blocking):** streaming BFF body passthrough (vs. buffered); XLSX date/number
fidelity beyond stringification; multi-sheet XLSX (phase-0 reads the active sheet only).
