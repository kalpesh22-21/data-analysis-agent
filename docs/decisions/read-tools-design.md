# Model-Facing Read Tools — Retrieval Slice 3 Design (D8/D77 precedent, on D86/D87)

**Status:** Proposed (design only — no code written this pass).
**Owner seam:** the three model-facing knowledge-plane read tools —
`searchBlueprints`, `getBlueprint`, `searchKnowledge` — implemented in the agent
runtime over the now-real D86 pipeline + D87 neo4j corpus. Builds *on top of*
Slices 1–2; redesigns nothing in them.
**Author's discipline:** this pass wrote exactly this one file. Every code/
schema/config/test change it recommends (`retrieval/vector_index.py`,
`retrieval/pipeline.py`, a new `retrieval/tools.py`, `retrieval/models.py`,
`mcp/tool_schema.py`, `loop/agent_loop.py`, `dispatch/denial_mapping.py`,
`observability/redaction.py`, `config.py`, `app.py`, Layer-1/2/3 tests, and the
DECISIONS/OPEN-QUESTIONS/TRACEABILITY/02/03/04/11/WORKLOG docs) is enumerated in
§10 "Doc/code updates on completion" for the implementing developer — none were
made here.

---

## 0. Ground truth this design relies on (verified by reading code + docs)

- **The tool surface is fixed (D4/02).** The knowledge plane is exactly four
  read tools: `searchBlueprints(query, k)`, `getBlueprint(id)`,
  `runBlueprint(id, slot_bindings)`, `searchKnowledge(query)`. This brick builds
  the **first, second, and fourth**; `runBlueprint` is Slice-3-execution
  (§8 out of scope). D8 fixes the contract: **thin cards are pre-injected for
  free; the model pulls full detail on demand** via `getBlueprint`, and
  reformulates via `searchBlueprints(query, k)` when the pre-injected 3 miss.
- **The established pattern for a model-facing, runtime-implemented tool is
  `resolveValues` (D77, BUILT Session 9).** It is authored locally in
  `mcp/tool_schema.py` (`RESOLVE_VALUES_TOOL_SCHEMA`, appended in
  `fetch_function_schemas` after `askUser`), **intercepted in the loop** by a
  branch symmetric to `askUser` (`agent_loop.py`: `if tool_call.name ==
  "resolveValues": tool_result = await self._resolve_values.run(args, creds)`),
  returns the **same `ToolResult` dataclass** the dispatcher returns (so the
  loop's `write_full_result` + `TrailEntry` + budget path is unchanged and it
  counts as **exactly one** `tool_calls_made`), and when its backing machinery is
  not wired the loop returns a **clean local error** (`_resolve_values_unavailable`)
  rather than an incoherent MCP unknown-tool denial. **These three read tools are
  the same shape** — the design mostly *reuses* this pattern and *generalizes the
  interception* (§2).
- **The D86 pipeline is real and does recall→scope-filter→rerank→cut, per corpus,
  with degrade-not-fail and spans.** `retrieval/pipeline.py::RetrievalPipeline`
  exposes `retrieve(question, column_scope, user_id)` (both corpora at once) and
  holds the per-corpus private helpers this brick reuses: `_recall_with_span`
  (recall + blueprint scope pre-filter inside a `recall_span` CHAIN),
  `_rerank_corpus` (rerank inside a `rerank_span` RERANKER, degrade to recall
  order), `_apply_knowledge_floor`, `_to_thin_card`, `_to_knowledge_hit`. It is a
  **stateless, shareable singleton** (its own docstring) — the same instance
  `ContextAssembler` holds for pre-injection can back the tools.
- **The D87 store is real and holds only the retrieval projection — the full DAG
  is deliberately NOT stored yet.** `corpus_loader.py::_UPSERT_BLUEPRINT` writes a
  `:Blueprint` node with **exactly**: `id, intent, slots_summary,
  intent_embedding, embedding_model, uses (denormalized "db.table.column" list),
  status, drift_status, catalog_sha, created_by, created_at, hit_count`. The
  fields `getBlueprint` "should" return per 02 — `resolves`, typed `slots`,
  `uses_rules`, `composes`, `sql_template` — **are the reserved full-DAG
  extension** (`04-blueprints.md` §Storage §1.5 / neo4j-corpus-design §1.5), not
  stored until `runBlueprint` lands. This directly constrains `getBlueprint`'s
  honest Slice-3 return shape (§1.2).
- **`Neo4jVectorIndex` holds the driver and already isolates its query seam.**
  `vector_index.py::Neo4jVectorIndex._run(query, params)` is the single
  driver-touching method (monkeypatchable for Layer-1). `recall` never raises
  (every failure → `[]`, D86 degrade). Adding a keyed `get_blueprint(id)` fetch
  is a natural sibling read on the same driver (§1.2/§4). `FakeVectorIndex`
  is the in-memory Layer-1 double.
- **`Candidate.uses` is a `frozenset[str]` of byte-exact `"database.table.column"`
  strings**, and `scope_filter.filter_blueprints_by_scope(candidates, scope)`
  keeps iff `uses <= column_scope` (`None` → drop fail-closed; empty scope →
  allow-all, D80(b)). Knowledge candidates carry `uses=None` and are **never**
  passed to the scope filter (entity-agnostic, D58(a)/D60). `credentials.column_scope`
  is the injected scope (D5) — `resolveValues` already reads it.
