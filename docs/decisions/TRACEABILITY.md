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
Phase-0 first brick, D52/D62) is built and passing Layer-1 tests. The **`clickhouse-api` MCP
extension** (D57/D63/D64 enforcement) is built + pushed (Session 5). The **Phase-0 agent runtime**
(`src/data_agent/runtime/`, Session 6) is now built and **Layer-1-green + reviewed** (296 tests): it
supplies the **agent-runtime** consumers of **D5** (credential injection / model-invisibility),
**D44** (replayed-trail *and* conversational-message scope re-filter — the 2026-07-01 clarification,
turn-scoped & status-gated), **D46/D50** (preview-only + filter-before-compact), **D47/D55** (budget
caps), and **D45** (pause/resume CAS, via the in-memory fake at Layer 1). **Honest caveat:** these are
proven at **Layer 1 with fakes** and have passed adversarial review — they are **not** yet
conformance-proven. The D68 release gate is **Layer-3** (Docker + Playwright), and the Component
layers (real Couchbase for D44/D45; real MCP↔ClickHouse for D57/D64) are **not yet built**, so the
rows below move only to `🟡 unit-green`, never `✅ green`, on the strength of this session. **Update
(Session 7, D83):** the `clickhouse-api` MCP now column-scopes `getTableSchema` (overlay + scope-filter)
and `sampleRows` (scope-reject) too — closing the gap that was here; the runtime's D44 drop is now
redundant-but-harmless belt-and-suspenders (OQ-4). That MCP work is unit-green in `clickhouse-api` but
uncommitted + Layer-2 (live ClickHouse) not yet run.

**Update (Session 9, D77/D85):** the runtime `resolveValues` composite (`src/data_agent/runtime/composite/`
+ `model/embedding_client.py`) is built, adversarially QA'd (~128 Layer-1 tests, incl. 5 xfail bug repros
now passing), and reviewed (REQUEST CHANGES → fixed → APPROVE). The D77/D85 rows below are `🟡 unit-green`:
injection/scope/credential/degrade paths are proven **at Layer 1 with fakes** (the `FakeEmbeddingClient`
and `FakeMCPClient`), including a real-OTel-exporter redaction e2e. **Not yet run:** Layer-2 over the real
`clickhouse-api` MCP↔ClickHouse for the backing `runQuery`, and any live custom-embedding-API call (the
endpoint/contract is still an open question — see OPEN-QUESTIONS §Retrieval).

**Update (Session 9b/10, D71 contracts + D86 retrieval Slice 1):** the embedding/reranker contracts are
now REAL (user-provided mocks, `~/Development/SQL/mocks`) and the "no live embedding call" gap above is
closed — `HttpEmbeddingClient`/`HttpRerankerClient` and the retrieval pipeline are Layer-2-validated
against the live mock services (18003/18004), including semantic-beats-frequency for `resolveValues`
(so `D85-embedding-failure-degrades-not-fails` and `D77-*` ranking behaviour now have a live-embedding
leg; statuses stay `🟡` pending the real MCP leg + production endpoint). The D86 rows below are
`🟡 unit-green` (Layer-1 with fakes) except where noted Layer-2-proven; the neo4j index (Slice 2) and
the D7/D8 Layer-3 conformance scenarios are not yet built.

**Update (Session 13, D89 — the `runBlueprint` brick, Slices A/B/C):** the blueprint execution engine
(`src/data_agent/runtime/blueprint/` — `executor.py`, `rules.py`, `template.py`, `slots.py`,
`verify.py`, `when.py`, `models.py` + the `RunBlueprintTool` and the `AgentLoop` pausing-runtime-tool
seam) is now built, adversarially QA'd, and reviewed across all three slices (Slice A: APPROVE WITH
FIXES → 2 rounds; Slice B: REQUEST CHANGES → fixed → APPROVE; Slice C: REQUEST CHANGES → fixed →
APPROVE). The D89/D56/D67/D45-blueprint rows below are `🟡 unit-green` — the executor, scope-honesty,
D56 verify, scalar-contract, resolve_via, and pause/resume paths are proven **at Layer 1** (the
executor/rules/verify/slots suites + the adversarial qa4/qa5/qa6 suites) — **with a Layer-2 live leg
green** where it ran: **single-node AND multi-node scalar DAGs execute end-to-end against real neo4j +
ClickHouse-via-MCP**, and the non-oracle `getBlueprint` NOT_FOUND holds for a narrow scope. **Not yet
built:** the runBlueprint Layer-3 Playwright conformance scenarios (authored/proven at Layer-1/2, not
yet demo-wired), any live `resolve_via` seed (D67 Layer-1 only), and the table-intermediate path (F2,
gated on a `clickhouse-api` scratch-write surface). Tests at slice close: **1225 passed / 44 skipped /
0 xfailed**, ruff clean; live Layer-2 3/3.

