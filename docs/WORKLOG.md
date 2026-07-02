# Build Worklog

A running log of what shipped, by which agent, so we can resume where we left off.
Newest session at the top. See [decisions/DECISIONS.md](decisions/DECISIONS.md) for the rationale
behind any `D##` cited here and [decisions/TRACEABILITY.md](decisions/TRACEABILITY.md) for the
invariant→test status board.

---

## ▶ RESUME HERE (next session)

**Where we are:** Phase 0 (walking skeleton, per **D68**).
- ✅ The `sqlglot` column-provenance extractor — built, reviewed, green.
- ✅ The **`clickhouse-api` MCP enforcement is built, tested, committed, and pushed** (branch
  `feat/scope-enforcement` on `kalpesh22-21/click-house-openapi`): D57 column-scope + D63 fail-closed
  + D64 scratch isolation; `column_scope` via a signed **JWT claim** (D79b), `session_id` via the
  **`X-Session-Id` header** (D81), empty scope = allow-all (D80), **MCP-only** enforcement (D80).
  Extractor delivered **copy-in** (D79a); interim `system.columns` catalog. 755+ tests green.
- ✅ `token_service` mints **column-scoped** tokens the MCP validates end-to-end (D79/D82); the UI
  mints its JWT server-side on login, defaulting to all-column access for now (D82).
- ✅ **The Phase-0 agent runtime is BUILT (strict Phase 0, Layer-1 green).** `src/data_agent/runtime/`:
  OpenAI raw loop (Responses primary / Chat fallback, D71), 6-MCP-tool + `askUser` dispatch with D5
  credential injection, minimal non-retrieval context assembly (D44/D46/D50), Couchbase session store
  (D22/D44/D45, CAS-guarded), progress streaming (D61) + Phoenix/OTel spans with PII redaction
  (D23/D24/D25), budget caps (D47/D55). **296 tests pass / 3 Layer-2 skipped, ruff clean.** Reviewed
  (2 review rounds): 5 blockers + a self-introduced current-turn PII-leak regression found and fixed;
  all closed and independently re-verified. Committed `6820677`.
- ✅ **D83/D84 — catalog overlay + column-scope enforcement in the `clickhouse-api` MCP** (Session 7),
  committed (`7a901ed` data-agent docs+loader; `b55b4de` clickhouse-api). `getTableSchema` merges
  introspection+catalog then scope-filters; `sampleRows` rejects out-of-scope. Reviewed — 3 metadata
  fail-opens found + fixed fail-closed.
- ✅ **Layer-2 validated against REAL infra** (Session 8, `206a29b`): 10 integration tests prove D57+D83
  scope enforcement vs real ClickHouse; Couchbase round-trip + D45 exactly-once CAS vs real Couchbase;
  2 integration bugs found+fixed (`list_tools` no-auth → 401; FastMCP-wrapped error-code parse).
- ✅ **Minimal UI** (Session 8, `206a29b`/`fbc224e`): FastAPI BFF (JWT server-side, D82) + vanilla SSE
  console; a browser Send-button bug found+fixed via Layer-3.
- ✅ **Layer-3 conformance** (Session 8, `fbc224e`): **5/6 Phase-0 scenarios green** via Playwright over
  the real UI (progress streaming D61, clarify/resume, scope denial D57, parser fail-closed D63,
  budget-cap D47), deterministic via scripted doubles.
- ✅ **LIVE end-to-end turn PROVEN**: real OpenAI (D71) → real MCP → real ClickHouse → **correct answer
  ("2" Sales employees)**, 4 tool calls, progress stream PII-clean. The D68 "real turn" criterion, met.
- ✅ **D77 `resolveValues` — the first Phase-1 brick — is BUILT (Session 9, Layer-1 green).**
  `src/data_agent/runtime/composite/` + `model/embedding_client.py`: a runtime composite over `runQuery`
  (single-dispatcher choke point reused for D57/D5 enforcement), catalog-allowlisted + sqlglot-AST SQL
  (`concept` provably never in SQL, D10), scope-aware description-column discovery, structured `period`,
  ranking `0.7·cosine+0.3·log-freq` top-10/LIMIT-200, embedding-failure **degrade-not-fail** (new **D85**).
  **512 pass / 18 skipped, ruff clean.** Adversarially QA'd (~128 tests) + reviewed (REQUEST CHANGES →
  fixed → APPROVE). See [decisions/resolvevalues-design.md](decisions/resolvevalues-design.md).
- ✅ **D71 embedding + reranker contracts are now REAL and Layer-2-validated LIVE** (Session 9b). The
  user-provided mocks at `~/Development/SQL/mocks` are the authoritative contracts: `POST /embed
  {"input_text":[…]}` → bare `[[float,…],…]` (768-dim); `POST /rerank {"query","documents"}` →
  `{"scores":[…]}`. `HttpEmbeddingClient` rewritten to match (hardening retained); new **unwired**
  `HttpRerankerClient` building block; both mocks added to `docker-compose.integration.yml`
  (`embedding-api` :18003, `reranker-api` :18004). **7/7 live Layer-2 tests green**, incl. the capstone:
  `resolveValues` with the real embedder ranks PTO (freq 5) over OT (freq 100) for "paid time off",
  `degraded=False`. **536 pass / 25 skipped, ruff clean.** Reviewed: APPROVE, 0 blockers.
- ✅ **Retrieval pipeline Slice 1 BUILT (Session 10, new D86)** — `src/data_agent/runtime/retrieval/`:
  embed→recall→scope-prefilter→rerank→cut behind a `VectorIndex` seam (in-memory now, neo4j Slice 2,
  D60), pre-injected as ONE structure-sanitized system message via `ContextAssembler`; degrade-not-fail
  at every stage, observably (`retrieval.degraded` span + zero-count event); one embed per budget
  window; **byte-identical context when unwired** (proven non-vacuously). Adversarially QA'd (+44 QA
  tests, 3 xfail bugs found → fixed → promoted) + reviewed (REQUEST CHANGES: silent degrade, render
  injection surface, silent candidate drop → fixed → **APPROVE**). **617 pass / 28 skipped, ruff
  clean; live Layer-2 3/3** (semantic ordering + rerank-flips-order vs real models). Design doc:
  [decisions/retrieval-pipeline-design.md](decisions/retrieval-pipeline-design.md) (+§8.1 trust
  boundary, OQ-R8/R9).