- **Provenance for a non-dispatched tool is set by the tool itself.**
  `resolveValues` copies its *inner runQuery's* provenance onto the `ToolResult`
  it returns; it never reaches `capture_provenance` (which returns `None` for
  unknown tool names). `provenance/capture.py` already treats the three
  data-less tools (`listDatabases`/`listTables`/`explainQuery`) as
  `frozenset()` ("determined, zero columns → always kept in replay"), as opposed
  to `None` ("undetermined → dropped fail-closed"). This is the template for §3.
- **Redaction masks by argument-key allowlist.** `redaction.py`:
  `_FULLY_REDACTED_ARG_KEYS = {"concept"}` replaces free user text with
  `"<redacted>"` in span attributes (the precedent that kept `askUser`'s
  `question` out of spans). `progress.py::_SHAPE_ALLOWLIST` already lists
  `tool_name`, `error_code`, `blueprints`, `knowledge`; `_STEP_LABELS` renders
  `tool_dispatch_start/ok/denied` from the (allowlisted) `tool_name`.
- **Denial rendering table.** The `RESOLVE_VALUES_*` codes were added to the
  denial table (`denial_mapping.py`, L5 review fix) so replayed trail entries
  render specific messages — new runtime-tool error codes need the same.
- **Layer-2 infra exists.** `docker-compose.integration.yml` stands up neo4j
  (7687) + embedding (18003) + reranker (18004); Slice-2 live tests skip-guard on
  `NEO4J_TEST_URI` (+ `EMBEDDING_TEST_URL`/`RERANKER_TEST_URL`). This brick
  extends that harness, adds no new service.

---

## 1. Q1 — Tool surface: schemas + return shapes

All three are **locally-authored** schemas (like `RESOLVE_VALUES_TOOL_SCHEMA`),
declaring **no** `session_id`/`jwt`/`scope` (D5), appended in
`fetch_function_schemas` after `resolveValues` — taking the count **8 → 11**
(6 MCP + askUser + resolveValues + these 3). *(The advertised total is **15**
today: `runBlueprint`, `recordAssumptions`, `answerWithTable` and
`updateAnalysisState` landed after this slice — see
[02-tools-and-api.md](../02-tools-and-api.md).)* Each returns a `ToolResult` whose
`result_full` is a small dict wrapper (the model reads only `result_preview`,
which the existing `_build_preview` non-tabular-dict branch carries whole).

### 1.1 `searchBlueprints(query, k)` → reranked thin cards, scope-pre-filtered

- **Params:** `query: string` (required; the reformulated intent, free text),
  `k: integer` (optional; how many cards; clamped `1..RETRIEVAL_SEARCH_MAX_K`
  (default 20); absent → `RETRIEVAL_SEARCH_DEFAULT_K` (default 5)).
- **Description guidance (when to call) — SUPERSEDED by Release 1 §3/§4.** The original
  text steered the model to lean on pre-injection first (D8) and call this only
  "when those 3 miss or you have reformulated the intent". **That framing shipped
  and was then removed**, from `AGENT_SYSTEM_PROMPT` (deliverable 01) and from
  `SEARCH_BLUEPRINTS_TOOL_SCHEMA` (its follow-up), because it is wrong on a
  multi-part request: the pre-injected cards were recalled from the **whole
  question as ONE string**, so on a request with several deliverables they
  under-serve every part of it. **The shipped instruction is per-deliverable
  search as normal practice, not a fallback:** call it for EVERY analytical
  deliverable the request contains, in the model's own words — **one search per
  deliverable, not one for the whole question** — **whether or not** one of the
  offered cards already fits. `getBlueprint(id)` is then needed only for the full
  DAG (the `uses` footprint, the SQL template), a composition summary, or a card
  carrying `slots_omitted`; the enriched card (below) otherwise carries enough to
  choose a candidate AND fill `runBlueprint`.
- **Return** (`result_full`):
  ```jsonc
  { "count": 2, "degraded": false,
    "blueprints": [ { "id": "bp_avg_salary_one_dept",
                      "intent": "average salary for a single named department…",
                      "slots_summary": "department (string, req), pay_period (period, req)",
                      "score": 0.81 }, … ] }
  ```
  Same `ThinCard` shape the pre-injection uses (`{id, intent, slots_summary,
  score}`) — one code path, one shape. `degraded: true` when the embedder/reranker
  degraded (recall order, no semantic rerank) so the model knows ranking is weaker.
  **As shipped (Release 1 §4), the search card is ENRICHED** beyond this block: when
  the blueprint stores a DAG it also carries `resolves`, a `{name, type, required}`
  `slots` summary (plus `slots_omitted` when the per-card cap dropped some) and
  `result_grain`. Keys are omitted when absent, so a DAG-less blueprint still
  serialises exactly as above. `status` is deliberately NOT added — recall already
  filters to `validated`. The enrichment also changes the entry's D44 posture: its
  provenance is the **union of the returned cards' `uses`**, not the safe-empty
  `frozenset()` a column-free card justified.
- **Scope:** the transitive-USES pre-filter is applied (§3) — a card the user
  cannot run is never returned.

### 1.2 `getBlueprint(id)` → the stored retrieval projection (honest Slice-3 shape)