**Update (Session 14 — the D68 release gate is GREEN): the FULL Layer-3 conformance suite is
`12/12 Playwright-green.`** The Layer-3 completion brick (Slices 1+2,
[layer3-conformance-design.md](layer3-conformance-design.md)) turned every previously-red scenario
green over the real UI + BFF + runtime with deterministic model/MCP/vector-index doubles: the **4
runBlueprint scenarios** (fast-path execution, no-silent-verification D56, approval pause/resume
D45/D59b, ask→clarify→resume D49) and the **3 remaining Phase-0 scenarios** (mid-session scope
narrowing D44, observability + PII span inspection D25, and **pause/resume durability across a genuine
runtime-process restart against live Couchbase** D45), joining the 5 pre-existing (D61/clarify/D57/
D63/D47). **This satisfies the D68 release gate** (the full Layer-3 burndown), the first time it has
been met. The conformance-scenario legs of the D44/D25/D45/D56/D49/D57/D61/D63/D47 rows below are now
**Layer-3-green**; rows may still read `🟡` where their real-infra **Component** layer (e.g. live
Couchbase for every D45 path, real MCP↔ClickHouse for every D57 path) is not yet built — the D68 gate
is the conformance suite, which is green. Tests at brick close: **1355 passed / 56 skipped**, ruff
clean; `RUN_E2E=1` → **13/13**. The three new test seams (span-exporter injection, `GET /_test/spans`,
the env-gated scope-narrow BFF endpoint) are structurally default-off (no production path enables
them) and the scope endpoint is monotonic-narrowing (rejects widening) — reviewer-verified.

See [docs/11-testing.md](../11-testing.md) for the four-layer test pyramid definition and the full
Layer 3 scenario list. See [docs/decisions/DECISIONS.md](DECISIONS.md) for the rationale behind each
decision cited here.

---

## Matrix