**Next brick:** **Retrieval Slice 2** (per retrieval-pipeline-design.md §build order): the **neo4j
corpus schema + `Neo4jVectorIndex`** (drop-in behind the `VectorIndex` seam; neo4j service into the
integration compose; offline corpus embedding with the same-model parity constraint; reconfirm the
§8.1 trust boundary + OQ-R8 budget accounting). Then: blueprint read tools
(`searchBlueprints`/`getBlueprint`/`searchKnowledge`) → `runBlueprint` + the **D56 verify gate** + the
**D67 `resolve_via` wiring** (the typed `resolveValues.resolve()` hook exists), with the offline
**Track B** learning loop parallel once the graph schema is fixed. The
runtime's Phase-1 `getTableSchema` is just a passthrough of the now-MCP-side overlay (D83/D84). ~~D77
`resolveValues`~~ **done (Session 9).** **Phase 0 is substantially complete and
validated end-to-end** (live turn works); the honest remaining Phase-0 gap is the **3 deferred Layer-3
scenarios** — mid-session scope narrowing (needs BFF per-turn scope switching), observability+PII span
inspection (needs a Phoenix collector in the stack — the progress channel is already PII-clean), and
pause/resume durability across a runtime restart (logic is Layer-1/Couchbase-tested). See
`tests/e2e/README.md`.

**Open follow-ups carried forward:**
- **NOTHING IS PUSHED (user deferred the push).** data-analysis-agent branch
  `phase0/provenance-extractor` has 7 commits (`6820677`→…→`192bf13` D77/D85→`b95c16d` D71 clients) —
  **this repo has NO git remote configured yet** (add an `origin` before push/PR). **The Session-10
  retrieval-Slice-1 work + these doc updates are UNCOMMITTED at time of writing** (committed right
  after this doc pass). clickhouse-api
  `feat/scope-enforcement` (origin `kalpesh22-21/click-house-openapi`) has `b55b4de` (D83/D84) +
  `143f0c1` "Stale changes" (unrelated branch WIP — settings/oauth/helm/diagnose_token, not ours)
  ahead of origin, unpushed.
- **How to re-up the Layer-2 stack next session:** `docker compose -f docker-compose.integration.yml
  up -d --wait`, then `bash scripts/couchbase-init.sh`. Run: MCP/ClickHouse integration →
  `MCP_TEST_URL=http://localhost:18090/mcp uv run pytest tests/integration`; Couchbase →
  `RUN_COUCHBASE_TESTS=1 COUCHBASE_CONNECTION_STRING=couchbase://localhost COUCHBASE_USERNAME=admin
  COUCHBASE_PASSWORD=password uv run pytest tests/runtime/session/test_couchbase_store.py`; Layer-3 →
  `RUN_E2E=1 uv run pytest tests/e2e` (needs a browser + `l2-token` up). Live UI/turn: run the real
  runtime `uv run uvicorn data_agent.runtime.app:create_app --factory --port 8000` (with the l2 env +
  the `.env` OpenAI key) + `scripts/run_ui.sh`.
- **3 deferred Layer-3 scenarios** (see Next brick + `tests/e2e/README.md`) — the remaining Phase-0 gap.
- `pytest-playwright` is a dev dep; the live OpenAI key is in a **gitignored `.env`** (`OPENAI_API_KEY`).
  The runtime's **defensive** D44 check stays as belt-and-suspenders now the MCP enforces scope too (OQ-4).
- Phase-0 runtime provisional tunables (`RuntimeSettings`): budget caps (15 iter / 60s / 3 windows),
  `SESSION_TTL`=7d, N=20 preview rows, history budget 20% — all set to defaults pending real traffic.
  **Session 9 adds:** `resolve_values_similarity_weight`=0.7 / `query_limit`=200 / `top_k`=10 (provisional).
- **`resolveValues` follow-ups (D77/D85):** ~~the custom embedding API contract is still an OQ~~
  **resolved Session 9b** — the `~/Development/SQL/mocks` contracts are authoritative; production
  endpoint/auth still TBD (unconfigured → freq-only degrade, D85, unchanged). Live embed env for the
  runtime: `EMBEDDING_API_URL=http://localhost:18003/embed`. Layer-2 test env:
  `EMBEDDING_TEST_URL=http://localhost:18003/embed RERANKER_TEST_URL=http://localhost:18004/rerank
  uv run pytest tests/integration/test_{embedding_api,reranker_api,resolve_values_live_embedding}.py`
  (services: `docker compose -f docker-compose.integration.yml up -d --wait embedding-api reranker-api`).
  **D67 `resolve_via` rule wiring** is still open (the typed `resolveValues.resolve()` hook exists for
  it). Deferred by design: per-(client,column) index, deictic/relative period-domain resolution
  (D41/D65), and a resolveValues Layer-2 run over the real MCP. Optional reviewer nit carried: the
  embedding client's bool/Infinity-element rejection branches lack the direct unit cases the reranker
  suite has (behaviour identical, symmetry only).
- Production IdP (Entra) must stamp the `column_scope` claim; wire per-user entitlements to replace the
  **D82 interim all-access** default (OPEN-QUESTIONS §Security/infra).
- **REVERSED by D83/D84** (was: "Replace the interim `system.columns` catalog in `clickhouse-api` with
  the D78 runtime catalog source"). The full semantic-catalog loader (`load_semantic_catalog`,
  mcp-overlay-design.md §3) now lives in / is consumed by `clickhouse-api` itself — copy-in'd from
  `data-analysis-agent`'s `databaseSchemaDocs/*.yaml` (D79a-style precedent) with a `CATALOG_SHA`
  sidecar — not sourced from the runtime. See Session 7.