- **Param:** `id: string` (required).
- **The honest decision — thin expand now, full DAG when `runBlueprint` lands.**
  02 specifies `getBlueprint` returns the "full DAG" (`resolves`, typed `slots`,
  `uses_rules`, `composes`, `sql_template`, `status`, `hit_count`). **In Slice 3
  those DAG fields are not stored** (D87 §1.5 reserves them for the
  `runBlueprint` brick). Two options were weighed:
  - **(A) Defer `getBlueprint` entirely** until full-DAG storage. Rejected — it
    leaves the D8 progressive-disclosure "expand" step with no tool at all, blocks
    the Slice-2-design §4.4 `getBlueprint` conformance scenario, and forces a
    later big-bang wiring of both the tool and the storage at once.
  - **(B, chosen) Ship `getBlueprint` now as a keyed fetch of the stored
    projection**, growing additively when the DAG lands. It returns:
    ```jsonc
    { "found": true,
      "id": "bp_avg_salary_one_dept",
      "intent": "average salary for a single named department, active staff…",
      "slots_summary": "department (string, req), pay_period (period, req)",
      "uses": ["hr.employee_info.department", "hr.payroll_fact.gross_pay", …],
      "status": "validated", "drift_status": "clean",
      "hit_count": 0, "catalog_sha": "…" }
    ```
  - **What this is honestly useful for now (pre-`runBlueprint`):** confirming a
    thin card's intent in full; seeing the blueprint's exact **table/column
    footprint** (`uses`) and **lifecycle** (`status`/`drift_status`/`hit_count`)
    before deciding to rely on it. It is **not** yet enough to slot-fill (needs
    typed `slots`) or inspect SQL — and it **cannot be**, because `runBlueprint`
    (the only consumer of typed slots + `sql_template`) does not exist this slice.
    So (B) ships precisely the fields that exist, is truthful about the rest, and
    the return shape is a **strict subset** that grows by pure addition (§1.5 of
    D87 — no migration). The tool description says: "Returns the blueprint's
    intent, the tables/columns it reads, and its status. Full parameter and SQL
    detail arrives with the ability to run it."
- **Miss handling is a non-oracle (§3):** an id that does not exist **and** an id
  whose stored `uses ⊄ scope` both return the identical
  `{ "found": false }` (status `ok`) — the model reacts the same way (re-search),
  and a caller cannot probe which blueprints exist outside its scope.

### 1.3 `searchKnowledge(query)` → reranked knowledge chunks (OQ-R3 interim: chunks)

- **Param:** `query: string` (required). **No `k`** (per 02); cut to
  `RETRIEVAL_SEARCH_KNOWLEDGE_K` (default 5 — a touch above the pre-inject top-3
  since the model asked explicitly).
- **Return** (`result_full`): `{ "count": n, "degraded": bool, "knowledge":
  [ { "id", "text", "score", "title" }, … ] }` — the same `KnowledgeHit` shape as
  pre-injection (OQ-R3 interim: **reranked chunks**, finalize with the knowledge
  store). Bypasses scope (§3).
- **Description guidance:** "RAG over global, entity-agnostic institutional
  knowledge and lessons — definitions, conventions, gotchas. Not client data; do
  not use it to look up a specific tenant's values (use `resolveValues` for that)."

---

## 2. Q2 — Seam: generalize the loop interception into a small runtime-tool registry

**Decision: YES, generalize — but surgically.** There are today two intercepted
tools (`askUser`, `resolveValues`) and three more arriving that are **all the
`resolveValues` shape** (return an inline `ToolResult`, count as one call). Five
per-name `if` branches in the hot path is the smell the brief flags. The minimal
generalization:

- **A `RuntimeTool` protocol** (one method, exactly the shape `resolveValues`
  already satisfies):
  ```python
  class RuntimeTool(Protocol):
      async def run(self, model_args: dict[str, Any],
                    credentials: RuntimeCredentials) -> ToolResult: ...
  ```
- **`AgentLoop` gains `runtime_tools: Mapping[str, RuntimeTool]`** (default empty)
  and the per-tool-call branch collapses to one lookup:
  ```python
  if tool_call.name == "askUser":        # unchanged — TERMINAL (pause), not a ToolResult
      … existing checkpoint path …
  handler = self._runtime_tools.get(tool_call.name)
  if handler is not None:
      tool_result = await handler.run(tool_call.arguments, credentials)
  else:
      tool_result = await self._tool_dispatcher.dispatch(
          tool_call.name, tool_call.arguments, credentials)
  tool_calls_made += 1
  # write_full_result + TrailEntry + budget path UNCHANGED
  ```
  `resolveValues` moves **into** the registry (app.py registers it); the read
  tools register alongside. `askUser` **stays a hardcoded terminal branch** — it
  pauses (writes a checkpoint, returns control), so it is genuinely not a
  `ToolResult`-producing tool and does not belong in the registry.
- **Why not keep per-tool branches:** four `resolveValues`-shaped branches is
  copy-paste that will rot; the registry is a two-line hot-path change plus a dict
  built once in `app.py`. **Why not a bigger abstraction** (self-describing tools
  that also emit their own schema, an MCP-style dispatcher-2): rejected as
  astronautics — the schema authoring stays in `tool_schema.py` (the existing
  single source), the registry only routes *dispatch*. Reversible: the registry is
  a `Mapping`; removing it is deleting a dict.
