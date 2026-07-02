# neo4j Corpus Schema + `Neo4jVectorIndex` — Retrieval Slice 2 Design (D60/D86)

**Status:** Proposed (design only — no code written this pass).
**Owner seam:** the neo4j-native realization of the `VectorIndex` protocol shipped in Slice 1
(D86) + the offline corpus write path + the Layer-2 neo4j infra. Builds *on top of* the
retrieval pipeline; does not redesign it.
**Author's discipline:** this pass wrote exactly this one file. Every code/schema/infra change it
recommends (`retrieval/vector_index.py`, a new `retrieval/corpus_loader.py`, `app.py`, `config.py`,
`pyproject.toml`, `docker-compose.integration.yml`, seed fixtures, Layer-2 tests, DECISIONS/
OPEN-QUESTIONS/TRACEABILITY/04/09/WORKLOG) is listed in §9 "Doc/code updates on completion" for the
implementing developer — none were made here.

---

## 0. Ground truth this design relies on (verified by reading code + docs, not assumed)

- **The recall seam already exists and is frozen (D86).** `retrieval/vector_index.py` defines
  `VectorIndex.recall(*, query_vector: list[float], kind: str, k: int) -> list[Candidate]`,
  order-preserving by descending similarity, each `Candidate.score` set to the recall similarity so
  the "no reranker" degrade path keeps a meaningful order. Slice 1 shipped `FakeVectorIndex` only and
  states in its own docstring that `Neo4jVectorIndex` "is Slice 2 … deliberately NOT built here".
  **This design fills exactly that stub — a drop-in behind the existing protocol, nothing else in the
  pipeline changes.**
- **The `Candidate` contract is the load-bearing interface — and `uses` is `frozenset[str]`, NOT
  tuples.** `retrieval/models.py`:
  ```python
  Candidate(id: str, kind: Literal["blueprint","knowledge"], text: str,
            uses: frozenset[str] | None, payload: dict[str, Any], score: float | None)
  ```
  `uses` is a `frozenset[str]` of **exact dotted `"database.table.column"` strings**, `None` for
  knowledge. (The Slice-2 brief's `frozenset[tuple[str,str]]` is a mis-statement — the code and the
  Slice-1 design §3.2 both say `frozenset[str]`. `Neo4jVectorIndex` MUST return dotted strings.)
- **The scope-filter format is a byte-exact string subset — this dictates how USES is stored.**
  `retrieval/scope_filter.py` keeps a blueprint iff `candidate.uses <= column_scope` (plain set
  subset; `None` → drop fail-closed; empty scope → allow-all, D80). `column_scope` is a
  `frozenset[str]` whose members are built in `context/scope_filter.py` as `f"{db_table}.{column}"`
  where `db_table` is itself `"database.table"` — so a scope key is `"database.table.column"`
  (≥3 dotted parts; the Slice-1 live test uses `"w.payroll.overtime"`). **The strings
  `Neo4jVectorIndex` returns in `Candidate.uses` must byte-match this format or the scope pre-filter
  silently drops every blueprint.** This is the single most important schema constraint.
- **The store is settled: neo4j-native, both corpora (D60).** neo4j hosts **both** blueprint intent
  embeddings **and** global-knowledge embeddings; the standalone vector store was removed. D60 also
  fixes the recall discipline: "`USES` sets are **precomputed at creation time**, so scope
  pre-filtering is a **set-subset check, not live traversal**." That sentence is the schema
  decision for USES (§1.3) — the transitive set is a stored value read with the node, not a
  traversal on the hot path.
- **Embedding is 768-dim, single fixed model, same model offline + online (parity).**
  `model/embedding_client.py`: `HttpEmbeddingClient.embed` POSTs `{"input_text":[...]}` → bare
  `[[float,...]]`, 768-dim all-mpnet-base-v2; `model` is span metadata only (endpoint serves one
  fixed model). The corpus is embedded **offline at write time** through the *same* endpoint; the
  question is embedded at turn time. Cosine recall is only meaningful if both used the same model —
  the schema must stamp the model id per node so recall can refuse a mismatched corpus (§2.3).
- **Config already carries the neo4j knobs (unwired).** `config.py` has `neo4j_url`,
  `neo4j_username`, `neo4j_password` (all `""` → "vector index unavailable (Slice 2)") and the full
  `retrieval_*` tunable set. `app.py` wires `HttpEmbeddingClient` only when `embedding_api_url` is
  set and passes `retrieval=None` today (Phase-0 parity). **Slice 2's app-wiring job is the neo4j
  analogue: construct `Neo4jVectorIndex` + `RetrievalPipeline` only when `neo4j_url` is configured,
  else leave `retrieval=None`.**