- **Layer-2 / integration tests** against a live containerized ClickHouse (scope denial, scratch, fail-closed).
- **`X-Session-Id` HMAC-bind-to-`sub`** hardening before the scratch-upload feature ships (D81).
- The **D62 `system.query_log.columns` oracle** (Layer-2 job) — measure the sqlglot false-reject rate
  before enabling enforcement in prod; also the D70 secondary body-walk still uses `find_all` (belt-and-suspenders).
- Local dev venv is Python 3.14; CI pins 3.12 — consider a CI matrix (3.12 + 3.14) later.
- 6 coverage gaps accepted into backlog (see TRACEABILITY.md §Coverage gaps) — Phase-2 test additions.

---

## 2026-07-01 — Session 10: Retrieval pipeline Slice 1 BUILT (D86) — embed→recall→scope-filter→rerank→inject, no neo4j

Implemented Slice 1 of retrieval-pipeline-design.md: the full pipeline core behind a `VectorIndex`
seam (in-memory index now; neo4j is Slice 2), Layer-2-proven against the live embedding/reranker
mocks. New decision **D86** (VectorIndex seam + observable degrade-not-fail + render trust boundary).
**Uncommitted at time of writing** — committed immediately after this doc pass.

### Shipped
| Area | Path | Agent |
|---|---|---|
| `retrieval/` package: frozen models, `VectorIndex` protocol + in-memory `FakeVectorIndex`, blueprint transitive-USES scope pre-filter (exact `db.table.column` keys, `uses=None` fail-closed, empty scope allow-all), `RetrievalPipeline` (embed→recall k=30/corpus→scope-filter→rerank→cut top-3), `NullUserMemoryProvider`, deterministic renderer → ONE system message | `src/data_agent/runtime/retrieval/{models,vector_index,scope_filter,pipeline,user_memory,render}.py` | `backend-developer` |
| Integration: `ContextAssembler` optional retrieval dep (prepends the block; **byte-identical when `None`**), `AgentLoop` per-budget-window memo (one embed per window; scope change busts it; resume re-embeds the original question), `app.py` master-switch wiring (`retrieval_enabled` off → `None` → Phase-0 parity), `retrieval_*`/`neo4j_*` settings, `recall_span` + extended `rerank_span`, `retrieval_start`/`retrieval` shape-only progress events | `context/assembly.py`, `loop/agent_loop.py`, `app.py`, `config.py`, `observability/{tracing,progress}.py` | `backend-developer` |
| Layer-1 suite (36) + Layer-2 live suite (3: semantic ordering, rerank-flips-adversarial-order, knowledge rerank) | `tests/runtime/retrieval/`, `tests/integration/test_retrieval_pipeline_live.py` | `backend-developer` |
| Adversarial QA: +44 tests in 5 `test_*_qa.py` files (hostile render content, malformed index/reranker results, scope key/case semantics, memo/resume, config/master-switch) — found **3 real bugs** (xfail repros) | `tests/runtime/retrieval/test_{pipeline,render,scope_filter}_adversarial_qa.py`, `test_memo_resume_qa.py`, `test_config_qa.py` | `qa` |
| Review round 1 **REQUEST CHANGES** → fixes → re-review **APPROVE** (probes re-run empirically) | — | `reviewer` (parallel with `qa`) |

### Review findings (recorded honestly)
- **H1** — embedder degrade was completely silent (no span/event/log; the D85 silent-degrade class).
  Fixed: every degrade path emits a shape-only `retrieval.degraded` span (reason attr) + zero-count event.
- **H2** — corpus text flowed unescaped into the **system-role** message; newlines forged sections /
  injected at system privilege. Fixed: `_sanitize` (controls stripped, whitespace collapsed, 500-char
  field / 2000-char chunk caps) on every interpolated field + design-doc §8.1 trust-boundary note.
- **M1 / QA-bug-3** — rerank score-count mismatch silently dropped candidates while claiming
  `reranked=True`. Fixed: `zip(strict=True)` → degrade to recall order, `reranked=False`.
- **M2 / QA-bugs-1,2** — the "never raises" contract held only by client conformance; non-typed
  embedder/reranker exceptions crashed the turn; user-memory/observer unguarded. Fixed: defensive
  broad-except at every external stage, server-side logging, nothing model/user-facing.
- **M3** — the byte-parity test was vacuous (compared two empty lists). Fixed: seeds 40 ok trail
  entries at a compaction-triggering budget; reviewer verified the comparison is sensitive.
- **M4** (rendered block not counted against the D46 budget) + **L5** (no `scratch.` exemption in the
  candidate scope filter, by design) — explicitly deferred/recorded as OQ-R8/OQ-R9 in the design doc.
- **L1–L3** — progress event mis-sequencing (now `retrieval_start` + completion counts), zero-duration
  marker spans (now wrap the awaited work), `reranked` flag semantics documented.