- **Diff surgery note:** the existing `resolve_values: ResolveValuesComposite |
  None` constructor arg and `_resolve_values_unavailable` helper are replaced by
  the registry + a shared `_runtime_tool_unavailable(name, code)` helper. This
  touches the handful of Layer-1 loop tests that inject `resolve_values=` — they
  move to `runtime_tools={"resolveValues": …}`. Called out in §10 as the one
  non-trivial existing-test migration.

**Tool module placement:** a new `src/data_agent/runtime/retrieval/tools.py`
holding `SearchBlueprintsTool`, `GetBlueprintTool`, `SearchKnowledgeTool` —
colocated with the `RetrievalPipeline` + `VectorIndex` they depend on (the loop
imports them exactly as it imports `ResolveValuesComposite`). `SearchBlueprintsTool`
and `SearchKnowledgeTool` hold the shared `RetrievalPipeline`; `GetBlueprintTool`
holds the `VectorIndex` store (keyed fetch, no pipeline).

---

## 3. Q3 — Scope enforcement + provenance

| Tool | Scope treatment | Rationale |
|---|---|---|
| `searchBlueprints` | **Transitive-USES pre-filter applied** — reuse `scope_filter.filter_blueprints_by_scope` with `credentials.column_scope`. Identical to pre-injection. | The read-path analogue of the D44 trail filter (03 §Scope): the user never sees a blueprint they can't run. Defense-in-depth; the hard gate stays `runQuery` execution. |
| `getBlueprint` | **Deny as not-found (fail-closed, non-oracle).** Fetch by id, then `is_blueprint_in_scope(candidate, scope)`; out-of-scope **or** absent → identical `{found:false}`. | Filtering a single keyed object is meaningless (there is nothing to filter *to*); denial is correct. Making out-of-scope **indistinguishable** from not-found prevents a scope-probing oracle (enumerate ids → learn which exist). Mirrors `resolveValues`' "name only the supplied target, never enumerate." |
| `searchKnowledge` | **Bypasses scope — confirmed against the docs.** Knowledge candidates carry `uses=None`, are never passed to the scope filter, and are **entity-agnostic + leakage-gated at write** — D58(a): only **human-approved** knowledge is retrievable; validation is entity-blind so the human pre-gate is the guarantee, not a read-time scope check. | Scope gates *warehouse column data*; knowledge is global institutional prose with no tenant columns. A scope filter here would be a category error (there are no `uses` to subset). |

**Provenance (D44) — two postures, split by what the result exposes (review
correction, S1).** All three tools set `ToolResult.provenance` **in the tool
code** (they are intercepted, never reaching `capture_provenance` — same as
`resolveValues` sourcing its own). But the posture is **not** uniform
`frozenset()`; it splits by whether the result carries a scope-relevant
**column footprint**:

- **`searchBlueprints` and `searchKnowledge` → `frozenset()` (safe-empty).** Their
  results are corpus metadata with **no per-result column footprint**: thin cards
  (id + intent + slots summary + score — the `uses` is NOT in the result), and
  entity-agnostic knowledge prose. `frozenset()` means **determined, zero
  warehouse columns referenced → trivially in-scope, always kept** — exactly the
  posture `capture.py` gives the data-less tools
  `listDatabases`/`listTables`/`explainQuery`; these two are their read-path
  siblings. `searchBlueprints` is additionally scope-pre-filtered, so nothing
  out-of-scope was ever returned. We **want** this reasoning to survive replay.
  (Defense-in-depth: both names are in `capture._NO_PROVENANCE_TOOLS`, so *if*
  one were ever dispatched it still maps to `frozenset()`.)
- **`getBlueprint` (FOUND path) → the blueprint's scoped `uses` footprint, NOT
  `frozenset()`.** `getBlueprint` exposes the blueprint's exact **table/column
  footprint** (`uses`) — the same *class* of information `getTableSchema` exposes
  (column identifiers), and, like `getTableSchema`, it **must drop from replay
  under a later scope narrowing**. If it were `frozenset()`, a mid-session scope
  narrowing would leave the entry immune to the D44 replay filter, so the model
  would keep re-surfacing a now-forbidden blueprint's **existence + full footprint**
  — contradicting `06-security-and-governance.md`'s scope table ("a restricted
  report's very existence isn't leaked"). So the tool derives provenance from the
  scope-checked `uses`: each `"database.table.column"` key is split (via
  `rpartition(".")`) into the `(db_table, column)` tuple shape
  `context/scope_filter.is_provenance_in_scope` consumes (it rebuilds
  `f"{db_table}.{column}"` and checks scope membership). Under the **unchanged**
  scope the entry is kept (it was in-scope at call time); under **narrowing** it
  drops. Fail-closed: a malformed `uses` key (no dot, empty parts) → `None` for
  the whole set (dropped). Note (re-review correction): the split is *last-dot*,
  so a hypothetical four-segment key round-trips byte-exactly rather than
  coercing to `None` — identical to the call-time exact-string subset check,
  which is the invariant that matters.
  `getBlueprint` is therefore **NOT** in `_NO_PROVENANCE_TOOLS` (an empty default
  there would be fail-open); an ever-dispatched getBlueprint falls through to the
  unknown-tool `None` — the safe posture.
