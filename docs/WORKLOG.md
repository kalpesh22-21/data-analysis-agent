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
  all closed and independently re-verified. `getTableSchema` is introspection-only (no overlay).

**Next brick:** **Phase 1** (per D68) — **D77 `resolveValues`** (runtime composite over `runQuery`).
**D78's `getTableSchema` catalog overlay is no longer a runtime item** — D83 (2026-07-01) relocates the
overlay + scope-filter permanently to the `clickhouse-api` MCP (see Session 7 below); the runtime's own
Phase-1 `getTableSchema` work shrinks to a passthrough of an already-overlaid, already-scope-filtered
MCP response, and `catalog_sha` stamping is likewise MCP-side (D84) for the MCP's own copy. Runtime
Phase-1 work otherwise continues with the retrieval pipeline, blueprints, the D56 verify gate, and the
learning-loop Track B. Before "Phase-0 done" can be *claimed*, the D68 exit criteria still need
**Layer-2** (real Couchbase/MCP + Phoenix on a live turn) and the **Layer-3** Docker+Playwright
conformance scenarios — the runtime code is Layer-1-proven and reviewed, not yet conformance-proven
against live infra.

**Open follow-ups carried forward:**
- **clickhouse-api MCP now column-scopes `getTableSchema`/`sampleRows`** (D83) — built on branch
  `feat/scope-enforcement`, unit-green, **not yet committed** (see Session 7). Layer-2 integration tests
  against a live containerized ClickHouse for the new scope enforcement have **not yet been run**. The
  Phase-0 runtime's **defensive** D44 check (drops out-of-scope tool results before the model sees them)
  stays in place as redundant-but-harmless belt-and-suspenders (OQ-4, mcp-overlay-design.md) now that the
  MCP boundary itself enforces this too, rather than covering a genuine gap.
- Phase-0 runtime **Layer-2** (containerized Couchbase/MCP, Phoenix) + **Layer-3** conformance are not
  yet built (need live infra + the UI); the runtime DI seams (fakes) are in place for them.
- Phase-0 runtime provisional tunables (`RuntimeSettings`): budget caps (15 iter / 60s / 3 windows),
  `SESSION_TTL`=7d, N=20 preview rows, history budget 20% — all set to defaults pending real traffic.
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
