# Phase-0 Agent Runtime — Design Document

**Status:** Proposed (design, not yet built). Companion to [DECISIONS.md](DECISIONS.md),
scoped strictly to **D68 Phase 0** (the raw-loop "walking skeleton" alpha).

**Out of scope (explicitly, per the brief):** D77 `resolveValues`; the D78 `getTableSchema` catalog
overlay — **note: D83 (2026-07-01) permanently relocates this to the `clickhouse-api` MCP, so it is not
merely deferred here, it is never coming to the runtime.** The runtime's `getTableSchema` stays a
passthrough of an already-overlaid, already-scope-filtered MCP response once D83 ships (see
[mcp-overlay-design.md](mcp-overlay-design.md)); no runtime-side overlay work is needed in Phase 1.
Also out of scope: the retrieval pipeline (D7/D8), blueprints (D9–D14, D32–D43, D56, D59), the learning
loop (D15–D18, D26–D31, D58), skills/hooks (D72–D74). These are Phase 1/2 and are not designed here.

## 0. Ground truth this design relies on (verified by reading code, not assumed)

- `clickhouse-api` (adopted MCP, do-not-modify) exposes exactly 6 tools over **FastMCP
  streamable-HTTP** at `POST http://<host>:<port>/mcp` (`app/mcp_server.py`). Auth is a pure-ASGI
  `JWTAuthMiddleware`: Bearer JWT → `validate_token()` (PyJWT + `PyJWKClient`, RS256, JWKS) →
  `Principal{subject, claims, column_scope: frozenset[str]}`; `X-Session-Id` header (unsigned) →
  `current_session_id`. Tool errors surface as `ToolError("[{CODE}] message")` — codes:
  `COLUMN_SCOPE_VIOLATION`, `SCRATCH_SESSION_VIOLATION`, `PARSE_FAILED_CLOSED`,
  `DATABASE_NOT_ALLOWED`, `TABLE_NOT_FOUND`, `CLICKHOUSE_QUERY_ERROR`, `CLICKHOUSE_UNAVAILABLE`.
- `getTableSchema(database, table)` → `{database, table, columns: [{name, type, comment}]}`
  (`app/service.py:get_table_schema`) — **introspection only** in Phase 0 today; this stays true only
  until the D83 MCP-side overlay + scope-filter ships (D78, which this line originally cited, is
  reversed by D83 — see the "Out of scope" note above).
- `runQuery` / `sampleRows` return the compact shape `{columns, rows, row_count, truncated}`.
- `column_scope` on the JWT is a **JSON-encoded list of `"database.table.column"` strings**
  (`app/token_service.py`); `[]` (empty list → empty frozenset) = **allow-all** (D80b). This is the
  exact granularity D69/OQ-3 requires our provenance extractor to produce.
- `clickhouse-api`'s own `examples/responses_api_demo.py` uses OpenAI's **hosted remote-MCP tool
  type** (`{"type": "mcp", "server_url": ...}`), where **OpenAI's servers call the MCP directly**.
  **This pattern is unusable for our runtime** — see §4 "Critical architecture call".
- Our repo already has `data_agent.catalog.build_sqlglot_schema()` and
  `data_agent.sqlparse.extract_column_provenance()`, both pure/sync, tested (39 Layer-1 cases),
  D69/D70-compliant. This design reuses them as-is; it does not change their signatures.

---

## 1. Module layout — `src/data_agent/runtime/`

```
src/data_agent/runtime/
  __init__.py
  config.py                # RuntimeSettings (pydantic-settings): env surface, tunables (§11)
  app.py                    # composition root: wires stores/clients into one TurnOrchestrator; HTTP entrypoint

  auth/
    __init__.py
    credentials.py          # RuntimeCredentials (frozen dataclass): jwt, session_id, column_scope
    jwt_verify.py            # local JWKS verification of the inbound JWT -> column_scope (mirrors clickhouse-api's validate_token; defense-in-depth + needed for D44 replay-filtering)

  mcp/
    __init__.py
    client.py                 # MCPClient Protocol
    real_client.py              # RealMCPClient: mcp SDK streamablehttp_client + ClientSession pool
    fake_client.py               # FakeMCPClient: scripted in-memory responses (Layer-1 tests)
    tool_schema.py               # fetch+cache MCP list_tools() -> translate to OpenAI function-tool JSON schema

  provenance/
    __init__.py
    capture.py                # per-tool-result provenance capture (runQuery via extract_column_provenance;
                               #   sampleRows/getTableSchema via "all columns of the referenced table")
    catalog_handle.py            # loads build_sqlglot_schema() once at startup; read-only handle passed around

  dispatch/
    __init__.py
    tool_dispatcher.py         # ToolDispatcher: model tool-call -> credential injection -> MCP call ->
                               #   provenance tag -> ToolResult; graceful-denial mapping (D57/06)
    denial_mapping.py             # ToolError code -> {retryable-by-model, user-facing-message}

  session/
    __init__.py
    models.py                  # TrailEntry, TurnMessage, PauseCheckpoint, SessionDoc (dataclasses)
    store.py                     # SessionStore Protocol
    memory_store.py                # InMemorySessionStore (fake, Layer-1)
    couchbase_store.py               # CouchbaseSessionStore (real, Layer-2/3)

  context/
    __init__.py
    scope_filter.py             # D44: drop trail entries whose provenance ⊄ current scope
    budget.py                     # D46: preview-only results + token-budgeted history + compaction cache
    assembly.py                     # ContextAssembler: D50 fixed order (load -> filter -> budget -> inject)

  model/
    __init__.py
    client.py                  # ModelClient Protocol; ModelTurnResult, ToolCallRequest
    openai_client.py              # OpenAIModelClient: Responses-primary / Chat-fallback (D71)
    scripted_client.py               # ScriptedModelClient: cassette-style double (Layer-1 tests)

  loop/
    __init__.py
    budget_guard.py             # D47/D55: iteration/token/wall-clock ceilings; fresh-window grant
    agent_loop.py                  # AgentLoop: turn state machine (§4)

  observability/
    __init__.py
    tracing.py                  # OTel/Phoenix setup; span helpers (AGENT/LLM/TOOL/CHAIN)
    redaction.py                   # PII redactor: scope hash, cell/literal masking, shape-only logging
    progress.py                      # progress-event emitter (same span boundaries, D61)
```