- **`getBlueprint` (NOT-FOUND path) and every tool's error path → `frozenset()`
  (or `None` on error).** The `{found:false}` result carries no footprint, so it
  stays the safe-empty `frozenset()` — which also keeps the §3 non-oracle intact
  (out-of-scope and absent are byte-identical, both `frozenset()`).

---

## 4. Q4 — Ranking / limits: reuse the pipeline; keyed fetch for getBlueprint

**Reuse `RetrievalPipeline`, do not hand-roll recall+rerank.** The pipeline's
degrade discipline, `recall_span`/`rerank_span`, and scope pre-filter come free;
a second copy would drift. The pipeline is "corpus-pair-shaped" (`retrieve` does
both) — the **refactor is minimal**: extract the single-corpus path (already
factored into `_recall_with_span` + `_rerank_corpus` + the cut helpers) behind
two thin **public** methods that `retrieve` *also* calls:

```python
# retrieval/pipeline.py (new public methods; retrieve() refactored to call them)
async def search_blueprints(self, *, question: str, column_scope: frozenset[str],
                            k: int, observer=None) -> tuple[list[ThinCard], bool]:
    """embed → recall(blueprint) → scope pre-filter → rerank → top-k.
    Returns (cards, reranked). Degrade-not-fail: empty (cards=[], reranked=False)
    on any embedder/index failure — never raises."""

async def search_knowledge(self, *, question: str, k: int,
                           observer=None) -> tuple[list[KnowledgeHit], bool]:
    """embed → recall(knowledge) → (no scope filter) → rerank → floor → top-k."""
```

- **`k` handling:** `searchBlueprints` recall uses `max(recall_k, k)` (so a large
  `k` still gets a full candidate pool), then the reranked list is cut to the
  clamped `k`. `searchKnowledge` cuts to `RETRIEVAL_SEARCH_KNOWLEDGE_K`.
- **`getBlueprint` is NOT a recall** — it is a keyed fetch, so it does **not**
  touch the pipeline. Add a keyed read to the store seam:
  ```python
  # VectorIndex protocol + FakeVectorIndex + Neo4jVectorIndex
  async def get_blueprint(self, blueprint_id: str) -> BlueprintDetail | None: ...
  ```
  `Neo4jVectorIndex.get_blueprint` runs one parameterized `MATCH (b:Blueprint
  {id:$id}) RETURN …` through the existing `_run` seam, maps to a new frozen
  `BlueprintDetail` (models.py) carrying the §1.2 projection fields, and — like
  `recall` — **never raises** (driver/query failure → `None`, D86 degrade =
  "not found", which the tool renders as `{found:false}`). `FakeVectorIndex`
  answers from its seeded entries. **Naming trade-off named:** `get_blueprint`
  lives on `VectorIndex` even though it is not a vector op — the alternative
  (a parallel `BlueprintStore` protocol) doubles the store abstractions and the
  app-wiring for one keyed read over the *same driver*. Accepted; documented.
- **Budget:** each tool = **exactly one** `tool_calls_made` (the registry
  increments once per `capped_tool_calls` element, §2), like `resolveValues`.
  There is **no inner `runQuery` fan-out** — unlike `resolveValues`, these tools
  call the pipeline/store directly, so "one model-visible call = one recall (or
  one keyed fetch)". Wall-clock/token budget is checked by the loop's existing
  `BudgetGuard` after the tool returns.

---

## 5. Q5 — Observability / redaction

- **Span kinds.** Each tool emits **one `TOOL` span** (reuse `tracing.tool_span`,
  exactly like `resolveValues`). For `searchBlueprints`/`searchKnowledge` the
  pipeline's `recall_span` (CHAIN), `rerank_span` (RERANKER) and `EMBEDDING` span
  **nest inside** it via the ambient OTel context — a clean
  `searchBlueprints → recall → rerank → embedding` trace. `getBlueprint`'s TOOL
  span wraps a single keyed neo4j read (no recall/rerank; a nested CHAIN is
  optional and low-value — skip it).
- **Redaction (D25, load-bearing).** `query` is **model-authored free text that
  may quote user PII** — treat it exactly like `concept`: add `"query"` to
  `redaction._FULLY_REDACTED_ARG_KEYS` so it becomes `"<redacted>"` in every span
  attribute. `id` (a structural blueprint id) is non-PII and kept for
  debuggability, like `table`/`column` on `resolveValues`. **Never** logged to
  spans/progress: query text, card intent text, knowledge chunk text, blueprint
  ids in *results*, scores (mirror the pipeline §3.5 discipline — ids/intent/chunk
  text "not worth the redaction surface").
- **Progress events (D61).** Reuse the existing `tool_dispatch_start` →
  `"running {tool_name}…"` and `tool_dispatch_ok`/`tool_dispatch_denied` labels
  (each tool emits them with `tool_name="searchBlueprints"` etc.). **No
  `_SHAPE_ALLOWLIST` change is required** — `tool_name`/`error_code` are already
  allowlisted, and `blueprints`/`knowledge` (shape-only counts) are too, so a tool
  *may* optionally emit a `{"blueprints": n}` / `{"knowledge": n}` completion count
  reusing those keys. No result values/text/ids/scores ever enter a progress
  payload. Optional nicety: add label entries (not allowlist entries) so the UI
  shows "searching the blueprint library…" — strictly cosmetic.