- **Degrade-not-fail is the law (D86 + D85).** `recall` returning `[]` is already the pipeline's
  "index unavailable/empty" degrade — an empty corpus never fails the turn; the pipeline emits a
  shape-only `retrieval.degraded` span. `Neo4jVectorIndex` therefore **catches every driver/query
  failure and returns `[]`**; it never raises into the pipeline.
- **The blueprint model is richer than the retrieval projection (04).** A full blueprint carries
  `composes` (DAG), `sql_template`, `resolves`, `uses_rules`, `canonical_key`, `drift_status`,
  `validated_against_catalog_sha`, `status`, `hit_count`. Slice 2 stores **only the fields recall +
  scope-filter need** (id, intent, slots_summary, embedding, uses, model, and the lifecycle/
  provenance stubs) — the full DAG (needed by `getBlueprint`/`runBlueprint`) is a **reserved,
  additive** extension (§1.5), explicitly out of scope (§7).
- **Layer-2 conventions are fixed.** Integration tests skip-guard on a `*_TEST_URL`/`*_TEST_URI`
  env var (`MCP_TEST_URL`, `EMBEDDING_TEST_URL`, `RERANKER_TEST_URL`); `docker-compose.integration.yml`
  already stands up ClickHouse + token + MCP + embedding(18003) + reranker(18004) + Couchbase, each
  with a healthcheck. The Slice-1 live test `tests/integration/test_retrieval_pipeline_live.py` is the
  exact template Slice 2 extends (swap `FakeVectorIndex` → `Neo4jVectorIndex`).

---

## 1. Q1 — Graph schema

### 1.1 Node labels + properties

**`:Blueprint`** — the retrieval projection of a blueprint (full DAG reserved, §1.5).

| Property | Type | Slice-2 role | Notes |
|---|---|---|---|
| `id` | string | **key** | uniqueness constraint; the `Candidate.id` |
| `intent` | string | recall text + `payload.intent` | the embedded NL description (04) |
| `slots_summary` | string | `payload.slots_summary` | short derived string, stored at write (03 thin card) |
| `intent_embedding` | list\<float\> (768) | **vector index** | offline-embedded intent (§3) |
| `embedding_model` | string | **parity stamp** | e.g. `all-mpnet-base-v2`; recall refuses mismatch (§2.3) |
| `uses` | list\<string\> | **scope pre-filter** | transitive `"db.table.column"` set, denormalized (§1.3) |
| `status` | string | lifecycle | `candidate`\|`validated`; Slice-2 seeds `validated` |
| `drift_status` | string | lifecycle (reserved) | `clean`\|`suspect` (D43); Slice-2 seeds `clean`, unused at recall |
| `catalog_sha` | string | provenance | `validated_against_catalog_sha` (D84 subtree SHA) |
| `created_by` | string | provenance (reserved) | `seed` in Slice 2; Track B writes the writer identity |
| `created_at` | datetime | provenance (reserved) | write timestamp |
| `canonical_key` | string | dedup (reserved) | D48; **not** written/enforced in Slice 2 (Track-B concern) |
| `hit_count` | int | lifecycle (reserved) | D48/04; seeded 0, not incremented in Slice 2 |

**`:KnowledgeChunk`** — one global-knowledge chunk (entity-agnostic, 03/D60).

| Property | Type | Slice-2 role | Notes |
|---|---|---|---|
| `id` | string | **key** | uniqueness constraint |
| `title` | string | `payload.title` / `KnowledgeHit.title` | may be null |
| `text` | string | recall text + `payload.chunk` | the embedded chunk |
| `text_embedding` | list\<float\> (768) | **vector index** | offline-embedded chunk |
| `embedding_model` | string | **parity stamp** | same discipline as blueprints |
| `doc_id` | string | `payload.doc_id` | source document grouping |
| `status`, `created_by`, `created_at` | — | provenance (reserved) | Slice-2 seeds `validated`/`seed`/now |

Rationale for the reserved lifecycle/provenance columns: **Track B writes must not require a schema
migration.** D43 lifecycle (`status`/`drift_status`), D48 dedup (`canonical_key`/`hit_count`), and
"who wrote it" provenance (`created_by`) are all *declared now, populated trivially by the seed
loader, and simply not read by the recall path*. This is the cheapest form of forward-compatibility:
columns cost nothing until queried.

### 1.2 Why per-corpus vector indexes, not one label with a `kind` property

Blueprints and knowledge get **separate labels + separate vector indexes**, and `recall(kind=…)`
selects the index by name (§2.2). Alternative — one `:CorpusItem` label with a `kind` property and a
single index filtered by `WHERE node.kind = $kind` — was rejected: neo4j's
`db.index.vector.queryNodes` returns the top-k *then* you filter, so a post-`WHERE` on `kind` would
shrink the result below `k` and force over-fetching. Two indexes make each corpus's recall a clean
top-k with no post-filter, and the two corpora have genuinely different property sets anyway.
Trade-off: two index definitions instead of one. Accepted — it matches the two-corpus recall the
pipeline already does (blueprints and knowledge recalled separately, `pipeline.py`).

