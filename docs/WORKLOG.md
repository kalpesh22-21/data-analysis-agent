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

- ✅ **Retrieval Slice 2 BUILT (Session 11, new D87)** — the neo4j corpus is real: `Neo4jVectorIndex`
  (async driver, per-corpus 768-dim cosine native vector indexes, parity `WHERE`, never-raises,
  per-row malformed-record skip, fail-closed `uses` coercion), `corpus_loader` + seed script
  (MERGE-by-id idempotent, write-time `uses` validation, atomic in-txn model-parity refusal,
  edge rewrite on re-seed), `neo4j:5.26` in the l2 stack, app wiring gated on
  `retrieval_enabled`+store+embedder. **Retrieval is now production-activatable via env.**
  Reviewed (REQUEST CHANGES: 2 blockers — a `uses` fail-open + a silently-retrieval-dead default
  config — → fixed → **APPROVE**, fixes live-re-proven) + QA'd (+65 tests, 8 pinned flags).
  **719 pass / 33 skipped, ruff clean; live Layer-2 8/8** (five D87 proofs + three pipeline).
  Design: [decisions/neo4j-corpus-design.md](decisions/neo4j-corpus-design.md).

- ✅ **Read tools BUILT (Session 12, new D88)** — `searchBlueprints`/`getBlueprint`/`searchKnowledge`
  as runtime tools behind a generalized `RuntimeTool` registry (`resolveValues` migrated in; registry
  B4-guarded + provenance-type-validated). Non-oracle `getBlueprint` (out-of-scope == not-found,
  byte-identical); **footprint-split provenance** (FOUND getBlueprint carries its scope-checked
  `uses` → drops from D44 replay under narrowing — a review correction to the design);
  `searchKnowledge` scope-bypass (write-gated); `query` fully redacted; schemas 8→11.
  Reviewed (APPROVE WITH FIXES → fixed → **APPROVE**, probes re-run) + QA'd (+56 tests, 5 pinned
  flags). **814 pass / 38 skipped, ruff clean; live Layer-2 5/5.**
  Design: [decisions/read-tools-design.md](decisions/read-tools-design.md).

- ✅ **`runBlueprint` brick BUILT (Session 13, new D89) across 3 slices (A/B/C)** —
  `src/data_agent/runtime/blueprint/` executes stored full-DAG blueprints as a `RuntimeTool`,
  fail-closed end-to-end. **Slice A** (`3218a84`): full-DAG storage (OQ-T1) + injection-safe pure
  functions + the **load-bearing scope-honesty gate** (a template can only read within its declared
  `uses`, table-aware — closes `SELECT *`/alias-mask/JOIN-to-unlisted/`dictGet`) + the two D88-owed
  guards (name-collision + dup-tool-call-id). **Slice B** (`8bb1a1b`): single-node execution engine
  + **D56 verify gate wired live** ("no unverified return") + the `RunBlueprintTool` + the pausing-
  runtime-tool loop seam. **Slice C** (`de320d4`): multi-node scalar DAG + **D45 mid-DAG approval
  pause/resume** (CAS-exactly-once, restart-durable, provenance union fail-closed across the pause,
  affirmative-only consent) + **D67 `resolve_via`** wiring. Scalar intermediates bound as **typed
  AST literals** (D59a/D10, F1 — no `runQuery` param surface); **table-intermediate DAGs rejected
  pre-dispatch** (F2). Adversarially QA'd (+118 A / +46 B / +48 C) + reviewed each slice (2–3 rounds;
  the 4 Slice-C fail-open blockers all fixed + re-exploited → APPROVE). **1225 pass / 44 skipped /
  0 xfailed, ruff clean; live Layer-2 3/3** (single + multi-node scalar DAG end-to-end vs real neo4j
  + ClickHouse-via-MCP; non-oracle NOT_FOUND). Design:
  [decisions/runblueprint-design.md](decisions/runblueprint-design.md).

**Where we are (Session 14):** the **deferred-items** sweep is done — D90 (extractor fail-open) and
D91 (D67 `resolve_via` wrong-answer) are both **fixed + reviewed**; the CI matrix, the D62 oracle, the
resolveValues Layer-2 leg, and the D67 seed leg all landed; and the next three bricks are **designed**
(2 design docs) with the auth posture **decided**. See Session 14 below for the full journey.

**DEFERRED-ITEMS status (the sweep):**
- **Item 1 (D67 seed / resolve_via correctness):** ✅ **done** — D91 concept-subset selection (gap-cut
  + sub-floor drop), live-proven `IN ('EARN')`→7350.
- **Item 2 (resolveValues Layer-2 over real MCP):** ✅ **done** — `test_resolve_values_live.py` (rank +
  scope-denial legs green).
- **Item 3 (Layer-3 conformance completion):** 🏗 **designed → building** — `layer3-conformance-design.md`
  (2 slices: wire demo retrieval + 4 `runBlueprint` scenarios + 3 Phase-0 scenarios).
- **Item 4 (CI matrix):** ✅ **done** — `.github/workflows/ci.yml` tests Python 3.12 + 3.14.
- **Item 5 (D62 query_log oracle):** ✅ **built** — `src/data_agent/sqlparse/oracle.py` + a Layer-2
  live job; production false-reject rate still needs a prod `query_log`.
