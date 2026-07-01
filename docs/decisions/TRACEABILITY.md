# Decision → Invariant → Test Traceability Matrix

## Rule

**A decision is not done until its tagged test is green in CI; CI fails if a hard-invariant decision
has no green test.** This file is the enforcement registry. Every row names the invariant a decision
asserts, the test layer that proves it, a stable slug to tag the test with, and the Layer 3
conformance scenario (if user-observable) or "—" if the invariant is structural/internal.

When a decision's tagged test does not exist, its Status is "⛔ not-built". A CI job enforces that
no row reaches production with Status "⛔ not-built" for the decisions below — if it does, the build
fails. New hard-invariant decisions added to [DECISIONS.md](DECISIONS.md) must add a row here before
the PR merges.

**Status legend:** `✅ green` — tagged test exists and passes in CI. `🟡 unit-green` — the Layer-1
unit slice is built and passing, but the named Component/E2E layers (their consumers) are not yet
built. `⛔ not-built` — no tagged test yet. A multi-layer row is only fully `✅` when **all** its
named layers pass; the release gate (D68) is the Layer-3 conformance suite, not the unit slice alone.

**Build progress:** the `sqlglot` column-provenance extractor (`src/data_agent/sqlparse/provenance.py`,
Phase-0 first brick, D52/D62) is built and passing 39 Layer-1 tests; it satisfies the unit slices
below. Its consumers — the MCP live-scope gate (D57), the replayed-trail filter (D44), and scratch
enforcement at the MCP (D64) — are **not yet built**, so those rows stay `🟡 unit-green`. Per D75,
the Component consumer for **D57/D63/D64** is the **`clickhouse-api` extension** (the existing MCP
service, adopted and extended); the provenance extractor will be delivered into `clickhouse-api`
(packaging TBD). **D44's** Component consumer is different — it is the **agent runtime's** replayed-trail
filter, also not yet built (not part of the `clickhouse-api` extension). No status changes until these ship.

See [docs/11-testing.md](../11-testing.md) for the four-layer test pyramid definition and the full
Layer 3 scenario list. See [docs/decisions/DECISIONS.md](DECISIONS.md) for the rationale behind each
decision cited here.

---

## Matrix