### 1.3 USES — stored as a denormalized node property, with edges reserved for the graph

**Decision: the transitive USES set is a `uses: list<string>` property on the `:Blueprint` node —
this is what `recall` returns, byte-matching the `"database.table.column"` scope format, with zero
traversal.** Additionally, Slice 2 *writes* (but does **not** read at recall) the graph edges
`(:Blueprint)-[:USES]->(:Column {key})` and `(:Column)-[:OF_TABLE]->(:Table {key})` so the D60
graph capability exists from day one.

Why property-first: D60 is explicit that USES sets are **precomputed at creation time** so scope
pre-filtering is a **set-subset check, not live traversal**. The scope filter (`candidate.uses <=
column_scope`) is a Python `frozenset` subset over exact strings. Serving that from a stored
`list<string>` property makes `recall` a **single-node read** — the `uses` list comes back with the
node in the same `queryNodes` row, no per-candidate `MATCH …-[:USES]->…` fan-out over 30 candidates
on the hot path. Returning `frozenset(node.uses)` is the whole mapping.

Why also write edges: D60's *justification* for paying neo4j's operational weight is genuine graph
traversal — "what's downstream of table/rule X" (D38 re-flag scan), blueprint-to-blueprint
composition. The `:Column`/`:Table` nodes + `:USES`/`:OF_TABLE` edges are the substrate for that.
They are the **transitive closure written as edges**; the `uses` property is a **denormalized cache
of that same closure**. They cannot drift because they are written in the **same transaction** from
the same computed set, and a blueprint's USES is immutable-at-write (re-validated only on a schema
change, 04 §Scope edges / D84).

Trade-off named: this duplicates the USES set (property + edges), so the writer owns keeping them
consistent (one txn, one source set). The alternative — edges only, `MATCH` the closure at recall —
was rejected because it puts a traversal on the latency-critical hot path that D60 explicitly says
should be a precomputed set check. **Minimal-slice option, if the developer wants to cut scope:**
the `uses` property is *required* for Slice 2; the edges are *recommended now* (trivial incremental
write, locks the D60 graph shape) but reading them is out of scope, so deferring the edge writes to
the first graph-traversal consumer is a defensible reduction — call it out in the PR if taken.

### 1.4 Vector indexes, constraints, DDL

```cypher
// Uniqueness constraints (also create backing range indexes on the key)
CREATE CONSTRAINT blueprint_id     IF NOT EXISTS FOR (b:Blueprint)      REQUIRE b.id  IS UNIQUE;
CREATE CONSTRAINT knowledge_id     IF NOT EXISTS FOR (k:KnowledgeChunk) REQUIRE k.id  IS UNIQUE;
CREATE CONSTRAINT column_key       IF NOT EXISTS FOR (c:Column)         REQUIRE c.key IS UNIQUE;
CREATE CONSTRAINT table_key        IF NOT EXISTS FOR (t:Table)          REQUIRE t.key IS UNIQUE;

// Native vector indexes (neo4j 5.15+ DDL; dim 768, cosine — §7 OQ-N1)
CREATE VECTOR INDEX blueprint_intent_vec IF NOT EXISTS
  FOR (b:Blueprint) ON (b.intent_embedding)
  OPTIONS { indexConfig: { `vector.dimensions`: 768, `vector.similarity_function`: 'cosine' } };

CREATE VECTOR INDEX knowledge_text_vec IF NOT EXISTS
  FOR (k:KnowledgeChunk) ON (k.text_embedding)
  OPTIONS { indexConfig: { `vector.dimensions`: 768, `vector.similarity_function`: 'cosine' } };
```

- **Dimension 768** — fixed by the mock/prod embedder (all-mpnet-base-v2).
- **Similarity `cosine`** — interim default (§7 OQ-N1). Rationale: mpnet embeddings are conventionally
  used with cosine, and `FakeVectorIndex` + `composite/ranking.cosine` (the Slice-1 fake and the
  `resolveValues` path) both rank by cosine, so choosing cosine keeps Layer-1 and Layer-2 ranking
  semantics identical. A metric change requires a reindex (offline, rare) — accepted.
- The uniqueness constraints give the loader a MERGE-by-id target (§3.2 idempotency) and are the
  `:Column`/`:Table` upsert keys for the edge writes.

### 1.5 Reserved extension: the full blueprint DAG (NOT built in Slice 2)