- **Items 6 & 7:** confirmed **Phase-2** (drift probes #2/#3; authoring-time static grain gate D37b).
- **Item 8 (F2 table-intermediate passing):** 🏗 **designed, SLICE 1 ONLY** — `table-intermediate-design.md`
  (the `clickhouse-api` scratch-write surface; runtime table-passing gated on a real consumer).
- **Item 9 (auth hardening):** **DECIDED → building** — restrictive `column_scope` end-to-end +
  per-user BFF mint + `X-Session-Id` HMAC-bind; only Entra OIDC deferred. Not yet designed/built.

**Next scheduled bricks, in order:** (1) **Layer-3 conformance** (Item 3, designed) → (2) **auth
readiness** (Item 9, decided) → (3) **table-intermediate Slice 1** (Item 8, designed). **Then Track B —
the offline learning loop** remains the **last big Phase-1 deliverable** (now unblocked: graph schema
fixed D87, blueprints executable D89). The runtime's Phase-1 `getTableSchema` is just a passthrough of
the now-MCP-side overlay (D83/D84).

**Honest remaining Phase-0 gaps** (carried): the **3 deferred Layer-3 scenarios** — mid-session scope
narrowing (needs BFF per-turn scope switching), observability+PII span inspection (needs a Phoenix
collector in the stack — the progress channel is already PII-clean), and pause/resume durability across
a runtime restart (logic is Layer-1/Couchbase-tested) — **plus the NEW Layer-3 `runBlueprint`
conformance scenarios** (No-silent verification / Ask→clarify / Scope denial / Pause-resume-durability),
authored + Layer-1/2-proven but **not yet demo-wired** (all folded into Item 3's design). See
`tests/e2e/README.md`.

**Open follow-ups carried forward:**
- **NOTHING IS PUSHED (user deferred the push).** data-analysis-agent branch
  `phase0/provenance-extractor` now has **15 commits** (`6820677`→…→`3218a84`/`8bb1a1b`/`de320d4`
  runBlueprint A/B/C→ Session-13 doc pass → `1b77747` **D90** extractor fail-open) — **this repo has
  NO git remote configured yet** (add an `origin` before push/PR). **The D91 code + this Session-14 doc
  pass are UNCOMMITTED / about-to-be-committed at time of writing** (D90 at `1b77747` is already
  committed with its own DECISIONS/TRACEABILITY entries). clickhouse-api `feat/scope-enforcement`
  (origin `kalpesh22-21/click-house-openapi`) has `b55b4de` (D83/D84) + **`f8d9467`** (D90 mirror,
  byte-identical extractor fix) + `143f0c1` "Stale changes" (unrelated branch WIP —
  settings/oauth/helm/diagnose_token, not ours) ahead of origin, unpushed.
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
  ~~**D67 `resolve_via` rule wiring** is still open (the typed `resolveValues.resolve()` hook exists for
  it).~~ **DONE (Session 13, D89)** — wired inside `runBlueprint` (single + multi-node); **still open:**
  no **live** `resolve_via` seed (Layer-1 only). Deferred by design: per-(client,column) index,
  deictic/relative period-domain resolution (D41/D65), and a resolveValues Layer-2 run over the real
  MCP. Optional reviewer nit carried: the
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
- **`runBlueprint` (D89) honest follow-ups carried forward:**
  - **Table-intermediate DAGs (F2)** — deferred; a table-intermediate is rejected pre-dispatch. Needs
    a **scratch-write surface** in `clickhouse-api` (Track A) before scalar-converge-only can be lifted.
  - **NEW Layer-3 `runBlueprint` conformance scenarios** (No-silent verification / Ask→clarify / Scope
    denial / Pause-resume-durability) — authored + Layer-1/2-proven, but **demo-wiring is deferred**
    (not yet Playwright-driven through the real UI). Add to the 3 already-deferred Phase-0 scenarios.
  - **Live `resolve_via` seed** — D67 is wired but Layer-1 only; no corpus blueprint exercises it live.
  - **`resolveValues` Layer-2 over the real MCP** — still not run (carried from Session 9).
  - The **authoring-time static grain gate (D37/D37b)** stays Phase-2 — the runtime D56 gate is the
    launch teeth; wrong-grain blueprints are caught at execution, not yet at authoring.

---

## 2026-07-03 — Session 17: Learning-loop Slice 2 (loader + triage + audit store) BUILT (D99/D100) + S3 design

Two increments this session. First, wrote the **S3 grounded-extractor design** (committed `fec94d1`):
`learning-loop-extractor-design.md` + **D97** (role classification is TOTAL over literal predicates —
slot|rule|inline, NO hard-drop; caller-specific→optional slot; unclassifiable→fail-to-review) + **D98**
(replay verifies STRUCTURE — grain-integrity + result_signature shape — NOT values; value-correctness is
layered, replay is not a value oracle). Then built **Slice 2**.

### Slice 2 — what shipped (new `src/data_agent/learning/{summary,triage,audit}/`)
- **SessionSummary loader** — a pure, READ-ONLY (D72) normalizer: `SessionDoc` (+ full results via the
  new read-only `read_full_result`, D46) → typed summary (turns, tool_calls preserving
  tool_call_id→turn_index + provenance verbatim, blueprint_usages, askUser exchanges, failed→fixed SQL,
  inferred `accepted_signal`).
- **`accepted_signal` inference (D99)** — deterministic first-match, **conservative bias** (ambiguous ⇒
  None; a false acceptance manufactures a candidate off a wrong answer); `thumbs_up` declared but never
  emitted in Phase 1.
- **Triage gate (D100)** — deterministic heuristics (KEEP on K1 accepted-query / K2 failed→fixed / K3
  answered-askUser / K4 corrected-blueprint; else SKIP-with-reason), no LLM. Cheaper + testable; LLM
  triage is a named later refinement.
- **`learning_audit` store provisioned (D95)** — dedicated Couchbase bucket + `learning_audit_writer`
  RBAC scoped to that bucket only (live-proven denied on `agent_sessions`), KV `AuditStore` client +
  `evidence_ref` minting + TTL. Per "build tooling, don't wire": **S2 writes NO evidence** (client
  dormant; first snapshot is S3's) — an executable zero-snapshot invariant.
- Consumer's no-op `_do_work` replaced with load→triage→SKIP(done)/KEEP(stub extractor seam); all
  Slice-1 invariants preserved (kill-switch, CAS state machine, dedup, dead-letter, D72).

### The review win (again: adversarial review + independent verification beat a green suite)
QA's first pass reported **"no bugs, all green"** — but the reviewer found **3 HIGH bugs**, and I
**confirmed all three by reading the code** before trusting either: (1) askUser pairing used `> turn_index`
while the real resume flow appends the answer at the **same** turn_index (agent_loop.py:451-452,
couchbase_store.py:235) → K3 dead, clarification answers invisible; (2) bare-substring confirmation
matching → *"I don't think that looks right"* scored `explicit_confirm` and *"yesterday"* matched *"yes"*;
(3) correction lexicon too narrow → *"that can't be right"* earned `no_correction`→K1 candidate. **Root
cause of the false-green: QA's own fixtures encoded the wrong askUser turn model**, so its tests asserted
the buggy behavior. Fixed: append-order same-turn pairing; word-boundary + clause-scoped negation guard;
broadened corrections (D99 asymmetry). Plus MEDIUM (transient full-result read retry so a blip doesn't
dead-letter a healthy session; conditional audit-client boot). QA then **corrected its fixtures to the
real runtime shape** + added the reviewer's exact adversarial reproductions.

### Process
explorer (recover intent) → planner (S3 design; then S2 design + D99/D100) → backend (S2) →
**reviewer REQUEST CHANGES** (3 HIGH inference bugs) → **coordinator verified all 3 in source** → backend
fix bundle → qa (corrected wrong-model fixtures + adversarial cases) → **reviewer delta APPROVE**. Gates:
**1591 passed / 87 skipped, ruff clean**; L2 live audit RBAC/TTL + consumer vs real Couchbase.

See [decisions/learning-loop-slice2-design.md](decisions/learning-loop-slice2-design.md) (BUILT;
Slice-3 forward note: backward-only negation guard, deferred to D100 tuning).

**Runway:** S1 ✅ S2 ✅ → **S3 grounded extractor (DESIGNED, next)** → S4 blueprint validate → S5 leakage
gate → S6 D48 dedup → S7 writers + review inbox → S8 user store + schema-edit PR bot → S9 promotion scheduler.

---

## 2026-07-02 — Session 16: Track B STARTED — learning-loop Slice 1 (infra spine) BUILT (D95/D96)

Began the offline learning loop (Track B), the last big Phase-1 deliverable. The full architecture was
already **Locked** (docs/05-memory-and-learning.md + D15–D31/D48/D51/D58/D72); this slice builds the
**transport + lifecycle spine** — NO extractor/writers/leakage-gate/dedup/promotion yet (later slices).
User chose the infra-complete path on all three scope forks: **infra spine first**, **real Redis Streams
now**, **dedicated audit store**. Committed both repos? No — data-agent only (no clickhouse-api change).

### What shipped (new `src/data_agent/learning/` package)
- **Sweeper** (session-close detection): scans idle Couchbase sessions, two-step CAS claim
  (active→pending, XADD, pending→queued) — **crash-recoverable** (a `pending` doc with no stream entry
  is re-detected + re-enqueued idempotently).
- **`learning_status` state machine** active→pending→queued→processing→done + `dead_letter`, every
  transition a **CAS with from-state assert** (single-writer-per-session, D48 spirit) writing ONLY the
  lifecycle flag (D72 read-only; `preserve_expiry=True` so it never re-arms the session TTL).
- **Real Redis Streams queue** (`learning:jobs` / group `learning-workers` / `learning:jobs:dead`):
  reference-not-transcript message (D30/D46), **idempotent by `content_hash`**, dead-letter after N=5 via
  XAUTOCLAIM/XPENDING.
- **No-op consumer** (real triage/extractor is Slice 2): idempotency check → CAS queued→processing→done
  → XACK, reclaim/dead-letter, fresh-hash-at-done.
- **`LEARNING_ENABLED` kill-switch (D58c)** — uncached, read fresh per cycle from **both `.env` and
  process env**, gates sweeper AND consumer before any I/O; reads/request-path structurally unaffected.
- Two traced entrypoints (`scripts/run_learning_{sweeper,consumer}.py`), `l2-redis` in
  docker-compose.integration.yml, session-store `scan_idle_sessions` + `transition_learning_status`
  (N1QL + CAS), additive `SessionDoc.learning_content_hash`.
- **D95** — resolves the D51 audit-store open question: **dedicated access-controlled Couchbase bucket
  `learning_audit`** (decision locked; provisioning DEFERRED to Slice 2, since the spine writes no
  evidence). **D96** — concretizes D30 (state machine, Redis topology, content_hash definition,
  dead-letter N=5). Also fixed a pre-existing latent circular import (`redaction.compute_scope_hash`
  made a lazy import — behavior-preserving, verified byte-identical).

### The review win (emphasis: live/adversarial review caught what unit tests couldn't)
Reviewer found a **data-loss BLOCKER** unit tests structurally couldn't reach: `enqueue` set the dedup
key **before** `XADD`, so a crash between them stranded the session in `queued` with zero stream messages
(the in-memory fake's enqueue is atomic, so Layer-1 couldn't reproduce it). Fixed by **inverting to
XADD-first, mark-second** (a set dedup key ⟹ XADD happened; absent ⟹ safe to re-XADD → benign duplicate
absorbed by consume-side idempotency). Plus 4 MEDIUM (daemon-dies-on-blip → catch-log-continue;
kill-switch ignored `.env`; two crash windows in the dead-letter path → "no irreversible XACK before the
session CAS" invariant + terminal ack-skip) and LOWs (ship the N1QL index; VALID_TRANSITIONS map). QA
added the exact crash-window regression (`test_crash_between_xadd_and_mark_is_benign_duplicate_not_strand`,
green on fake AND real Redis).

### Process
explorer (recover the Locked design) → **user scope decisions** → planner (design + D95/D96 + TRACEABILITY)
→ backend (spine) → **reviewer REQUEST CHANGES** (BLOCKER + 4 MEDIUM) → **qa** (98 L1 + 13 L2 live) →
backend (fix bundle) → qa (crash-window regression + .env + dead-letter crash-safety) → **reviewer delta
APPROVE**. Gates: **1517 passed / 79 skipped, ruff clean**; live 7 Redis + 6 Couchbase incl. a full
sweeper→Redis→consumer round-trip on real infra.

See [decisions/learning-loop-infra-design.md](decisions/learning-loop-infra-design.md) (Status: BUILT;
Slice-2 forward notes recorded).

**Forward runway (planner map, not yet built):** S2 loader/triage + provision `learning_audit` +
first evidence snapshots → S3 grounded extractor (D34/D35) → S4 blueprint generalize/validate → S5
leakage gate (D58) → S6 D48 dedup → S7 writers + review inbox → S8 user store + D53 schema-edit PR bot →
S9 promotion scheduler.

---

## 2026-07-02 — Session 15: D94 — None-provenance stranding fix (last tracked follow-up; PAUSE before the learning loop)

Closed the **one remaining tracked follow-up** (task #11) surfaced by the Layer-3 reviewer, then paused
by request ahead of Track B (the offline learning loop). No new capability — a **silent-hang / diagnosis**
fix on the raw-loop and `runBlueprint` paths. Committed `31a6064` (data-agent only; `provenance.py`
untouched so no cross-repo sync). **Full suite 1419 passed / 66 skipped, ruff clean; `scope_filter.py`
byte-identical.**

### The bug (fail-closed, but a silent hang with zero root-cause signal)

A tool result with **`status="ok"` but `provenance=None`** — produced when the runtime's independent
`sqlglot` re-parse fails against a **skewed `CatalogHandle`** while the MCP's own parse succeeded
(`capture.py:86/94`), or when a `runBlueprint` union is **poisoned to `None`** by one inner `runQuery`
(`_union_provenance`) — was persisted to the trail, then **dropped on every context rebuild** by
`filter_trail` (an `ok` entry is never current-turn-exempt, by PII-safety design; `is_provenance_in_scope`
drops `None`). The model never saw its own result → **re-emitted the identical call** → burned to the
budget cap, with only the generic `loop_paused_budget_cap` / `loop_hard_ceiling_stop` event as signal.

### The fix (three parts; `scope_filter.py` left byte-identical)

- **Sentinel injection** (`ContextAssembler.assemble`) — for a **current-turn** `ok`+`None` entry (covers
  both `runQuery` and `runBlueprint` via one predicate), inject a **non-data-bearing** sentinel tool
  message in the dropped entry's ordinal slot, keyed to the dangling `tool_call_id` so the OpenAI
  assistant/tool pairing stays valid and the loop breaks: *"result withheld: provenance could not be
  determined … Do not retry the identical call …"*. **Two-sided data contract (decided during review):**
  the sentinel's tool-**result** content carries zero result payload; the **paired assistant call replays
  the model's own `args`** (its own current-turn SQL — causally prior to the withheld result, so it cannot
  contain it; and current-turn *denied* entries already replay full args via `budget._render_entry`) so the
  model can **correlate** the marker to the exact call. Without the args it saw `runQuery({})` and could
  re-strand — the exact hang, in multi-call turns.
- **Telemetry event** `loop_result_withheld_provenance` (tool_name / turn_index / tool_call_id /
  blueprint_id / reason — **no data**), deduped per `tool_call_id` per budget window via a
  `withheld_call_ids` memo (sibling to `retrieval_memo`). Telemetry-only (no `progress.py` change).
- **Soft seed-time skew warning** — `load_corpus(catalog=…)` logs a WARNING per blueprint whose `uses`
  table is absent from the `CatalogHandle`; **load proceeds** (a blueprint may legitimately reference
  tables outside a given dev snapshot). Wired into `scripts/seed_neo4j_corpus.py` via a soft catalog load.
- **Record correction (D94):** production protection against this skew is **MCP-fails-closed +
  both-catalogs-in-agreement**, **not** load-time catalog validation — the seed-time check is a dev-time
  early-warning aid only.

### Process

planner (design + **D94** + TRACEABILITY) → backend (impl) → **reviewer** (no blockers; 1 HIGH: sentinel
stripped `args` → lost correlation; 2 MEDIUM: Part-3 dead code, implicit discriminator) → backend (FIX 1
args-replay, FIX 2 explicit `withheld_sentinel` flag, FIX 3 seed-script wiring, FIX 4/5 nits) → **qa** (25
tests incl. byte-level RESULT-payload PII checks, multi-call correlation, end-to-end loop-break,
corpus-skew) → **reviewer delta-review APPROVE**. The one judgment call — replaying the model's own SQL on
the sentinel's assistant side — was ruled PII-sound (it's the model's own output, causally prior to the
result, and consistent with the pre-existing denied-entry exemption) and recorded in D94.

See [decisions/none-provenance-stranding-design.md](decisions/none-provenance-stranding-design.md).

**⏸ PAUSED HERE by request** — Track B (offline learning loop), the last big Phase-1 deliverable, is the
next major undertaking and has **not** been started.

---

## 2026-07-02 — Session 14: Deferred-items resolution + 2 live-testing-surfaced security/correctness fixes

The "resolve the deferred items" sweep. No new brick — instead we **closed the backlog gaps** left by
Sessions 9–13 and **teed up the next three bricks** (2 design docs + 1 auth decision). The headline is
**two real bugs found only because we insisted on live validation** — a scope **fail-open** in the
provenance extractor (D90) and a wrong-"verified"-answer in `resolve_via` (D91) — both fixed, reviewed,
QA'd. **D90 is committed (`1b77747` / clickhouse-api `f8d9467`); D91 + this doc pass are
about-to-be-committed.**

### The two live-testing wins (emphasis: both were invisible to Layer-1)

- **D90 — extractor uncatalogued-column fail-*open* (committed `1b77747`).** While **building the D62
  oracle** (Item 5), replaying `system.query_log` surfaced an incidental finding: the `sqlglot`
  provenance extractor **silently dropped** an unqualified column absent from the catalog
  (`col.table == ''`, not a case variant) instead of failing closed — a D70-class silent-drop that
  **understated the USES set**, so a D44 replay could fail **open** under catalog drift. `explorer`
  investigation → `backend-developer` fix in **both repos byte-identical** (D79a copy-in discipline):
  an uncatalogued unqualified column now **fails closed** unless it is a provable SELECT-list
  output-alias reference (declared alias in GROUP BY/ORDER BY/HAVING) or a bare column of a scratch-only
  SELECT; uncatalogued lambda-body columns also fail closed. `reviewer` **APPROVE**; `qa` wrote **27
  adversarial tests** (alias masks, lambda bodies, scratch-only, case variants) — **no fail-open, no
  false-reject**. Committed `1b77747` (data-agent) / `f8d9467` (clickhouse-api). Its DECISIONS/
  TRACEABILITY entries landed **with** that commit — this doc pass does **not** re-add them.

- **D91 — `resolve_via` bound the full ranked domain → wrong "verified" answer.** The D67 Layer-2 live
  test (Item 1) showed an `earnings` filter binding `RegisterType IN ('EARN','DEDUCTION')` and returning
  a confidently **wrong 7200** (true **7350**) — a wrong *value set* that still passed the D56 grain gate
  (fan-out was correct), i.e. a correctness bug **masquerading as a verified total**. `expand_rule`
  previously bound the **entire** ranked `resolveValues` domain (D77 ranks but does not threshold).
  `backend-developer` fix = **concept-subset selection**: **gap-cut** (bind the top prefix up to the
  first score gap `> resolve_via_gap_threshold` 0.15; no gap ⇒ bind all — uniform relevance) **then
  sub-floor drop** (`< resolve_via_min_confidence` 0.3); a top score below the floor **`RuleFallback`s
  to the raw loop** (never an unanswerable pause, per Slice-C S2). **Replaces** the prior top-`margin`
  ambiguity fallback. Shape-only `blueprint_rule_resolved` telemetry (no raw codes, D25). Both tunables
  provisional (`RuntimeSettings`). `reviewer` **APPROVE**. **Live-confirmed:** `IN ('EARN')` → **7350**.

### Shipped (the rest of the sweep)

| Area | Path | Agent |
|---|---|---|
| **D90** extractor fail-open fix (both repos, byte-identical) — committed | `src/data_agent/sqlparse/provenance.py` (+ clickhouse-api mirror) | `explorer` → `backend-developer` → `reviewer` → `qa` |
| **D91** `resolve_via` concept-subset selection (gap-cut + sub-floor drop + confidence-floor→raw-loop) | `src/data_agent/runtime/blueprint/rules.py` (+ `RuntimeSettings` tunables) | `backend-developer` → `reviewer` |
| **D62 oracle** (Item 5) — `classify_query` (EXTRACTED_OK/FAIL_CLOSED_REJECT/PARSE_ERROR) + `run_oracle` (rate + redacted rejected samples) + a Layer-2 env-guarded live job replaying `system.query_log`; **also surfaced D90** | `src/data_agent/sqlparse/oracle.py`, `tests/integration/test_provenance_oracle_live.py` | `backend-developer` |
| **resolveValues Layer-2** (Item 2) — standalone model-facing `run()`→`ToolResult` over real MCP↔ClickHouse + real embedder (EARN 0.75 > DEDUCTION 0.35, `degraded=False`, provenance carried) **and** the scope-denial path (JWT excluding `RegisterType` → real `COLUMN_SCOPE_VIOLATION`) | `tests/integration/test_resolve_values_live.py` | `backend-developer` |
| **CI matrix** (Item 4) — Python 3.12 + 3.14 | `.github/workflows/ci.yml` | `backend-developer` |
| **Design doc — Layer-3 conformance completion** (Item 3; DESIGNED, not built): 2 slices — wire demo retrieval + 4 `runBlueprint` scenarios + 3 Phase-0 scenarios | `docs/decisions/layer3-conformance-design.md` | `planner` |
| **Design doc — F2 table-passing** (Item 8; DESIGNED, **Slice 1 only** per user): the `clickhouse-api` scratch-write surface; runtime table-passing gated on a real consumer | `docs/decisions/table-intermediate-design.md` | `planner` |

### Review journeys (recorded honestly)
- **D90** — `reviewer` APPROVE; `qa` 27 adversarial tests, **no fail-open / no false-reject**; committed
  `1b77747` (data-agent) / `f8d9467` (clickhouse-api).
- **D91** — the fix originated as a `reviewer` **blocker on the small batch** below: reviewing the
  resolveValues-L2 + oracle + CI batch, the reviewer flagged the D67 full-domain bind as a **seed
  correctness blocker** → escalated into its own fix (D91) → `reviewer` **APPROVE**.
- **resolveValues L2 + D62 oracle + CI matrix** — landed as one small `backend-developer` batch;
  `reviewer` **APPROVE** *with* the D67-seed blocker that became **D91**.

### Design decisions (DESIGNED / DECIDED, not built)
- **Layer-3 conformance** (Item 3) and **table-intermediate Slice-1-only** (Item 8) are **design docs
  in the tree**, not code — the next scheduled bricks (see ▶ RESUME HERE for the order).
- **Item 9 auth-hardening readiness is DECIDED** (build restrictive `column_scope` end-to-end +
  per-user BFF mint + `X-Session-Id` HMAC-bind; **defer only Entra OIDC**) but **NOT yet designed/built**.

### Verification status
- `uv run pytest` → **1279 passed / 48 skipped**, ruff clean (up from 1225 at Session 13 close).
- **Live legs green:** the resolveValues Layer-2 rank + scope-denial legs; the D67/D91 `IN ('EARN')`→7350
  margin-cut case; the D62 oracle live job (0 rejects on 12 test SELECTs — **not** a production rate).
- **D90** re-verified across both repos (byte-identical); **27** adversarial QA tests green.

### Honest deferrals / carried gaps
- **Production false-reject rate** — the D62 oracle exists but the l2 `query_log` is only our own test
  queries; a real number needs a prod/staging `query_log`.
- **Live `resolve_via` seed** — D67/D91 are Layer-1 + the single margin-cut live case only; no corpus
  blueprint exercises `resolve_via` live end-to-end yet.
- **Items 6 & 7 stay Phase-2** (drift probes #2/#3; authoring-time static grain gate D37b).
- **Nothing pushed** — 15 data-agent commits, still no git remote (see the carried-forward bullet above).

---

## 2026-07-02 — Session 13: `runBlueprint` brick BUILT (D89) — full-DAG execution across 3 slices (A/B/C)

The fast-path executor lands: `runBlueprint(id, slot_bindings)` runs stored full-DAG blueprints as a
`RuntimeTool`, fail-closed end-to-end — scope-honest at load, D56-verified on return, injection-safe at
every bind, durable across a mid-DAG approval pause. Honest boundary: **scalar-converging DAGs only**
(table intermediates rejected pre-dispatch, F2). Shipped as 3 committed slices; **this doc pass is
UNCOMMITTED** (the orchestrator commits it).

### Shipped
| Area | Path | Agent |
|---|---|---|
| Design doc (full-DAG storage, execution engine, slot binding, D56 gate, `resolve_via`, pause/resume, slicing, the two owed guards) | `docs/decisions/runblueprint-design.md` | `planner` |
| **Slice A** (`3218a84`) — `runtime/blueprint/` pure core (`template.py` F1 AST-literal binding, `slots.py` D49 resolvers, `when.py` whitelist evaluator, `verify.py` D56 grain-integrity, `models.py`); full-DAG storage (6 JSON props + write-validation) in `corpus_loader`/`vector_index`/`BlueprintDetail`; the **load-bearing scope-honesty gate** (`qualify_columns` + `_assert_source_tables_in_uses`); `SemanticCatalogHandle`; the two D88-owed guards (name-collision + dup-tool-call-id) | `runtime/blueprint/*`, `runtime/retrieval/corpus_loader.py`, `runtime/provenance/catalog_handle.py`, `runtime/mcp/tool_schema.py` | `backend-developer` |
| **Slice B** (`8bb1a1b`) — single-node `BlueprintExecutor` (scope-checked fetch → slot probe → typed-literal bind → `runQuery` dispatch → D56 verify), `RunBlueprintTool` (schema 11→12, `RUN_BLUEPRINT_*` family, one redacted TOOL span), the pausing-runtime-tool loop seam + additive `PauseCheckpoint.blueprint_*` fields; D56 "no unverified return" wired live | `runtime/blueprint/{executor,tool}.py`, `runtime/loop/agent_loop.py`, `runtime/dispatch/tool_dispatcher.py`, `runtime/session/models.py`, `runtime/app.py` | `backend-developer` |
| **Slice C** (`de320d4`) — multi-node topo DAG walk (scalar-passing, D33 no branching), scalar contract fail-closed, approval nodes + **D45 mid-DAG pause/resume** (CAS-exactly-once, restart-durable, provenance union fail-closed across the pause, affirmative-only consent), **D67 `resolve_via`** (single + multi-node), F2 table-intermediate rejection | `runtime/blueprint/{executor,rules}.py`, `runtime/loop/agent_loop.py`, `runtime/retrieval/corpus_loader.py` | `backend-developer` |
| Adversarial QA across all slices: +118 (A) / +46 (B) / +48 (C); each slice found 2–4 xfail bug repros → fixed → promoted | `tests/runtime/blueprint/*`, `tests/runtime/retrieval/test_corpus_loader_*.py`, `tests/runtime/mcp/test_tool_schema_qa*.py` | `qa` |
| Live Layer-2 suite (single + multi-node scalar DAG end-to-end vs real neo4j + ClickHouse-via-MCP; non-oracle NOT_FOUND) | `tests/integration/test_run_blueprint_live.py`, `tests/integration/test_blueprint_dag_live.py` | `backend-developer` |
| Review each slice (A: APPROVE WITH FIXES → 2 rounds; B: REQUEST CHANGES → fixed → APPROVE; C: REQUEST CHANGES → fixed → APPROVE — every exploit re-run) | — | `reviewer` (parallel with `qa`) |

### Review findings (recorded honestly)
- **Slice A — the load-bearing scope-honesty gate (hardened over 2 rounds).** First fix (footprint
  leniency + statement-kind guard) still left a **qualified-JOIN-to-unlisted-table** bypass; the second
  round added `_assert_source_tables_in_uses` (rejects any JOINed/subquery/table-function source absent
  from `uses`). Now closes `SELECT *`, alias-mask, table-blind bare-name, `dictGet`, cross-db, and
  qualified-JOIN bypasses — all reviewer/QA exploits verified closed; legit in-`uses` multi-table JOINs
  still accepted. This is what makes the D88(c) stored `uses` footprint **honest**.
- **Slice B — 3 blockers + a V1 false-pass.** B1: the tool was advertised but unwired. B2: the
  slot-domain probe leaked provenance fail-**open**. B3: an unreferenced slot was silently dropped →
  a filterless "verified" wrong answer. V1: a grain-mapping false-pass (casefold collision /
  slot-value output-name injection) — fixed to pre-bind exact/unambiguous → fail-closed. All fixed +
  independently re-exploited → APPROVE.
- **Slice C — 4 fail-open / wrong-answer blockers.** (1) the scalar contract was **unenforced** — a
  fanned-out intermediate (`!=1` row) flowed downstream; now fails closed before the consumer runs.
  (2) **provenance loss across resume** — the resumed union dropped completed nodes' provenance,
  breaking D44 fail-closed replay across the pause; now unioned. (3) the approval gate **approved on
  ambiguous denials** (fail-open consent); now affirmative-only. (4) a hybrid-record **B3 regression**.
  All fixed, re-review re-ran every exploit → APPROVE.

### Verification status
- `uv run pytest` → **1225 passed / 44 skipped / 0 xfailed**, ruff clean.
- **Live Layer-2 3/3** — single-node **and** multi-node scalar DAG verified end-to-end via real
  MCP→ClickHouse + neo4j (live union provenance); narrow-scope non-oracle `getBlueprint` NOT_FOUND.
- The D8 progressive-disclosure **fast path** is now fully live end-to-end (pre-injected cards →
  `getBlueprint` expand → `runBlueprint` execute → D56-verified answer).

### Honest deferrals (NOT built this brick)
- **Table-intermediate DAGs (F2)** — rejected pre-dispatch; needs a `clickhouse-api` scratch-write
  surface (Track A). Only scalar-converging DAGs execute.
- **Layer-3 Playwright demo-wiring** — the 4 runBlueprint conformance scenarios are Layer-1/2-proven,
  not yet demo-green.
- **Live `resolve_via` seed** — D67 wired but Layer-1 only.
- **Drift probes #2/#3** (catalog-conformance, rule-currency) — Phase-2 scheduled (D43).
- **Authoring-time static grain gate (D37b)** — Phase-2; the runtime D56 gate is the launch teeth.

## 2026-07-01 — Session 12: Read tools BUILT (D88) — searchBlueprints / getBlueprint / searchKnowledge

Same-day continuation. The three model-facing read tools ship over the real corpus, behind a new
generalized runtime-tool registry. **Uncommitted at time of writing** — committed right after this
doc pass.

### Shipped
| Area | Path | Agent |
|---|---|---|
| Design doc (tool surface, registry seam, non-oracle scope posture, provenance decision, degrade/error family, test plan) | `docs/decisions/read-tools-design.md` | `planner` |
| `retrieval/tools.py`: three tools over a `_ReadTool` base (TOOL span, `query` redaction, B4 self-guard); `BlueprintDetail`; `get_blueprint` keyed fetch on the store seam; pipeline public `search_blueprints`/`search_knowledge` sharing the pre-injection helpers (single-embed invariant kept) | `retrieval/{tools,models,vector_index,pipeline}.py` | `backend-developer` |
| `RuntimeTool` registry in the loop (replaces per-tool branches; `resolveValues` migrated, behaviour-exact; `askUser` still the terminal pause) + registry-level B4 guard (`RUNTIME_TOOL_INTERNAL_ERROR`) + provenance-type validation (coerce → `None` fail-closed) | `loop/agent_loop.py`, `dispatch/denial_mapping.py` | `backend-developer` |
| Schemas 8→11, `query` fully redacted, `RETRIEVAL_TOOL_*` codes, k-clamp settings, app wiring (tools registered only when the retrieval stack is active; unwired → clean local `RETRIEVAL_TOOL_UNAVAILABLE`) | `mcp/tool_schema.py`, `observability/redaction.py`, `config.py`, `app.py` | `backend-developer` |
| Layer-1 (~30) + redaction e2e + Layer-2 live suite (semantic match, scope drop, getBlueprint round-trip + non-oracle, knowledge scope-bypass, degrade) | `tests/runtime/retrieval/test_read_tools*.py`, `tests/integration/test_read_tools_live.py` | `backend-developer` |
| Adversarial QA: +56 tests (arg matrices, registry attacks — mixed-call ordering, cap boundary, duplicate ids, name shadowing —, non-oracle byte-identity, schema validity) — no shipped bugs, 5 pinned latent flags | `tests/runtime/retrieval/test_read_tools_qa3.py`, `tests/runtime/loop/test_read_tools_registry_qa3.py` | `qa` |
| Review APPROVE WITH FIXES → fixes → re-review **APPROVE** (all probes re-run empirically) | — | `reviewer` (parallel with `qa`) |

### Review findings (recorded honestly)
- **S1 (the load-bearing one)** — the design's all-`frozenset()` provenance had a hole: a FOUND
  `getBlueprint` entry would be immune to D44 replay-dropping after mid-session scope narrowing,
  re-surfacing a now-forbidden blueprint's existence + full `uses` footprint (contradicting the 06
  scope table + the `getTableSchema` precedent). Fixed: FOUND `getBlueprint` provenance = the
  scope-checked `uses` (last-dot split into the D44 tuple shape; malformed → `None` fail-closed);
  search tools stay safe-empty `frozenset()`. Design doc §3 rewritten (amendment recorded in D88(c)).
- **S2 / QA flags 1–2** — the registry trusted handlers to self-guard; a raising tool crashed the
  turn, a wrong-type provenance crashed the NEXT round-trip inside the D44 filter. Fixed:
  registry-level B4 guard + provenance sanitization (fail-closed `None`).
- Nits: dead `GET_BLUEPRINT_NOT_FOUND` code removed; model-supplied id bounded/quoted in logs;
  `default_k` clamped to `max_k`.
- Re-review corrections folded into docs: `_uses_to_provenance` is a last-dot split (a four-segment
  key round-trips byte-exactly rather than coercing to `None` — consistent with the call-time check).
- **Deferred to the `runBlueprint` brick** (QA-pinned): runtime/MCP tool-name collision guard;
  duplicate-tool-call-id replay semantics.

### Verification status
- `uv run pytest` → **814 passed, 38 skipped**, ruff clean. **Live Layer-2 5/5** (incl. the
  S1-touched getBlueprint round-trip: byte-exact `uses`, out-of-scope == absent).
- The D8 progressive-disclosure **pull** path is now fully live (pre-injected cards + reformulate →
  searchBlueprints → getBlueprint expand + searchKnowledge); fast-path execution awaits `runBlueprint`.

## 2026-07-01 — Session 11: Retrieval Slice 2 BUILT (D87) — neo4j corpus + Neo4jVectorIndex, live-proven

Same-day continuation. The neo4j corpus schema was designed (D87), built, and dropped in behind the
frozen D86 `VectorIndex` seam — retrieval is now activatable in production via env config.
**Uncommitted at time of writing** — committed immediately after this doc pass.

### Shipped
| Area | Path | Agent |
|---|---|---|
| Design doc: graph schema (denormalized byte-exact `uses` + reserved edges, per-corpus 768-dim cosine indexes, parity stamp, Track-B lifecycle columns reserved), driver/loader/Layer-2 decisions, §8.1 trust-boundary reconfirmation, OQ-R8 quantified re-deferral | `docs/decisions/neo4j-corpus-design.md` | `planner` |
| `Neo4jVectorIndex` (async official driver, `db.index.vector.queryNodes` per kind via fixed dicts — no interpolation, parity `WHERE`, never-raises→`[]`, per-row malformed-record skip, fail-closed `_coerce_uses`, model-mismatch span flag, `close()` via app lifespan) | `src/data_agent/runtime/retrieval/vector_index.py` | `backend-developer` |
| Corpus loader + seed CLI: idempotent DDL, MERGE-by-id, write-time `uses` validation (≥3-part dotted strings, fail-closed), atomic in-txn model-parity refusal (empty stamp = conflict), `:USES` edge delete+rewrite per re-seed, duplicate-id refusal; hand-authored YAML fixtures byte-matching the HR-warehouse schema | `src/data_agent/runtime/retrieval/corpus_loader.py`, `scripts/seed_neo4j_corpus.py`, `tests/fixtures/corpus/*.yaml` | `backend-developer` |
| Wiring: `retrieval_enabled` + store + embedder gate (no idle driver pool when disabled), `embedding_model` promoted to the load-bearing read-path parity key (default = corpus stamp), lifespan shutdown; `neo4j:5.26` service in the l2 compose; `neo4j==5.28.4` dep | `app.py`, `config.py`, `docker-compose.integration.yml`, `pyproject.toml` | `backend-developer` |
| Layer-1 (26+) + wiring tests; Layer-2 live suite: the five D87 proofs (recall→Candidate byte-exact mapping, scope filter on real stored USES, full e2e real embed+neo4j+rerank, parity guard, unreachable-neo4j degrade) | `tests/runtime/retrieval/test_{neo4j_index,corpus_loader,neo4j_wiring_qa}.py`, `tests/integration/test_neo4j_vector_index_live.py` | `backend-developer` |
| Adversarial QA: +65 tests (malformed-record matrix via the `_run` seam, exception flavors incl. the CancelledError carve-out, loader/fixture/gating attacks, fixture↔ClickHouse-DDL byte-exactness) — 8 pinned-behaviour flags | `tests/runtime/retrieval/test_*_qa2.py` | `qa` |
| Review round 1 **REQUEST CHANGES** → fixes → re-review **APPROVE** (all probes re-run live) | — | `reviewer` (parallel with `qa`) |

### Review findings (recorded honestly)
- **B1** — null/empty stored `uses` mapped to `frozenset()` → passed **every** scope (fail-open on the
  slice's highest-risk contract, locked in by a test). Fixed: `_coerce_uses` → undetermined/`None` →
  dropped fail-closed; non-list / non-str / bare-string shapes likewise.
- **B2** — `expected_model` read from a config field defaulting `""` (still described as cosmetic) →
  a by-the-book deploy silently retrieval-dead via the parity `WHERE`. Fixed: default = corpus stamp,
  description rewritten as load-bearing, loud wiring warning on empty.
- **S1** — `:USES` edges drifted from the `uses` property on re-seed (live-proven phantom edges).
  Fixed: delete+rewrite in the same txn. **S2** — loader stored garbage `uses` silently / crashed raw
  on non-str. Fixed: write-time fail-closed validation. **S3** — parity check was TOCTOU (read outside
  the write txn). Fixed: in-txn atomic. **S4** — `retrieval_enabled=False` still opened a driver pool.
  Fixed: gate includes the master switch.
- QA flags folded in: per-row malformed-record skip (one bad row no longer blanks the batch),
  duplicate-id refusal, empty-string parity stamp = conflict. Nits: destructive-wipe comment,
  lifespan migration. Residual nit-grade items carried: empty-`uses` seeds are stored-but-unretrievable
  (safe direction), the in-txn parity race is narrowed not serialized (comment softening pending).

### Verification status
- `uv run pytest` → **719 passed, 33 skipped**, ruff clean.
- **Live Layer-2 8/8** (5 D87 proofs + 3 pipeline) against real neo4j 5.26 + embedding + reranker;
  the two blocker fixes re-proven live (null-`uses` dropped end-to-end; default-config recall works).
- Production activation documented (NEO4J_URL + EMBEDDING_API_URL + EMBEDDING_MODEL + seed script).

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