- Reviewer nits carried: "found matching context" label copy on a 0/0 degrade; bidi Cf chars pass
  through render (single-line + capped, can't forge structure) — revisit with Slice-2 trust boundary.

### Verification status
- `uv run pytest` → **617 passed, 28 skipped, 0 xfailed** (3 QA bug repros promoted to passing), ruff clean.
- Live Layer-2 vs mocks → **3/3**; Phase-0 parity proven byte-identical on a compacted non-trivial history.
- Production remains **unwired** (`retrieval=None` until Slice 2 lands the corpus + neo4j index).

## 2026-07-01 — Session 9b: D71 clients aligned to REAL contracts (user mocks) + retrieval-pipeline design

Same-day continuation of Session 9. The user unblocked the D71 embedding question by providing
authoritative mock APIs at `~/Development/SQL/mocks` ("use the mock … even for reranking"), resolving
the resolvevalues-design OQ-1. **Uncommitted at time of writing** — committed immediately after this
doc pass. One backend-developer run was interrupted twice by transient provider 529s; the work was
already complete on disk and the orchestrator ran the verification gates directly.

### Shipped
| Area | Path | Agent |
|---|---|---|
| `HttpEmbeddingClient` rewritten to the real contract (`POST /embed {"input_text":[…]}` → bare `[[float,…],…]`); ALL hardening retained (parse+validate inside `try` → `EmbeddingError`, finite/numeric/no-bool/count checks, type-name-only error text) | `src/data_agent/runtime/model/embedding_client.py` | `backend-developer` |
| **New `HttpRerankerClient`** (protocol + `FakeRerankerClient` + HTTP; `POST /rerank {"query","documents"}` → `{"scores":[…]}`; mirror discipline; `RERANK` span = model+count only, D25) — deliberately **UNWIRED**, a building block for the retrieval brick | `src/data_agent/runtime/model/reranker_client.py`, `observability/tracing.py` | `backend-developer` |
| Settings: `embedding_api_url` = full endpoint; new `reranker_api_url/key/timeout_seconds` | `src/data_agent/runtime/config.py` | `backend-developer` |
| Mock services in the Layer-2 stack: `embedding-api` (host **18003**) + `reranker-api` (host **18004**), built from the SQL repo (absolute build context, commented as local-dev-intentional) | `docker-compose.integration.yml` | `backend-developer` |
| Env-guarded Layer-2 suites: live embed shape/768-dim/finite + semantic sanity; live rerank order + semantic sanity; **capstone**: `ResolveValuesComposite` + REAL embedder ranks PTO (freq 5) over OT (freq 100) for "paid time off", `degraded=False`, `ranking="semantic+freq"` — **7/7 green live** | `tests/integration/test_{embedding_api,reranker_api,resolve_values_live_embedding}.py` | `backend-developer` (gates re-run by orchestrator) |
| Unit suites rewritten to the bare-array contract — every old malformed-shape invariant kept + 3 new cases; reranker suite adds bool/inf rejection | `tests/runtime/model/test_embedding_client*.py`, `test_reranker_client.py` | `backend-developer` |
| Review: **APPROVE, 0 blockers / 0 suggestions / 2 optional nits** (bool-inf test symmetry; format drift) | — | `reviewer` |
| **Retrieval-pipeline design (Proposed, NOT built)**: D7/D8 pipeline over the new clients; neo4j-native vectors (D60); `VectorIndex` seam (core ships pre-neo4j); 3 degrade-not-fail paths; Phase-1 dependency-ordered build plan (Slice 1 = retrieval core, no neo4j) | `docs/decisions/retrieval-pipeline-design.md` | `planner` |
| OQ resolutions (embedding/reranker contract fixed; production endpoint/auth still open) | `docs/decisions/{OPEN-QUESTIONS,resolvevalues-design}.md` | `backend-developer` + orchestrator |

### Verification status
- `uv run pytest` → **536 passed, 25 skipped** (7 new guarded Layer-2 skips without env), ruff clean.
- Live vs healthy containers → **7/7 passed** (embedding + reranker + semantic-beats-frequency composite).
- `resolveValues` semantic ranking is now proven with real embeddings, not just fakes.

## 2026-07-01 — Session 9: D77 `resolveValues` — first Phase-1 brick (runtime composite over `runQuery`)

Built the first Phase-1 tool: `resolveValues`, a model-facing composite implemented in the agent runtime
over the existing `runQuery` MCP tool (D77), with a new embedding client (D71) and a new
embedding-failure-degrade decision (**D85**). Design-first, then implemented, adversarially QA'd, reviewed,
fixed, re-approved. **No pushes** (user still deferring; repo has no remote). **Uncommitted at time of
writing this entry** — committed immediately after the doc pass.

### Shipped
| Area | Path | Agent |
|---|---|---|
| Design doc (Q1–Q10, seam/injection/ranking/degrade/observability decisions, build order, OQs) | `docs/decisions/resolvevalues-design.md` | orchestrator → `planner` |
| `ResolveValuesComposite`: fail-closed catalog-allowlist validation (D70 exact-case), sqlglot-AST SQL (`concept` never in SQL, D10), inner `runQuery` via `ToolDispatcher`, scope-aware description-column discovery, structured `period`, typed `resolve()` for D67 | `src/data_agent/runtime/composite/{__init__,resolve_values,sql_builder,ranking}.py` | `backend-developer` |
| Embedding client: `EmbeddingClient` protocol + `FakeEmbeddingClient` (test-only) + `HttpEmbeddingClient` (settings-driven, manual `EMBEDDING` span; request contract assumed pending OQ-1) | `src/data_agent/runtime/model/embedding_client.py` | `backend-developer` |
| Loop interception (2nd intercepted tool after `askUser`; inline result; 1 `tool_calls_made`), `RESOLVE_VALUES_TOOL_SCHEMA` (period `"type": ["object","null"]`), `concept`+period redaction, `RuntimeSettings` additions, `app.py` wiring (unconfigured embedding API → no client → freq-only degrade) | `runtime/{loop/agent_loop,mcp/tool_schema,observability/redaction,config,app}.py` | `backend-developer` |
| ~128 adversarial Layer-1 tests (incl. 5 xfail bug repros, now regular passes); real-OTel-exporter redaction e2e | `tests/runtime/composite/*`, `test_resolve_values_{adversarial,loop_adversarial,redaction_e2e,schema_adversarial}.py`, `test_{ranking,sql_builder,embedding_client}_adversarial.py` | `qa` |
| Reviews: first **REQUEST CHANGES** (H1–H3, M1–M2, L1–L4) → fixed → **APPROVE**; post-approval residual **L5** (denial-table entries) | — | `reviewer` + `qa` (parallel) |

### Review findings (recorded honestly)
First review returned **REQUEST CHANGES**:
- **H1** — malformed inner-result shapes crashed the turn (no B4-parity guard). Fixed: `_safe_run_inner`
  broad-except → clean `RESOLVE_VALUES_INTERNAL_ERROR` + `_extract_rows` guards.
- **H2** — `HttpEmbeddingClient` element-level garbage escaped as raw exceptions, bypassing degrade. Fixed:
  parse+validate inside `try`; vectors validated non-empty / numeric / finite.
- **H3** — the `degraded` flag was dropped and a freq-only top score read `1.0`, masquerading as a perfect
  semantic match. Fixed: `result_full` wrapper `{degraded, ranking, top_margin, values}`; schema explains
  `freq_only` → `askUser`. (Motivated the new **D85**.)
- **M1** — description-column discovery ignored `column_scope` → permanent denial when the sibling desc
  column is out of scope. Fixed: candidates filtered via `scope_filter.is_provenance_in_scope`; value-only
  fallback.
- **M2** — mismatched-length vectors silently truncated in cosine. Fixed: `_validate_embed_shape` → degrade.
- **L1–L4** — full start/end redaction; unwired composite → local `RESOLVE_VALUES_UNAVAILABLE` (never
  dispatched); observer-event symmetry; JSON-Schema `period` typing.
- **L5** (post-approval residual) — `RESOLVE_VALUES_*` codes added to `_DENIAL_TABLE` so replayed trail
  entries render specific messages.

Re-review **APPROVED**: injection/scope/credential paths verified empirically safe (sqlglot ClickHouse
escaping incl. backslashes; `concept` provably never reaches SQL; D70-consistent allowlisting; M1 scope-key
correctness by construction via `is_provenance_in_scope`).

### Verification status
**512 passed / 18 skipped, ruff clean.** All 5 xfail bug repros are now regular passes. Proven **at Layer 1
with fakes** (`FakeEmbeddingClient`, `FakeMCPClient`) — **not** yet Layer-2 over the real MCP↔ClickHouse,
and **no** live custom-embedding-API call (endpoint/contract still an OQ). Decisions recorded: D77 annotated
BUILT, new **D85** (embedding-failure degrade). Traceability rows added (`🟡 unit-green`).

### Honest status
D77 done at Layer 1 + reviewed; **uncommitted** at time of writing (committed right after this doc pass).
Carried-forward: custom embedding-API contract OQ, D67 `resolve_via` wiring, per-(client,column) index +
period-domain resolution (deferred by design), and a Layer-2 run over real infra. Nothing pushed (no remote).

## 2026-07-01 — Session 8: Phase-0 VALIDATED against real infra — Layer-2 + UI + Layer-3 + live turn

Took the Layer-1-green Phase-0 build and proved it end-to-end against real infrastructure. **No pushes**
(user deferred). Committed on `phase0/provenance-extractor`: `206a29b` (Layer-2 + UI + 2 fixes),
`fbc224e` (Layer-3 + UI browser-fix).

### Shipped
| Area | Path / commit | Agent |
|---|---|---|
| Layer-2 harness: `docker-compose.integration.yml` (real ClickHouse + seeded `dbpcm_warehouse` HR + token IdP + D83 MCP + Couchbase) + `docker/clickhouse-init/hr-warehouse.sql` + `scripts/couchbase-init.sh` | `206a29b` | orchestrator + `backend-developer` |
| Layer-2 MCP integration suite (10 tests): D57 + D83 scope enforcement vs **real ClickHouse**, read-only, overlay, `catalog_sha` | `tests/integration/` `206a29b` | `backend-developer` |
| Couchbase session-store Layer-2: round-trip + D45 exactly-once CAS vs **real Couchbase** | `tests/runtime/session/test_couchbase_store.py` `206a29b` | orchestrator |
| **2 integration bugs found+fixed** (hidden by fakes): `RealMCPClient.list_tools()` sent no JWT → 401 (credentials threaded through list_tools→tool_schema→loop); FastMCP wraps errors as `Error executing tool X: [CODE]` → parser `.match()`→`.search()` | `src/data_agent/runtime/mcp/*`, `loop/agent_loop.py`, `app.py` `206a29b` | `backend-developer` |
| Minimal UI: FastAPI BFF (`ui/server.py`, mints JWT server-side D82, proxies SSE) + vanilla console (`ui/static/index.html`) + scripted-model launcher (`scripts/run_ui_runtime.py`) | `206a29b`/`fbc224e` | `frontend-developer` + `qa` |
| Layer-3 conformance (5/6): Playwright over the real UI — progress/clarify/scope-denial/parser-fail-closed/budget-cap | `tests/e2e/` `fbc224e` | `qa` |
| UI browser-bug fix (Send button dead in a real browser: error spans lacked `id`) | `ui/static/index.html` `fbc224e` | `qa` |
| Reviews: Layer-2+UI+fixes (APPROVE, 3 suggestions folded incl. `.search()` regex tightening) | — | `reviewer` |

### Live turn (the capstone)
Ran the **real** runtime (`create_app()` → real OpenAI D71 + `RealMCPClient` @ l2 MCP + real Couchbase +
JWKS via `l2-token`) and asked *"How many employees are in the Sales department?"* → the model
autonomously ran `listDatabases`→`listTables`→`getTableSchema`→`runQuery` against **real ClickHouse** →
answered **"2"** (correct; Alice + Carol in the seed). Progress stream PII-clean (shape only). Verified.

### Honest status
Phase 0 substantially complete + validated. Remaining: **3 deferred Layer-3 scenarios** (mid-session
narrowing, observability+PII/Phoenix, restart-durability — documented in `tests/e2e/README.md` with
server-side/unit coverage noted), and **nothing pushed** (data-agent has no remote configured yet).

## 2026-07-01 — Session 7: D83/D84 — getTableSchema overlay + scope-filter relocated to clickhouse-api

### Scope this session
- **D83 reverses D78:** the `getTableSchema` `introspection ⨝ catalog overlay` join moves back into the
  `clickhouse-api` MCP, and now also **scope-filters** the merged result (columns dropped by
  `column_scope`, plus block-level `grain`/`primary_key`/`join_keys`/`measures`/`temporal`/`rules`/
  `ambiguities` entries dropped whenever they reference an out-of-scope column). `sampleRows` gains
  **column-scope enforcement by reject** (`COLUMN_SCOPE_VIOLATION`), matching `runQuery`'s `SELECT *`
  treatment — previously unscoped.
- **D84 amends D53:** the Semantic Catalog now deploys to **both** the runtime and the MCP; `catalog_sha`
  is redefined as the catalog subtree's own git SHA (not either service's deploy SHA) so cross-service
  version skew is observable.