`getBlueprint`/`runBlueprint` need the full DAG (`composes`, `sql_template`, `resolves`,
`uses_rules`). The schema reserves room additively: a `:Blueprint` node later grows either JSON-typed
properties (`composes`, `sql_template`) or child `(:Blueprint)-[:COMPOSES]->(:BlueprintNode)` nodes,
with `(:Blueprint)-[:APPLIES]->(:Rule)` for `uses_rules`. **None of this is written or read in Slice
2** — the retrieval projection (§1.1) is a strict subset, so adding the DAG later is a pure superset
write, no migration of existing properties.

---

## 2. Q2 — `Neo4jVectorIndex`

### 2.1 Driver: official `neo4j` **async** driver

Use `neo4j.AsyncGraphDatabase.driver(url, auth=(user, password))`. Async because the entire runtime
is async (`await index.recall(...)`), the pipeline must not block the event loop, and it mirrors the
`HttpEmbeddingClient`/`HttpRerankerClient` async posture. Add `neo4j>=5.26` to `pyproject.toml`
(§9). The driver holds a **long-lived connection pool** — one instance per process, created at app
startup, reused across turns, closed at shutdown (§2.4).

### 2.2 Query shape → `Candidate` mapping

One parameterized read per corpus, selecting the index by name:

```cypher
// blueprints  (index = 'blueprint_intent_vec', label = Blueprint)
CALL db.index.vector.queryNodes($index_name, $k, $query_vector)
YIELD node, score
WHERE node.embedding_model = $expected_model          // parity guard (§2.3)
RETURN node.id AS id, node.intent AS text, node.slots_summary AS slots_summary,
       node.uses AS uses, score
ORDER BY score DESC
```

```cypher
// knowledge  (index = 'knowledge_text_vec', label = KnowledgeChunk)
CALL db.index.vector.queryNodes($index_name, $k, $query_vector)
YIELD node, score
WHERE node.embedding_model = $expected_model
RETURN node.id AS id, node.text AS text, node.title AS title,
       node.doc_id AS doc_id, score
```

Mapping (the whole point of the class):

- **blueprint** → `Candidate(id, kind="blueprint", text=intent, uses=frozenset(row["uses"]),
  payload={"intent": text, "slots_summary": slots_summary}, score=score)`. `frozenset(row["uses"])`
  is the byte-exact scope-format set — nothing is re-derived.
- **knowledge** → `Candidate(id, kind="knowledge", text=text, uses=None,
  payload={"title": title, "chunk": text, "doc_id": doc_id}, score=score)`.

`kind` selects `(index_name, label)` internally: `{"blueprint": "blueprint_intent_vec",
"knowledge": "knowledge_text_vec"}`. An unknown `kind` → `[]` (defensive, never raise).

**Note on neo4j cosine score range vs. the pipeline.** neo4j's cosine similarity score is normalized
to `(1 + cosine)/2 ∈ [0,1]`, whereas `FakeVectorIndex` returns raw cosine `∈ [-1,1]`. This does **not
matter**: `recall` only guarantees *descending order* (both are monotonic in cosine), and
`Candidate.score` from recall is only used as the *degrade-path* order when rerank is off — order is
preserved either way. Documented so no one "fixes" a non-bug.

### 2.3 Model-parity guard at recall