One-line responsibility per package: `auth` = model-invisible credential handling (D5); `mcp` =
transport + schema bridging to the adopted MCP (D75); `provenance` = the D44/D57 USES-set
computation reused from `data_agent.sqlparse`/`catalog`; `dispatch` = the single choke point where
every tool call is executed and tagged; `session` = Couchbase persistence + DI seam (D22/D44/D45);
`context` = D50 assembly pipeline; `model` = the OpenAI provider abstraction (D71); `loop` = the
per-turn state machine + budget caps (D47); `observability` = D23/D24/D25/D61 as one instrumentation.

---

## 2. Per-session/request state model (D5 model-invisibility)

```python
# auth/credentials.py
@dataclass(frozen=True)
class RuntimeCredentials:
    session_id: str                 # from UI, forwarded as X-Session-Id (D81)
    jwt: str                        # opaque bearer string, forwarded as-is to the MCP (D79b/D82)
    column_scope: frozenset[str]    # decoded+verified locally from the jwt (see §11 OQ-A);
                                     # "" (empty) == allow-all, matching clickhouse-api's Principal
```

**Design rule (the load-bearing property):** `RuntimeCredentials` is constructed exactly once per
inbound HTTP turn request (in `app.py`, from the request's `Authorization` header + `X-Session-Id`
header — the UI/UI-backend already minted and holds the JWT per D82) and is then **threaded as an
explicit function/constructor argument** down the call chain:

```
app.py (turn handler)
  -> ContextAssembler.assemble(session_id, column_scope)      # scope only, no jwt needed here
  -> AgentLoop.run(credentials, context, ...)
       -> ToolDispatcher.dispatch(tool_name, model_args, credentials)
            -> MCPClient.call_tool(tool_name, model_args, jwt=credentials.jwt,
                                    session_id=credentials.session_id)
```

`RuntimeCredentials` is **never** placed into the `messages`/`input` payload sent to
`ModelClient.send_turn(...)` — the model only ever sees `tool_name` + the model-declared argument
shape (§3). This is deliberately explicit-parameter-passing rather than a `contextvars.ContextVar`
(unlike `clickhouse-api`, which needs a ContextVar because FastMCP's tool functions have no
credential parameter it controls): our own runtime code controls every call site, so a plain
argument is simpler to reason about, and — critically — trivially testable: a unit test can assert
"no message ever serialized to `ModelClient` contains the JWT substring" without needing to reason
about async task-local context propagation.

**Catalog schema handle:** `provenance/catalog_handle.py` calls
`data_agent.catalog.build_sqlglot_schema()` **once** at process startup (it is pure/deploy-coupled,
D53) and hands the resulting dict to `ContextAssembler`/`ToolDispatcher` as a read-only, shared,
immutable object — never a global. Same handle used for all sessions and requests.

---

## 3. MCP client + tool-dispatch layer

### 3.1 Transport

`RealMCPClient` wraps the `mcp` Python SDK exactly as `clickhouse-api`'s own
`examples/mcp_client.py` demonstrates:

```python
async with streamablehttp_client(
    mcp_url, headers={"Authorization": f"Bearer {jwt}", "X-Session-Id": session_id}
) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(tool_name, args)
```

A `ClientSession` is opened **per dispatched tool call** in the first cut (simplest, matches the
MCP's own `stateless_http=True` posture — "any replica can serve any request" — so there is no
server-side session affinity to preserve). Session/connection pooling (reuse one `ClientSession`
per runtime-turn or per HTTP/2 connection) is a pure performance optimization deferred to an open
question (§11) — correctness does not depend on it.

### 3.2 Model-visible tool schemas (6 + `askUser`)

Rather than hand-authoring 6 JSON schemas that can drift from the MCP's own (a real risk — the MCP
is adopted/external, D75, and can change independently), **`tool_schema.py` fetches
`session.list_tools()` from the live MCP at runtime startup** (cached; refreshed on a configurable
TTL or on explicit reload) and translates each `Tool.inputSchema` (JSON Schema, produced by FastMCP
from the `Annotated[..., Field(...)]` signatures we read in `app/mcp_server.py`) into an OpenAI
function-tool declaration:

```python
{
    "type": "function",
    "name": tool.name,              # listDatabases | listTables | getTableSchema |
                                     # sampleRows | runQuery | explainQuery
    "description": tool.description,
    "parameters": tool.inputSchema, # passed through verbatim — single source of truth is the MCP
}
```

`askUser` is the one **locally-authored** tool (it has no MCP equivalent — it is a runtime control
primitive, D6/D45):

```python
{
    "type": "function",
    "name": "askUser",
    "description": "Pause and ask the user a clarifying question, then resume with their answer.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "nullable": True},
        },
        "required": ["question"],
    },
}
```

Neither schema declares `session_id`, `jwt`, or `scope` as a parameter (D5) — those never appear in
any model-visible JSON.

### 3.3 `ToolDispatcher` — the single choke point

```python
class ToolDispatcher:
    def __init__(self, mcp_client: MCPClient, catalog_schema: dict, redactor: Redactor): ...

    async def dispatch(
        self, tool_name: str, model_args: dict, credentials: RuntimeCredentials
    ) -> ToolResult:
        """Execute one model-requested tool call end-to-end:
        1. credential injection (jwt/session_id attached at the MCP-call boundary only)
        2. MCP call (askUser is intercepted upstream in the agent loop, never reaches here)
        3. graceful-denial mapping on ToolError (denial_mapping.py)
        4. provenance capture for data-returning tools (runQuery/sampleRows/getTableSchema)
        5. emit a TOOL span (redacted) + a progress event
        """
```