### Shipped artifacts (clickhouse-api, branch `feat/scope-enforcement`)
- `getTableSchema` overlay + scope-filter and `sampleRows` scope-reject, per `mcp-overlay-design.md`
  §1/§2.
- A canonical `load_semantic_catalog` loader (mcp-overlay-design.md §3) — the full parsed-YAML structure
  (grain, temporal, primary key, join_keys, measures, rules, ambiguities, per-column semantics), not just
  the `{col: type}` view `build_sqlglot_schema()` extracts. Delivered **copy-in** (D79a-style precedent)
  from `data-analysis-agent`'s `databaseSchemaDocs/*.yaml`, with a `CATALOG_SHA` sidecar file recording
  the source commit at copy time (OQ-2 of mcp-overlay-design.md).
- A YAML defect fix: 5 columns had flow-style `Decimal` values that didn't parse as intended, fixed in
  the source YAML.
- **Review journey:** a reviewer + QA pass found **3 metadata fail-open gaps** in the free-text
  predicate/resolution scope-scan (documented in mcp-overlay-design.md §1.4.1): (1) scan vocabulary was
  scoped to the catalog's documented `columns:` block instead of the full introspected column universe,
  hiding references to undocumented-but-real columns; (2) `rules[].predicate` resolution erred toward
  keeping unparseable/unresolvable references instead of dropping them; (3) `ambiguities[].resolves_to`
  cross-table name collisions (e.g. `EmployeeCode` matching 6 tables) were treated as unresolvable and
  kept instead of resolved fail-closed against all candidates. All three fixed fail-closed: scan
  vocabulary is now the introspected-column universe (cross-table too), `rules[].predicate` is parsed
  with `sqlglot` (fail-closed on parse failure or unresolved identifier), and `ambiguities[]` collisions
  resolve against every candidate table and drop if any is out of scope.