| Decision | Invariant | Test layer | Test slug | Conformance scenario | Status |
|---|---|---|---|---|---|
| **D5** | `session_id`/JWT/scope are injected by code — the model never sees or supplies them; it cannot forge or escalate scope | Unit | `D5-scope-never-model-visible` | — | 🟡 unit-green |
| **D44** | Every stored tool result carries a column-provenance set; on scope narrowing, trail entries whose provenance ⊄ current scope are dropped before context assembly — never replayed | Unit + Component | `D44-provenance-drop-on-scope-narrow` | **Mid-session scope narrowing** | 🟡 unit-green |
| **D44** | SQL parse failure → provenance unknown → entry dropped from replay (fail-closed, never assumed in-scope) | Unit | `D44-parse-fail-drops-entry` | — | 🟡 unit-green |
| **D45** | A pause checkpoint is persisted before yielding; any runtime instance can resume at `awaiting_node` after a restart; completed DAG nodes never re-run; `consumed` CAS flag prevents double-entry (BUILT for mid-DAG approval per D89, Slice C: checkpoint carries completed nodes' scalar outputs + per-node provenance + SQL; resume CAS-consumes exactly-once, double-resume rejected; survives a runtime restart from checkpoint alone; budget 1 `tool_calls_made` across pause+resume) | Component + E2E | `D45-pause-resume-survives-restart` | **Pause/resume durability** | 🟡 unit-green (Layer-3 pending) |
| **D46** | Results are preview-only in model context (≤N rows + `truncated` flag + `row_count`); full results persist to Couchbase and appear in UI; preview never causes the model to mistake a truncated set for a complete answer | Unit | `D46-preview-truncated-flag-present` | — | 🟡 unit-green |
| **D47** | The raw agent loop never runs past its per-turn budget (iterations/tokens/wall-clock) without pausing via `askUser`; "continue" grants exactly one fresh budget window, not unlimited continuation | E2E | `D47-budget-cap-triggers-pause` | **Budget-cap pause** | 🟡 unit-green |
| **D48** | Two blueprints with identical SQL semantics (same `resolves`, `uses_rules`, normalized AST) produce the same `canonical_key`; concurrent writes result in one create + one `hit_count` increment, never a duplicate | Unit + Component | `D48-same-semantics-same-key` | — | ⛔ not-built |
| **D48** | Two blueprints with materially different semantics (gross vs net, period filter present vs absent) produce different `canonical_key` values | Unit | `D48-different-semantics-different-key` | — | ⛔ not-built |
| **D48** | SQL parse failure during dedup → skip the hard key, fall through to soft embedding layer (fail-soft — never a wrong merge) | Unit | `D48-parse-fail-falls-to-soft` | — | ⛔ not-built |
| **D50** | Scope-filter (D44) runs before history compaction (D46); the prose summary is derived only from in-scope entries and is never persisted as an artifact | Unit | `D50-filter-before-compact-order` | — | 🟡 unit-green |
| **D52** | `sqlglot` extracts the qualified `(table, column)` USES set from a query, including columns referenced only in derived expressions (e.g. `AVG(gross_pay)` yields `payroll_fact.gross_pay`) | Unit | `D52-derived-column-in-uses-set` | — | ✅ green |
| **D52** | `sqlglot` parse failure behavior is per-consumer: D57-consumer → fail-closed reject; D48-consumer → fail-soft skip-hard-key; D35-consumer → fail-to-review; none silently proceeds | Unit | `D52-per-consumer-fail-behavior` | — | 🟡 unit-green |
| **D53** | A table with no catalog entry is served structural-only (not an error); it is ineligible for blueprint promotion. Graceful degradation is **MCP-side again per D83** (D78's runtime-side placement is reversed) — the MCP overlays nothing and returns normal introspection for that table (test at the **MCP** component layer, not the runtime). D83's new `getTableSchema` scope-filter and `sampleRows` scope-reject are likewise MCP-component-tested (unit-green in `clickhouse-api` as of WORKLOG.md Session 7; Layer-2 live-ClickHouse coverage not yet run) | Component | `D53-uncatalogued-table-structural-only` | — | ⛔ not-built |
| **D53** | A `schema_edit` candidate opens a branch + YAML patch PR with CI (schema lint + `explainQuery` dry-run); it is never auto-committed to the catalog | Component | `D53-schema-edit-opens-pr-not-auto-commit` | — | ⛔ not-built |
| **D56** | Every `runBlueprint` response passes the deterministic grain-integrity check before being returned; a wrong-grain result (fan-out double-count) is caught and falls back to the raw loop — never returned to the user (BUILT D89, Slice B: verify-FAIL / unmappable grain / grain-probe error / denied probe **all withhold** the rows; `grain_verifiable:false` skips visibly with `grain_checked:false`) | Unit + E2E | `D56-wrong-grain-falls-back` | **No-silent verification** | 🟡 unit-green (Layer-2 live single+multi-node DAG green; Layer-3 pending) |
| **D56** | The LLM verification gate runs on every blueprint response; the user is never asked to verify; SQL is visible but verification is the agent's job (BUILT D89: LLM review is a loop round-trip after the tool returns, not a nested call — satisfies D49 no-LLM-inside) | E2E | `D56-verify-gate-always-runs` | **No-silent verification** | 🟡 unit-green (Layer-3 pending) |
| **D57** | On every `runQuery`, the MCP parses the SQL, extracts referenced columns, and rejects the query if referenced columns ⊄ injected scope; derived aggregates over forbidden columns are caught (`AVG(gross_pay)` with no payroll scope → rejected) | Unit + Component | `D57-derived-agg-over-forbidden-rejected` | **Scope denial** | 🟡 unit-green |
| **D57** | An out-of-scope blueprint is not surfaced in thin cards (retrieval pre-filter); the existence of a restricted report is not leaked to the user | Component + E2E | `D57-out-of-scope-blueprint-not-surfaced` | **Scope denial** | ⛔ not-built |
| **D58** | A `global_knowledge` candidate is not returned by `searchKnowledge` until a human approves it in the review inbox; approve → immediately retrievable | Component + E2E | `D58a-knowledge-not-retrievable-before-approval` | **Knowledge human-gate** | ⛔ not-built |
| **D58** | `LEARNING_ENABLED=false` halts the write router and promotion scheduler without a deploy; reads (`searchKnowledge`, `searchBlueprints`) continue unaffected | Component + E2E | `D58c-learning-kill-switch-halts-writes` | **Correction → learning** | ⛔ not-built |
| **D59** | Composite blueprint inter-node scalar outputs are bound as **typed sqlglot-AST literals** (F1 — the D10-safe path, corrected from the original "server-side parameters" design at build; no `runQuery` param surface opened), never string-interpolated into SQL text; a query-result cell binds as one escaped literal | Unit + Component | `D59a-scalar-intermediate-as-typed-param` | — | 🟡 unit-green (Layer-2 live multi-node DAG green; Layer-3 pending) |
| **D59** | Composite blueprint inter-node **table** outputs — DEFERRED (F2, D89): no scratch-write surface exists yet, so a table-intermediate DAG is **rejected pre-dispatch** (scalar-converging only); scratch materialization/`JOIN` is gated on the `clickhouse-api` Track-A surface | Unit + Component | `D59a-table-intermediate-to-scratch` | — | 🟡 unit-green (rejection path proven; scratch-write path deferred) |
| **D59** | `requires_approval` fires before the node runs and may reference only upstream/completed outputs; a reference to the gated node's own un-computed output is rejected (BUILT D89, Slice C; approval decision is affirmative-only — ambiguous/negative → deny/re-pause, never fail-open consent) | Unit | `D59b-approval-upstream-only-reference` | — | 🟡 unit-green (Layer-3 pending) |
| **D61** | Step-level progress events stream to the UI throughout a turn (context assembly, blueprint pick, each DAG node, D56 verify gate); the first progress event arrives before the final answer | E2E | `D61-progress-events-stream` | **Ask → fast path** | ⛔ not-built |
| **D61** | Progress event labels contain step/shape only — no cell values, no bound slot values, no JWT content | E2E | `D61-progress-pii-safe` | **Observability + PII** | ⛔ not-built |
| **D63** | When `sqlglot` cannot parse a live `runQuery` well enough to extract referenced columns, the MCP rejects the query and alerts — it never runs an unverifiable query against the warehouse | Unit + E2E | `D63-parse-fail-rejects-not-runs` | **Parser fail-closed** | 🟡 unit-green |
| **D63** | The `system.query_log.columns` oracle correctly identifies cases where parser-extracted columns differ from engine-reported columns, enabling the false-reject rate to be measured and driven down | Component | `D63-query-log-oracle-diff` | — | 🟡 oracle built (Session 14): `src/data_agent/sqlparse/oracle.py` (`classify_query`/`run_oracle`) + a Layer-2 env-guarded live job replays `system.query_log` (0 rejects on 12 test SELECTs — **NOT** a production rate; reusable against a prod/staging query_log for a real number). Surfaced the D90 fail-open. **Production false-reject rate pending a prod query_log.** |
| **D64** | Scratch tables named outside `scratch.s_<session_id>_*` are rejected by the MCP on every `runQuery`; a cross-session scratch reference (session A referencing session B's table) is always rejected | Unit + Component | `D64-scratch-cross-session-rejected` | — | 🟡 unit-green |
| **D64** | An unparseable scratch table reference is fail-closed: rejected, never run unchecked | Unit | `D64-scratch-parse-fail-closed` | — | ✅ green |
| **D25** | Per-turn Phoenix spans contain no JWT, no raw scope token, no cell values, no bound slot values; shape/count/latency only | E2E | `D25-spans-pii-redacted` | **Observability + PII** | ⛔ not-built |
| **D25** | The leakage gate emits a `GUARDRAIL` span in Phoenix when a candidate is flagged | Component | `D25-leakage-gate-guardrail-span` | **Observability + PII** | ⛔ not-built |
| **D34** | The blueprint extractor parameterizes only real SQL that ran and was accepted (from the `runQuery` trail) — it never synthesizes fresh SQL; a session with no acceptance signal produces no blueprint candidate | Component | `D34-no-synthesis-only-accepted-sql` | **Correction → learning** | ⛔ not-built |
| **D35** | The SQL-AST template rewrite stage locates predicates and substitutes `{slot}` placeholders deterministically; the LLM never re-emits SQL; an unrewritable query fails-to-review rather than producing a bad template | Unit | `D35-ast-rewrite-no-llm-sql-emission` | — | ⛔ not-built |
| **D97** | Role classification is TOTAL: every literal predicate gets exactly one `slot\|rule\|inline` entry (a missing entry fails validation); a caller/session-specific predicate becomes an **optional slot** (`required:false` + `optional_pattern`), never an AST deletion; a metric-defining predicate stays `inline` | Unit | `S3-no-drop-totality` | **Correction → learning** | ⛔ not-built |
| **D97** | A predicate the extractor cannot confidently classify into exactly one role (ambiguous inline-vs-optional-slot; rule-shaped with no catalog rule) routes to human review — never auto-promoted; an adversarial metric-defining-as-optional-slot proposal is rejected to review | Unit | `S3-unclassifiable-fails-to-review` | — | ⛔ not-built |
| **D98** | Promotion replay samples slot values (no stored entity inputs) and asserts grain-integrity (D56 fan-out teeth) + result_signature shape — catching a fan-out or shape-drift template | Unit + Component | `S3-replay-catches-grain-and-shape` | **No-silent verification** | ⛔ not-built |
| **D98** | Replay does NOT gate on values: a semantically-plausible-but-value-changed template on a single session leaves the candidate at `candidate` (replay verifies structure, not the returned number — the honest-scope invariant is encoded as an executable test) | Unit | `S3-replay-not-a-value-oracle` | — | ⛔ not-built |
| **D45** | A crash during active (non-paused) compute results in a clean re-run of the whole turn — not a partial or corrupted state — because the data path is read-only and idempotent | Component | `D45-crash-rerun-idempotent` | — | ⛔ not-built |
| **D49** | Slot resolvers are deterministic code — no LLM call occurs inside `runBlueprint`; multi-match or fuzzy NL slot values route to `askUser`, never to an LLM guess (BUILT D89) | Unit | `D49-resolver-no-llm-multi-match-asks-user` | **Ask → clarify** | 🟡 unit-green (Layer-3 pending) |

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
| **D90** | An unqualified column absent from the catalog (`col.table==''`, not a case variant) fails closed unless it is a provable SELECT-list output-alias reference (declared alias in GROUP BY/ORDER BY/HAVING) or a bare column of a scratch-only SELECT; uncatalogued lambda-body columns also fail closed — closing the D70-class silent-drop that understated the USES set (D44 replay fail-open under catalog drift). Byte-identical in both extractor copies (D79a); adversarially verified (no fail-open, no false-reject) | Unit | `D90-uncatalogued-unqualified-fails-closed` | — | ✅ green |
| **D92** | `X-Session-Id` is bound to the JWT: the MCP constant-time-compares `sha256(header)` to the `sid_hash` claim after signature validation and before any tool/D64 extractor — a header not matching the token's session (or, for a bound token, an omitted/empty header) → `403 SESSION_BINDING_MISMATCH`; `require_sid_binding` defaults on. A cross-session scratch read is rejected (proven live: victim PII returned pre-fix, `SCRATCH_SESSION_VIOLATION` post-fix) | Unit + Component | `D92-session-id-bound-to-jwt` | **Mid-session scope narrowing / session isolation** | 🟡 unit-green (Layer-2 live vs enforcing MCP green) |
| **D92** | A `scratch.*` reference with no bound session (`session_id is None`) fails closed (`SCRATCH_SESSION_VIOLATION`) in every SQL position — closes the omit-header scratch bypass; byte-identical both extractor copies (D79a), no D90/D64/D70 regression | Unit + Component | `D92-scratch-no-session-fails-closed` | — | 🟡 unit-green (Layer-2 live green) |
| **D92** | A mint request cannot forge/shadow `sid_hash` or other reserved claims (`sub/iss/aud/exp/nbf/iat/user_name/column_scope/sid_hash` rejected; computed claims applied last) | Unit | `D92-reserved-claim-guard` | — | ✅ green |
| **D92** | Per-user entitlement mint (readiness): the BFF resolves identity → `column_scope` via the `ui/entitlements.py` seam and mints that scope; a restricted user's out-of-entitlement column is denied by the MCP (`COLUMN_SCOPE_VIOLATION`, proven live); demo `ui-user` stays allow-all; unmapped identity → default-with-warning; scope-narrow is monotonic within the entitled base | Unit + Component | `D92-per-user-entitlement-mint` | — | 🟡 unit-green (Layer-2 live restricted-scope denial green) |
| **D93** | The scratch-write side-channel confines to `scratch.*` STRUCTURALLY: the database is hardcoded and the table name derives solely from the D92-bound `X-Session-Id` — no request-body field can steer a write to the warehouse or another session (holds even without the scratch-only grant) | Unit + Component | `D93-scratch-write-structural-confinement` | — | 🟡 unit-green (Layer-2 live vs real ClickHouse green) |
| **D93** | The write routes are non-`@mcp.tool` (absent from `list_tools` — the model can never reach them); rows load via native bulk insert as DATA (a hostile cell never reaches SQL `command()`); unauth → 401, no bound session → fail closed | Unit + Component | `D93-scratch-write-not-a-tool-rows-are-data` | — | 🟡 unit-green (Layer-2 live green) |
| **D93** | Exact-structure drop authorization (`^s_<sid>_bp_[0-9a-f]{32}$`) — a session can only drop a table its own materialize produced; the `_`-boundary ambiguity is closed independent of session-id minting; cross-session drop/read → `SCRATCH_SESSION_VIOLATION` | Unit + Component | `D93-scratch-exact-ownership` | — | 🟡 unit-green (Layer-2 live isolation proof green) |
| **D93 / D59a** | Runtime materialize-and-join (Slice 2): a table-node's rows are written to scratch (D93 endpoint, rows-as-data), the downstream `FROM`/`JOIN` placeholder is AST-rewritten to the returned scratch identifier (not model text / not a cell), JOINed with the warehouse same-instance; a real table-passing blueprint verifies (`total_earnings==7350`) with warehouse-only provenance | Component | `D93-materialize-and-join-live` | — | 🟡 unit-green (Layer-2 live JOIN + isolation green) |
| **D93 / D64** | session_ids are underscore-free (`s<32hex>`); the write boundary rejects an underscore-containing sid so it's structural; the read-gate `_validate_scratch_name` uses exact session extraction (byte-identical both repos) — the `_`-boundary cross-session read is closed; a **truncated** intermediate fails closed (never a partial "verified" answer) | Unit + Component | `D93-underscore-free-exact-read-gate` | — | 🟡 unit-green (Layer-2 live underscore-reject green) |
| **D77** | `concept` never reaches SQL — the backing `runQuery` is built from catalog-allowlisted identifiers + sqlglot literals only; `concept` is used solely for in-runtime embedding/ranking (D10). Verified empirically incl. ClickHouse-escaping of adversarial `concept`/period values | Unit | `D77-concept-never-in-sql` | — | 🟡 unit-green |
| **D77** | Enforcement is the inner `runQuery`'s — no separate scope path in the composite; an inner `COLUMN_SCOPE_VIOLATION` (incl. an out-of-scope description or period column) passes through as a `resolveValues` denial, never bypassed | Unit | `D77-enforcement-via-inner-runquery` | **Scope denial** | 🟡 unit-green |
| **D77** | Unknown/ambiguous `table` or `column`, or unknown `period.column`, fails closed (`RESOLVE_VALUES_UNKNOWN_TARGET`, retryable) before any query is issued; the message names only the supplied target (no catalog enumeration → no scope leak) | Unit | `D77-unknown-target-fails-closed` | — | 🟡 unit-green |
| **D77** | A `resolveValues` trail entry carries the inner `runQuery`'s extracted provenance (the value/description/period columns) — correct D44 USES-set, no `capture.py` special-case | Unit | `D77-provenance-carried-from-inner` | — | 🟡 unit-green |
| **D77** | `resolveValues` counts as exactly one `tool_calls_made`; the single inner `runQuery` is never double-counted against the budget | Unit | `D77-one-budget-count` | — | 🟡 unit-green |
| **D77** | Empty in-scope result set → `status="ok"` with an empty value list (a valid "no matches" answer), never an error | Unit | `D77-empty-result-is-ok` | — | 🟡 unit-green |
| **D77 / D25** | `concept` (and `period` start/end literals) are redacted from every span and progress payload — proven by a real-OTel-exporter e2e asserting `concept` is absent from all emitted attributes | Unit | `D77-concept-redacted-from-telemetry` | — | 🟡 unit-green |
| **D85** | An embedding failure (unreachable / non-2xx / malformed / non-finite / length-mismatch vectors) degrades to freq-only ranking, never crashes the turn and never fails the call; `result_full.degraded`/`ranking="freq_only"` surface the state so a freq-only top score can't masquerade as a semantic match | Unit | `D85-embedding-failure-degrades-not-fails` | — | 🟡 unit-green |
| **D86** | Phase-0 parity: with retrieval unwired (`None`/disabled) — or retrieving an empty/degraded result — the assembled context is **byte-identical** to the pre-retrieval runtime, proven on a non-trivial compaction-triggering history | Unit | `D86-unwired-retrieval-byte-parity` | — | 🟡 unit-green |
| **D86** | A blueprint candidate whose transitive USES ⊄ `column_scope` is dropped before rerank/render (exact-string `db.table.column` keys, D70 case-exact; `uses=None` dropped fail-closed even under allow-all; empty scope = allow-all, D80) | Unit | `D86-blueprint-scope-prefilter-fail-closed` | **Scope denial** | 🟡 unit-green |
| **D86** | `retrieve()` never raises: any exception from embedder/index/reranker/user-memory/observer degrades (empty context, recall order, or skipped item respectively) and **never crashes the turn**; a rerank score-count mismatch degrades to recall order with `reranked=false`, never silently drops candidates | Unit | `D86-retrieve-never-raises-degrades` | — | 🟡 unit-green |
| **D86** | No degrade is silent: every degrade path emits a shape-only `retrieval.degraded` span (reason attr) + the retrieval progress event with zero counts | Unit | `D86-degrade-is-observable` | — | 🟡 unit-green |
| **D86** | Retrieved corpus text is structure-sanitized at render (newlines/control chars neutralized, per-item length caps) — hostile card/chunk text cannot forge sections or inject instructions into the pre-injection system message | Unit | `D86-render-structure-sanitized` | — | 🟡 unit-green |
| **D86 / D25** | The user's question text appears in NO retrieval span attribute or progress payload (counts/reasons only), proven with a real OTel exporter | Unit | `D86-question-never-in-telemetry` | — | 🟡 unit-green |
| **D86** | Exactly one embed per budget window (memo keyed on question+scope): per-round-trip context rebuilds never re-embed; a scope change busts the memo; resume re-embeds the original turn question, not the clarify answer | Unit | `D86-one-embed-per-window` | — | 🟡 unit-green |
| **D86 / D71** | The end-to-end embed→recall→scope-filter→rerank→render path works against the **live** embedding + reranker services (semantic ordering + rerank-changes-order asserted live) | Component | `D86-pipeline-live-e2e` | — | 🟡 unit-green (Layer-2 vs mock services green; Layer-3 pending) |
| **D87** | Stored blueprint `uses` keys byte-match the runtime `column_scope` format (`database.table.column`, case-exact): loader refuses malformed entries at write (fail-closed `CorpusLoadError`); recall coerces missing/empty/malformed `uses` to *undetermined* → dropped by the scope filter, **never** allow-all | Unit + Component | `D87-uses-byte-exact-fail-closed` | **Scope denial** | 🟡 unit-green (Layer-2 vs live neo4j green) |
| **D87** | Embedding-model parity: mixed-model writes refused atomically inside the write txn (empty stamp = conflict); read-side recall excludes mismatched nodes and flags `retrieval.model_mismatch`; a default-config deploy's parity key matches the seed stamp | Unit + Component | `D87-model-parity-strict-write-degrade-read` | — | 🟡 unit-green (Layer-2 vs live neo4j green) |
| **D87** | `Neo4jVectorIndex.recall` never raises: driver failures/timeouts → `[]` (observed degrade); a malformed row is skipped per-row (good rows survive); re-seed rewrites `:USES` edges so property/edges cannot drift | Unit + Component | `D87-neo4j-recall-never-raises` | — | 🟡 unit-green (Layer-2 incl. unreachable-neo4j degrade green) |
| **D87 / D86** | Full retrieval e2e against **real neo4j + real embedder + real reranker**: recall→Candidate mapping (byte-exact `uses`), scope filtering on real stored USES, semantic ordering through the unmodified Slice-1 pipeline | Component | `D87-neo4j-e2e-live` | — | 🟡 unit-green (all five Layer-2 proofs green; Layer-3 pending) |
| **D88 / D44** | `searchBlueprints` applies the canonical transitive-USES scope pre-filter (same code path as pre-injection); empty scope = allow-all | Unit + Component | `D88-searchblueprints-scope-prefilter` | **Scope denial** | 🟡 unit-green (Layer-2 live green) |
| **D88** | `getBlueprint` non-oracle: out-of-scope is byte-identical to not-found (`ok`+`{found:false}` — status/code/message/provenance/preview all identical); restricted blueprints' existence never leaks | Unit + Component | `D88-getblueprint-non-oracle` | **Scope denial** | 🟡 unit-green (Layer-2 live green) |
| **D88 / D44** | Provenance split by footprint: FOUND `getBlueprint` carries its scope-checked `uses` (drops from D44 replay under mid-session narrowing, `getTableSchema` posture); `searchBlueprints`/`searchKnowledge` carry safe-empty `frozenset()` (kept) | Unit | `D88-provenance-footprint-split` | — | 🟡 unit-green |
| **D88 / D58** | `searchKnowledge` bypasses column scope (entity-agnostic, human-gated at write); no blueprint data reachable via knowledge queries | Unit + Component | `D88-searchknowledge-scope-bypass` | — | 🟡 unit-green (Layer-2 live green) |
| **D88 / D25** | Model-authored `query` fully redacted from every span attribute and progress payload (real-OTel e2e); registry-level guard contains a raising runtime tool and coerces malformed provenance to `None` fail-closed | Unit | `D88-query-redacted-registry-guarded` | — | 🟡 unit-green |

---

## Additional tagged invariants from D89 (the `runBlueprint` brick, Session 13)

The following invariants are the load-bearing safety properties of the blueprint execution engine
(D89, Slices A/B/C). They extend the D56/D59/D45/D49/D67 rows above and are `🟡 unit-green` — Layer-1
proven with adversarial qa4/qa5/qa6 suites, with a **Layer-2 live leg green where noted** (single- and
multi-node scalar DAGs end-to-end vs real neo4j + ClickHouse-via-MCP; non-oracle `getBlueprint`
NOT_FOUND for a narrow scope). Layer-3 Playwright demo-wiring is deferred.

| Decision | Invariant | Test layer | Test slug | Conformance scenario | Status |
|---|---|---|---|---|---|
| **D89** | The scope-honesty gate (load-bearing): a blueprint's `sql_template` can only read within its declared `uses` — table-aware (`qualify_columns` over a `uses`-derived schema + `_assert_source_tables_in_uses`); `SELECT *`, alias-mask, table-blind bare-name, `dictGet`, cross-db, and qualified-JOIN-to-unlisted-table bypasses all **fail-closed at load** (legit in-`uses` multi-table JOINs accepted). Makes the D88(c) stored `uses` footprint honest | Unit | `D89-template-reads-within-uses-failclosed` | **Scope denial** | 🟡 unit-green (Layer-2 live green) |
| **D89 / D56** | No unverified return: verify-FAIL / unmappable grain / grain-probe error / denied probe all **withhold** the rows (`ExecFailed` carries none); `grain_verifiable:false` skips visibly (`grain_checked:false`) | Unit + E2E | `D89-no-unverified-return` | **No-silent verification** | 🟡 unit-green (Layer-2 live green) |
| **D89** | A scalar-consumed node returning `!=1` row / wrong column count / NULL **fails closed** (`SLOT_INVALID`) before the downstream consumer runs (D56 only verifies the terminal, so a fanned-out intermediate must fail closed on its own) | Unit | `D89-scalar-contract-failclosed` | — | 🟡 unit-green (Layer-2 live multi-node green) |
| **D89 / D59a / D10** | Slot values AND scalar/`resolve_via` intermediates are bound as **typed AST literals** (one escaped literal inside one `SELECT`) — never string-interpolated; a query-result cell is treated as hostile data on the same D10-safe path as a slot; no `runQuery` param surface opened | Unit | `D89-values-bound-as-literals` | — | 🟡 unit-green (Layer-2 live green) |
| **D89 / F2** | A table-intermediate DAG is **rejected pre-dispatch** (scalar-converging only) — no scratch-write surface exists yet | Unit | `D89-table-intermediate-rejected` | — | 🟡 unit-green |
| **D89 / D44** | The resumed provenance union + SQL span the **whole DAG** across a pause — D44 replay stays **fail-closed across the pause** (no provenance loss for completed nodes when the runtime re-enters at `awaiting_node`) | Unit | `D89-resumed-provenance-union-failclosed` | **Pause/resume durability** | 🟡 unit-green (Layer-3 pending) |
| **D89 / D59b** | The approval decision is **affirmative-only**: an ambiguous or negative response denies / re-pauses — **never fail-open consent** | Unit | `D89-approval-affirmative-only` | — | 🟡 unit-green (Layer-3 pending) |
| **D89 / D67** | `resolve_via` expands a concept → value-set via the typed `resolveValues.resolve()` hook (no model round-trip), bound as an IN-list of literals; **empty → fail-closed**, **degraded → raw-loop fallback**, denial passed through verbatim; wired for single- AND multi-node | Unit | `D89-resolve-via-empty-failclosed-degraded-rawloop` | — | 🟡 unit-green (D67 Layer-1 only; no live seed) |
| **D91 / D67** | `resolve_via` binds **only the concept-matching subset** of the ranked code domain — **gap-cut** (top prefix up to the first score gap `> resolve_via_gap_threshold`; no gap ⇒ bind all) **then sub-floor drop** (drop selected codes scoring `< resolve_via_min_confidence`); a top score below the floor `RuleFallback`s to the raw loop, never a pause. **Never binds the full domain** — the wrong-"verified"-answer guard: an `earnings` concept binds `IN ('EARN')`, not `IN ('EARN','DEDUCTION')`, so a wrong value-set can't slip past the D56 grain gate as a verified total. Shape-only `blueprint_rule_resolved` telemetry (no raw codes, D25). Replaces the prior top-`margin` pause fallback | Unit + Component | `D91-resolve-via-concept-subset-not-full-domain` | — | 🟡 unit-green (Layer-2 live-proven: 7350 / `IN ('EARN')`) |
| **D89** | A DAG counts as exactly **1 `tool_calls_made` per `runBlueprint`** across pause+resume, despite N inner node queries | Unit | `D89-one-budget-per-runblueprint` | — | 🟡 unit-green |
| **D89 / D88** | `getBlueprint` stays **non-oracle for full-DAG blueprints**: an out-of-scope DAG blueprint is byte-identical to not-found; the additive full-DAG projection doesn't leak a restricted blueprint's existence | Unit + Component | `D89-getblueprint-non-oracle-dag` | **Scope denial** | 🟡 unit-green (Layer-2 live NOT_FOUND green) |

---

## Additional tagged invariants from D94 (`ok`+`None` provenance-stranding silent-hang fix, Session 15)

The following invariants are the load-bearing properties of the D94 fix. They extend the D44/D47
rows above and are `⛔ not-built` — DESIGNED (see
[none-provenance-stranding-design.md](none-provenance-stranding-design.md)), no tagged tests yet.
The overriding invariant is **fail-closed preservation**: the sentinel and event surface no
data-bearing content under any scope, so the fix diagnoses the hang without weakening D44.

| Decision | Invariant | Test layer | Test slug | Conformance scenario | Status |
|---|---|---|---|---|---|
| **D94 / D44** | A **current-turn** `ok`+`None` entry (raw `runQuery` OR `runBlueprint`) that `filter_trail` drops is replaced in assembled context by a sentinel tool message keyed to its `tool_call_id` whose **content** is the fixed withheld string ONLY — **no** RESULT data (`result_preview`/`result_full`/columns/cells), PII-safe under empty AND narrow scope (byte-inspection). Dict keys `{role, tool_call_id, tool_name, args, withheld_sentinel, content}`; verbatim-render discriminator is the explicit `withheld_sentinel` flag, not a `content` key. `filter_trail` itself is byte-unchanged (pure gate, no observer) | Unit | `D94-current-turn-ok-none-sentinel-no-result-data` | — | ⛔ not-built |
| **D94 / D44** | The paired ASSISTANT `tool_call` **replays the model's own `args`** (not `{}`) so a multi-call turn correlates the withheld marker to the exact call — PII-safe (own causally-prior output; parity with denied-entry arg replay in `budget.py::_render_entry`; scope gate guards result data, not query text). Stripping the args re-strands in multi-call turns | Unit | `D94-sentinel-replays-own-args-for-correlation` | — | ⛔ not-built |
| **D94 / D47** | The injected sentinel **breaks the retry loop**: with it present the model does not re-emit the identical call, and the turn terminates before `_max_budget_windows` instead of hitting `loop_paused_budget_cap` / `loop_hard_ceiling_stop` | E2E | `D94-sentinel-breaks-retry-loop` | — | ⛔ not-built |
| **D94 / D44** | A **cross-turn** `ok`+`None` entry stays dropped as history — no sentinel, no event; the exemption is current-turn-only and the status-gated non-`ok` exemption is unchanged (no regression) | Unit | `D94-cross-turn-ok-none-still-dropped` | — | ⛔ not-built |
| **D94 / D25** | `assemble` emits `loop_result_withheld_provenance` (payload `tool_name`/`turn_index`/`tool_call_id`/`blueprint_id`/`reason` only — no SQL/columns/cells/scope/JWT), **at most once per `tool_call_id` per turn** across repeated round-trip rebuilds (turn-local memo de-dup); telemetry-only, no `progress.py` allowlist/label change | Unit | `D94-withheld-event-once-pii-safe` | — | ⛔ not-built |
| **D94** | Seed/load skew warning: `corpus_loader.load_corpus` cross-checks each `BlueprintSeed.uses` `(db,table)` against an optional `CatalogHandle` and logs a **soft WARNING** (naming blueprint id + missing `db.table`) on skew — load still succeeds (**never** `CorpusLoadError`); with no `CatalogHandle` supplied the check is skipped silently. Prod safety is MCP-fails-closed + catalogs-in-agreement, **not** this load-time check | Unit | `D94-seed-skew-soft-warning-not-failclosed` | — | ⛔ not-built |

---

## Additional tagged invariants from D95/D96 (Track-B learning-loop infra spine, Slice 1, Session 16)

The load-bearing invariants of the learning-loop transport + lifecycle spine (D96) and the audit-store
lock (D95). All `⛔ not-built` — DESIGNED (see
[learning-loop-infra-design.md](learning-loop-infra-design.md)), no tagged tests yet; they map to the
Slice-1 build. Statuses move `⛔ → 🟡 unit-green` as the Layer-1 suite lands and `→ ✅ green` when the
Layer-2 legs (real Redis + real Couchbase) pass. The pre-existing D58c row above
(`D58c-learning-kill-switch-halts-writes`) is this design's kill-switch invariant — it now has a home.

| Decision | Invariant | Test layer | Test slug | Conformance scenario | Status |
|---|---|---|---|---|---|
| **D58c / D96** | `LEARNING_ENABLED=false` (read uncached, per cycle) halts **both** the sweeper's enqueue AND the consumer's processing — no `XADD`, no `XREADGROUP`/process; work waits in the stream (no loss); toggles without a deploy | Unit + Component | `D58c-learning-kill-switch-halts-writes` | **Correction → learning** | ⛔ not-built |
| **D58c / D96** | Reads are unaffected by the kill-switch: `searchKnowledge`/`searchBlueprints` serve with the switch off; the request path never imports `LEARNING_ENABLED` (no-import assertion) | Unit + Component | `D58c-kill-switch-reads-unaffected` | — | ⛔ not-built |
| **D30 / D96** | Idempotency by `content_hash`: a Redis re-delivery OR a re-swept identical session processes exactly once (session already `done` with the same recorded hash → ACK + dedup_skip, no state change) | Unit + Component | `D30-idempotent-by-content-hash` | — | ⛔ not-built |
| **D96** | `content_hash` is deterministic and canonical: identical transcript → identical hash; a new turn/tool-call → different hash; `learning_status`/timestamps/`result_full_ref`/`provenance` are excluded (do not affect the hash) | Unit | `D96-content-hash-canonical` | — | ⛔ not-built |
| **D96 / D48** | Race-safe CAS transition = single-writer-per-session: two concurrent sweepers → exactly one `active→pending` claim (loser `CASMismatchError`, skips); two consumers → exactly one `queued→processing`; every transition asserts its expected `from` state | Unit + Component | `D96-cas-single-writer-transition` | — | ⛔ not-built |
| **D30 / D96** | The sweeper detects close = idle: it claims **only** `active`/`pending` sessions with `last_activity < now − idle_threshold`; a fresh or resumed session (activity bumped after scan) is skipped via CAS mismatch | Unit + Component | `D30-sweeper-idle-detection` | — | ⛔ not-built |
| **D30 / D96** | Dead-letter after `N` (=5) delivery attempts: a poison job is `XADD`ed to `learning:jobs:dead`, `XACK`ed off the work stream, and its session CAS-marked `dead_letter`; the stream is never head-of-line-blocked | Unit + Component | `D30-dead-letter-after-n` | — | ⛔ not-built |
| **D96** | `done` is terminal: a completed job's session ends `done` with its `learning_content_hash` recorded; an unchanged `done` session is not re-enqueued | Unit + Component | `D96-done-terminal` | — | ⛔ not-built |
| **D30 / D96 / D25** | The queued message is a **reference** (session_id, couchbase_doc_id, cas, user_id, scope_ref, trace_id, session_closed_at, content_hash) — NEVER the transcript, NEVER the raw JWT or raw `column_scope` (only a scope id/hash) | Unit + Component | `D30-message-is-reference` | — | ⛔ not-built |
| **D96** | Spine end-to-end (real Redis + real Couchbase): sweep→enqueue→consume→done leaves the doc at `done` and the stream fully ACKed (empty PEL, no leak) | Component | `D96-spine-end-to-end-live` | — | ⛔ not-built |
| **D95** | Candidates carry only `evidence_ref`; the entity-bearing snapshot is NEVER inlined into the entity-free global stores (neo4j / knowledge vector index) — the dedicated `learning_audit` bucket is the sole home (store provisioned in Slice 2; the entity-free-global invariant is testable at write time once the extractor lands) | Unit + Component | `D95-evidence-ref-not-inlined` | — | ⛔ not-built |