**Provenance capture per tool** (task requirement #3):

| Tool | Provenance source | Notes |
|---|---|---|
| `runQuery(sql, limit)` | `extract_column_provenance(sql, catalog_schema, session_id=credentials.session_id)` | Sync/pure — see §3.4 for the async-loop concern. Raises `ProvenanceExtractionError`/`ScratchSessionError` on fail-closed cases (D63/D69/D70); the runtime's **own** independent parse (separate from the MCP's, which already gated the *live* call) — see OQ-B in §11 on version-skew risk. |
| `sampleRows(database, table, limit)` | Declarative: `{(f"{database}.{table}", col) for col in catalog_schema.get(f"{database}.{table}", {})}` | `SELECT *` semantics — every column of the table is "referenced." If the table is uncatalogued, provenance is empty/undetermined → **fail-closed, drop from replay** (consistent with D44's "no provenance ⇒ drop"). |
| `getTableSchema(database, table)` | Same declarative rule as `sampleRows` | No PII *rows* are returned by this tool, but it is tagged for uniformity/defense-in-depth per the brief's explicit instruction and 06's "computable for every data-returning tool" language. |
| `listDatabases`, `listTables`, `explainQuery` | No provenance (no column-level data exposure) | Recorded in the trail with an empty provenance set; never gated. |

**Async/sync boundary (task requirement #3, explicit callout):** `extract_column_provenance` is
pure CPU-bound Python (a `sqlglot` parse + optimizer pass), not I/O, but it runs inside an `asyncio`
event loop that is also juggling concurrent MCP calls and the OpenAI client. To avoid one large/
pathological query's parse blocking the loop, `ToolDispatcher` calls it via
`await asyncio.to_thread(extract_column_provenance, sql, catalog_schema, session_id=...)`.
This is a pure performance/fairness measure — correctness (fail-closed on parse failure) is
identical whether run inline or in a thread; the design does not rely on thread-pool semantics for
correctness.

### 3.4 Graceful-denial mapping (D57/06 §Graceful denial)

`denial_mapping.py` parses the `[{CODE}] ...` prefix `_domain_to_tool_error` already emits
(app/mcp_server.py) and classifies:

| Code | Model retry makes sense? | User-facing surfacing |
|---|---|---|
| `COLUMN_SCOPE_VIOLATION` | No — no query rewrite grants access | Clean denial: "This needs access to columns outside your current permissions." Tagged as a `GUARDRAIL`-adjacent event, not silently retried. |
| `SCRATCH_SESSION_VIOLATION` | No | "That data isn't available in this session." (Phase 0 has no upload feature yet, but the tool/error path exists and must be handled — D64.) |
| `PARSE_FAILED_CLOSED` | Sometimes (rephrase) | Tool error text passed back to the model **and** flagged for the UI as "I couldn't validate that query safely — let me try `explainQuery` first" nudging the model's own next step, per the MCP's own error message. |
| `DATABASE_NOT_ALLOWED`, `TABLE_NOT_FOUND`, `CLICKHOUSE_QUERY_ERROR` | Yes | Fed back to the model as a normal tool-error content block; the model self-corrects (matches the MCP's own design intent). |
| `CLICKHOUSE_UNAVAILABLE` | No (transient) | Surfaced to the user as "the data warehouse is temporarily unavailable"; counts toward the D47 budget so a flapping backend still terminates the loop rather than spinning. |

All seven code paths increment the loop's iteration/token counters (§4) — a rejected call is not
free.

---

## 4. Agent loop

### Critical architecture call (flag for sign-off, §11 sub-decision A)

`clickhouse-api`'s own `examples/responses_api_demo.py` shows OpenAI's **hosted remote-MCP tool
type** (`{"type": "mcp", "server_url": MCP_URL, "authorization": MCP_API_KEY}`). In that mode
**OpenAI's servers connect directly to the MCP** — our runtime process is not in the loop for
individual tool calls. This is architecturally incompatible with Phase 0's hard requirements:

- **D5** requires the runtime to inject credentials at dispatch and keep them model-invisible; a
  hosted-MCP tool instead hands the *raw bearer token* to OpenAI as a tool-config field — a
  different (weaker) trust boundary than "the model never holds the token."
- **D44** requires the runtime to compute and persist per-result column-provenance for every trail
  entry; with hosted MCP, tool results never pass through our process.
- **D25/D61** require redacted spans and progress events at each tool call; hosted MCP tool calls
  are invisible to our OTel instrumentation and progress emitter.

**Therefore the runtime must use OpenAI's ordinary `type: "function"` tool-calling** (Responses API
function tools / Chat Completions `tools`), where **our own `AgentLoop` executes every tool call**
via `ToolDispatcher` → `RealMCPClient`, exactly like a conventional agent framework. This is a new,
previously-unstated sub-decision that follows necessarily from D5+D44+D61 but is not itself locked
in DECISIONS.md — flagged in §11 for explicit sign-off (it forecloses ever using hosted-MCP mode
for this agent, which is a real trade-off: we give up OpenAI's server-side tool-call
infrastructure and own the whole dispatch loop).

### 4.1 Turn state machine

```
1. Inbound turn: {message, session_id, jwt} → build RuntimeCredentials.
2. ContextAssembler.assemble(session_id, column_scope) → messages[] (§5).
3. loop:
     a. LLM span: ModelClient.send_turn(messages, tools) → ModelTurnResult
        (tries Responses API; falls back to Chat Completions on a defined
        transient/availability error class — see OQ-C)
     b. if result.tool_calls is empty: turn complete → persist final assistant
        message, stream it, return. [TERMINATION: normal]
     c. for each tool_call (parallel calls dispatched concurrently, capped —
        see §11 tunables):
          - if tool_call.name == "askUser":
                PAUSE: persist PauseCheckpoint (§6), stream the question,
                return control to the caller. [TERMINATION: pause]
          - else:
                result = ToolDispatcher.dispatch(name, args, credentials)
                append {tool_call_id, result} to messages + to the trail
     d. BudgetGuard.check(iterations, tokens, wall_clock):
          - under cap → continue loop (back to 3a)
          - at cap → synthesize an askUser("this is taking a while; continue,
            refine, or stop?") exactly like 3c's pause path, but
            runtime-triggered rather than model-triggered. [TERMINATION: budget pause]
4. Resume path (separate endpoint): CAS-consume the checkpoint (§6), thread the
   user's answer back into messages, and re-enter the loop at step 3 with a
   FRESH budget window if the pause reason was "budget" and the answer was
   "continue" (D55/N8). A "stop" answer ends the turn with the best partial
   result already in the trail.
```

**Termination conditions, explicit:**
1. Normal — model emits a tool-call-free response.
2. Pause (model-invoked `askUser`) — durable checkpoint, stateless resume.
3. Pause (budget-cap, D47) — same checkpoint mechanism, different trigger; "continue" grants a
   fresh window (D55), re-prompting only if that window also exhausts.
4. Hard outer ceiling — an absolute cap on the *number of budget windows* a single turn may grant
   itself (proposed default 3, tunable — §11 OQ-D) prevents an unbounded "continue" loop from a
   confused or adversarial session; on hit, the runtime force-stops with the best partial answer
   and does **not** offer another "continue."

**Read-only, no verify gate:** Phase 0 has no blueprints and no D56 verification gate — the raw
loop's only "safety net" is the MCP's own read-only enforcement + column-scope enforcement (D57)
and the budget cap (D47). The final assistant message is returned to the user as-is; there is no
code-computed grain check (that's D56/Phase 1).

### 4.2 Responses-primary / Chat-fallback (D71)

`OpenAIModelClient.send_turn` tries `client.responses.create(...)` first. On a defined set of
errors — 5xx from OpenAI, timeouts, or a hard "feature unsupported" error — it retries the **same
logical turn** via `client.chat.completions.create(...)`, translating the tool-call/tool-result
shapes between the two APIs' formats. Once a turn has fallen back, subsequent iterations **within
that turn** stay on Chat Completions (avoids mixing message formats mid-turn); the **next** turn
re-attempts Responses first. The precise error taxonomy that triggers fallback is not specified by
D71 and is flagged as an open question (§11 OQ-C).

---

## 5. Context assembly (non-retrieval, D50 fixed order)

```python
# context/assembly.py
class ContextAssembler:
    async def assemble(self, session_id: str, column_scope: frozenset[str]) -> list[Message]:
        raw_trail = await self.session_store.load_trail(session_id)          # 1. load
        in_scope = scope_filter.filter_trail(raw_trail, column_scope)         # 2. D44 filter
        budgeted = budget.compact(in_scope, token_budget=self.settings.history_token_budget)
                                                                               # 3. D46 budget/compact
        return budget.render_messages(budgeted, preview_n=self.settings.preview_row_count)
                                                                               # 4. inject
```

- **Step 2 (D44):** `scope_filter.filter_trail` drops any `TrailEntry` whose `provenance` (the
  frozenset captured at write-time by `ToolDispatcher`, §3.3) is not a subset of `column_scope`,
  **unless `column_scope` is empty** (`[]` = allow-all, matching the MCP's own D80b semantics
  exactly — the runtime's replay-filter must use the identical convention as the live-query gate,
  or a narrow-but-nonempty scope would filter differently than the MCP enforces). Pure function,
  no I/O — the primary Layer-1 test target for D44.
- **Step 3 (D46):** newest turns verbatim up to `history_token_budget`; overflow is compacted via
  one running-summary LLM call that **must preserve every kept turn's SQL verbatim** and
  paraphrase only prose. The summary is a **derived view**, recomputed per turn from the (already
  scope-filtered) survivors and **cached by `(scope_hash, content_hash_of_compacted_set)`** — an
  in-process LRU by default (a cache miss is always safe/correct, just slower — never a
  correctness dependency; see §11 OQ-E for whether to also persist the cache on the session doc so
  multiple runtime instances share it).
- **Preview object** built once at write-time (not recomputed every assembly) and stored on the
  `TrailEntry` itself: `{sql, columns, row_count, truncated, preview_rows: rows[:N]}` — the **full**
  result is written separately for learning-loop consumption (Phase 1 concern; Phase 0 just needs
  to persist it, not read it back).
- **Ordering is load-bearing** (D50): filtering strictly before compaction means the summarizer LLM
  call in step 3 never sees an out-of-scope entry, so the resulting prose is safe by construction
  and needs no residual per-column provenance tag.

### 5.1 Message-provenance filter (2026-07-01 D44 clarification)

The tool trail was the only thing D44's scope re-filter originally covered — but `assemble()`
(step 1's `_build_canonical_messages` in `loop/agent_loop.py`) also replays every persisted
`TurnMessage` (prior user/assistant prose) verbatim into the model context. An assistant's free-text
answer from a wide-scope turn ("Jane Doe's salary is $85,000...") is derived from exactly the same
warehouse data the tool trail is, so replaying it unfiltered after the user's `column_scope` narrows
is the same leak D44 closes for the trail — just via a different surface. **This is now a scope-filtered
layer too, using the identical subset/`None` semantics as the trail filter:**

- Every **assistant** `TurnMessage` is tagged, at write time (`loop/agent_loop.py`, when the loop
  persists the end-of-turn assistant message), with the **union of the column-provenance of every
  `TrailEntry` produced in that message's `turn_index`** (across every budget window of that turn).
  If **any** of that turn's tool results had undetermined provenance (`None`), the assistant
  message's provenance is `None` too — fail-closed, matching `TrailEntry`'s own rule. A turn with no
  tool calls at all (a pure clarification/chat turn) is determined-empty (`frozenset()`) — always
  kept.
- **User** `TurnMessage`s carry no warehouse-derived data (they are the user's own input) — always
  `frozenset()` (never `None`), and never dropped by the replay filter.
- At assembly time, `context/scope_filter.py::filter_messages` drops any assistant message whose
  provenance is not a subset of the current `column_scope`, using the **exact same** subset/`None`
  logic as `filter_trail` — factored into a single shared helper (`is_provenance_in_scope`) so the
  trail filter and the message filter can never diverge.
- `TurnMessage.provenance` uses the identical wire encoding as `TrailEntry.provenance`
  (`session/models.py`): `None` -> JSON `null`, `frozenset()` -> `[]`, non-empty -> `[["db.table",
  "column"], ...]`.

This closes D44's highest-value leak surface: a narrowed-scope user re-asking a question can no
longer see a prior turn's already-answered PII replayed back into the model's context, even though
the underlying tool-result rows were themselves correctly dropped by the existing trail filter.

### 5.2 Turn-scoped continuity refinement (2026-07-01, correctness fix)

D44's replay filter (§5 step 2) as originally implemented gated **every** trail entry identically on
every `assemble()` call, including entries from the turn currently in progress. Because `AgentLoop`
rebuilds its canonical message list from the store on every single model round-trip (D45
statelessness), this meant a denied/errored tool call (`TrailEntry.provenance` is always `None` for a
denial/error — `dispatch/tool_dispatcher.py`) was invisible to the model even on the very next
iteration of the **same** turn — defeating §3.4's "retryable codes like `TABLE_NOT_FOUND` /
`CLICKHOUSE_QUERY_ERROR` are fed back to the model as a tool-error block; the model self-corrects."

**The fix — clarify that D44 governs REPLAY of PRIOR turns, not the in-progress one — but ONLY for
entries that carry no result rows:** `scope_filter.filter_trail` (and `ContextAssembler.assemble`)
take an optional `current_turn_index: int | None = None`: an entry with `entry.turn_index ==
current_turn_index` **AND `entry.status != "ok"`** is kept regardless of provenance; every other
entry — any prior-turn entry, AND any current-turn entry that succeeded (`status == "ok"`) — is still
gated by the unchanged, strict subset/`None` check. `AgentLoop._build_canonical_messages` threads the
loop's own `turn_index` through as `current_turn_index`.

The status gate is load-bearing, not defensive: a **successful** current-turn tool result IS
data-bearing (`ToolResult.result_preview`/`result_full` are populated only for `status == "ok"`), and
the adopted MCP does **not** itself column-scope `sampleRows`/`getTableSchema` results (only
`runQuery` is column-scoped server-side, D80(b)) — `provenance/capture.py` computes their provenance
declaratively as "all columns of the referenced table" (`SELECT *` semantics), which is frequently
**not** a subset of a narrow `column_scope`. Exempting `status == "ok"` entries unconditionally on the
current turn would surface exactly these out-of-scope rows to the model this turn — a real PII leak,
not a hypothetical one. Only `status in {"denied", "error"}` entries — which per
`dispatch/tool_dispatcher.py` NEVER set `result_preview`/`result_full` — are exempt from the drop.

- **Cross-turn D44 is unchanged**: a prior turn's undetermined/denied entry is still always dropped
  on replay, under every scope including allow-all — the exemption only ever applies to
  `entry.turn_index == current_turn_index`.
- **A successful current-turn entry is never exempt**: it is always subject to the ordinary strict
  `is_entry_in_scope` check, exactly as if it belonged to a prior turn.
- **Default `None` preserves the original all-strict behavior exactly** — no caller that omits the
  parameter (e.g. the QA-locked
  `tests/runtime/provenance/test_fail_closed_replay_adversarial.py`) observes any behavior change.
- The **message filter** (§5.1, `filter_messages`) is untouched: an end-of-turn assistant
  `TurnMessage` is only ever persisted *after* the turn's model round-trips are done, so there is
  never an "in-progress" assistant message to exempt.

---

## 6. Session store (Couchbase, D22/D44/D45)

### Proposed document shape

```jsonc
{
  "_id": "session::<session_id>",
  "session_id": "<uuid>",
  "created_at": "2026-07-01T12:00:00Z",
  "last_activity": "2026-07-01T12:03:41Z",
  "learning_status": "active",              // active|pending|queued|processing|done (Phase-1 consumer;
                                             // Phase-0 just needs to bump last_activity)
  "messages": [
    {"turn_index": 0, "role": "user", "content": "...", "ts": "..."},
    {"turn_index": 0, "role": "assistant", "content": "...", "ts": "..."}
    // thinking discarded (D22) — only user/assistant/tool-result content persists
  ],
  "tool_trail": [
    {
      "turn_index": 0,
      "tool_call_id": "call_abc123",
      "tool_name": "runQuery",
      "args": {"sql": "SELECT ... FROM payroll_fact ...", "limit": null},
      "status": "ok",                          // ok | denied | error
      "error_code": null,                       // e.g. COLUMN_SCOPE_VIOLATION when status=denied
      "provenance": [["dbpcm_warehouse.payroll_fact", "GrossPay"], ["dbpcm_warehouse.payroll_fact", "PayPeriod"]],
      "result_preview": {
        "columns": ["dept", "gross_pay"],
        "row_count": 4213,
        "truncated": true,
        "preview_rows": [["Sales", 128000.0], ["Eng", 341000.0]]   // ≤ N rows (tunable)
      },
      "result_full_ref": "result::<uuid>",      // separate doc/collection if large — see note below
      "ts": "..."
    }
  ],
  "pause_checkpoint": null,
  // when paused:
  // "pause_checkpoint": {
  //   "reason": "askUser" | "budget_cap",
  //   "pending_question": {"question": "...", "options": null},
  //   "awaiting": "user_answer",
  //   "consumed": false,
  //   "budget_window_count": 1          // only meaningful for reason=budget_cap (D55)
  // }
  "context_summary_cache": {
    "scope_hash": "sha256:...",
    "content_hash": "sha256:...",
    "summary_text": "..."
  }
}
```

**Full-result storage note:** Couchbase documents have a practical size ceiling; a 10k-row
`runQuery` result could be large. Phase 0 proposal: store `result_full_ref` pointing at a
**separate Couchbase collection** (`session_results`, same `SESSION_TTL`) keyed by a UUID, keeping
the primary session document small and fast to load every turn (only `tool_trail` entries +
previews are read on the hot path; full results are read only by the Phase-1 learning loop, which
is out of scope here but the storage shape should not need to change when it lands).

**Pause checkpoint / CAS-exactly-once (D45):** resume is a `replace()` write guarded by the
document's Couchbase CAS value:

```python
async def resume_checkpoint(session_id, cas, answer) -> ResumeResult:
    doc, doc_cas = await bucket.get(session_id, with_cas=True)
    if doc["pause_checkpoint"] is None or doc["pause_checkpoint"]["consumed"]:
        raise AlreadyConsumedError(...)
    doc["pause_checkpoint"]["consumed"] = True
    doc["messages"].append({"role": "user", "content": answer, ...})
    await bucket.replace(session_id, doc, cas=doc_cas)   # raises CASMismatchError on race
```

A `CASMismatchError` means a concurrent resume already won; the loser is told "already answered."
This makes the runtime **stateless across pauses** — any process can resume any paused session, and
a crash before the checkpoint write simply loses nothing (the turn had not started), while a crash
during active (non-paused) compute is safe to just re-run because the data path is read-only +
idempotent (D45's stated compensation model — re-run, not undo).

**Retention:** single Couchbase document-level TTL = `SESSION_TTL` (D44), applied to both the
session doc and the `session_results` collection entries it references (same expiry so a dangling
`result_full_ref` never outlives its parent).

---

## 7. Progress streaming (D61) + Phoenix/OTel (D23/D24/D25)

**One instrumentation, two consumers**, as specified: the same stage-boundary functions call both
`tracer.start_span(...)` (OTel → self-hosted Phoenix) and `progress_emitter.emit(...)` (→ UI
stream, e.g. Server-Sent Events on the same HTTP turn request — see §11 OQ-F on transport choice).

### Span map — Phase 0 subset

| Component | Span kind | Key attributes | Redaction |
|---|---|---|---|
| One turn | `AGENT` | `session.id`, `scope_hash` (never raw scope), turn index | — |
| Context assembly | `CHAIN` | trail entries loaded, dropped-by-scope count (D44), compaction hit/miss | no cell values |
| Model call | `LLM` | auto-instrumented via `openinference-instrumentation-openai` (D24) — model name, token counts, cost, latency | SDK auto-instrumentor's hide flags (`HIDE_INPUTS`/`HIDE_OUTPUTS` etc.) |
| Each tool call | `TOOL` | `tool.name`, args (SQL **allowed** for transparency per 08-ui.md, but literal string values inside the SQL are masked by the same regex-based literal-masking convention `clickhouse-api` already applies server-side — see below), status, `error_code` if denied | mask string/numeric literals in SQL text; never log result rows |
| `askUser` pause | event/`TOOL` | trigger reason (`model` \| `budget_cap`) | — |
| Budget-cap check | `GUARDRAIL` | iteration/token/wall-clock counters vs. caps, window count | — |

**Why SQL literal-masking in spans, specifically:** SQL *shape* is intentionally transparent
(08-ui.md's transparency principle, D61 "SQL may still be surfaced"), but a literal like
`WHERE EmployeeName = 'Jane Doe'` is HR PII sitting in plain text inside that SQL. `redaction.py`
applies a lightweight masking pass (mirroring the "string-literal masking" `clickhouse-api` already
does server-side per D75) **before** the SQL is written into any span attribute or progress event —
the *un-redacted* SQL is still what's dispatched to the MCP and shown in the UI result panel (per
08's transparency principle for the user themselves), only the **telemetry copy** is masked.

**Never logged, anywhere in spans/progress text:** the JWT, the raw `column_scope` list (only its
hash), result rows, and bound literal values. Progress events are the exact same redacted
shape/step labels as spans — "running query…", "step complete: 4,213 rows (truncated)" — never
"WHERE dept = 'Sales'"-style content in the *progress* channel (progress is coarser-grained than
spans; spans may carry the masked SQL, progress events do not need to).

---

## 8. Testability seams (Layer-1 no-live-infra requirement)

| Seam | Protocol | Fake (Layer 1) | Real (Layer 2/3) |
|---|---|---|---|
| Session store | `SessionStore` | `InMemorySessionStore` (dict-backed, emulates CAS with an in-memory version counter) | `CouchbaseSessionStore` |
| MCP client | `MCPClient` | `FakeMCPClient` (scripted `{tool_name: [responses...]}`, can simulate `ToolError` codes) | `RealMCPClient` (mcp SDK) |
| Model client | `ModelClient` | `ScriptedModelClient` (cassette-style: a fixed sequence of `ModelTurnResult`s, including forced tool-call loops for budget-cap tests) | `OpenAIModelClient` |
| JWT verification | plain function | test JWKS fixture (self-signed test key pair) | real IdP/`token_service` JWKS |

### Invariant → test mapping

| Invariant | Test target | Layer | No-infra? |
|---|---|---|---|
| D44 scope re-filter | `context/scope_filter.py` pure function; fixed `TrailEntry` fixtures × scope sets, incl. allow-all (`[]`) semantics | 1 | Yes |
| D46/D50 ordering | `context/assembly.py` with `InMemorySessionStore`; assert filter-before-compact, SQL preserved verbatim in summary, preview `truncated` flag correctness | 1 | Yes |
| D5 injection integrity | Scan every message dict passed to `ScriptedModelClient.send_turn` for the JWT/session_id substrings — must never appear; `FakeMCPClient` asserts it *did* receive them at the transport boundary | 1 | Yes |
| Provenance capture (runQuery) | Reuses the existing 39-case `tests/sqlparse/test_column_provenance.py` suite unmodified; new cases for the `sampleRows`/`getTableSchema` declarative-provenance rule | 1 | Yes |
| D45 pause/resume CAS | `InMemorySessionStore`'s CAS emulation (fast, deterministic race simulation) at Layer 1; **real** Couchbase CAS semantics confirmed at Layer 2 (containerized) | 1 + 2 | Fake at L1; real store needed at L2 |
| Budget-cap pause (D47/D55) | `budget_guard.py` pure counters; `agent_loop.py` driven by a `ScriptedModelClient` that always requests another tool call — assert pause fires at the cap and "continue" grants exactly one fresh window (never two) | 1 | Yes |
| PII redaction (D25) | `redaction.py`: given sample tool args/results with literals, assert no raw JWT/scope, masked literals, scope hashed not logged | 1 | Yes |
| Graceful denial mapping | `denial_mapping.py`: table-driven test over all 5 `ToolError` codes → correct retryable/user-facing classification | 1 | Yes |
| MCP transport correctness (real streamable-HTTP, real auth headers reach the MCP) | Component test against the real `clickhouse-api` container | 2 | No — needs the container |
| Couchbase TTL / real CAS races under concurrency | Component test against a containerized Couchbase | 2 | No |
| Pause/resume durability across a runtime restart; budget-cap "continue" full round-trip; observability+PII e2e; parser fail-closed e2e | Full flows | 3 | No — **deferred**, needs the UI (per the brief's instruction; these map 1:1 onto rows already flagged `⛔ not-built` in [TRACEABILITY.md](TRACEABILITY.md) for the runtime side) |

---

## 9. Dependencies to add (`pyproject.toml`)

| Package | Justification |
|---|---|
| `openai` | Agent LLM client — Responses API primary, Chat Completions fallback (D71). |
| `mcp` | Official Python MCP SDK — `streamablehttp_client` + `ClientSession` to call the adopted `clickhouse-api` MCP (D75). |
| `couchbase` | Official Couchbase Python SDK — session store (D22/D44/D45). |
| `pyjwt` (`PyJWT[crypto]`) | Local JWKS-based verification of the inbound JWT to decode `column_scope` for the runtime's own D44 replay-filter — mirrors `clickhouse-api`'s own `auth_jwt.py` approach (same library, same RS256/JWKS pattern) so the two services can never disagree on how a JWT is validated. |
| `opentelemetry-api` / `opentelemetry-sdk` | Core tracing primitives — spans, exporters (D23). |
| `opentelemetry-exporter-otlp` | OTLP export to the self-hosted Phoenix collector (D24). |
| `openinference-instrumentation-openai` | Auto-instruments the OpenAI SDK calls as `LLM` spans (D24) — the only auto-instrumented piece; everything else is manual spans. |
| `fastapi` + `uvicorn[standard]` | The runtime's own inbound HTTP surface (turn requests from the UI/UI-backend, SSE progress streaming, resume endpoint) — matches the stack `clickhouse-api` already uses, minimizing operational surface-area diversity. |
| `pydantic-settings` | `RuntimeSettings` env-var config surface (§11 tunables), consistent with `clickhouse-api`'s `Settings` pattern. |
| `httpx` | Already a transitive dependency of `openai`/`mcp`/`fastapi`'s test client, but declared explicitly since the dev-harness JWT-minting script (§11 OQ-A) calls `token_service`'s `POST /token` directly over HTTP. |

**Unchanged:** `sqlglot`, `pyyaml` — reused as-is from the existing `data_agent.sqlparse`/
`data_agent.catalog` modules; no version bump implied by this design.

**Deliberately NOT added in Phase 0:** any embedding/reranker client (D71 — custom API, Phase 1
retrieval only), `neo4j` driver, `redis` client, `tenacity` (retry logic is simple enough to
hand-roll for the two call sites that need it — OpenAI fallback and MCP transient errors — avoiding
a dependency for ~20 lines of backoff logic; revisit if retry policy grows more elaborate).

---

## 10. Build order (dependency-ordered, each slice independently unit-testable)

1. **Interfaces + fakes first.** `session/store.py` + `memory_store.py`; `mcp/client.py` +
   `fake_client.py`; `model/client.py` + `scripted_client.py`. Nothing here touches real infra —
   this slice defines every DI seam in §8 before any real implementation exists, so every
   subsequent slice can be unit-tested against a fake immediately.
2. **Provenance/dispatch bridge.** `provenance/catalog_handle.py` (wraps the existing
   `build_sqlglot_schema()`), `provenance/capture.py` (wraps the existing
   `extract_column_provenance` + the new declarative `sampleRows`/`getTableSchema` rule),
   `dispatch/denial_mapping.py`. Fully testable against `FakeMCPClient` — no real MCP needed yet.
3. **`dispatch/tool_dispatcher.py`** wired to the fakes; then `mcp/real_client.py` +
   `mcp/tool_schema.py` against the real `clickhouse-api` (Layer 2 — first point this design touches
   live infra).
4. **`context/scope_filter.py`, `context/budget.py`, `context/assembly.py`** against
   `InMemorySessionStore` — the D44/D46/D50 ordering is provable entirely with fakes.
5. **`session/couchbase_store.py`** (real) — Layer 2, containerized Couchbase; validates the
   document shape (§6) and CAS resume semantics for real.
6. **`model/openai_client.py`** — Responses/Chat-fallback wiring; unit-testable in isolation
   against a recorded/mocked `openai` client, separate from the loop.
7. **`loop/budget_guard.py`, `loop/agent_loop.py`** — the full turn state machine, exercised end-to-
   end against `ScriptedModelClient` + `FakeMCPClient` + `InMemorySessionStore` (all fakes) before
   ever touching real infra. This is where D47/D55/askUser-pause invariants get their thorough
   Layer-1 coverage.
8. **`observability/*`** — tracing/redaction/progress wired in last (it's cross-cutting and every
   earlier slice already has a "span boundary" seam by construction — the stage functions were
   written to be wrap-able); Phoenix container stood up for Layer-2 confirmation that spans arrive
   PII-clean.
9. **`auth/*` + `app.py`** — the composition root, real JWT verification, and the actual HTTP
   entrypoint wiring everything above into one running service, last — by this point every
   component underneath it already has its own test suite, so `app.py` mostly needs
   integration/smoke coverage rather than exhaustive unit tests.

This order lets Layer-1 tests accumulate from slice 1 onward with zero infrastructure, matches the
brief's "interfaces+fakes → MCP client → session store → context assembly → loop →
streaming/Phoenix" shape, and defers every real-infra dependency (MCP container, Couchbase
container, live OpenAI calls, Phoenix container) to the latest point that still lets each slice be
proven correct against a fake first.

---

## 11. New sub-decisions & open questions (for human sign-off — not unilaterally locked here)

### Sub-decisions this design introduces

**A. No hosted remote-MCP tool type; the runtime owns the full tool-dispatch loop.** Forced by
D5+D44+D61 (see §4's "critical architecture call"). Trade-off: gives up OpenAI's server-side MCP
tool-call infrastructure entirely; every tool call round-trips through our own process. Needs
explicit sign-off because it's a real capability the OpenAI Responses API offers that this design
deliberately does not use.

**B. Runtime independently verifies the inbound JWT (via the same PyJWT/JWKS pattern as
`clickhouse-api`) rather than treating it as an opaque pass-through string.** Needed so the
runtime's own D44 replay-filter scope is trustworthy; the MCP remains the sole *enforcement*
boundary for live queries (D57) — this local verification only shapes what conversation history is
re-shown to the same authenticated user, so a forged/stale JWT here is low-severity (self-serves
the same session, not cross-tenant), but it's still a new piece of duplicated security-relevant
logic across two repos worth flagging.

**C. Tool schemas for the 6 MCP tools are fetched live from `list_tools()` and translated, not
hand-authored.** Avoids drift between the adopted (externally-changeable) MCP and our tool
declarations; introduces a startup dependency on the MCP being reachable (need a documented
fallback/cache-on-disk behavior for local dev when the MCP is down — open question D below).

**D. An outer hard ceiling on budget-window grants** (proposed default: 3 windows per turn) on top
of D55's "continue grants a fresh window" — prevents an unbounded chain of "continue" answers from
one confused/adversarial session. Not specified by D47/D55; needs a concrete number.

### Open questions (need values or a decision, not just architecture)

- **OQ-A (dev JWT acquisition).** Local/dev testing of the runtime standalone (no UI yet) needs a
  harness script that mints a token directly via `token_service`'s guarded `POST /token`
  (`TOKEN_ISSUER_API_KEY`) and passes it as if it were the UI-forwarded JWT. Confirm this is
  acceptable as a dev-only fixture, separate from the production path (D82: UI backend mints,
  forwards per-turn) which the runtime code itself is agnostic to (it just reads an inbound
  `Authorization` header either way).
- **OQ-B (parser version skew between repos).** D79a copies the extractor into `clickhouse-api`;
  our runtime uses the same module from *this* repo. If the two copies drift (a mirror is missed),
  the runtime's own provenance capture for the trail could disagree with what the MCP actually
  enforced on the live call. Propose a CI check (file-diff, per D79a) that also covers this
  repo-internal reuse, not just the `clickhouse-api` mirror.
- **OQ-C (Responses→Chat fallback trigger taxonomy).** D71 mandates the two-endpoint design but not
  the exact error classes that trigger a mid-turn fallback. Needs a concrete list (e.g. HTTP 5xx,
  specific OpenAI SDK exception types, timeout thresholds) before implementation.
- **OQ-D (outer budget-window ceiling value)** — see sub-decision D above; proposed default 3,
  needs sign-off or a different number.
- **OQ-E (context-summary cache persistence).** In-process LRU (always-correct-on-miss) vs. also
  persisting `context_summary_cache` on the session doc so multiple runtime replicas share a warm
  cache. Proposed default: in-process only for Phase 0 (simpler, no correctness risk); revisit if
  multi-replica cache-miss cost proves material.
- **OQ-F (progress-streaming transport).** SSE (one-directional, closes cleanly at a pause,
  matches the checkpoint-based resume-as-a-new-request model) vs. WebSocket (bidirectional, more
  moving parts). Proposed default: **SSE**, given D45's resume is already a fresh request/response,
  not a held-open connection.
- **OQ-G (`N` preview-row count and history token budget, D46).** Already flagged as open in
  03-context-and-retrieval.md; propose `N=20` preview rows and a history budget as a fraction of
  the model's context window (e.g. 20%) as starting defaults, tunable via `RuntimeSettings`.
- **OQ-H (D47 budget-cap values).** Max tool-call iterations / tokens / wall-clock per window — no
  values exist yet anywhere in the spec. Propose starting defaults (e.g. 15 iterations / a token
  budget tied to the model's context window / 60s wall-clock per window) as `RuntimeSettings`
  fields, explicitly labeled provisional pending real Phase-0 traffic.
- **OQ-I (`SESSION_TTL` value).** Already an open parameter per D44/06; this design's Couchbase doc
  shape (§6) is agnostic to the value but needs one to configure `RuntimeSettings.session_ttl_seconds`.
- **OQ-J (MCP connection pooling).** §3.1 opens a `ClientSession` per dispatched call for
  simplicity; whether to pool/reuse sessions per turn or per runtime process is a pure performance
  question deferred until load-testing shows it matters.

---

## Cross-references

Honors, per major section: D5/D82 (§2), D75/D57/D63/D64/D69/D70 (§3), D5/D44/D61/D71 (§4, incl. the
new hosted-MCP exclusion), D44/D46/D50 (§5), D22/D44/D45 (§6), D23/D24/D25/D61 (§7), D76 (§8), D71
(§9), D68 first-brick sequencing philosophy (§10).