| Decision | Invariant | Test layer | Test slug | Conformance scenario | Status |
|---|---|---|---|---|---|
| **D5** | `session_id`/JWT/scope are injected by code — the model never sees or supplies them; it cannot forge or escalate scope | Unit | `D5-scope-never-model-visible` | — | ⛔ not-built |
| **D44** | Every stored tool result carries a column-provenance set; on scope narrowing, trail entries whose provenance ⊄ current scope are dropped before context assembly — never replayed | Unit + Component | `D44-provenance-drop-on-scope-narrow` | **Mid-session scope narrowing** | ⛔ not-built |
| **D44** | SQL parse failure → provenance unknown → entry dropped from replay (fail-closed, never assumed in-scope) | Unit | `D44-parse-fail-drops-entry` | — | 🟡 unit-green |
| **D45** | A pause checkpoint is persisted before yielding; any runtime instance can resume at `awaiting_node` after a restart; completed DAG nodes never re-run; `consumed` CAS flag prevents double-entry | Component + E2E | `D45-pause-resume-survives-restart` | **Pause/resume durability** | ⛔ not-built |
| **D46** | Results are preview-only in model context (≤N rows + `truncated` flag + `row_count`); full results persist to Couchbase and appear in UI; preview never causes the model to mistake a truncated set for a complete answer | Unit | `D46-preview-truncated-flag-present` | — | ⛔ not-built |
| **D47** | The raw agent loop never runs past its per-turn budget (iterations/tokens/wall-clock) without pausing via `askUser`; "continue" grants exactly one fresh budget window, not unlimited continuation | E2E | `D47-budget-cap-triggers-pause` | **Budget-cap pause** | ⛔ not-built |
| **D48** | Two blueprints with identical SQL semantics (same `resolves`, `uses_rules`, normalized AST) produce the same `canonical_key`; concurrent writes result in one create + one `hit_count` increment, never a duplicate | Unit + Component | `D48-same-semantics-same-key` | — | ⛔ not-built |
| **D48** | Two blueprints with materially different semantics (gross vs net, period filter present vs absent) produce different `canonical_key` values | Unit | `D48-different-semantics-different-key` | — | ⛔ not-built |
| **D48** | SQL parse failure during dedup → skip the hard key, fall through to soft embedding layer (fail-soft — never a wrong merge) | Unit | `D48-parse-fail-falls-to-soft` | — | ⛔ not-built |
| **D50** | Scope-filter (D44) runs before history compaction (D46); the prose summary is derived only from in-scope entries and is never persisted as an artifact | Unit | `D50-filter-before-compact-order` | — | ⛔ not-built |
| **D52** | `sqlglot` extracts the qualified `(table, column)` USES set from a query, including columns referenced only in derived expressions (e.g. `AVG(gross_pay)` yields `payroll_fact.gross_pay`) | Unit | `D52-derived-column-in-uses-set` | — | ✅ green |
| **D52** | `sqlglot` parse failure behavior is per-consumer: D57-consumer → fail-closed reject; D48-consumer → fail-soft skip-hard-key; D35-consumer → fail-to-review; none silently proceeds | Unit | `D52-per-consumer-fail-behavior` | — | 🟡 unit-green |
| **D53** | A table with no catalog entry is served structural-only (not an error); it is ineligible for blueprint promotion. Graceful degradation is **runtime-side per D78** — the runtime overlays nothing; the MCP returns normal introspection (test at the runtime component layer, not the MCP) | Component | `D53-uncatalogued-table-structural-only` | — | ⛔ not-built |
| **D53** | A `schema_edit` candidate opens a branch + YAML patch PR with CI (schema lint + `explainQuery` dry-run); it is never auto-committed to the catalog | Component | `D53-schema-edit-opens-pr-not-auto-commit` | — | ⛔ not-built |
| **D56** | Every `runBlueprint` response passes the deterministic grain-integrity check before being returned; a wrong-grain result (fan-out double-count) is caught and falls back to the raw loop — never returned to the user | Unit + E2E | `D56-wrong-grain-falls-back` | **No-silent verification** | ⛔ not-built |
| **D56** | The LLM verification gate runs on every blueprint response; the user is never asked to verify; SQL is visible but verification is the agent's job | E2E | `D56-verify-gate-always-runs` | **No-silent verification** | ⛔ not-built |
| **D57** | On every `runQuery`, the MCP parses the SQL, extracts referenced columns, and rejects the query if referenced columns ⊄ injected scope; derived aggregates over forbidden columns are caught (`AVG(gross_pay)` with no payroll scope → rejected) | Unit + Component | `D57-derived-agg-over-forbidden-rejected` | **Scope denial** | 🟡 unit-green |
| **D57** | An out-of-scope blueprint is not surfaced in thin cards (retrieval pre-filter); the existence of a restricted report is not leaked to the user | Component + E2E | `D57-out-of-scope-blueprint-not-surfaced` | **Scope denial** | ⛔ not-built |
| **D58** | A `global_knowledge` candidate is not returned by `searchKnowledge` until a human approves it in the review inbox; approve → immediately retrievable | Component + E2E | `D58a-knowledge-not-retrievable-before-approval` | **Knowledge human-gate** | ⛔ not-built |
| **D58** | `LEARNING_ENABLED=false` halts the write router and promotion scheduler without a deploy; reads (`searchKnowledge`, `searchBlueprints`) continue unaffected | Component + E2E | `D58c-learning-kill-switch-halts-writes` | **Correction → learning** | ⛔ not-built |
| **D59** | Composite blueprint inter-node scalar outputs are bound as typed ClickHouse server-side parameters — never string-interpolated into SQL text | Unit + Component | `D59a-scalar-intermediate-as-typed-param` | — | ⛔ not-built |
| **D59** | Composite blueprint inter-node table outputs are materialized to session scratch and `JOIN`ed — never inlined as `CTE`/`VALUES` in SQL | Unit + Component | `D59a-table-intermediate-to-scratch` | — | ⛔ not-built |
| **D59** | `requires_approval` fires before the node runs and may reference only upstream/completed outputs; a reference to the gated node's own un-computed output is rejected | Unit | `D59b-approval-upstream-only-reference` | — | ⛔ not-built |
| **D61** | Step-level progress events stream to the UI throughout a turn (context assembly, blueprint pick, each DAG node, D56 verify gate); the first progress event arrives before the final answer | E2E | `D61-progress-events-stream` | **Ask → fast path** | ⛔ not-built |
| **D61** | Progress event labels contain step/shape only — no cell values, no bound slot values, no JWT content | E2E | `D61-progress-pii-safe` | **Observability + PII** | ⛔ not-built |
| **D63** | When `sqlglot` cannot parse a live `runQuery` well enough to extract referenced columns, the MCP rejects the query and alerts — it never runs an unverifiable query against the warehouse | Unit + E2E | `D63-parse-fail-rejects-not-runs` | **Parser fail-closed** | 🟡 unit-green |
| **D63** | The `system.query_log.columns` oracle correctly identifies cases where parser-extracted columns differ from engine-reported columns, enabling the false-reject rate to be measured and driven down | Component | `D63-query-log-oracle-diff` | — | ⛔ not-built |
| **D64** | Scratch tables named outside `scratch.s_<session_id>_*` are rejected by the MCP on every `runQuery`; a cross-session scratch reference (session A referencing session B's table) is always rejected | Unit + Component | `D64-scratch-cross-session-rejected` | — | 🟡 unit-green |
| **D64** | An unparseable scratch table reference is fail-closed: rejected, never run unchecked | Unit | `D64-scratch-parse-fail-closed` | — | ✅ green |
| **D25** | Per-turn Phoenix spans contain no JWT, no raw scope token, no cell values, no bound slot values; shape/count/latency only | E2E | `D25-spans-pii-redacted` | **Observability + PII** | ⛔ not-built |
| **D25** | The leakage gate emits a `GUARDRAIL` span in Phoenix when a candidate is flagged | Component | `D25-leakage-gate-guardrail-span` | **Observability + PII** | ⛔ not-built |
| **D34** | The blueprint extractor parameterizes only real SQL that ran and was accepted (from the `runQuery` trail) — it never synthesizes fresh SQL; a session with no acceptance signal produces no blueprint candidate | Component | `D34-no-synthesis-only-accepted-sql` | **Correction → learning** | ⛔ not-built |
| **D35** | The SQL-AST template rewrite stage locates predicates and substitutes `{slot}` placeholders deterministically; the LLM never re-emits SQL; an unrewritable query fails-to-review rather than producing a bad template | Unit | `D35-ast-rewrite-no-llm-sql-emission` | — | ⛔ not-built |
| **D45** | A crash during active (non-paused) compute results in a clean re-run of the whole turn — not a partial or corrupted state — because the data path is read-only and idempotent | Component | `D45-crash-rerun-idempotent` | — | ⛔ not-built |
| **D49** | Slot resolvers are deterministic code — no LLM call occurs inside `runBlueprint`; multi-match or fuzzy NL slot values route to `askUser`, never to an LLM guess | Unit | `D49-resolver-no-llm-multi-match-asks-user` | **Ask → clarify** | ⛔ not-built |

| **D72** | Hook failures (unhandled exceptions) are logged as `GUARDRAIL` spans and treated as `continue`; a buggy hook never drops a live turn | Unit | `D72-hook-exception-continues-turn` | — | ⛔ not-built |
| **D72** | H1 ON_REQUEST_RECEIVED veto → turn is aborted cleanly (before it starts); veto from H7/H9 → graceful denial / fallback (turn continues) | Unit + E2E | `D72-hook-veto-correct-action` | — | ⛔ not-built |
| **D72** | H15–H17 (learning-loop hooks) are read-only; a hook at these points that attempts a write is rejected by the runtime | Unit | `D72-learning-hook-readonly` | — | ⛔ not-built |
| **D73** | A skill never receives the raw JWT, raw scope token, or raw session_id; `HookContext` carries only hashed/id forms (D5 parity) | Unit | `D73-skill-no-raw-credentials` | — | ⛔ not-built |
| **D73** | A skill-triggered tool call has credentials injected by the runtime at dispatch; the skill cannot supply its own scope | Unit + Component | `D73-skill-tool-call-scope-injected` | — | ⛔ not-built |
| **D73** | A skill that exceeds `max_latency_ms` is terminated; a `GUARDRAIL` span is emitted; the turn continues | Unit | `D73-skill-timeout-guardrail` | — | ⛔ not-built |
| **D74** | Skill execution emits a `CHAIN` span; veto/reject/fallback emits a `GUARDRAIL` span; no cell values or slot values appear in span attributes | E2E | `D74-skill-spans-pii-safe` | **Observability + PII** | ⛔ not-built |

---

## Coverage gaps — ACCEPTED into backlog (2026-06-30)

The following six gaps were noted at matrix creation and have been reviewed and **accepted as
backlog test additions** by the human (accepted 2026-06-30). They are not blocking the first
release but must be addressed before Phase 2 completion. Each gap has a proposed backlog slug for
the tracking board.

1. **D48 dedup race safety** — `ACCEPTED-into-backlog` — backlog slug: `D48-dedup-race-component-test`.
   Add a "concurrent-extraction dedup" component test: drive two concurrent sessions producing the
   same blueprint and assert one create + one `hit_count` increment, never a duplicate.

2. **D50 filter-before-compact ordering** — `ACCEPTED-into-backlog` — backlog slug: `D50-filter-before-compact-summary-clean`.
   The "mid-session scope narrowing" scenario should be extended to assert that the *history summary*
   (not just replayed entries) is clean of out-of-scope column references.

3. **D34/D35 extraction path** — `ACCEPTED-into-backlog` — backlog slug: `D34-D35-no-synthesis-layer2-component`.
   Add a Layer 2 component test that seeds a fabricated transcript with a non-accepted query and
   asserts no blueprint candidate is produced; separately assert the AST rewrite never re-emits
   LLM-generated SQL.

4. **D59 injection-safe intermediates** — `ACCEPTED-into-backlog` — backlog slug: `D59-inter-node-passing-composite-scenario`.
   The composite/approval blueprint fixture should drive a Layer 3 scenario that explicitly asserts
   D59a: scalar intermediates bound as typed params, table intermediates materialized to scratch.

5. **D49 resolver determinism** — `ACCEPTED-into-backlog` — backlog slug: `D49-resolver-determinism-layer1-authoritative`.
   Document that Layer 1 is the authoritative gate for this invariant (structurally hard to assert
   end-to-end); the "ask → clarify" Layer 3 scenario is not extended for this.

6. **D5 scope injection opacity** — `ACCEPTED-into-backlog` — backlog slug: `D5-scope-tool-call-no-injected-fields`.
   Add an assertion on tool-call argument shapes in the Phoenix trace (no `session_id`/JWT/scope
   raw values visible in tool-call records) as a complement to the span-level PII check.

---

## Additional tagged invariants from D69 (column-provenance contract, 2026-06-30)

The following invariants are derived from D69's OQ resolutions and must be tagged and tracked in
the Layer 1 test suite. They extend the existing D57/D63/D64 rows in the matrix above.

| Decision | Invariant | Test layer | Test slug | Conformance scenario | Status |
|---|---|---|---|---|---|
| **D69 / OQ-1** | Lambda-body columns ARE in the USES set; if `sqlglot` cannot prove the lambda body was walked, `ProvenanceExtractionError` is raised — no silent skip | Unit | `D57-lambda-body-failclosed` | — | ✅ green |
| **D69 / OQ-2** | `SELECT *` expands to ALL catalog columns of referenced tables; any out-of-scope expanded column → reject; table not in catalog → fail-closed | Unit | `D57-star-expand-reject-out-of-scope` | — | ✅ green |
| **D69 / OQ-3** | Canonical USES pair is `(database.table, column)` three-part; scope vector uses same granularity; extractor resolves three-part SQL references without stripping the database prefix | Unit | `D69-uses-pair-fully-qualified` | — | ✅ green |
| **D69 / OQ-4** | Scratch table columns are NOT column-scope-checked; `qualify_columns` is not run for scratch tables; session-ID name-match (D64) is the sole gate | Unit | `D69-scratch-column-no-scope-check` | — | ✅ green |
| **D69 / OQ-5** | `extract_column_provenance` is never called on EXPLAIN queries; the caller enforces this precondition before invoking the extractor | Unit (caller precondition test) | `D69-explain-caller-precondition` | — | ✅ green |
| **D70** | Identifier matching is case-sensitive + exact; a table/column reference unresolvable against the catalog → `ProvenanceExtractionError` (no case-insensitive fallback, no silent skip); scratch columns are the documented exception (D69/OQ-4) | Unit | `D70-case-mismatch-fails-closed` | — | ✅ green |
