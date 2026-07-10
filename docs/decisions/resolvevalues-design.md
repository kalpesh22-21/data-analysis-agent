# `resolveValues` — Runtime Composite Tool Design (D77)

**Status:** **BUILT (Session 9, 2026-07-01) — Layer-1 green, adversarially QA'd, reviewed
(REQUEST CHANGES → fixed → APPROVE).** Companion to [DECISIONS.md](DECISIONS.md) (D66/D77/D85)
and [phase0-runtime-design.md](phase0-runtime-design.md); this is the **first Phase-1 brick** per
[D68](DECISIONS.md#phasing). Scoped strictly to the model-facing `resolveValues` composite tool and
its backing runtime machinery (the D71 embedding client, ranking, SQL construction over `runQuery`).

> **As-built (Session 9).** Implemented per this design with the deviations noted inline as
> **[As-built]** callouts below. Summary of what changed from the proposed design:
> - **`app.py` degrade default (§3.2 / OQ-1):** an *unconfigured* embedding API wires **no client** →
>   the composite runs freq-only-degraded, rather than substituting `FakeEmbeddingClient`. The fake is
>   kept **test-only**. (Deliberate — production must never silently rank via a stub.)
> - **Result wrapper (H3 review fix):** `result_full` is a wrapper
>   `{degraded, ranking: "semantic+freq"|"freq_only", top_margin, values}` — not a bare list — so the
>   `degraded`/`freq_only` state and `top_margin` reach the model (a freq-only top score of `1.0` was
>   masquerading as a perfect semantic match).
> - **Scope-aware description-column discovery (M1 review fix):** description-column candidates are
>   filtered through `scope_filter.is_provenance_in_scope`, with value-only fallback when the sibling
>   desc column is out of scope — otherwise a permanently-denied inner query.
> - **B4-parity guard (H1 review fix):** `_safe_run_inner` broad-except + `_extract_rows` guards degrade
>   malformed inner-result shapes to a clean `RESOLVE_VALUES_INTERNAL_ERROR` instead of crashing the turn.
> - **Denial-table entries (L5 review fix):** `RESOLVE_VALUES_*` codes were added to `_DENIAL_TABLE` so
>   replayed trail entries render specific messages.
> - **Module placement:** `ResolvedValue`/`Period`/`ResolveOutcome` live in `composite/resolve_values.py`
>   (`ResolveOutcome` carries extra fields — `degraded`, `ranking`, `top_margin` — feeding the wrapper).
> - **Schema `period` typing (L4):** JSON-Schema `"type": ["object", "null"]` (not `nullable`); §10 was
>   already patched to match during implementation.

**Fixed contract (not relitigated here — see D66/D77):** model interface
`resolveValues(table, column, concept, period?) -> [{value, description, score, freq}]`. Under the
hood the runtime issues **one ordinary `runQuery`**
(`SELECT <column>, <descriptionCol>, count() AS freq FROM <table> [WHERE <period>] GROUP BY … ORDER BY freq DESC LIMIT N`)
then ranks the returned rows by **semantic similarity of `concept`** (runtime embedding client, D71)
combined with `freq`. `concept` **NEVER** touches SQL (D10). `ClientCode`/scope is **injected** by the
backing `runQuery`'s D5 tenant RLS + D57 column-scope — never a model parameter and never re-implemented
here. Enforcement is *free* because the inner `runQuery` passes through the exact same D57 column-scope +
D5 tenant-RLS gate as any other query.

**Out of scope (explicitly):**
- **D67 rule-expansion wiring** (`resolve_via` rules → concrete param sets). This brick makes the
  composite *programmatically callable* (§1.4) so D67 can consume it later, but the D67 catalog-rule
  binding itself is a separate brick.
- **Per-`(client, column)` caching / vector index** — D66 says "no tenant-knowledge cache for now;
  index later". Each call is a live `DISTINCT` + live embed.
- **Tenant-knowledge scope** (global / tenant / user) — D66 explicitly defers it.
- ~~**Real custom-embedding-API integration test** — the endpoint + model id are TBD (D71).~~
  **RESOLVED (OQ-1, Session 9+):** the contract is now known from the user-provided mocks
  (`~/Development/SQL/mocks`) and `HttpEmbeddingClient` is aligned to it; a live Layer-2 test runs
  against the dockerized mock. See the OQ-1 as-built note in §3.1. (Production endpoint host/auth is
  still TBD, but no longer blocks the seam.)
- **Full D41/D65 period-domain resolution** (deictic / relative / "latest settled period" fill). This
  brick accepts only an already-concrete *structured* `period` (§2.4) and defers period-domain
  resolution to the D67 era.

---

## 0. Ground truth this design relies on (verified by reading code, not assumed)

- **`resolveValues` is not an MCP tool** (D77 reduces the MCP data plane to exactly 6 read tools). It is
  a runtime composite. Therefore it must be *authored locally* like `askUser`
  (`runtime/mcp/tool_schema.py::ASK_USER_TOOL_SCHEMA`) and *intercepted in the loop* like `askUser`
  (`runtime/loop/agent_loop.py`), never dispatched to the MCP.
- **The only MCP choke point is `ToolDispatcher.dispatch(tool_name, model_args, credentials)`**
  (`runtime/dispatch/tool_dispatcher.py`). It injects credentials (D5), classifies denials
  (`denial_mapping.classify_denial`), captures provenance (`provenance/capture.py::capture_provenance`),
  builds the preview (`_build_preview`), emits a `TOOL` span (`redact_tool_args`), and returns a
  `ToolResult{status, tool_name, error_code, retryable, user_message, provenance, result_preview,
  result_full}`. It does **no session I/O** — the loop persists the `TrailEntry`.
- **The loop already special-cases one non-dispatched tool (`askUser`)** in the per-tool-call section
  of `_run_loop` (`agent_loop.py`), producing a terminal outcome instead of a `ToolResult`.
  `resolveValues` is the symmetric case: intercepted, but producing an *inline* `ToolResult` that flows
  through the loop's existing `TrailEntry` + budget path unchanged.
- **`runQuery` has no bound-parameter surface.** `MCPClient.call_tool(tool_name, args, *, jwt,
  session_id)` (`runtime/mcp/client.py`) and the MCP's `runQuery(sql, limit)` schema take a raw SQL
  string + optional `limit` — there is no `params` dict to bind ClickHouse server-side params through
  (the D41/D66 "server-side params" mechanism is a separate future surface). So the composite must
  construct final SQL text itself, safely (§2).
- **The runtime catalog is `{database.table: {column: type}}` only** (`provenance/catalog_handle.py`
  wrapping `data_agent.catalog.build_sqlglot_schema()`). The *rich* semantic catalog (per-column
  `description`, `client_defined`, and the description-column linkage) lives MCP-side per D83
  ([mcp-overlay-design.md](mcp-overlay-design.md)) and is **not** available to the runtime. This
  constrains description-column discovery to a convention + catalog-validation approach (§2.3 / OQ-2).
- **`capture_provenance` returns `None` (fail-closed) for unknown tool names.** `resolveValues` never
  reaches `capture_provenance` (it is never dispatched as an MCP tool); its provenance comes from the
  *inner* `runQuery`'s already-computed provenance (§8). No `capture.py` change is needed.
- **No embedding client exists.** Phase-0 design §9 explicitly excluded it ("custom API, Phase 1
  retrieval only", D71). This brick introduces it as a new seam (§3).
- **`redact_tool_args`** (`observability/redaction.py`) masks only the `sql` key today; `concept` (free
  user text) would pass through unmasked (§6). Precedent: the Session-6 review kept `askUser`'s
  `question` out of spans (`tracing.guardrail_observer` strict attribute allowlist; `app.py` line ~204
  comment). `concept` gets the same treatment.

---

## 1. Q1 — Seam: dedicated `composite/` module, intercepted in the loop

**Decision.** A new package `src/data_agent/runtime/composite/` with a `ResolveValuesComposite` class,
**invoked from `AgentLoop._run_loop`** by a branch symmetric to the existing `askUser` interception —
*not* a branch inside `ToolDispatcher`, and *not* a pause.

### 1.1 Why not a dispatcher branch

Putting `if tool_name == "resolveValues"` inside `ToolDispatcher.dispatch` would make the dispatcher
call *itself* re-entrantly (the composite must issue an inner `runQuery` *through* the dispatcher to get
provenance/preview/denial free). That creates a dispatcher↔composite circular dependency and muddies the
dispatcher's single, auditable responsibility ("execute exactly one MCP tool call, inject credentials,
tag it"). The dispatcher stays MCP-only.

### 1.2 Why not an `askUser`-style terminal interception

`askUser` *pauses* (writes a checkpoint, returns control). `resolveValues` must return a **tool result
inline** so the model can keep going in the same iteration. So it is intercepted like `askUser` (routed
away from `dispatch`) but behaves like a normal dispatched tool afterward (produces a `ToolResult`, gets
a `TrailEntry`, counts one tool call).

### 1.3 Chosen dependency graph (one direction, no cycle)

```
AgentLoop ──(normal MCP tools)──▶ ToolDispatcher ──▶ MCPClient
AgentLoop ──(resolveValues)─────▶ ResolveValuesComposite
                                        │
                                        ├─(inner runQuery)──▶ ToolDispatcher ──▶ MCPClient
                                        └─(ranking)─────────▶ EmbeddingClient
```

The composite depends on `ToolDispatcher` (for the inner `runQuery`) and `EmbeddingClient` (for ranking)
— both one-directional. The loop depends on both `ToolDispatcher` and `ResolveValuesComposite`. Because
the inner `runQuery` still flows through `ToolDispatcher.dispatch`, **every MCP call — including the one
resolveValues makes — still funnels through the single choke point**: D5 credential injection, D57/D5
enforcement, provenance capture, denial mapping, and the inner `TOOL` span all apply unchanged.

Loop change (the *only* edit to `agent_loop.py`'s hot path), inside the `for tool_call in
capped_tool_calls` block:

```python
if tool_call.name == "resolveValues":
    tool_result = await self._resolve_values.run(tool_call.arguments, credentials)
else:
    tool_result = await self._tool_dispatcher.dispatch(
        tool_call.name, tool_call.arguments, credentials
    )
tool_calls_made += 1
# ... existing write_full_result + TrailEntry + budget code is UNCHANGED ...
```

`ResolveValuesComposite.run(...) -> ToolResult` returns the *same* `ToolResult` dataclass the dispatcher
returns, so the loop's trail-entry construction, `write_full_result`, and budget accounting need **zero**
further change.

### 1.4 Programmatic callability (D67 hook, designed-for, not wired)

`ResolveValuesComposite` exposes two methods:

```python
class ResolveValuesComposite:
    async def run(self, model_args: dict, credentials: RuntimeCredentials) -> ToolResult:
        """Model tool-call path: validate args, resolve, wrap as a ToolResult."""

    async def resolve(
        self, *, table: str, column: str, concept: str,
        period: Period | None, credentials: RuntimeCredentials,
    ) -> ResolveOutcome:
        """Programmatic path (D67): typed in, typed out — no ToolResult wrapping.
        Returns ResolveOutcome{status, values: list[ResolvedValue], provenance,
        denial: DenialInfo | None, degraded: bool}."""
```

`run()` is a thin adapter over `resolve()` (parse/validate model args → call `resolve()` → wrap the
`ResolveOutcome` into a `ToolResult`). D67's rule expander later calls `resolve()` directly to turn a
`resolve_via` concept into a concrete value set for param binding — **no model round-trip, no
`ToolResult`**. Wiring that is out of scope; exposing `resolve()` now costs nothing and avoids a later
refactor.

### 1.5 Module layout

```
src/data_agent/runtime/composite/
  __init__.py
  resolve_values.py     # ResolveValuesComposite, ResolveOutcome, ResolvedValue, Period
  sql_builder.py        # pure: validated (table, column, descCol, period) -> runQuery SQL string
  ranking.py            # pure: (concept_vec, row_vecs, freqs) -> ranked [ResolvedValue]

src/data_agent/runtime/model/
  embedding_client.py   # EmbeddingClient Protocol; FakeEmbeddingClient; HttpEmbeddingClient
```

`sql_builder.py` and `ranking.py` are **pure/sync** (no I/O) — the primary Layer-1 targets (§8).

---

## 2. Q2 — SQL construction + injection safety

### 2.1 Target validation (fail-closed against the catalog allowlist)

`table` and `column` are model-supplied. Before any SQL is built:

1. **Resolve `table` to a catalogued `database.table`.** Accept either a fully-qualified
   `"database.table"` or a bare `"table"`. Match against `CatalogHandle.schema` keys. A bare name that
   resolves to exactly one `database.table` is accepted; **zero matches → fail-closed error**
   (`RESOLVE_VALUES_UNKNOWN_TARGET`); **≥2 matches (ambiguous bare name) → fail-closed error** asking the
   model to qualify it. This makes `table` an **allowlisted identifier**, not free text.
2. **Validate `column ∈ catalog[database.table]`.** Unknown column → `RESOLVE_VALUES_UNKNOWN_TARGET`.

Fail-closed errors are `ToolResult(status="error", error_code="RESOLVE_VALUES_UNKNOWN_TARGET",
retryable=True, user_message="No column '<c>' on table '<t>' is available.")` — `retryable=True` because
the model may have mistyped and can self-correct; the message names only the specific target the model
already supplied (no catalog enumeration → no scope leak).

### 2.2 Identifier quoting (defense in depth)

Even though `table`/`column`/`descCol` are now allowlisted catalog identifiers (so they *cannot* carry
injection), the SQL is assembled with **sqlglot** identifier nodes (`exp.column(...)`,
`exp.table_(...)`), never Python string concatenation of the raw names. Two independent guarantees stack:
(a) the identifiers are catalog-allowlisted before use, and (b) the **inner `runQuery` is re-parsed and
column-scope-enforced by the MCP itself** (D57) exactly like any model-authored query — so a
hypothetical validation gap still cannot exfiltrate an out-of-scope column. `concept` is **never** a SQL
input at all (D10) — it is only ever embedded and compared in-runtime after the query returns.

### 2.3 Description-column discovery (convention + catalog validation; value-only fallback)

The fixed contract has **no `descriptionCol` parameter** (correctly — the model asks about a code column;
it should not have to know the sibling description column's name). The rich semantic catalog that *links*
`EarnCode → EarnDescription` lives MCP-side (D83) and is not runtime-visible. So the runtime discovers the
description column by **convention, validated against the `{col: type}` catalog**:

- Generate candidate names from `column` via an ordered, configurable transform list (default):
  `"<Base>Description"`, `"<Base>Name"`, `"<column>Description"` where `<Base>` is `column` with a
  trailing `"Code"` stripped (e.g. `EarnCode → EarnDescription`, `TypeCode → TypeDescription`,
  `DepartmentCode → DepartmentName`).
- Pick the **first candidate that exists** in `catalog[database.table]`.
- **None found → value-only query** (`SELECT <column>, count() AS freq …`); rows return with
  `description=None`. Ranking then embeds `value` alone (§4).

This is deliberately reversible and low-ceremony.

> **[As-built] OQ-2 RESOLVED — structured `description_col` field added.** The "right" long-term answer
> is now built: a per-column `description_col:` catalog field (authored in `databaseSchemaDocs/*.yaml`)
> declares a code column's sibling label column explicitly. Discovery order in `sql_builder.resolve_target`
> is now **declared → convention → value-only**: the authored `description_col` is tried first, then the
> naming-convention candidates, then value-only — each still gated by the same `candidate in columns` +
> `is_provenance_in_scope` (M1) check, so a mis-authored/out-of-scope/self-referential declaration
> degrades gracefully. Surfaced to the runtime via `CatalogHandle.description_col_for()` (a new
> `{db_table: {code_col: desc_col}}` projection from `loader.load_description_cols()`), so `resolveValues`
> needed no new wiring. This unblocked non-conventional pairs the convention silently missed —
> `FieldId → FieldLabel`, and `DistributedDepartmentCode → distributedDepartmentDescription` (a D70
> case-mismatch the strip-Code/append-Description convention could never reach).

### 2.4 `period?` — validated structured form, sqlglot-literal bound (no interpolation)

`runQuery` has no bound-param surface (§0), so a period predicate must become SQL *text*. To keep it
injection-safe, `period` is **not free text** — it is an optional **structured object** the model fills:

```jsonc
"period": {
  "column": "PayPeriodEndDate",     // must validate against catalog[table] (a real column)
  "start":  "2026-01-01",           // optional; rendered as a sqlglot literal
  "end":    "2026-03-31"            // optional; rendered as a sqlglot literal
}
```

- `period.column` is validated against `catalog[database.table]` exactly like `column` (§2.1) — an
  unknown period column fails closed, it is never trusted as text.
- `start`/`end` become **sqlglot literal nodes** (`exp.Literal.string(...)`), assembled into
  `WHERE <col> >= <start> AND <col> <= <end>` via the sqlglot AST — the values are properly-escaped SQL
  literals, never concatenated. This is the same injection posture as a bound param for a read-only,
  RLS-isolated query, and the MCP re-parses + scope-checks the result regardless.
- **`period.column` out of scope** is *not* a runtime concern — the inner `runQuery` will simply be
  denied `COLUMN_SCOPE_VIOLATION` by the MCP and that denial passes through (§7).
- **Deferred (OQ-3):** resolving *deictic/relative* periods ("latest", "Q2", "last quarter") to concrete
  bounds — that is D41/D65 period-domain resolution and belongs to the D67 era. This brick honors only an
  already-concrete structured `period`; absent `period` → no `WHERE` clause.

### 2.5 Final SQL shape

```sql
SELECT <column> [, <descriptionCol>], count() AS freq
FROM <database>.<table>
[WHERE <period.column> >= <start> AND <period.column> <= <end>]
GROUP BY <column> [, <descriptionCol>]
ORDER BY freq DESC
LIMIT <resolve_values_query_limit>
```

Issued via `ToolDispatcher.dispatch("runQuery", {"sql": <built_sql>, "limit": None}, credentials)`.

---

## 3. Q3 — Embedding client (D71 seam) + graceful degradation

### 3.1 Protocol + implementations (`model/embedding_client.py`)

```python
class EmbeddingClient(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one dense vector per input text, order-preserving.
        Raises EmbeddingError on any transport/serialization failure."""
```

- **`FakeEmbeddingClient` (Layer 1):** deterministic vectors from a seeded hash of each text (so cosine
  ordering is reproducible in tests), or an explicit scripted `{text: vector}` map for hand-crafted
  ranking assertions. No network.
- **`HttpEmbeddingClient` (Layer 2/3, real):** a plain `httpx.AsyncClient` `POST` to the custom
  embedding API (D71 — *not* the OpenAI SDK), reading `embedding_api_url` / `embedding_api_key` /
  `embedding_model` from `RuntimeSettings` (§ "New RuntimeSettings"; secrets gitignored via `.env`). Wraps
  the call in a **manual `EMBEDDING` span** (D24 — the OpenInference auto-instrumentor covers only the
  agent LLM, not this custom endpoint). Raises `EmbeddingError` on non-2xx / timeout / malformed body.

> **OQ-1 as-built (contract RESOLVED, Session 9+).** The D71 embedding-API contract is now known from
> the user-provided authoritative mocks (`~/Development/SQL/mocks/embedding_api`):
> - **Request:** `POST <embedding_api_url>` with body `{"input_text": [<text>, ...]}` — `embedding_api_url`
>   is now the **full endpoint URL** (e.g. `http://localhost:8003/embed`), not a base.
> - **Response:** a **bare JSON array** `[[float, ...], ...]` (768-dim `all-mpnet-base-v2`), one vector
>   per input, order-preserving — *not* an OpenAI-shaped `{"embeddings"|"data": ...}` envelope. The
>   old dual-shape parser was dropped; a non-list body / scalar list / count-mismatch / non-finite
>   element all raise `EmbeddingError`.
> - **Auth:** the mock needs **none**; `embedding_api_key` is retained as an optional bearer header for
>   the eventual production endpoint. `embedding_model` is no longer a request param — it survives only
>   as the `EMBEDDING` span's `embedding.model` attribute.
> - **Validated live:** `tests/integration/test_embedding_api.py` + `test_resolve_values_live_embedding.py`
>   (skip-guarded on `EMBEDDING_TEST_URL`) run against the dockerized mock and confirm the semantic win
>   (cosine("paid time off", "PTO - Paid Time Off")≈0.72 > cosine(…, "OT - Overtime")≈0.24; the composite
>   ranks PTO above a 20×-more-frequent OT). Production endpoint host/auth remains TBD.
>
> A sibling **`HttpRerankerClient`** (`model/reranker_client.py`, same discipline, `RERANKER` span) was
> built alongside for the upcoming retrieval brick — `POST {"query","documents"}` → `{"scores": [...]}`.
> It is NOT wired into the agent loop yet.

### 3.2 Degradation policy: fall back to freq-only ranking, flagged (do NOT fail the call)

If `embed(...)` raises, the composite **does not fail the tool call**. It falls back to **frequency-only
ranking** (`score` derived from normalized log-freq alone, §4) and sets `degraded=True` /
`ranking="freq_only"` on the outcome.

**Rationale.** D63 fail-closed governs *enforcement* (scope), not *ranking quality*. Enforcement already
happened: the inner `runQuery` is scope-checked, so **every returned value is provably in-scope**
regardless of how it is ranked. Failing the whole call on an embedding outage would convert a *quality*
degradation (worse ordering of already-safe values) into an *availability* outage of a core Phase-1 tool
— strictly worse for the user, and safe to avoid. The `degraded` flag is surfaced in the tool result so
the model knows semantic ranking was not applied (and should lean toward `askUser` per D66(c)).

This degrade-vs-fail choice is a genuine *policy* decision (not derivable from an existing D-number) — it
is flagged in **OQ-4** for whether it should be minted as its own decision or annotated under D77.

---

## 4. Q4 — Ranking formula + accept-vs-clarify signal

### 4.1 Score (pure, in `ranking.py`)

For each returned row `i` with distinct `value`, optional `description`, and `freq`:

- **Embedding text:** `f"{value}: {description}"` when a description exists, else `value` alone.
- **Concept vector** `c = embed([concept])[0]`; **row vectors** `r_i = embed([row_text_i])` (batched in
  one `embed` call with `concept` prepended, so it is a single round-trip).
- **Similarity:** `sim_i = cosine(c, r_i)`, clamped/normalized to `[0,1]` via `sim_norm_i = max(0.0,
  sim_i)` (cosine on typical embeddings is ~[0,1]; the clamp guards negatives).
- **Normalized log-frequency:** `lf_i = log1p(freq_i)`, then `lf_norm_i = lf_i / max_j(lf_j)` (→ `[0,1]`;
  if all freqs equal, this is a constant and similarity fully decides).
- **Combined score:**
  `score_i = w · sim_norm_i + (1 − w) · lf_norm_i`, with `w = resolve_values_similarity_weight`
  (**default 0.7** — semantic-dominant, since the model asked for a *concept*; freq is a prior/tiebreaker).
- **Degraded (embedding failed):** `score_i = lf_norm_i` (i.e. `w = 0` effectively), `degraded=True`.

Rows are sorted by `score` desc and truncated to `resolve_values_top_k` (**default 10**). Each returned
item is exactly the contract shape `{value, description, score, freq}` (`score` rounded for readability).

### 4.2 Accept-vs-clarify: return scores, let the model decide (no hard runtime threshold)

**Decision.** The runtime does **not** apply a hard confidence threshold that auto-routes to `askUser`.
It returns the ranked list with numeric `score`s; the **tool description** (§10) instructs the model to
call `askUser` when the top scores are low or clustered (D66(c)). Optionally the outcome carries a cheap
computed hint — `top_margin = score[0] − score[1]` — to make that judgment easier, but the *decision* to
clarify stays with the model.

**Rationale.** A hard runtime threshold is a policy number that is hard to calibrate without traffic,
duplicates the model's own judgment, and couples the runtime to a clarify UX that D66(c) already assigns
to the model (`resolveValues` is the *resolver*; `askUser` is the *clarifier* — separate tools). Keeping
the runtime minimal (rank + return) and letting the model own the clarify branch is the reversible choice:
a soft runtime hint or hard threshold can be added later if traffic shows the model over-accepts.

---

## 5. Q5 — Budget metering

**Decision.** `resolveValues` counts as **exactly one** tool call toward `tool_calls_made` /
`max_tool_calls_per_iteration` — the outer composite call. The **inner `runQuery` does NOT separately
increment** the loop's counters.

**Why it falls out naturally.** The loop increments `tool_calls_made += 1` once per `capped_tool_calls`
element (§1.3). The inner `runQuery` is issued by the composite via `dispatcher.dispatch(...)` directly —
it never passes through the loop's counting path, so it is structurally uncounted. The composite fires
**exactly one** inner query per call by design (§2.5), so "one model-visible call = one MCP query" holds;
there is no fan-out to meter. Wall-clock and token budgets are checked by the loop's existing
`BudgetGuard` after the composite returns, identically to any dispatched tool — a slow embedding call or
slow inner query is naturally caught by the per-tool-call wall-clock check the loop already runs.

---

## 6. Q6 — Observability

### 6.1 `TOOL` span for `resolveValues`, with `concept` fully redacted

The composite emits **one `TOOL` span** for the `resolveValues` call (reusing
`observability/tracing.tool_span`), inside which the inner `runQuery`'s own `TOOL` span (emitted by
`ToolDispatcher._emit_tool_span`) **nests naturally** via the ambient OTel context — giving a clean
`resolveValues → runQuery` parent/child trace.

**`concept` redaction (decision):** `concept` is free user text (the highest-PII-risk arg on this tool —
a user's phrasing of intent). It is **fully redacted** in span attributes, following the Session-6
precedent that kept `askUser`'s `question` out of spans (`tracing.guardrail_observer`). Concretely,
extend `redact_tool_args` with a `_FULLY_REDACTED_ARG_KEYS = {"concept"}` set whose values are replaced
with `"<redacted>"` (distinct from the SQL-literal masking applied to `sql`). `table` and `column` are
structural catalog identifiers (not PII) and are kept for debuggability, exactly like `database`/`table`
on other tools. `period` values (dates) are masked like SQL literals (keep `period.column`, mask
`start`/`end`).

### 6.2 Progress events + `_SHAPE_ALLOWLIST`

The composite emits the **existing** observer events with `tool_name="resolveValues"`:
`tool_dispatch_start` → `"running resolveValues…"`, `tool_dispatch_ok` → `"step complete:
resolveValues"`, `tool_dispatch_denied` → `"step denied: resolveValues"` (from `progress._STEP_LABELS`,
which interpolate the already-allowlisted `tool_name` key). **No `_SHAPE_ALLOWLIST` addition is
required** — `tool_name` and `error_code` are already allowlisted, and no new sensitive payload key is
introduced (`degraded`, `score`, `concept`, row values are **not** put into progress payloads).
Optionally add a dedicated label `"resolving values…"` to `_STEP_LABELS` (a label map entry, *not* an
allowlist entry) for a nicer UX; strictly optional.

### 6.3 Inner-query nesting summary

| Span | Emitter | Redaction |
|---|---|---|
| `resolveValues` (`TOOL`) | `ResolveValuesComposite` | `concept` → `<redacted>`; `period` literals masked; `table`/`column` kept |
| ↳ `runQuery` (`TOOL`, child) | `ToolDispatcher._emit_tool_span` (unchanged) | `sql` literals masked (existing) |
| ↳ `EMBEDDING` | `HttpEmbeddingClient` | no text logged — vector counts/latency only |

---

## 7. Q7 — Error mapping

| Condition | Where detected | `ToolResult` surfaced to the model |
|---|---|---|
| Inner `runQuery` **denied** (e.g. `COLUMN_SCOPE_VIOLATION`, `TABLE_NOT_FOUND`, `CLICKHOUSE_UNAVAILABLE`) | inner `dispatch` returns `status="denied"` | **Pass through** `classify_denial`'s `error_code`/`retryable`/`user_message` verbatim (already model/user-facing + PII-safe), with `tool_name="resolveValues"`. The model sees the same denial semantics it would for a direct query — no re-wrapping that could hide the retryable hint. |
| Inner `runQuery` **transport error** | inner `dispatch` returns `status="error"` (`INTERNAL_TRANSPORT_ERROR`) | Pass through as `status="error"`, generic canned `user_message` (never raw text — the dispatcher already guarantees this). |
| **Unknown / ambiguous table or column** (§2.1) | composite validation, *before* any query | `status="error"`, `error_code="RESOLVE_VALUES_UNKNOWN_TARGET"`, `retryable=True`, message names only the supplied target (no catalog enumeration). |
| **Empty result set** (0 distinct values in scope) | after inner `runQuery` returns 0 rows | `status="ok"`, `result_full=[]`, empty preview. An empty list is a *valid* answer ("no matching values for this tenant/scope"); the model can broaden or `askUser`. **Not** an error. |
| **Embedding failure** | `embed()` raises | `status="ok"`, `degraded=True`, freq-only ranking (§3.2). **Not** an error. |

Because denials/errors from the inner query are surfaced with `provenance=None` (the inner `ToolResult`
already carries `None` provenance on non-`ok` status), the resolveValues `TrailEntry` for those cases is
`None`-provenance too — correctly fail-closed for D44 replay, and (via the turn-scoped continuity rule,
phase0-runtime-design §5.2) still visible to the model *this* turn for self-correction on retryable codes.

---

## 8. Q8 — Provenance / D44

**Decision.** The `resolveValues` `TrailEntry` carries the **inner `runQuery`'s extracted provenance**,
unchanged — `ResolveValuesComposite.run` copies `inner_result.provenance` onto the `ToolResult` it
returns. That provenance is exactly `{(db.table, column), (db.table, descCol), (db.table, periodCol)}` —
the columns the inner SQL references — because `capture_provenance` runs `extract_column_provenance` over
the *built* SQL on the inner dispatch (§0). This is precisely the USES-set D44 needs; no special-casing.

**No `capture.py` change.** `resolveValues` never reaches `capture_provenance` (it is never dispatched as
an MCP tool name), so the "unknown tool → `None`" branch is never exercised for it. The provenance is
sourced entirely from the inner `runQuery`, whose capture path is already correct and tested.

**Couchbase / session impact: none.** The `TrailEntry`/`TurnMessage`/`SessionDoc` shapes are unchanged —
a resolveValues entry is an ordinary trail entry (`tool_name="resolveValues"`, args `{table, column,
concept, period?}`, a provenance set, a preview, a `result_full_ref`). `result_full` is the ranked
`[{value, description, score, freq}]` list; `result_preview` is its first `preview_row_count` items via
the existing `_build_preview` list-handling branch. No new collection, no schema migration.

---

## 9. Q9 — Limits

| Setting | Default | Meaning |
|---|---|---|
| `resolve_values_query_limit` | **200** | `LIMIT N` on the backing `runQuery` — the distinct-value candidate pool fetched before ranking. Big enough to cover realistic client code spaces (EarnCode/TypeCode/departments are typically ≪200 distinct); bounds the embed batch size and result size. |
| `resolve_values_top_k` | **10** | Max ranked values returned to the model after ranking. Keeps the model-visible payload small; the model refines via `concept`/`askUser` if 10 is not enough. |
| `resolve_values_similarity_weight` | **0.7** | `w` in the score blend (§4). Semantic-dominant. |

All three are provisional pending real Phase-1 traffic (OQ-5), consistent with how phase-0 budget/preview
defaults are labelled provisional.

---

## 10. Q10 — `RESOLVE_VALUES_TOOL_SCHEMA` (exact JSON)

Authored locally in `runtime/mcp/tool_schema.py` (alongside `ASK_USER_TOOL_SCHEMA`) and appended in
`fetch_function_schemas` after `askUser`. Declares **no** `session_id`/`jwt`/`scope` (D5).

```python
RESOLVE_VALUES_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "resolveValues",
    "description": (
        "Resolve a fuzzy business CONCEPT to the concrete, client-specific values of a "
        "code/category column, ranked by how well each value matches the concept and how "
        "frequently it occurs for THIS client. Use this for client-defined or time-varying "
        "code spaces (e.g. EarnCode, TypeCode, department codes) where the exact codes differ "
        "per client and drift over time — never hardcode such codes. Prefer this over sampleRows "
        "when you need the values that mean a concept (e.g. 'PTO earn codes'), not a raw sample. "
        "Each result has a `score` (0-1); if the top scores are low or clustered (no clear "
        "winner), ask the user to confirm with askUser before filtering on a guessed value. "
        "The client/tenant is applied automatically — do not pass any client identifier."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "description": "Table holding the column, e.g. 'accrual_events' or "
                               "'dbpcm_warehouse.accrual_events'.",
            },
            "column": {
                "type": "string",
                "description": "The code/category column to resolve values for, e.g. 'EarnCode'.",
            },
            "concept": {
                "type": "string",
                "description": "The business concept to match, in the user's own words, e.g. "
                               "'paid time off' or 'overtime'. Free text — never a code.",
            },
            "period": {
                "type": ["object", "null"],  # JSON-Schema-valid nullable (not the non-standard "nullable": true)
                "description": "Optional. Restrict to a concrete date window (helps when codes "
                               "drift over time). Omit if not needed.",
                "properties": {
                    "column": {"type": "string",
                               "description": "A date/time column on the table to filter on."},
                    "start": {"type": "string", "description": "Inclusive start (ISO date)."},
                    "end": {"type": "string", "description": "Inclusive end (ISO date)."},
                },
            },
        },
        "required": ["table", "column", "concept"],
    },
}
```

`fetch_function_schemas` becomes:

```python
schemas = [translate_tool_spec(tool) for tool in tools]
schemas.append(ASK_USER_TOOL_SCHEMA)
schemas.append(RESOLVE_VALUES_TOOL_SCHEMA)   # NEW
return schemas
```

**Test impact:** `tests/runtime/mcp/test_tool_schema.py::test_fetch_function_schemas_includes_all_6_plus_ask_user`
(and `test_tool_schema_cache_caches_until_reload`, which asserts `len == 7`) must move to **8** and be
renamed (e.g. `…_all_6_plus_ask_user_plus_resolve_values`). The `test_no_credential_params_leak_in_any_schema`
scan auto-covers the new schema (it contains no credential params → passes as-is).

---

## New `RuntimeSettings` entries (with defaults)

Added to `runtime/config.py::RuntimeSettings` (env-var names uppercased, `.env`-backed, secrets
gitignored):

```python
# --- Embedding client (D71 custom API — resolveValues ranking) ---
embedding_api_url: str = Field("", description="Custom embedding API endpoint (D71). Empty => fake/degraded.")
embedding_api_key: str = Field("", description="Embedding API key (secret).")
embedding_model: str = Field("", description="Embedding model id (custom API).")
embedding_timeout_seconds: float = Field(10.0, gt=0, description="Per-request embedding timeout.")

# --- resolveValues composite (D77) ---
resolve_values_query_limit: int = Field(200, ge=1, description="LIMIT N on the backing runQuery.")
resolve_values_top_k: int = Field(10, ge=1, description="Max ranked values returned to the model.")
resolve_values_similarity_weight: float = Field(
    0.7, ge=0, le=1, description="Weight w on cosine similarity vs. normalized log-freq in the score.")
```

**Composition (`app.py`):** build the `EmbeddingClient` (real `HttpEmbeddingClient` from the embedding
settings, or `FakeEmbeddingClient` when unconfigured — decided in the factory, mirroring how other real
clients default), construct `ResolveValuesComposite(tool_dispatcher=dispatcher, embedding_client=...,
catalog=catalog, settings=…)`, and pass it into `AgentLoop(resolve_values=…)`. `create_app(...)` gains an
optional `embedding_client=` / `resolve_values=` injection point for Layer-1 smoke tests, exactly like
the existing `session_store=`/`mcp_client=`/`model_client=` seams.

---

## Test seams + Layer-1 test list

| Seam | Protocol | Fake (Layer 1) | Real (Layer 2/3) |
|---|---|---|---|
| Embedding | `EmbeddingClient` | `FakeEmbeddingClient` (seeded/scripted vectors) | `HttpEmbeddingClient` (custom API, `EMBEDDING` span) |
| Inner MCP call | (existing) `MCPClient` | `FakeMCPClient` scripting a `runQuery` response | `RealMCPClient` |

**Layer-1 tests (no infra):**
1. `sql_builder`: value+description query shape; value-only fallback when no descCol; sqlglot-quoted
   identifiers; period `WHERE` built from literals (assert values are quoted literals, not concatenated);
   `LIMIT` from settings.
2. `sql_builder` / validation: unknown table → error; bare table unique-resolves; bare table ambiguous →
   error; unknown column → error; unknown `period.column` → error.
3. `ranking`: `score = w·sim + (1−w)·logfreq` with scripted vectors — assert ordering; equal-freq case
   (similarity decides); degraded (freq-only) ordering; `top_k` truncation; `top_margin` computation.
4. `ResolveValuesComposite.run` happy path (FakeMCP `runQuery` + FakeEmbedding): returns
   `status="ok"`, contract-shaped `[{value, description, score, freq}]`, provenance == inner runQuery's.
5. Composite denial pass-through: FakeMCP raises `COLUMN_SCOPE_VIOLATION` on the inner `runQuery` →
   resolveValues `ToolResult(status="denied", …)` with the same code/retryable/user_message.
6. Composite empty-result → `status="ok"`, empty list.
7. Composite embedding-failure → `status="ok"`, `degraded=True`, freq-only order.
8. Redaction: `redact_tool_args("resolveValues", {"concept": ...})` → `concept == "<redacted>"`;
   `table`/`column` preserved; `period` literals masked.
9. Loop integration (Scripted model requests `resolveValues` → Fake composite): one `tool_calls_made`
   increment (inner query not double-counted); `TrailEntry` persisted with resolveValues args + inner
   provenance; D5 scan (no jwt/session_id in any message handed to `ScriptedModelClient`).
10. `tool_schema`: `fetch_function_schemas` now yields 8 (6 + askUser + resolveValues); credential-leak
    scan passes for the new schema.
11. `resolve()` programmatic path returns a typed `ResolveOutcome` without a `ToolResult` wrapper (D67
    hook).

**Deferred (needs infra / TBD endpoint):** real `HttpEmbeddingClient` against the live custom embedding
API (Layer 2) — deferred until the endpoint + model id exist (OQ-1); full resolveValues flow over the
real `clickhouse-api` MCP (Layer 2); Layer-3 conformance scenario for a client-defined-code question.

---

## Build order (each slice independently unit-testable, fakes first)

1. **`model/embedding_client.py`** — `EmbeddingClient` protocol + `FakeEmbeddingClient`. Pure seam,
   no infra.
2. **`composite/ranking.py`** — pure score function. Layer-1 with scripted vectors.
3. **`composite/sql_builder.py`** — validated target resolution + sqlglot SQL construction (incl. period
   literals + value-only fallback). Pure, Layer-1.
4. **`composite/resolve_values.py`** — `ResolveValuesComposite` wiring `sql_builder` + inner
   `dispatcher.dispatch("runQuery", …)` (FakeMCP) + `ranking` + FakeEmbedding; `run()` + `resolve()`.
   Layer-1.
5. **`mcp/tool_schema.py`** — add `RESOLVE_VALUES_TOOL_SCHEMA` + append; update the two count tests.
6. **`loop/agent_loop.py`** — the `resolveValues` interception branch (§1.3); trail-entry/budget path
   unchanged. Layer-1 loop test.
7. **`observability/redaction.py`** — `concept` full-redaction + `period` literal masking. Layer-1.
8. **`model/embedding_client.py::HttpEmbeddingClient`** + `EMBEDDING` span — real client. Layer-2
   deferred (OQ-1).
9. **`config.py` + `app.py`** — new settings + composition wiring (real-vs-fake embedding client
   selection, composite construction, `AgentLoop` injection). Smoke-tested via `create_app(...)` fakes.

---

## Couchbase / session impact

**None.** No document-shape change, no new collection, no migration. A `resolveValues` trail entry is an
ordinary `TrailEntry` (§8). Full ranked results ride on the existing `result_full`/`result_full_ref`
mechanism; previews on the existing `_build_preview` list branch. `SESSION_TTL` unchanged.

---

## Doc updates on completion (when this is built, not now)

- **`docs/decisions/DECISIONS.md`** — annotate **D77** with the resolved implementation details settled
  here (the composite seam, description-column convention, structured-`period` scope, ranking default
  `w=0.7`, limits). **Candidate new decision (OQ-4):** the *embedding-failure → freq-only degrade*
  policy is a genuine new choice — either mint it as a new D-number (e.g. D85) or record it as a named
  sub-decision under D77. Recommend a short sub-decision under D77 to avoid decision sprawl; defer the
  D-number call to the user.
- **`docs/decisions/TRACEABILITY.md`** — add invariant rows:
  (a) *resolveValues enforcement is the inner runQuery's* (D57/D5 via the backing query — no separate
  enforcement path); (b) *`concept` never appears in SQL* (D10); (c) *resolveValues provenance == inner
  runQuery provenance* (D44); (d) *embedding failure degrades to freq-only, never fails/leaks* (D77 +
  OQ-4 policy); (e) *`concept` redacted from spans* (D25). Each maps to a Layer-1 test slug above; the
  Layer-2/3 rows start `⛔ not-built`.
- **`docs/02-tools-and-api.md`** — the `resolveValues` row already exists (Runtime composite tools). Add
  only the *observable* additions this design introduces: the structured `period` shape and the
  `degraded`/freq-only fallback behaviour (and that low/clustered scores route to `askUser`). No count
  change (still 6 MCP + composites + control).
- **`docs/11-testing.md`** — add the Layer-1 test list above under Layer 1; note the deferred Layer-2
  real-embedding test (OQ-1) and a Layer-3 client-defined-code conformance scenario as `⛔ not-built`.
- **`docs/decisions/OPEN-QUESTIONS.md`** — mark the "resolveValues accept-vs-clarify threshold" and
  "ranking signals weighting" items resolved-for-Phase-1 by this design (model-owned clarify; `w=0.7`
  provisional), leaving the per-`(client,column)` index and period-domain resolution as still-open.

---

## Open Questions (safe interim defaults chosen — not blocking)

- **OQ-1 (custom embedding API endpoint/auth/model id).** TBD (D71/OPEN-QUESTIONS §Retrieval). **Interim
  default:** `embedding_*` settings default empty; `app.py` selects `FakeEmbeddingClient` when
  unconfigured; the degrade-to-freq path (§3.2) means an unconfigured/unreachable embedder never breaks
  the tool. The real `HttpEmbeddingClient` ships wired but its live Layer-2 test is deferred until the
  endpoint exists.
- **OQ-2 (description-column discovery). RESOLVED (structured `description_col` field, see §2.3 as-built).**
  A per-column `description_col:` catalog field now declares the sibling label column explicitly; discovery
  is declared → convention → value-only, surfaced via `CatalogHandle.description_col_for()`. The convention
  is retained as the fallback for undeclared columns. (Superseded the "deferred, loader is MCP-side only"
  interim — the runtime already had `load_semantic_catalog`; only the field + a small projection were new.)
- **OQ-3 (period-domain resolution).** This brick honors only a *concrete structured* `period`; deictic/
  relative/"latest settled period" resolution (D41/D65) is deferred to the D67 era. Absent `period` → no
  filter (freq-ranking still surfaces currently-common codes).
- **OQ-4 (embedding-failure policy as a decision).** Degrade-to-freq-only (§3.2) is recommended and used;
  whether it becomes its own D-number or a D77 sub-decision is the user's call (recommend: sub-decision
  under D77).
- **OQ-5 (ranking weight + limits defaults).** `w=0.7`, `query_limit=200`, `top_k=10` are provisional
  pending real Phase-1 traffic, mirroring how phase-0 budget/preview defaults are labelled.
```