The class is constructed with `expected_model` (the online embedder's model id from settings). The
`WHERE node.embedding_model = $expected_model` clause means a corpus embedded with a *different*
model never comes back — recall returns `[]` for that corpus rather than garbage neighbours in a
mismatched vector space (Slice-1 design §2/§11 constraint). When the index is non-empty but the guard
drops everything (total model mismatch → 0 rows), emit a **shape-only warn span attribute**
`retrieval.model_mismatch=true` so the misconfiguration is observable (D24/D25 — no text). This is a
**degrade, not a raise**: empty recall flows into the pipeline's existing empty-corpus degrade (D86).
Write-time is stricter (§3.3): the loader refuses to *mix* models into one index.

### 2.4 Connection lifecycle + wiring (only when configured)

- **Construction (app.py):** mirror the `HttpEmbeddingClient` gate exactly —
  ```
  if neo4j_url and embedding_client is not None:
      vector_index = Neo4jVectorIndex(url, auth=(user, password),
                                      expected_model=embedding_model, tracer=tracer, ...)
      retrieval = RetrievalPipeline(embedding_client=embedding_client,
                                    reranker=<http reranker if configured>,
                                    vector_index=vector_index, user_memory=NullUserMemoryProvider(),
                                    ...settings...)
  else:
      retrieval = None      # D86 Phase-0 parity — unchanged today's behaviour
  ```
  Retrieval needs **both** an embedder (to embed the question) **and** a store; absent either →
  `retrieval=None`. `retrieval_enabled=False` still forces `None` (existing `create_app` gate).
- **Pool:** the async driver is created once and held on the `Neo4jVectorIndex`. Add an async
  `close()` and call it from the FastAPI **shutdown/lifespan** hook (a new coordination point in
  `app.py`, §9). Connection-acquisition + per-query timeouts come from settings (§9 new
  `neo4j_timeout_seconds`, default 10s, matching the embedding/reranker timeout convention).
- **Failure modes → `[]` (never raise):** unreachable host, auth failure, driver `ServiceUnavailable`,
  query error, timeout — all caught broadly in `recall`, logged server-side with traceback, and
  returned as `[]`. This is the "index unavailable" degrade the pipeline already handles (observed in
  Slice 1). Auth (`neo4j_username`/`neo4j_password`) is its own secret in `RuntimeSettings`,
  `.env`-backed and gitignored — same posture as Couchbase creds; never mixed with the warehouse JWT
  (D5/D25).

---

## 3. Q3 — Corpus loading (offline, minimal Slice-2 form)

### 3.1 A loader module + a thin script

`src/data_agent/runtime/retrieval/corpus_loader.py` — a reusable async function
`load_corpus(driver, embedding_client, blueprints, knowledge, *, model_id) -> LoadReport` that:
1. embeds every blueprint `intent` and knowledge `text` via the **real `HttpEmbeddingClient`**
   (same D71 endpoint the online path uses — parity by construction);
2. writes nodes with the embedding + `embedding_model=model_id` stamp + the `uses` list property;
3. upserts `:Column`/`:Table` nodes and `:USES`/`:OF_TABLE` edges from each blueprint's transitive
   set (§1.3);
4. is **idempotent** — `MERGE (b:Blueprint {id})` then `SET` properties, so re-running the loader
   updates in place, never duplicates.

`scripts/seed_neo4j_corpus.py` is a ~20-line CLI wrapper (read fixtures → build clients from settings
→ `load_corpus`) used by the Layer-2 seed and for local demo. Keeping the write logic in a Python
module (not a raw Cypher dump) means the seed's node shape **cannot drift** from what `recall` reads —
same code writes what the tests read back.

### 3.2 Seed source in Slice 2: hand-authored fixtures (Track B is later)

Seed blueprints + knowledge come from **hand-authored YAML/JSON fixtures** under
`tests/fixtures/corpus/` (blueprints) and `.../knowledge/`. Each blueprint fixture carries
`{id, intent, slots_summary, uses: [<db.table.column>...], status: validated, catalog_sha}`; each
knowledge fixture `{id, title, text, doc_id}`. **The `uses` strings are authored to byte-match the
Layer-2 HR-warehouse scope keys** (same warehouse the ClickHouse seed + Semantic Catalog describe), so
scope filtering against a real JWT scope is genuinely exercised (§4). Track B (learning-loop-authored
blueprints, extractor → leakage gate → dedup → promotion) is **out of scope** (§7) — Slice 2's corpus
is a small, curated, trusted seed.

### 3.3 Parity enforcement — strict at write, degrade at read

- **Write (strict):** the loader stamps `embedding_model` on every node with the id it actually
  embedded with, and **refuses to write two different model ids into one index** (a `LoadReport`
  error / non-zero exit) — write-time is the right place to be strict because a mixed index is
  silently broken.
- **Read (degrade):** `recall` filters by `expected_model` and returns `[]` + warn on total mismatch
  (§2.3) — never fails the turn (D86).

A model swap therefore means a **full reindex** (re-run the loader with the new endpoint) — accepted;
it is offline and rare (Slice-1 design §7 trade-off, restated).

### 3.4 What the loader does NOT do (Track-B boundary)

No `canonical_key` computation, no D48 single-writer-per-key serialization, no dedup, no leakage
gate, no promotion lifecycle. The loader is a **bulk idempotent upsert of a trusted seed**, not the
Track-B write pipeline. `canonical_key`/`hit_count`/`status` transitions are Track-B's job against the
same schema.

---

## 4. Q4 — Layer-2 infra

### 4.1 neo4j service in `docker-compose.integration.yml`

```yaml
  neo4j:
    image: neo4j:5.26              # LTS; native vector index GA (5.15+) — §7 OQ-N1
    container_name: l2-neo4j
    environment:
      NEO4J_AUTH: "neo4j/testpassword"          # DEV/TEST only, ephemeral
      NEO4J_server_memory_pagecache_size: "512M"
    ports:
      - "7474:7474"   # http/browser
      - "7687:7687"   # bolt
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:7474 >/dev/null 2>&1 || exit 1"]
      interval: 5s
      timeout: 5s
      retries: 30
      start_period: 20s
```

Rationale for **5.26**: it is the current LTS and native vector indexes (`CREATE VECTOR INDEX` DDL +
`db.index.vector.queryNodes`) are GA (introduced 5.13, DDL 5.15). No APOC/GDS plugin needed — native
vector search is core. Community edition suffices (single default `neo4j` database).

### 4.2 Seeding the corpus for tests

The Layer-2 test session **seeds via the loader** (§3.1) against the live neo4j + embedding-api
services — a session-scoped pytest fixture that (a) creates constraints + vector indexes, (b) calls
`load_corpus` with the fixtures, (c) waits for the vector index to come online
(`SHOW INDEXES` state `ONLINE`, or `db.awaitIndexes`). Seeding in Python (not a Cypher dump) reuses
the exact write path under test. The neo4j service needs no bespoke init container — the fixture owns
schema + seed, torn down with `-v`.

### 4.3 Env-guarded test suite

Skip-guard on **`NEO4J_TEST_URI`** (bolt URL), consistent with `EMBEDDING_TEST_URL`/
`RERANKER_TEST_URL`/`MCP_TEST_URL`. The end-to-end pipeline test additionally requires
`EMBEDDING_TEST_URL` (+ `RERANKER_TEST_URL` for the rerank leg) to embed the query/seed:

```
pytestmark = pytest.mark.skipif(
    not os.environ.get("NEO4J_TEST_URI"),
    reason="Requires a live neo4j (set NEO4J_TEST_URI).",
)
# run:
docker compose -f docker-compose.integration.yml up -d --wait neo4j embedding-api reranker-api
NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j NEO4J_TEST_PASSWORD=testpassword \
EMBEDDING_TEST_URL=http://localhost:18003/embed RERANKER_TEST_URL=http://localhost:18004/rerank \
    uv run pytest tests/integration/test_neo4j_vector_index_live.py -v
```

`uv run pytest` with no live stack stays fully green (every neo4j test skipped).

### 4.4 What the Layer-2 tests must prove

1. **Recall → Candidate mapping.** Seed a blueprint; embed a semantically-matching query; recall
   returns a `Candidate` with correct `id`, `text=intent`, `payload.slots_summary`, and — the
   load-bearing assertion — **`uses` is a `frozenset[str]` whose members byte-match
   `"database.table.column"`** (assert the exact strings, e.g. `{"w.payroll.overtime"}`).
2. **Scope filtering against real stored USES.** Recall a blueprint whose stored `uses` ⊄ a narrow
   `column_scope` → `filter_blueprints_by_scope` drops it; a scope ⊇ its `uses` → kept. Proves the
   `uses` list round-trips through neo4j as exact strings the *unmodified Slice-1 scope filter*
   accepts — this is the neo4j↔scope-format contract test.
3. **End-to-end pipeline, real everything.** `HttpEmbeddingClient` + `Neo4jVectorIndex` +
   `HttpRerankerClient` → the overtime blueprint outranks an irrelevant one for an overtime question
   (the Slice-1 `test_retrieval_pipeline_live.py` scenarios, `FakeVectorIndex` swapped for the real
   store). Confirms the drop-in changes nothing observable but the store.
4. **Parity guard.** Seed with `embedding_model="X"`, construct `Neo4jVectorIndex(expected_model="Y")`
   → recall returns `[]` (+ `retrieval.model_mismatch` span attr), never cross-space neighbours.
5. **Degrade.** Point `Neo4jVectorIndex` at an unreachable URI → `recall` returns `[]`, the pipeline
   returns an empty `RetrievedContext`, the turn proceeds (D86 degrade, live-proven).

Layer-1 additions are minimal (the pipeline/scope/mapping logic is already Slice-1-green with
`FakeVectorIndex`); Slice 2 may add a unit test of the row→`Candidate` mapping with a fake neo4j
`Record` to lock the `frozenset(uses)` conversion without infra.

---

## 5. Q5 — Trust boundary + budget (carried from Slice 1)

### 5.1 §8.1 trust-boundary reconfirmation

**In Slice 2 the corpus is still hand-seeded (§3.2) — content-trusted.** The D86 render posture
(retrieved text is *content-trusted*, *structure-sanitized* at render — control chars stripped,
per-field length caps, so corpus text cannot forge system-message structure) **remains exactly
sufficient**. Slice 2 changes the *store* (in-memory → neo4j) but not the *provenance* of the content
(curated seed, not learned). Therefore Slice 2 **does not relax** structural sanitization and **does
not add** content-level treatment.

**What changes when Track B lands (the trigger to revisit):** Track-B-written blueprints/knowledge are
*extracted from sessions* and thus *less trusted*. At that point the write-time **leakage gate** is
the primary content guarantee, and render may need **content-level** treatment (not just structural)
— e.g. because a learned intent string could carry adversarial instructions that structural
sanitization (which only stops *structure* forging) does not neutralize. This design flags Track B's
first corpus write as the point to re-open §8.1; Slice 2 itself carries no such content.

### 5.2 OQ-R8 — retrieved-block token budget: explicitly re-deferred, now quantified

Slice 1 deferred D46 budget accounting for the rendered block "to Slice 2 … couples to neo4j corpus
sizing." Now that we can see Slice 2's corpus, the decision is to **re-defer, with a quantified
bound** rather than build budget accounting against an unrepresentative size:

- **Why re-defer:** Slice 2's corpus is a *hand-seed of tens of items*, not the realistic Track-B
  corpus. Reserving/charging/trimming the block against `context/budget.py` now would tune against a
  size that will change by orders of magnitude when Track B populates the corpus. Premature.
- **Why it's safe to re-defer (the quantified part):** the rendered block is **hard-bounded and
  deterministic** regardless of corpus size — the pipeline cuts to `top_k_blueprints=3` thin cards +
  `top_k_knowledge=3` hits, and `render.py` caps each field (500 chars/card field, 2000/knowledge
  chunk). Worst case ≈ `3×(500 intent + 500 slots) + 3×(2000 chunk) ≈ 9,000 chars ≈ ~2–3k tokens` —
  a fixed ceiling that does **not** grow with the neo4j corpus. Unbounded prompt growth is therefore
  **not** a Slice-2 risk. Full D46 accounting (reserve the block, trim history under joint pressure)
  moves to the slice where the corpus is realistic (Track B / a dedicated budget slice). OQ-R8 stays
  open, now annotated with this bound.

---

## 6. Q7 — Open questions (safe interim defaults chosen — not blocking)

- **OQ-N1 vector similarity metric** → **cosine** (matches mpnet convention + Slice-1 `FakeVectorIndex`
  / `ranking.cosine`, so Layer-1↔Layer-2 ranking semantics are identical). A change needs an offline
  reindex. Revisit if recall quality on real traffic argues for dot/euclidean.
- **OQ-N2 neo4j version** → **5.26 LTS** (native vector index GA, community edition, no plugins).
- **OQ-N3 corpus versioning vs. D84 `catalog_sha`** → blueprints stamp `catalog_sha` =
  `validated_against_catalog_sha` (the D84 catalog-subtree SHA the fixture was authored against); a
  catalog bump → re-validate via the D43 conformance probe (**out of scope** for Slice 2, which only
  *stores* the field). Knowledge chunks are entity-agnostic and **not** catalog-bound, so they carry
  no `catalog_sha`; for reproducibility the loader may stamp a `source_sha` of the seed fixture. No
  `canonical_key`/dedup versioning in Slice 2 (Track B, D48).
- **OQ-N4 embedding_model default** → `all-mpnet-base-v2` (the mock/prod model id); the loader stamps
  it, recall's `expected_model` comes from `embedding_model` settings — they must match or recall
  degrades to empty.
- **OQ-N5 loader idempotency key** → `MERGE`-by-`id` (not `canonical_key`); re-running updates in
  place. Structural dedup is Track B.
- **OQ-N6 USES edges written vs. deferred** → **write them** (§1.3) to lock the D60 graph shape, but
  recall reads only the `uses` property; deferring the edge writes to the first traversal consumer is
  a defensible scope cut if flagged.
- **OQ-N7 neo4j database name** → default `neo4j` (community single-db); make it a settings field only
  if enterprise multi-db is adopted later.

---

## 7. Out of scope (stated explicitly)

- **Model-facing tools** `searchBlueprints` / `getBlueprint` / `searchKnowledge` — Slice 2 builds the
  *store + recall*; the tools that let the model pull are a separate step. (`getBlueprint` further
  needs the full DAG, §1.5, not stored here.)
- **`runBlueprint` execution + the D56 verify gate + D67 `resolve_via`** — Slice 3 (Slice-1 design
  §4.3). Needs the full blueprint DAG (`composes`/`sql_template`), which Slice 2 does not store.
- **Track B learning loop** — the extractor → leakage gate → dedup (D48) → promotion write pipeline,
  `canonical_key`, `hit_count` increments, `status`/`drift_status` transitions. Slice 2 seeds a
  trusted corpus by hand; Track B writes against the same (forward-reserved) schema later.
- **The user-memory store** — `NullUserMemoryProvider` stays (Slice-1 design §OQ-R4); no store built.
- **Rendered-block D46 budget accounting** (OQ-R8) — re-deferred with a quantified bound (§5.2).

---

## 8. Risks & trade-offs (named explicitly)

- **USES-string format skew is a silent scope-drop.** If a loader fixture authors `uses` in any format
  other than the exact `"database.table.column"` scope key, `candidate.uses <= column_scope` fails and
  *every* such blueprint is silently dropped by the scope pre-filter — no error, just empty retrieval.
  Mitigation: Layer-2 test #2 asserts the exact byte strings round-trip through neo4j and pass the
  unmodified Slice-1 filter; fixtures are authored against the same HR-warehouse scope keys the
  ClickHouse seed uses. **This is the highest-risk contract in the slice.**
- **Denormalized USES (property + edges) can drift** if a future writer updates one without the other.
  Mitigation: single-transaction write from one computed set; blueprint USES is immutable-at-write.
- **Model-parity misconfiguration degrades to *empty*, which looks like "no matches".** A wrong
  `embedding_model` setting silently yields empty recall. Mitigation: the `retrieval.model_mismatch`
  warn span makes it observable; the loader is strict at write. Trade: a swap requires a full reindex.
- **neo4j becomes a real production dependency for the retrieval path.** Mitigation: the D86 degrade
  path (`recall → []` → empty `RetrievedContext` → Phase-0-style turn) means an unreachable neo4j
  degrades gracefully, never fails the turn; `retrieval=None` when unconfigured keeps Phase-0 parity.
- **neo4j cosine score is `[0,1]`, fake is `[-1,1]`.** A non-issue (order-only contract, §2.2) —
  documented to prevent a spurious "fix".
- **Seeding depends on the embedding service being up** (the loader embeds at write time). Mitigation:
  the Layer-2 seed fixture depends on `embedding-api` healthy, same as the pipeline live test.

---

## 9. Doc/code updates on completion (NOT done this pass — for the implementing developer)

Because file discipline limited this pass to writing this one design doc, the following are
**recommended** edits to make when Slice 2 is built, not changes made now:

- **`src/data_agent/runtime/retrieval/vector_index.py`** — add `Neo4jVectorIndex` implementing the
  existing `VectorIndex` protocol (async neo4j driver, per-corpus `queryNodes`, row→`Candidate`
  mapping with `frozenset(uses)`, parity `WHERE`, broad-except → `[]`, `close()`); extend `__all__`.
- **`src/data_agent/runtime/retrieval/corpus_loader.py`** (new) — `load_corpus(...)` + `LoadReport`;
  constraints + vector-index DDL; MERGE-by-id upsert; `:Column`/`:Table` + `:USES`/`:OF_TABLE` edges;
  strict single-model-per-index write.
- **`scripts/seed_neo4j_corpus.py`** (new) — CLI wrapper: fixtures + settings → `load_corpus`.
- **`tests/fixtures/corpus/*.yaml`** (new) — hand-authored blueprint + knowledge seed, `uses` strings
  byte-matching the HR-warehouse scope keys.
- **`config.py` (`RuntimeSettings`)** — add `neo4j_timeout_seconds` (default 10.0); the
  `neo4j_url`/`neo4j_username`/`neo4j_password` fields already exist. Update the Slice-1 "Slice 2"
  comment to "wired".
- **`app.py`** — construct `Neo4jVectorIndex` + `RetrievalPipeline` when `neo4j_url` *and* an embedder
  are configured (else `retrieval=None`); add the async `close()` call to the FastAPI shutdown/lifespan.
- **`pyproject.toml`** — add `neo4j>=5.26` to `dependencies`.
- **`docker-compose.integration.yml`** — add the `neo4j` service (§4.1).
- **`tests/integration/test_neo4j_vector_index_live.py`** (new) — the five §4.4 proofs, skip-guarded on
  `NEO4J_TEST_URI` (+ embedding/reranker URLs for the e2e leg).
- **Docs** — **DECISIONS.md**: a new decision (candidate **D87**) for the neo4j corpus schema (USES =
  denormalized property + reserved edges; per-corpus vector indexes; cosine/768; model-parity stamp;
  lifecycle/provenance columns reserved for Track B). **OPEN-QUESTIONS.md**: close the "neo4j graph
  schema (node labels, edge types, indexes) — to be specified" stub (04 §Storage + OQ §Blueprints)
  and the Retrieval-§ "neo4j vector index + corpus schema (Slice-2 first task)" item; annotate OQ-R8
  with the §5.2 quantified bound. **04-blueprints.md**: replace the "Storage (neo4j) — stub" section
  with the schema (§1) and flip Status "neo4j schema Stub" → "Slice-2 retrieval projection Locked;
  full-DAG storage reserved". **09-infrastructure.md**: note the neo4j service is now stood up in
  Layer-2. **TRACEABILITY**: link D60/D86 → this design → the neo4j code. **WORKLOG**: a Slice-2 entry.
```
