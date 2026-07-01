# 11 — Testing & Spec Conformance

Testing is not an afterthought here: nearly every locked decision is a **hard invariant** (no silent
path D56, live scope enforcement D57, knowledge human-gate D58, provenance filter D44, injection-safe
intermediates D59, progress streaming D61, fail-closed parser D63). Those are the trust and security
guarantees — so they must be **proven by automated tests**, not assumed. The strategy is a four-layer
pyramid, where the top layer is a **spec-conformance suite**: a full stack in Docker, driven through
the real UI by **Playwright (MCP)**, asserting the observable behaviors *and* the
security/correctness invariants the spec promises.

The **enforcement registry** — mapping each hard-invariant decision to its invariant statement, test
layer, stable test slug, and conformance scenario — lives in
[decisions/TRACEABILITY.md](decisions/TRACEABILITY.md). CI fails if any hard-invariant decision has
no green tagged test.

## Layers

```
            ┌───────────────────────────────────────────────┐
  few, slow │  4. EVAL / CANARY — golden Q&A, result-set     │  data correctness over time
            │     graded, Phoenix experiments (05, 10)       │  (LLM-as-judge + golden SQL)
            ├───────────────────────────────────────────────┤
            │  3. E2E SPEC-CONFORMANCE — full Docker stack,  │  "does it behave per spec?"
            │     Playwright(MCP) through the UI             │  trust + security invariants
            ├───────────────────────────────────────────────┤
            │  2. COMPONENT / INTEGRATION — one component +  │  real store, no full stack
            │     its real store (ClickHouse/neo4j/CB/Redis) │
            ├───────────────────────────────────────────────┤
many, fast  │  1. UNIT — pure deterministic logic, no I/O    │  the security/correctness primitives
            └───────────────────────────────────────────────┘
```

## Layer 1 — Unit (pure logic, no I/O)

The deterministic primitives that guard hard boundaries. Each gets exhaustive positive **and adversarial/negative** cases:

| Unit under test | Decision | Must prove |
|---|---|---|
| **Column-provenance extraction** (sqlglot wrapper) | D62/D57/D44 | qualified `(table,column)` USES set from SQL + schema; derived `AVG(gross_pay)` is captured; **unparseable → no provenance (fail-closed)** |
| **Scope subset check** | D44/D57 | provenance ⊄ scope ⇒ reject (live) / drop (replay); subset ⇒ allow |
| **Grain-integrity assertion** | D56 | rowcount `== COUNT(DISTINCT grain)`; measure agg matches declared `{agg, defined_over}`; **wrong-grain result ⇒ FAIL** (the fan-out canary) |
| **Canonical dedup key** | D48/D62 | same-semantics/different-text ⇒ same key; different semantics (gross vs net, period filter) ⇒ different key; **parse fail ⇒ no hard key (fail-soft)** |
| **Slot resolvers** (per-type) | D41/D49 | enum/entity/period/as_of_date resolution; multi-match & fuzzy NL ⇒ `askUser` signal, **never an LLM call** |
| **Template AST rewrite** | D35 | parameterization plan ⇒ safe `{slot}` template; injection attempts neutralized; unrewritable ⇒ fail-to-review |
| **`when` predicate evaluator** | D32/D59 | declarative predicate semantics; entity-valued predicate ⇒ rejected as non-agnostic |
| **History budget / compaction** | D46/D50 | filter-before-compact ordering; SQL preserved verbatim; preview-only + `truncated` flag |
| **Leakage gate (regex/NER unit)** | D58 | entity-detection cases incl. non-Western names / word-like names (the correlated-miss tail) |

## Layer 2 — Component / Integration (one component + its real store)

Run against a **real** containerized store, not the full stack:

- **MCP data-plane** (vs. ClickHouse): read-only enforced (INSERT/UPDATE/DELETE/DDL/table-functions blocked), row caps + time limits, **scope rejection by SQL parse (D57)**, `getTableSchema` returns **live introspection only (D78)**.
- **Runtime catalog overlay** (vs. catalog + MCP): the runtime performs `introspection ⨝ catalog overlay` incl. the uncatalogued-table structural-only path (D53/D78) — a **runtime** component test, not an MCP one.
- **`runBlueprint` executor** (vs. ClickHouse + neo4j): topo-sort + parallel branches; **scalar→typed param / table→scratch (D59a)**; approval gate references upstream-only (D59b); **pause checkpoint + CAS-exactly-once resume (D45)**; verification gate runs + falls back on failure (D56).
- **Learning loop** (vs. neo4j/Redis/provenance store): each stage triage→extractor→static-validate→**leakage gate**→dedup→writers; **idempotency by `content_hash`**; **single-writer-per-`canonical_key` race (D48)**; knowledge ⇒ review inbox, **not retrievable until approved (D58a)**; `LEARNING_ENABLED=false` halts write-back (D58c).
- **Retrieval** (vs. neo4j): embed→recall→**scope pre-filter drops out-of-scope blueprints**→rerank→top-3 thin cards; both vector corpora served from neo4j (D60).
- **Session store** (vs. Couchbase): `SESSION_TTL` expiry; per-result column-provenance persisted (D44); pause checkpoint CAS flag.