---

## 6. Q6 — Degrade / errors (fail-closed, aligned with resolveValues L2)

Two distinct "not fully working" cases, deliberately different outcomes:

- **Backing pipeline/store NOT wired at all** (no neo4j + no embedder → `app.py`
  leaves `retrieval=None`): the tool is **still advertised** (its schema is
  locally authored, always present — like `resolveValues`/`askUser`), but the
  loop's registry has no handler *or* the handler holds a `None` pipeline → return
  a **clean local error** `RETRIEVAL_TOOL_UNAVAILABLE` (`retryable=False`,
  canned message "Blueprint/knowledge search is not available right now."). This
  is the exact `resolveValues` L2 precedent (`_resolve_values_unavailable`),
  generalized to `_runtime_tool_unavailable(name, code)`.
- **Backing stack wired but degraded** (embedder down / index empty / model-parity
  mismatch): return **`status="ok"` with empty results + `degraded=true`** — the
  same posture the pre-injection takes (empty `RetrievedContext`, never fail the
  turn, D86). The model sees zero cards + a degrade flag and falls back to the raw
  loop. `getBlueprint` under a store failure → `{found:false}` (indistinguishable
  from a real miss, §3).
- **Empty results** (search returned nothing in scope): `status="ok"`, empty list
  — a *valid* answer ("no matching blueprint/knowledge"), exactly like
  `resolveValues`' empty-result-is-ok.
- **Malformed args** (missing/blank `query`; missing/blank `id`; non-integer/
  negative `k`): fail-closed **before** any work → `status="error"`,
  `RETRIEVAL_TOOL_INVALID_ARGS` (`retryable=True`, message names only the bad
  arg, no enumeration). `k` out of range is **clamped**, not an error (a lenient
  choice — a model over-asking for 100 cards gets 20, not a rejection).
- **Error-code family — shared `RETRIEVAL_TOOL_*`, not per-tool.** The brief asks
  `SEARCH_BLUEPRINTS_*` vs a shared prefix. **Decision: shared**, because three
  tools share one machinery and one failure vocabulary; the tool name already
  rides in the message and the span. The codes:
  `RETRIEVAL_TOOL_INVALID_ARGS`, `RETRIEVAL_TOOL_UNAVAILABLE`, and (as-built,
  review round) `RUNTIME_TOOL_INTERNAL_ERROR` for the registry-level B4 guard.
  A blueprint miss is surfaced as `ok`+`{found:false}` — a *state*, not an error
  code (the originally-proposed `GET_BLUEPRINT_NOT_FOUND` code was dead by
  construction and removed at review). All are
  added to `denial_mapping._DENIAL_TABLE` (L5 precedent) so replayed trail entries
  render specific, PII-safe messages.

---

## 7. Q7 — Tests per layer + Layer-3 conformance unlocked (D68 red burndown)

- **Layer-1 (no infra), the bulk:**
  - `pipeline.search_blueprints` / `search_knowledge`: recall→scope-filter→
    rerank→cut ordering; degrade (no embedder → empty+degraded; no reranker →
    recall order, `reranked=False`); `k` clamp + recall-pool sizing; knowledge
    NOT scope-filtered.
  - `scope_filter` reuse in `searchBlueprints`: a card whose `uses ⊄ scope` is
    dropped; `uses ⊆ scope` kept; empty scope → allow-all (D80(b)).
  - `getBlueprint`: in-scope hit returns the projection; **out-of-scope and
    absent both return identical `{found:false}`** (the non-oracle assertion);
    `FakeVectorIndex.get_blueprint` mapping; store failure → `{found:false}`.
  - `BlueprintDetail` record mapping from a fake neo4j `Record` (locks the field
    set without infra).
  - Each tool's `ToolResult`: `provenance == frozenset()`; empty-result ok;
    malformed-args → `RETRIEVAL_TOOL_INVALID_ARGS`; unwired → `RETRIEVAL_TOOL_UNAVAILABLE`.
  - Redaction: `redact_tool_args("searchBlueprints", {"query": …})` →
    `query == "<redacted>"`; `id` preserved.
  - Loop registry: a scripted model requesting each tool → one `tool_calls_made`
    increment each; `TrailEntry` persisted with `frozenset()` provenance; D5 scan
    (no jwt/session_id in any message handed to the scripted model). `askUser`
    still terminal-pauses (registry did not swallow it).
  - `tool_schema`: `fetch_function_schemas` yields **11**; credential-leak scan
    passes for the 3 new schemas.
- **Layer-2 (live mocks + neo4j), skip-guarded on `NEO4J_TEST_URI`
  (+ `EMBEDDING_TEST_URL`/`RERANKER_TEST_URL`):** seed the corpus via
  `corpus_loader` (Slice-2 fixture), then `searchBlueprints` reranks the overtime
  blueprint above an irrelevant one; `getBlueprint(id)` returns the stored
  projection with byte-exact `uses`; a narrow real JWT scope drops an out-of-scope
  blueprint from `searchBlueprints` and yields `{found:false}` from `getBlueprint`;
  `searchKnowledge` returns a seeded chunk and is unaffected by scope; unreachable
  neo4j → all three degrade cleanly (empty / not-found / unavailable).