- Both test suites green: `data-analysis-agent` 315 tests, `clickhouse-api` 821 tests.

### Honest status (not yet done)
- The `clickhouse-api` work above is **built, reviewed, and unit-green — but NOT committed** (still on
  the local `feat/scope-enforcement` branch).
- **Layer-2 integration tests** against a live ClickHouse for the new `getTableSchema`/`sampleRows` scope
  enforcement have **not yet been run**.

### Downstream doc updates this session
| Area | Path | Agent |
|---|---|---|
| D83/D84 ADRs + D42/D53/D78 cross-references | `docs/decisions/DECISIONS.md` | `planner` (prior session) |
| `mcp-overlay-design.md` (design brief incl. §1.4.1 fail-closed fixes) | `docs/decisions/mcp-overlay-design.md` | `planner` (prior session) |
| `getTableSchema`/`sampleRows` rows + extension-scope paragraph updated for D83 | `docs/02-tools-and-api.md` | `planner` |
| Semantic Catalog section (deploy-coupled load, mismatch handling, extension list) updated for D83/D84 | `docs/09-infrastructure.md` | `planner` |
| Layer-2 MCP-vs-runtime component test reclassified to MCP-side | `docs/11-testing.md` | `planner` |
| Next-brick / follow-up bullets + this Session 7 entry | `docs/WORKLOG.md` | `planner` |
| D53 row test-layer annotation flipped to MCP | `docs/decisions/TRACEABILITY.md` | `planner` |
| Semantic Catalog framing sentence corrected | `docs/decisions/OPEN-QUESTIONS.md` | `planner` |
| "Out of scope" line annotated as permanently relocated (not deferred) | `docs/decisions/phase0-runtime-design.md` | `planner` |

---

## 2026-07-01 — Session 6: Phase-0 agent runtime BUILT (strict Phase 0, Layer-1 green + reviewed)

### Scope decision this session
- User chose **strict Phase 0** for the runtime: raw loop + context assembly + Couchbase + progress
  streaming only. **D77 `resolveValues` and D78 `getTableSchema` overlay stay Phase 1** (declined to
  pull forward). `getTableSchema` ships **introspection-only**. Provider = **OpenAI** (D71), not Claude.

### Shipped artifacts
| Area | Path | Agent |
|---|---|---|
| Phase-0 runtime design doc (module layout, interfaces, Couchbase doc model, span/redaction map, test seams, deps, build order, OQs) | `docs/decisions/phase0-runtime-design.md` | `planner` |
| Runtime **Pass A** (below-the-loop): config, credentials, session store (+CAS) & models, MCP client + fake + tool-schema translation, provenance capture, tool dispatcher + denial mapping, D44/D46/D50 context assembly | `src/data_agent/runtime/{config,auth,session,mcp,provenance,dispatch,context}` | `backend-developer` |
| Runtime **Pass B**: OpenAI Responses/Chat client, agent loop + budget guard, observability (tracing/redaction/progress), JWKS auth, LLM summarizer, `app.py` composition root (SSE `/turn` + `/turn/resume`) | `src/data_agent/runtime/{model,loop,observability,context/llm_summarizer.py,auth/jwt_verify.py,app.py}` | `backend-developer` |
| Adversarial Layer-1 test hardening (D5 injection, D44 narrowing, fail-closed, budget termination, CAS, redaction, denial, fallback) | `tests/runtime/**` | `qa` |
| Two review rounds + fixes (5 blockers + 1 self-introduced regression) | `src/data_agent/runtime/**`, docs | `reviewer` + `backend-developer` |
| D44 clarification (extends to conversational messages, turn-scoped & status-gated) | `docs/decisions/DECISIONS.md` (D44), `phase0-runtime-design.md` §5.1–§5.2 | `backend-developer` |