## Layer 3 — E2E spec-conformance (Docker stack + Playwright MCP)

The headline layer: a `docker-compose` stack with **every component** wired, seeded with a deterministic fixture, driven through the **real UI by Playwright (MCP)**. This is the executable form of "everything functions according to the spec."

### Stack topology (docker-compose)

```
clickhouse (seeded HR warehouse + scratch db) · neo4j (seeded blueprints + knowledge vectors)
couchbase · redis · phoenix · agent-runtime · ui · privileged-ingestion side-channel
```

### Fixture (deterministic, seeded)

- A small HR warehouse: `employee_info`, `payroll_fact` (**multi-component pay rows** so fan-out is reproducible), `time_attendance`; a Semantic Catalog with grain declarations.
- Seeded blueprints: a correct `avg_salary_by_dept`; a **deliberately wrong-grain** blueprint (negative fixture for the D56 gate); a composite/approval-gate blueprint.
- Seeded knowledge docs + a user with **narrow (no-payroll)** scope and one with full scope.

### Conformance scenarios (each maps to a flow/decision)

| Scenario | Asserts | Spec ref |
|---|---|---|
| **Ask → fast path** | answer + SQL shown + "Using blueprint…" chip with filled slots; **progress events stream** | 08, D61 |
| **No-silent verification** | the verification gate runs before the answer; **the wrong-grain fixture is caught/falls back, never returned silently** | D56 |
| **Ask → clarify** | ambiguous query renders a clarification chip; answer resumes the turn | 06, 08 |
| **Scope denial** | no-payroll user asks payroll ⇒ graceful "needs payroll access", **no data**; out-of-scope blueprint **not surfaced** in thin cards | D57, 06 |
| **Mid-session scope narrowing** | scope narrows ⇒ previously-shown payroll context **no longer replayed** | D44 |
| **Upload → join** | CSV upload → column-mapping step → scratch join answer | 07 |
| **Correction → learning** | a correction is captured to the trail; offline ⇒ a candidate appears; **`LEARNING_ENABLED=false` ⇒ no write-back** | 05, 08, D58c |
| **Knowledge human-gate** | a `global_knowledge` candidate is **not** returned by `searchKnowledge` until approved in the review inbox; approve ⇒ retrievable | D58a |
| **Budget-cap pause** | a long loop ⇒ "continue / refine / stop"; **continue grants a fresh window** | D47, D55 |
| **Pause/resume durability** | approval pause ⇒ **restart the runtime container** ⇒ turn resumes cleanly at `awaiting_node` | D45 |
| **Observability + PII** | per-turn spans reach Phoenix; **no cell values / JWT in spans**; leakage gate emits a `GUARDRAIL` span | 10, D25 |
| **Parser fail-closed** | a query the parser can't parse for scope is **rejected, not run** | D63 |

### Determinism of the LLM in e2e

The agent uses a real LLM (OpenAI — D71), so e2e must not assert exact NL. Two modes:
- **CI (deterministic):** record/replay LLM interactions (cassette-style) + assert on **structural/behavioral invariants** (SQL shape, chips present, scope denied, span emitted), never exact prose.
- **Nightly (live):** run the same suite against the live model to catch drift; grade with the Layer-4 result-set/LLM-judge harness rather than string equality.

## Layer 4 — Eval / Canary (already specified)

The data-correctness layer from [05](05-memory-and-learning.md) §Evaluation + [10](10-observability.md):
curated **golden Q&A with golden SQL**, graded by **result-set comparison + LLM-as-judge**, run as
**Phoenix experiments / production canaries**. **Golden replay** doubles as blueprint regression
(D36). This complements Layer 3: Layer 3 proves the system *behaves* per spec; Layer 4 proves the
*answers stay correct* over time. The Phase-0 session corpus seeds the canary set.

## CI wiring

- **Per-PR (fast):** Layers 1–2. Catalog PRs additionally run the D53 CI (schema lint + `explainQuery` dry-run against the patched catalog).
- **Nightly + release (slow):** Layer 3 (Docker + Playwright, both deterministic and live modes) and Layer 4 canaries.
- The **`system.query_log.columns` oracle** (D62) runs as a parser-accuracy job: replay real queries, diff parser-extracted columns vs. engine-reported ⇒ track the D63 false-reject rate.

---

**Status:** Locked (strategy); concrete fixtures + cassette tooling Partial (need the runtime/UI to exist — sequence after Phase-0 build)
**Open questions:**
- Cassette/record-replay tool for deterministic LLM e2e (VCR-style vs. a scripted-model double).
- Playwright(MCP) test-authoring conventions + selectors contract with the UI.
- Fixture warehouse size/shape + the canonical grain-edge-case set.
- Which conformance scenarios gate a release vs. run informationally.