- **Layer-3 (Playwright, D68 red burndown).** This brick greens the **D8
  progressive-disclosure pull scenarios** that Slice-2-design §4.4 named for "the
  read tools": the model **reformulates → `searchBlueprints` → `getBlueprint`
  expand**, and **`searchKnowledge` retrieval**, over the real UI (deterministic
  via scripted model doubles / cassettes, D76). It does **NOT** green the
  *fast-path execution* scenarios (pick → run → D56 verify) — those need
  `runBlueprint` (Slice-3-execution). Concretely: the "model can find + expand a
  blueprint / retrieve knowledge" red scenarios flip green; the "wrong-grain
  blueprint caught by the verify gate" red stays red until execution lands.

---

## 8. Q8 — Out of scope + open questions

**Out of scope (explicit):**
- **`runBlueprint` + the D56 verify gate + D67 `resolve_via` wiring** — Slice-3
  execution, a separate brick.
- **Full-DAG blueprint fields** (`composes`, `sql_template`, `resolves`,
  `uses_rules`, typed `slots`) — reserved D87 §1.5; `getBlueprint` returns the
  thin projection until they are stored (§1.2).
- **Track B** (learning-loop-authored corpus) — reads here are over the
  hand-seeded trusted corpus; the render trust-boundary reconfirmation (structural
  sanitization sufficient while content is curated) is unchanged (Slice-1 §8.1 /
  Slice-2 §5.1). Track-B *learned* content is the trigger to revisit.
- **User-memory tool** — none; user memory is pre-injection-only, never a tool.

**Open questions (safe interim defaults chosen — not blocking):**
- **OQ-T1 `getBlueprint` full-DAG shape** → interim = stored projection (§1.2);
  grows additively when `runBlueprint` + full-DAG storage land. Resolve *with*
  that brick (it owns the DAG schema).
- **OQ-T2 `searchKnowledge` chunks vs. summarized facts** (02 open-q / OQ-R3) →
  interim = **reranked chunks**; finalize with the knowledge store.
- **OQ-T3 `k` defaults/clamps** (`RETRIEVAL_SEARCH_DEFAULT_K=5`,
  `_MAX_K=20`, `_KNOWLEDGE_K=5`) → provisional, tune on traffic (mirrors OQ-R1).
- **OQ-T4 `getBlueprint` out-of-scope = not-found (non-oracle)** → chosen for
  anti-enumeration; revisit only if a UX need to *explain* the denial emerges
  (then a distinct in-scope-but-denied message, still leaking nothing about
  existence).
- **OQ-T5 error-code family** → shared `RETRIEVAL_TOOL_*` (§6); revisit only if a
  per-tool code proves necessary for the UI.

---

## 9. Risks & trade-offs (named explicitly)

