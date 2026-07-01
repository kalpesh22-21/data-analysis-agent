# Build Worklog

A running log of what shipped, by which agent, so we can resume where we left off.
Newest session at the top. See [decisions/DECISIONS.md](decisions/DECISIONS.md) for the rationale
behind any `D##` cited here and [decisions/TRACEABILITY.md](decisions/TRACEABILITY.md) for the
invariant→test status board.

---

## ▶ RESUME HERE (next session)

**Where we are:** Phase 0 (walking skeleton, per **D68**). The first brick — the `sqlglot`
column-provenance extractor — is **built, reviewed, fixed, and green**. Nothing else in the runtime
exists yet.

**Next brick:** **extend the existing `clickhouse-api` service** (D75) — NOT build a new MCP from
scratch. The extension adds the missing enforcement pieces to `clickhouse-api`:
- D57/D62/D63 — wire `sqlglot` column-scope enforcement + fail-closed policy.
- D64 — scratch `s_<session_id>_*` isolation (same parse, same fail-closed posture).
- D5 — add `scope` + `session_id` parameters to `runQuery` (injected, not model-visible).
- ~~D66 — implement the `resolveValues` tool (the 7th data-plane tool).~~ **Removed (D77,
  2026-06-30):** `resolveValues` is now a runtime composite over `runQuery`, not an MCP addition.
  Its implementation belongs in the agent runtime, not `clickhouse-api`.
- ~~D42 — merge the semantic-catalog overlay into `getTableSchema`.~~ **Removed (D78,
  2026-06-30):** the `introspection ⨝ catalog overlay` join moves to the agent runtime. The MCP
  returns introspection only; `clickhouse-api` extension scope is now enforcement-only
  (D57/D63/D64/D5).
- Deliver the Phase-0 provenance extractor **into** `clickhouse-api` (packaging TBD — see
  OPEN-QUESTIONS.md §Security/infra D75).

Building this extension turns the `🟡 unit-green` rows (D57 live scope, D63 fail-closed, D64 scratch)
toward `✅` by adding their Component layer. It needs a **containerized ClickHouse** for Layer-2
tests (read-only enforcement, scope-by-parse; catalog overlay test now lives in the runtime layer, D78).
The Component layer now lives in `clickhouse-api`, not a new service.

**Open follow-ups carried forward:**
- The lambda fail-closed has a structural check (D70); the secondary body-walk cross-check still uses
  `find_all` — fine as belt-and-suspenders, but the **D62 `system.query_log.columns` oracle** (a
  Layer-2 job) is the real long-term validation of parser coverage. Not built yet.
- Local dev venv is Python 3.14; CI pins 3.12 (the `requires-python` floor). Consider a CI matrix
  (3.12 + 3.14) later.
- 6 coverage gaps accepted into backlog (see TRACEABILITY.md §Coverage gaps) — Phase-2 test additions.

---

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
