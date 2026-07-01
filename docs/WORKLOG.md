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

**Next brick:** **agent-runtime work** (the pieces D77/D78 moved out of the MCP, plus the Phase-0 loop):
- Runtime `resolveValues` composite over `runQuery` (D77).
- Runtime `getTableSchema` catalog overlay (`introspection ⨝ catalog YAML`, D78) + `catalog_sha` stamping.
- The Phase-0 raw agent loop + context assembly + Couchbase session store + progress streaming.

**Open follow-ups carried forward:**
- Production IdP (Entra) must stamp the `column_scope` claim; wire per-user entitlements to replace the
  **D82 interim all-access** default (OPEN-QUESTIONS §Security/infra).
- Replace the interim `system.columns` catalog in `clickhouse-api` with the **D78 runtime catalog** source.
- **Layer-2 / integration tests** against a live containerized ClickHouse (scope denial, scratch, fail-closed).
- **`X-Session-Id` HMAC-bind-to-`sub`** hardening before the scratch-upload feature ships (D81).
- The **D62 `system.query_log.columns` oracle** (Layer-2 job) — measure the sqlglot false-reject rate
  before enabling enforcement in prod; also the D70 secondary body-walk still uses `find_all` (belt-and-suspenders).
- Local dev venv is Python 3.14; CI pins 3.12 — consider a CI matrix (3.12 + 3.14) later.
- 6 coverage gaps accepted into backlog (see TRACEABILITY.md §Coverage gaps) — Phase-2 test additions.

---

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