### Review findings fixed (all closed, independently re-verified)
- **[CRITICAL] D44 leaked via conversational history** — assistant prose replayed unfiltered; fixed by
  tagging assistant messages with their turn's tool-result provenance-union and filtering like the trail.
- **[HIGH]** Couchbase writes not CAS-guarded (lost updates); shared OpenAI fallback state across
  concurrent turns; transport exceptions leaked raw `str(exc)` to the user + uncounted; TOOL spans never
  emitted so D25 redaction was dead on the live path (+ askUser question leaked into spans).
- **[CRITICAL, self-introduced during a fix]** the turn-scoped error-visibility fix exempted *all*
  current-turn entries from the scope check → a successful out-of-scope `sampleRows` leaked PII rows to
  the model. Caught by the re-review, reproduced e2e, fixed by **status-gating** the exemption
  (`status != "ok"` only; successful entries always scope-checked). Orchestrator independently
  confirmed the repro test fails without the guard and passes with it.
- Plus hardening: event-loop-blocking summarizer, uncapped tool-calls-per-response, denial message fed
  back for self-correction, `X-Session-Id` validation, `app.py` generic-SSE-error (no raw `str(exc)`).

### Verification status
- `uv run pytest` → **296 passed, 3 skipped** (Layer-2 Couchbase/MCP guarded); `uv run ruff check` → clean.
- **Layer-1 + reviewed only.** Layer-2/3 conformance (live infra + UI) NOT yet run — Phase-0 *conformance*
  per D68 is not yet claimable; the code is complete, reviewed, and unit-proven.

## 2026-07-01 — Session 5: clickhouse-api enforcement BUILT; D79–D82 locked

### Decisions locked this session
- **D79** — extractor delivery = **copy-in**; scope transport = signed JWT claim (session_id later moved to a header by D81).
- **D80** — **MCP-only** enforcement (REST is a trusted unscoped surface); empty `column_scope` = **allow-all** (D63/D64 still fire).
- **D81** — `session_id` travels via the **`X-Session-Id` header**, not a JWT claim; `column_scope` stays a JWT claim.
- **D82** — the UI mints its JWT server-side on login; **interim** default is all-column access; future = managed IdP (Entra).

### Shipped artifacts
| Area | Path / branch | Agent |
|---|---|---|
| Column-scope + fail-closed + scratch enforcement, extractor copy-in, sqlglot dep, JWT-claim + `X-Session-Id` wiring, interim catalog | `clickhouse-api` branch `feat/scope-enforcement` (pushed) | `backend-developer` |
| `token_service` mints column-scoped tokens; mint→validate round-trip test | `clickhouse-api/app/token_service.py` | `backend-developer` |
| D79–D82 recorded; D79–D82 relocated under `## ClickHouse MCP — adoption decision`; full-doc consistency audit + fixes | `docs/decisions/*`, chapters 02/04/05/06/07/08/README | `planner` + `reviewer` |

### Notes
- `clickhouse-api` enforcement is **built, tested (755+ green), and pushed** — no PR opened yet (per user).
- Security review found **no fail-open** on the MCP path; interim gaps documented (REST unscoped, `X-Session-Id` unsigned, `system.columns` interim catalog).

## 2026-06-30 — Session 4: D78 — getTableSchema catalog overlay moves to agent runtime

### Decision locked this session
- **D78** — The `introspection ⨝ catalog YAML overlay` join for `getTableSchema` moves from the ClickHouse MCP to the agent runtime. The MCP returns introspection only; the runtime applies grain, per-measure `{agg, defined_over}`, `temporal`, rules, ambiguities, synonyms, enum values, and `sensitive`/`client_defined` flags. Amends D42 (overlay location only; catalog content/format unchanged). Clarifies D53 (catalog deploys with the runtime, not the MCP service). After D77 + D78, `clickhouse-api` extension scope is enforcement-only (D57/D63/D64/D5).

### Shipped artifacts

| Area | Path | Agent |
|---|---|---|
| D78 ADR + D42 amendment + D75 update + D4/D68 cleanup | `docs/decisions/DECISIONS.md` | `planner` |
| getTableSchema updated to introspection-only (MCP), overlay in runtime; "four groups" count fix | `docs/02-tools-and-api.md` | `planner` |
| Semantic Catalog section + clickhouse-api section updated for D78; deploy-coupled clarified | `docs/09-infrastructure.md` | `planner` |
| D42 catalog-overlay item struck from clickhouse-api extension list; D78 session entry | `docs/WORKLOG.md` | `planner` |
| Catalog-overlay / getTableSchema open items annotated runtime-side | `docs/decisions/OPEN-QUESTIONS.md` | `planner` |

### Traceability deltas (this session)
- No test-status changes. D78 is a location shift; the D53 invariant row (`D53-uncatalogued-table-structural-only`) now applies to the runtime overlay path, not the MCP — no slug change needed (the observable invariant is identical).

---

## 2026-06-30 — Session 3: D77 — resolveValues moves to agent-runtime composite

### Decision locked this session
- **D77** — `resolveValues` is no longer a ClickHouse MCP data-plane tool; it is a model-facing
  tool implemented in the agent runtime over `runQuery`. MCP data plane is exactly 6 read tools
  (reconciles D4). D66 concept/ranking behaviour and model interface unchanged. Amends D66;
  reconciles D4; drops `resolveValues` from the D75 `clickhouse-api` extension scope.

### Shipped artifacts

| Area | Path | Agent |
|---|---|---|
| D77 ADR + D66 amendment note + D75 update | `docs/decisions/DECISIONS.md` | `planner` |
| Tools & API spec — resolveValues relocated to runtime section, counts fixed | `docs/02-tools-and-api.md` | `planner` |
| Worklog — resolveValues removed from clickhouse-api extension list | `docs/WORKLOG.md` | `planner` |
| Open Questions — D66/D77 annotations | `docs/decisions/OPEN-QUESTIONS.md` | `planner` |