- **`getBlueprint` under-delivers vs. its 02 contract this slice.** It returns a
  projection, not the DAG — a model expecting `sql_template`/typed `slots` finds
  them absent. Mitigation: the tool *description* states the boundary; the shape
  is a strict, additively-growing subset; and there is no consumer of the missing
  fields yet (`runBlueprint` doesn't exist). Trade: a thin-but-real expand now vs.
  a big-bang later — chose reversible/incremental.
- **Non-oracle not-found could mildly confuse debugging** (a real id typo looks
  like an out-of-scope denial). Mitigated by server-side logging distinguishing
  the two; the *model/user* deliberately cannot. Security > debuggability here.
- **Registry generalization touches existing `resolveValues` loop tests.** One
  bounded migration (`resolve_values=` → `runtime_tools={…}`), fully enumerated in
  §10; the runtime behavior for `resolveValues` is unchanged.
- **Shared pipeline singleton across pre-inject + tools.** Correct (it is
  stateless), but a config/degrade change now affects both surfaces at once —
  acceptable and intended (one source of retrieval truth).
- **`k` clamping hides model over-asks silently.** A model asking for 100 gets 20
  with no signal. Accepted (lenient > brittle); the count is visible in the
  result so the model can notice it got fewer than asked.

---

## 10. Doc/code updates on completion (NOT done this pass)

- **`retrieval/vector_index.py`** — add `get_blueprint(id) -> BlueprintDetail |
  None` to the `VectorIndex` protocol, `FakeVectorIndex`, and `Neo4jVectorIndex`
  (keyed `MATCH` through `_run`, map to `BlueprintDetail`, never-raise → `None`);
  extend `__all__`.
- **`retrieval/models.py`** — add the frozen `BlueprintDetail` (§1.2 projection
  fields); extend `__all__`.
- **`retrieval/pipeline.py`** — add public `search_blueprints` / `search_knowledge`
  single-corpus methods; refactor `retrieve` to call them (behavior-preserving).
- **`retrieval/tools.py`** (new) — `SearchBlueprintsTool`, `GetBlueprintTool`,
  `SearchKnowledgeTool` (each `.run(args, creds) -> ToolResult`, `provenance =
  frozenset()`, the §6 degrade/error handling), plus the `RuntimeTool` protocol
  (or place it beside `AgentLoop`).
- **`mcp/tool_schema.py`** — add `SEARCH_BLUEPRINTS_TOOL_SCHEMA`,
  `GET_BLUEPRINT_TOOL_SCHEMA`, `SEARCH_KNOWLEDGE_TOOL_SCHEMA`; append all three in
  `fetch_function_schemas` after `resolveValues`; update the count tests (7 → and
  the resolveValues test 8 →) to **11** and rename.
- **`loop/agent_loop.py`** — add `runtime_tools: Mapping[str, RuntimeTool]`;
  replace the `resolveValues` branch with the registry lookup; keep `askUser`
  terminal; replace `_resolve_values_unavailable` with a shared
  `_runtime_tool_unavailable(name, code)`.
- **`dispatch/denial_mapping.py`** — add `RETRIEVAL_TOOL_INVALID_ARGS`,
  `RETRIEVAL_TOOL_UNAVAILABLE` (+ as-built `RUNTIME_TOOL_INTERNAL_ERROR`;
  `GET_BLUEPRINT_NOT_FOUND` dropped as dead) to `_DENIAL_TABLE`.
- **`observability/redaction.py`** — add `"query"` to `_FULLY_REDACTED_ARG_KEYS`.
- **`provenance/capture.py`** — *optional* defense-in-depth: add the three tool
  names to `_NO_PROVENANCE_TOOLS` (`frozenset()` if ever dispatched).
- **`config.py`** — `retrieval_search_default_k` (5), `retrieval_search_max_k`
  (20), `retrieval_search_knowledge_k` (5).
- **`app.py`** — construct the three tools when `retrieval`/`vector_index` are
  wired (share the pipeline + store singletons); build the `runtime_tools` dict
  (`resolveValues` + the three) and pass it to `AgentLoop`; unwired → the tools
  return `RETRIEVAL_TOOL_UNAVAILABLE`.
- **Tests** — the Layer-1 list (§7), a Layer-2 `test_read_tools_live.py`
  (skip-guarded), and the Layer-3 progressive-disclosure scenarios; migrate the
  `resolve_values=` loop tests to `runtime_tools=`.
- **Docs** — **DECISIONS.md**: a new decision (candidate **D88**) for the read-tools
  seam (runtime-tool registry generalization; `getBlueprint` thin-projection until
  full-DAG; scope-deny-as-not-found non-oracle; searchKnowledge scope-bypass;
  `frozenset()` provenance; shared `RETRIEVAL_TOOL_*` family). **OPEN-QUESTIONS.md**:
  resolve OQ-R3 (searchKnowledge = chunks, for Phase 1); open OQ-T1 (getBlueprint
  full DAG, owned by the runBlueprint brick). **02-tools-and-api.md**: mark the 3
  knowledge-plane read tools **built**, document `getBlueprint`'s Slice-3 thin
  projection + the `k`/return shapes; note the count stays 12 agent-facing.
  **03-context-and-retrieval.md**: note the pull tools are now real (the "if the 3
  are off, reformulate → searchBlueprints" path is live). **04-blueprints.md**:
  note `getBlueprint` currently reads the retrieval projection; full DAG arrives
  with `runBlueprint`. **11-testing.md**: add the Layer-1 list + the deferred
  Layer-2/3 rows. **TRACEABILITY.md**: invariant rows — (a) searchBlueprints
  applies the transitive-USES scope pre-filter (D44/D60); (b) getBlueprint
  out-of-scope is indistinguishable from not-found (non-oracle); (c) searchKnowledge
  bypasses scope, gated at write by D58(a); (d) read-tool provenance = `frozenset()`
  (D44 safe-empty); (e) `query` redacted from spans (D25). **WORKLOG.md**: a
  Slice-3-read-tools entry.

---

## 11. Recommended build order within this brick (each slice fakes-first, Layer-1-green)

1. **`BlueprintDetail` + `VectorIndex.get_blueprint`** (protocol +
   `FakeVectorIndex` + `Neo4jVectorIndex._run` MATCH + record mapping). Pure seam,
   Layer-1 with the fake + a fake-`Record` mapping test.
2. **Pipeline single-corpus methods** (`search_blueprints`/`search_knowledge`),
   refactoring `retrieve` to call them. Layer-1 with the existing fakes — proves
   ordering + degrade unchanged.
3. **`retrieval/tools.py`** — the three tool classes over (1)+(2): arg validation,
   scope handling (§3), degrade/error (§6), `provenance=frozenset()`. Layer-1.
4. **`mcp/tool_schema.py`** — the three schemas + append; update count tests to 11.
5. **`redaction.py`** — `query` full-redaction. Layer-1.
6. **`loop/agent_loop.py`** — the runtime-tool registry (§2); migrate
   `resolveValues` into it; `askUser` unchanged. Layer-1 loop tests (one call each,
   registry routing, askUser still terminal) + the `resolve_values=`→`runtime_tools=`
   test migration.
7. **`denial_mapping.py`** — the `RETRIEVAL_TOOL_*` rows (as-built: +
   `RUNTIME_TOOL_INTERNAL_ERROR`; no `GET_BLUEPRINT_NOT_FOUND` — dead code). Layer-1.
8. **`config.py` + `app.py`** — settings + wiring (construct tools when retrieval
   is configured, build the registry, unwired → clean local error). Smoke-tested
   via `create_app(...)` fakes.
9. **Layer-2 `test_read_tools_live.py`** + **Layer-3** progressive-disclosure
   conformance scenarios (D68 burndown).