### Traceability deltas (this session)
- No test-status changes. No new invariant rows needed: the D77 runtime composite is covered by
  D57/D10 invariants already in the matrix (the backing `runQuery` is the enforcement site).

---

## 2026-06-30 — Session 2: D75 — adopt clickhouse-api as the ClickHouse MCP data plane

### Decision locked this session
- **D75** — Adopt `clickhouse-api` (FastAPI + MCP, JWT/OIDC) as the ClickHouse MCP data plane;
  do not build a new MCP from scratch. Extend it with D57/D62/D63/D64 enforcement, D5 scope
  injection, ~~D66 `resolveValues`, D42 catalog overlay~~ (removed by D77/D78 respectively — now
  runtime-side; see Sessions 3–4), and the Phase-0 provenance extractor.

### Shipped artifacts

| Area | Path | Agent |
|---|---|---|
| D75 ADR | `docs/decisions/DECISIONS.md` (D75) | `planner` |
| Spec updates (architecture, tools, infrastructure, traceability, open questions) | `docs/01-architecture.md`, `docs/02-tools-and-api.md`, `docs/09-infrastructure.md`, `docs/decisions/TRACEABILITY.md`, `docs/decisions/OPEN-QUESTIONS.md` | `planner` |

### Traceability deltas (this session)
- No test-status changes. The `🟡 unit-green` rows (D57, D63, D64) remain unit-green; their
  Component consumer is now identified as the `clickhouse-api` extension (D75), not yet built.

---

## 2026-06-30 — Session 1: Planning lock-in + Phase-0 first brick

### Decisions locked this session
- **D68** — Three-phase delivery (walking skeleton → blueprints+verify with parallel learning loop →
  drift-defense fast-follow); **full** Layer-3 conformance suite gates release; conformance harness
  stands up in Phase 0 as a **red burndown**.
- **D69** — Column-provenance extraction contract (lambda fail-closed, `SELECT *` expand-and-reject,
  fully-qualified `(database.table, column)` USES pairs, session-gated scratch, EXPLAIN precondition).
- **D70** — Identifier matching is case-sensitive + exact; any unresolvable reference fails closed
  (no case-insensitive fallback, no silent skip); structural (non-tautological) lambda check.
  *Surfaced by the security review below.*

### Shipped artifacts

| Area | Path | Agent |
|---|---|---|
| Sequencing ADR + traceability registry | `docs/decisions/DECISIONS.md` (D68), `docs/decisions/TRACEABILITY.md` | `planner` |
| Parser test plan (36→39 cases) | `test-plans/column-provenance-extraction.md` | `qa` |
| D69 contract + OQ resolutions | `docs/decisions/DECISIONS.md` (D69) | `planner` |
| Project skeleton (uv/pytest/ruff, src-layout) | `pyproject.toml`, `uv.lock`, `README.md`, `.gitignore`, `.github/workflows/ci.yml` | `backend-developer` |
| Catalog-schema loader | `src/data_agent/catalog/loader.py` | `backend-developer` |
| **Column-provenance extractor** (D52/D62 — Phase-0 first brick) | `src/data_agent/sqlparse/provenance.py` | `backend-developer` |
| Layer-1 test suite (39 cases) | `tests/sqlparse/test_column_provenance.py` | `backend-developer` |

### Agent activity log (chronological)
1. **`planner`** — wrote the D68 sequencing ADR + created TRACEABILITY.md (35 rows / 18 decisions);
   surfaced 6 coverage gaps (invariants with no release-gating scenario).
2. **`qa`** — turned 11-testing.md §Layer-1 into a 36-case adversarial test plan for the parser;
   raised 5 spec ambiguities (OQ-1..OQ-5) that blocked implementation.
3. **Human** — resolved OQ-1..OQ-5 (incl. correcting an initial `SELECT *` choice that would have
   leaked out-of-scope columns); accepted all 6 coverage gaps.
4. **`planner`** — recorded the resolutions as **D69**; closed the OQs in the test plan.
5. **`backend-developer`** — scaffolded the repo + built the catalog loader, the extractor, and the
   36-case suite test-first. Green: 36/36.
6. **`reviewer`** — adversarial security review. Verdict **REQUEST CHANGES**: 1 Critical
   (tautological lambda check), 3 High (silent under-extraction on case-mismatch; `SELECT t.*` guard
   only incidental; test fixtures used phantom columns). Confirmed a real leak path.
7. **Human** — issued the case-sensitivity contract ruling (→ D70); resumed the agent for fixes.
8. **`backend-developer`** — applied all 7 fixes; structural lambda check; validating-qualify so
   case-mismatch raises; removed case-insensitive table fallback; explicit `t.*` guard; fixtures
   reconciled to production YAML; sqlglot pinned `>=30.12.0,<31`. Green: **39/39**.
9. **Orchestrator (me)** — independently verified (39 green, ruff clean, leak path closed, scratch
   isolation intact); recorded D70; updated TRACEABILITY statuses honestly (✅ unit-complete vs.
   🟡 unit-green where consumers aren't built).

### Verification status
- `uv run pytest` → **39 passed**; `uv run ruff check` → clean.
- Behavioral spot-checks (run by orchestrator): case-mismatch raises; `SELECT t.*` on uncatalogued
  table raises; own-session scratch passes; cross-session scratch raises; positive joins extract
  correctly.

### Traceability deltas (this session)
- `✅ green`: `D52-derived-column-in-uses-set`, `D64-scratch-parse-fail-closed`, all five D69/OQ rows,
  `D70-case-mismatch-fails-closed`.
- `🟡 unit-green` (unit slice proven, MCP/E2E consumers not built): `D57-derived-agg-over-forbidden-rejected`,
  `D63-parse-fail-rejects-not-runs`, `D64-scratch-cross-session-rejected`, `D52-per-consumer-fail-behavior`,
  `D44-parse-fail-drops-entry`.
</content>
</invoke>
