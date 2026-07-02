# Retrieval Pipeline — Phase-1 Brick Design (D7/D8, on D71 custom embed+rerank)

**Status:** Proposed (design only — no code written this pass).
**Owner seam:** a new `src/data_agent/runtime/retrieval/` package + one integration point
in context assembly. Builds *on top of* the D71 model primitives, does not redesign them.
**Author's discipline:** this pass wrote exactly this one file. Every code change it recommends
(to `tracing.py`, `config.py`, `context/assembly.py`, `loop/agent_loop.py`, `app.py`,
`docker-compose.integration.yml`) is listed in §12 "Doc/code updates on adoption" for the
implementing developer — none were made here.

---

## 0. Ground truth this design relies on (verified by reading code + docs, not assumed)

- **The pipeline is spec'd, not invented.** `docs/03-context-and-retrieval.md` fixes the shape:
  `embed question → vector recall → SCOPE PRE-FILTER → cross-encoder rerank → pre-inject top-3
  blueprint thin cards + relevant global knowledge + user memory`. D7 (pipeline) and D8
  (progressive disclosure) lock it; D68 Phase-1 scope names it explicitly ("retrieval pipeline
  (D7/D8) on the custom (non-OpenAI) embedding + reranker API (D71)").
- **The vector store is already decided: neo4j-native (D60).** neo4j hosts **both** blueprint
  intent embeddings **and** global-knowledge embeddings; the standalone "vector index + RAG" store
  was removed. So this design does **not** pick a store — it defines a `VectorIndex` seam whose
  Phase-1 real implementation is neo4j, with an in-memory fake for Layer-1 and a swap seam if
  neo4j is unavailable for an early slice.
- **The embedding primitive exists and is being aligned to the real contract.**
  `model/embedding_client.py`: `EmbeddingClient` protocol `async embed(texts) -> list[list[float]]`,
  order-preserving, `EmbeddingError` on failure; `FakeEmbeddingClient` (Layer-1, scripted or seeded
  hash vectors, `fail=True` toggle); `HttpEmbeddingClient` (POST, settings-driven, manual
  `EMBEDDING` span). The developer is aligning it to the mock at `/Users/kalpeshmulye/Development/SQL/mocks/embedding_api`:
  `POST /embed {"input_text":[...]}` → bare `[[float,...],...]`, all-mpnet-base-v2, **768-dim**.
- **The reranker primitive is being added right now** as `model/reranker_client.py` — protocol +
  fake + http, settings-driven, unwired — against the mock at `.../reranker_api`:
  `POST /rerank {"query","documents"}` → `{"scores":[...]}`, **same order, higher=better, caller
  re-sorts**. This design consumes that protocol; it does not define its transport.
- **Context assembly today is history-only.** `context/assembly.py::ContextAssembler.assemble(
  session_id, column_scope, current_turn_index)` runs the D50 fixed order (load → D44 filter → D46
  budget/compact → render) and returns `AssembledContext(messages, ...)`. It **does not take the
  user's question** and does **no retrieval**. Retrieval is an additive pre-loop stage, not a
  rewrite of this pipeline.
- **Schema is on-demand only (03 §"Context assembled per turn").** `getTableSchema` (MCP, D83) is
  **not** pre-injected by retrieval. Retrieval pre-injects blueprint thin cards + knowledge + user
  memory only. Catalog/schema hints reach the model via the tool, not via this brick.
- **`resolveValues` set the pattern** for a runtime component over the custom embedding API:
  degrade-not-fail on embedder unavailability (D85), `EMBEDDING` span with text redacted (D25),
  pure ranking in a side module (`composite/ranking.py::cosine/rank`). This brick reuses that
  posture and can reuse `ranking.cosine`.
- **The loop already rebuilds context every round-trip** from the session store (D45
  statelessness). Retrieval output must therefore be a **deterministic function of (question,
  scope, corpus snapshot)** so a resume re-derives the same block — see §6.

---

## 1. Q1 — What is retrieved, from what corpora, and when

Three corpora, two delivery modes. The split between **runtime-internal pre-injection** and
**model-facing tools** is exactly the progressive-disclosure design (D8): cheap thin signals are
pushed for free before the loop; the model pulls full detail on demand.

| Corpus | Store | Pre-loop (runtime-internal enrichment) | Model-facing tool (pull) |
|---|---|---|---|
| **Blueprints** | neo4j vector idx (intent embeddings) + `USES` edges (D60) | **top-3 thin cards** `{id, intent, slots_summary}`, scope-pre-filtered (03 §Thin cards) | `searchBlueprints(query, k)` → reranked thin cards; `getBlueprint(id)` → full DAG (02) |
| **Global knowledge** | neo4j vector idx (D60) | **top-N reranked knowledge hits** (entity-agnostic) | `searchKnowledge(query)` → RAG hits (02) |
| **User memory** | user store (**not built** — 03 §"user store"; no code exists) | user prefs / entity defaults (personal, entity-bearing OK) | none (never a model tool; personal) |
| ~~Schema~~ | MCP (D83) | **NOT pre-injected** — on-demand only (03) | `getTableSchema` (existing) |

Precise answers to the sub-questions:

- **Model-facing tools:** `searchBlueprints`, `getBlueprint`, `runBlueprint`, `searchKnowledge`
  (02 §Knowledge plane). The first, second and fourth are *read/retrieval* tools that **reuse this
  brick's recall+rerank** over the blueprint / knowledge corpus. `getBlueprint` is a keyed fetch
  (no retrieval). `runBlueprint` is execution (separate brick, §4).
- **Runtime-internal context enrichment:** the pre-loop pass — thin cards + knowledge + user
  memory — is **injected by runtime code**, never a tool the model calls. It is the "top of
  funnel" that makes the tools rarely necessary.
- **When in the turn:** *before* the first model round-trip (03: "runs **before** the agent loop"),
  once per turn, on the user's raw question. It runs **alongside** the existing D50 history
  assembly, not inside it — a separate stage with its own span (§6). On a `resume`, retrieval
  re-runs on the original turn's question (deterministic; §6).
- **D44/D46/D50 relationship (important — do not conflate):** those decisions govern **session
  history** assembly (the trail). Retrieval is a **separate, additive** pre-loop stage that
  produces *new* system content. The one shared invariant is **scope**: retrieval's blueprint
  pre-filter (03 §Scope) is the read-path analogue of D44's trail filter — both keep the model from
  reasoning over anything outside `column_scope`.

---

## 2. Q2 — Pipeline shape (embed → recall → scope-filter → rerank → cut)

```
turn question q, column_scope S
   │
   ▼  EMBED (turn-time, once)         HttpEmbeddingClient.embed([q]) -> v_q   (768-dim)
   │
   ▼  RECALL (per corpus)             VectorIndex.recall(v_q, kind, k=recall_k)
   │     blueprints -> candidates[]   (each: id, kind, text=intent, uses:(tables,cols), payload)
   │     knowledge  -> candidates[]   (each: id, kind, text=chunk,  uses=None,          payload)
   │
   ▼  SCOPE PRE-FILTER (blueprints only, 03 §Scope / D60)
   │     drop bp whose transitive USES (tables,cols) ⊄ S   (set-subset, precomputed edges)
   │     knowledge is entity-agnostic (leakage gate guarantees it) -> not scope-filtered
   │
   ▼  RERANK (per corpus)             RerankerClient.rerank(q, [c.text ...]) -> scores[]
   │     re-sort desc by score (caller re-sorts — mock contract)
   │
   ▼  CUT                             blueprints -> top-3 thin cards (03: exactly 3)
   │                                  knowledge  -> top-N (default 3), optional score floor
   │
   ▼  ASSEMBLE RetrievedContext       {thin_cards[], knowledge_hits[], user_memory[]}
```

**What is embedded offline vs. at turn time (the load-bearing asymmetry):**
- **Offline / at write time:** blueprint `intent` strings and knowledge-doc chunks are embedded
  **when they are written** (blueprint creation / knowledge promotion — the Track B learning loop,
  or a one-shot indexing job before Track B exists) and stored as vectors **in neo4j** (D60).
- **At turn time:** only the **user question** is embedded (one `embed([q])` call). Recall is a
  neo4j vector search; no bulk embedding on the hot path.
- **Hard constraint — model parity:** offline and online embeddings **must come from the same
  model** (same 768-dim space) or cosine recall is meaningless. Both paths therefore call the
  **same D71 embedding API**. The index stores the `embedding_model` id alongside vectors;
  `VectorIndex` skips/warns on a model-id mismatch rather than returning garbage neighbours (§11 OQ).

**top-k / cutoffs (all provisional `RuntimeSettings`, tune on traffic — §11):**
- `retrieval_recall_k = 30` per corpus (high-recall; recall is cheap, precision is restored by
  rerank — 03 §Reranker).
- Blueprints: **top-3** thin cards, unconditional (03 fixes 3; thin cards are ~free on budget).
- Knowledge: **top-3**, `retrieval_knowledge_min_score` floor **disabled by default** (`None`).
  Rationale: ms-marco cross-encoder logits are **uncalibrated** (not 0–1), so an arbitrary logit
  threshold is more likely to wrongly drop good hits than to catch junk; gate by *count* first,
  add a floor only if traffic shows noise injection (OQ-R2).
- Reranker input is the **candidate text** (`intent` for blueprints, `chunk` for knowledge), query
  is the raw question `q`.

**Vector store choice — already settled, restated with the swap seam:** neo4j-native (D60). This
design does **not** re-open it. The only new abstraction is `VectorIndex` (§3), whose Phase-1 real
impl is `Neo4jVectorIndex`. The seam exists so (a) Layer-1 runs with an in-memory fake, and (b) the
**retrieval brick can ship and be Layer-1-green before the neo4j blueprint-execution brick lands**
(build-order §4). No ClickHouse / Couchbase vector store is proposed — that would contradict D60.

**Degrade-not-fail (mirrors D85), three independent failure points:**
- **No embedder configured / `EmbeddingError`** → skip retrieval entirely; return an **empty**
  `RetrievedContext`. The raw loop still runs (model can still `searchBlueprints` if a tool path is
  wired, or just derive SQL). Never fail the turn.
- **No reranker configured / rerank error** → **use recall order** (vector-similarity order) as the
  rerank order; still cut to top-3 / top-N. Flag `reranked=false` on the span.
- **VectorIndex unavailable / empty** → empty per-corpus result; other corpora unaffected.

---

## 3. Q3 — Module layout + interfaces + integration seams

### 3.1 New package `src/data_agent/runtime/retrieval/`

```
retrieval/
  __init__.py
  models.py        # Candidate, ThinCard, KnowledgeHit, UserMemoryItem, RetrievedContext (frozen dataclasses)
  vector_index.py  # VectorIndex protocol + FakeVectorIndex (Layer-1) + Neo4jVectorIndex (Slice B)
  scope_filter.py  # blueprint USES ⊄ scope drop (candidate-level; distinct from context/scope_filter which filters the trail)
  pipeline.py      # RetrievalPipeline: embed -> recall -> scope-filter -> rerank -> cut -> RetrievedContext
  user_memory.py   # UserMemoryProvider protocol + NullUserMemoryProvider (Phase-1 first slice returns [])
  render.py        # RetrievedContext -> a single model-facing system message (pure formatting)
```

Reranker transport (`model/reranker_client.py`) stays in `model/` beside `embedding_client.py`
(the developer is building it there) — retrieval depends on its **protocol**, not its module.

### 3.2 Interfaces (shapes, not final signatures)

```python
# models.py
@dataclass(frozen=True)
class Candidate:
    id: str
    kind: Literal["blueprint", "knowledge"]
    text: str                              # intent (bp) | chunk (knowledge) — the rerank/embed text
    uses: frozenset[str] | None            # blueprint transitive (table.column) set; None for knowledge
    payload: dict[str, Any]                # bp: {intent, slots_summary}; knowledge: {title, chunk, doc_id}
    score: float | None = None             # filled by rerank (or recall sim on degrade)

@dataclass(frozen=True)
class ThinCard:      id: str; intent: str; slots_summary: str; score: float
@dataclass(frozen=True)
class KnowledgeHit:  id: str; text: str; score: float; title: str | None
@dataclass(frozen=True)
class UserMemoryItem: kind: str; text: str          # e.g. {"entity_default", "'my team' -> department=0420"}

@dataclass(frozen=True)
class RetrievedContext:
    thin_cards: list[ThinCard]
    knowledge_hits: list[KnowledgeHit]
    user_memory: list[UserMemoryItem]
    reranked: bool                         # False when rerank was skipped (degrade)
    def is_empty(self) -> bool: ...

# vector_index.py
class VectorIndex(Protocol):
    async def recall(self, *, query_vector: list[float], kind: str, k: int) -> list[Candidate]: ...
    # Neo4jVectorIndex reads the model-id stamped on stored vectors; mismatch -> [] + a warn span attr.

# model/reranker_client.py  (developer-owned; consumed here)
class RerankerClient(Protocol):
    async def rerank(self, *, query: str, documents: list[str]) -> list[float]: ...   # same order; caller re-sorts

# user_memory.py
class UserMemoryProvider(Protocol):
    async def fetch(self, *, user_id: str, column_scope: frozenset[str]) -> list[UserMemoryItem]: ...
class NullUserMemoryProvider:  # Phase-1 first slice: returns []  (user store not built yet)
    async def fetch(self, **_) -> list[UserMemoryItem]: return []

# pipeline.py
class RetrievalPipeline:
    def __init__(self, *, embedding_client: EmbeddingClient | None, reranker: RerankerClient | None,
                 vector_index: VectorIndex, user_memory: UserMemoryProvider,
                 recall_k: int, top_k_blueprints: int, top_k_knowledge: int,
                 knowledge_min_score: float | None, observer: ToolObserver | None = None,
                 tracer: Tracer | None = None) -> None: ...
    async def retrieve(self, *, question: str, column_scope: frozenset[str],
                       user_id: str | None) -> RetrievedContext: ...
```

**`slots_summary`** is stored on the blueprint node at write time (a short string derived from
`slots`), not computed at recall — keeps recall a pure read.

### 3.3 Integration seams

- **With context assembly.** `ContextAssembler` gains an **optional** `retrieval:
  RetrievalPipeline | None` dependency and `assemble(...)` gains a `user_message: str` +
  `user_id: str | None` argument. When `retrieval` is present, `assemble` calls
  `retrieve(...)`, renders the result to **one system message** via `retrieval/render.py`, and
  **prepends it** to the returned `messages` (after the base system prompt, before history). When
  `None` (Layer-1 history-only tests, unconfigured deploy), behaviour is byte-identical to today.
  This keeps a **single "assemble the model's context" entry point** (matches 01/03 framing that
  retrieval *is* part of context assembly) while the pipeline stays independently unit-testable.
  *This changes the `assemble()` signature* — a coordination item (§12), since `loop/agent_loop.py`
  is the caller and passes `user_message` already at turn start.
- **With the loop.** `AgentLoop` already has the raw `user_message` at `run(...)` start and rebuilds
  context each round-trip. It threads `user_message`/`user_id` into `assemble(...)`. **Retrieval
  runs once per turn, not per round-trip** — cache the `RetrievedContext` on the in-turn working
  state keyed by `(question, scope_hash)` so the D45 per-round-trip rebuild does not re-embed /
  re-recall 15 times. (Cheap correctness: same inputs → same block; the cache is a turn-local
  memo, nothing persisted.)
- **With the model-facing tools (Slice B).** `searchBlueprints` / `searchKnowledge` are thin
  wrappers that call `RetrievalPipeline.retrieve(...)`-style recall+rerank for a *single* corpus
  and return the reranked list. They intercept in the loop like `resolveValues` (inline tool
  result), or dispatch — TBD in the blueprint brick; either way they **reuse this pipeline's
  recall+rerank**, not a second copy.

### 3.4 Config / settings additions (`RuntimeSettings`)

```python
# --- Reranker client (D71 custom API) --- (developer may already be adding url/key/model/timeout)
reranker_api_url: str = ""          # empty => rerank skipped (recall-order degrade)
reranker_api_key: str = ""
reranker_model: str = ""
reranker_timeout_seconds: float = 10.0
# --- Retrieval pipeline (D7/D8) ---
retrieval_enabled: bool = True                  # master switch; False => empty RetrievedContext (Phase-0 parity)
retrieval_recall_k: int = 30
retrieval_top_k_blueprints: int = 3             # 03 fixes 3
retrieval_top_k_knowledge: int = 3
retrieval_knowledge_min_score: float | None = None   # off by default (uncalibrated logits)
neo4j_url: str = ""                             # empty => VectorIndex unavailable => empty recall
neo4j_username: str = ""
neo4j_password: str = ""
```

Unconfigured embedding API (already `embedding_api_url=""`) **plus** unconfigured neo4j is the
Phase-0-equivalent state: `retrieve()` returns empty, the loop behaves exactly as Phase 0. This is
the safe default until neo4j + the corpus exist.

### 3.5 Observability (D23/D24/D25, D61)

- **New spans** (require two helper additions to `observability/tracing.py` — §12):
  - `recall_span` — `CHAIN` kind; attrs: `retrieval.corpus`, `retrieval.recall_k`,
    `retrieval.candidate_count`, `retrieval.dropped_by_scope_count`. **No query text.**
  - `rerank_span` — **`RERANKER`** kind (OpenInference has this kind; D24 calls for a manual
    `RERANKER` span); attrs: `reranker.model`, `reranker.document_count`, `reranker.reranked`
    (false on degrade). **No query text, no document text.**
  - `EMBEDDING` span is **reused** from `embedding_client.py` unchanged (counts + model + latency
    only; text never logged — the existing D25 guarantee).
- **Redaction (D25, load-bearing).** The question is user text and may quote PII; it is **never** a
  span attribute and **never** in a progress event. Blueprint ids and knowledge doc ids are
  structural/non-PII and may be logged; **knowledge chunk text and thin-card intent text are not**
  logged to spans (intent can be entity-agnostic but is not worth the redaction-audit surface —
  log ids + scores only). Reuse the existing `_SHAPE_ALLOWLIST` discipline from the progress path.
- **Progress events (D61).** One event at retrieval start ("searching for a matching blueprint…")
  with **shape only** — `{step: "retrieval", shape: {blueprints: n, knowledge: m}}` — no values.
  This is the D61 "context assembly / blueprint pick" progress boundary reusing the same span
  boundary.

---

## 4. Q4 — Dependency-ordered build plan for the remaining Phase-1 bricks

Remaining Phase-1 bricks (D68 Phase-1 scope): **retrieval pipeline**, **blueprints + neo4j**
(`searchBlueprints`/`getBlueprint`/`runBlueprint`), **D56 verify gate**, **searchKnowledge**,
**D67 `resolve_via` wiring**, **Track B learning loop**.

### 4.1 Dependency graph

```
embedding client (done/aligning) ─┐
reranker client (in progress) ─────┼─▶ [A] RETRIEVAL PIPELINE (VectorIndex protocol + FakeVectorIndex)
                                   │        │  Layer-1 green with NO neo4j
                                   │        ▼
neo4j graph+vector schema (STUB) ──┴─▶ [B] Neo4jVectorIndex + offline indexing job
                                            + searchBlueprints / getBlueprint / searchKnowledge (read tools)
                                            │
                                            ▼
grain decl (D37a/D40 — MCP-side, present) ─▶ [C] runBlueprint execution engine
                                            + D56 VERIFY GATE (grain-integrity code check + LLM review)
                                            + D67 resolve_via wiring (uses resolveValues.resolve(), hook exists)
                                            │
Track B (offline) ──────────────────────────┴─▶ writes the blueprint/knowledge corpus [A]/[B] read
   (extractor -> leakage gate -> dedup -> promotion) — parallelizable after neo4j schema is fixed
```

**Key dependency facts:**
- **Retrieval [A] depends only on the two model clients + a `VectorIndex` seam** — *not* on neo4j,
  *not* on blueprint execution. With `FakeVectorIndex` it is fully Layer-1-testable **now**. This
  is why it is the recommended next brick: it is unblocked and it unblocks everything downstream
  that needs recall+rerank (`searchBlueprints`, `searchKnowledge`).
- **neo4j graph+vector schema is a STUB** (04 §Storage, OPEN-QUESTIONS "neo4j graph schema … to be
  specified"). It is the **shared prerequisite** of [B] and Track B. Define it once, early (as the
  first task of Slice B), so read [B] and write [Track B] can proceed in parallel against it.
- **searchKnowledge is smaller than blueprints** and shares [A]+[B]'s recall+rerank over the
  knowledge corpus — it lands as part of Slice B, not a separate brick.
- **D56 verify gate is coupled to `runBlueprint`** (it verifies a blueprint result), but its
  **teeth — the code-computed grain-integrity assertion — is a pure function** (`result row count ==
  COUNT(DISTINCT declared result-grain)`; each measure at its declared `{agg, defined_over}`,
  04 §Execution step 6 / D56). Build that pure assertion **early and standalone** (Layer-1, against
  fabricated results + the deliberately-wrong-grain fixture D76 requires), then wire it into
  `runBlueprint` in Slice C. Note: the raw loop still returns as-is in Phase 1
  (`agent_loop.py` docstring) — D56 is the **blueprint fast-path** gate, not a raw-loop gate.
- **D67 `resolve_via` wiring lives inside `runBlueprint`** (resolve a rule's concept → code set via
  `resolveValues.resolve()` — the typed hook already exists — and bind as params, D10/D67). It
  therefore **follows** the execution engine (Slice C).
- **Track B is offline and decoupled** (D68 Parallel-learning commitment): no request-path
  prerequisite beyond session store (done) + Redis + neo4j schema. It can start as soon as the
  neo4j schema is fixed and run in parallel with Slices B/C — but it **must** use the **same
  embedding API** when it writes corpus vectors (§2 model-parity constraint).

### 4.2 What is parallelizable
- **[A] retrieval** and **Track B extractor/leakage-gate** touch disjoint code and can run in
  parallel once the neo4j schema is drafted (Track B needs the write schema; [A] needs only the
  read protocol + fake).
- Within **[B]**, the offline indexing job and the read tools (`searchBlueprints` etc.) can be
  split across two developers against the fixed schema.
- **D56 grain-integrity assertion** (pure) can be built in parallel with [A]/[B] since it has no
  retrieval dependency — only the catalog grain declaration, which is MCP-side and present.

### 4.3 Recommended next 2–3 slices (with scope boundaries)

- **Slice 1 (NEXT) — Retrieval pipeline core, no neo4j.**
  Deliver `retrieval/` (models, `VectorIndex` protocol, `FakeVectorIndex`, `scope_filter`,
  `pipeline`, `NullUserMemoryProvider`, `render`), the degrade paths, the two tracing spans, the
  settings, and the `ContextAssembler` integration (optional `retrieval` dep + `user_message` arg,
  turn-local memo). **Boundary: NO neo4j, NO model-facing tools, NO user-memory content** (Null
  provider). Pre-injects thin cards + knowledge from an **in-memory index seeded by a small loader
  fixture**. Layer-1 green with `FakeEmbeddingClient` + `FakeRerankerClient` + `FakeVectorIndex`;
  Layer-2 exercises the **real** embedding+reranker mocks (18003/18004) end-to-end through the
  pipeline (no neo4j yet). This proves the whole embed→rerank→inject path against real models with
  zero graph-store risk, and de-risks the D71 client contracts before the big neo4j brick.
- **Slice 2 — neo4j: schema + `Neo4jVectorIndex` + offline indexing job + read tools.**
  First task: **fix the neo4j graph+vector schema** (node labels for blueprint/knowledge, `USES`
  edges, the vector index, `embedding_model` stamp). Then `Neo4jVectorIndex.recall`, a one-shot
  **indexing job** (embed blueprint intents + knowledge chunks via the D71 API → neo4j), and the
  `searchBlueprints` / `getBlueprint` / `searchKnowledge` model-facing tools (getBlueprint is a
  keyed fetch of the stored DAG). **Boundary: retrieval + expansion only — NO `runBlueprint`
  execution.** Layer-2 = retrieval↔neo4j + live embedding/reranker mocks (D76 layer-2 list already
  names "retrieval↔neo4j").
- **Slice 3 — `runBlueprint` execution engine + D56 verify gate + D67 wiring.**
  The fast path: slot filling (04 §Slot filling, deterministic resolvers, no LLM — D49), DAG
  topological execution, injection-safe intermediates (scalars→typed params, tables→scratch JOIN —
  D59a), pause/resume (D45), the **D56 gate** (the pre-built grain-integrity assertion + LLM
  review; fall back to raw loop on failure — no silent path), and **D67 `resolve_via`** rule
  resolution via `resolveValues.resolve()`. Biggest brick; depends on Slices 1–2 for the corpus and
  on the catalog grain declaration.

### 4.4 Layer-2 / Layer-3 conformance implications (D68 red burndown)
- The Phase-0 harness already stands up the ~13-scenario red burndown (D68/D76). Retrieval bricks
  flip the **blueprint/knowledge scenarios** green in stages, not all at once:
  - **Slice 1** makes the *retrieval-suggestion* behaviour real (thin cards pre-injected) but,
    absent model-facing tools + execution, does **not** by itself green a full fast-path scenario —
    it green-ables the **"relevant blueprint is surfaced / context-assembly retrieval runs"** slice
    of the harness and unblocks the two D71-client Layer-2 checks.
  - **Slice 2** greens **`searchBlueprints`/`getBlueprint`/`searchKnowledge`** observable-flow
    scenarios (model can find + expand a blueprint / retrieve knowledge).
  - **Slice 3** greens the **fast-path execution + D56 verify** scenarios — including the
    **deliberately wrong-grain blueprint** negative that D76 requires the fixture to carry (the gate
    must catch it and fall back, never return the fan-out double-count).
  - Track B greens the three learning-loop scenarios (`correction→learning`, `knowledge human-gate`,
    `LEARNING_ENABLED=false`) — the reason D68's full-conformance gate cannot ship without it.

---

## 5. Q5 — Test seams per layer

- **Layer-1 (no infra).** `FakeEmbeddingClient` (exists; scripted `{text: vector}` + `fail=True`),
  `FakeRerankerClient` (developer-owned; scripted `{doc: score}` + fail toggle), `FakeVectorIndex`
  (in-memory `{id: Candidate}` seeded per test, returns all/top-k by a stubbed sim). Assert:
  recall→scope-filter→rerank→cut ordering; blueprint `USES ⊄ scope` drop (fabricated `uses` sets vs
  scope); exactly-3 blueprint cut; knowledge top-N + optional floor; **all three degrade paths**
  (no embedder → empty; no reranker → recall order + `reranked=false`; empty index → empty corpus);
  render output is a single system message with **no** raw question / no scope / no jwt; turn-local
  memo returns the same `RetrievedContext` on a second `retrieve` with identical inputs. Reuse
  `ranking.cosine` for any similarity assertions.
- **Layer-2 (live mocks).** Against the **real** embedding mock on **18003** and reranker mock on
  **18004** (the developer is wiring both into `docker-compose.integration.yml`): `HttpEmbeddingClient`
  returns **768-dim** vectors for `POST /embed {"input_text":[...]}`; `HttpRerankerClient` returns
  same-order scores for `POST /rerank`, and the pipeline **re-sorts** correctly (higher=better).
  Slice-2 adds retrieval↔**neo4j** round-trip (index a fixture blueprint+knowledge set, recall,
  assert neighbours). Gate with an env flag mirroring the existing pattern (e.g.
  `RUN_RETRIEVAL_TESTS=1` + `EMBEDDING_API_URL=http://localhost:18003/embed`
  `RERANKER_API_URL=http://localhost:18004/rerank`), so `uv run pytest` stays green with zero infra.
- **Layer-3 (Playwright, Slice 2+).** A conformance scenario where a **seeded blueprint** is
  surfaced as a thin card and the model expands it via `getBlueprint` over the real UI —
  deterministic via scripted model doubles / cassettes (D76 LLM-determinism). Deferred until the
  read tools exist.

---

## 6. Determinism across resume (D45 interaction)

Because the loop rebuilds context on every round-trip and any instance may resume a paused turn,
`retrieve()` must be a **pure function of `(question, column_scope, corpus-snapshot)`**. Two
consequences: (1) the **turn-local memo** (§3.3) prevents 15× re-embedding within a turn; (2) a
`resume` re-runs retrieval on the **original turn's question** (already in the trail) and gets the
same block — unless the corpus changed between pause and resume (rare; Track B writes are offline
and a mid-pause corpus bump only *adds* candidates). We accept eventual-consistency here: a resumed
turn may see a slightly fresher corpus; it never sees a *wrong-scope* one (scope filter re-runs).
No retrieval state is persisted to Couchbase — the block is re-derived, consistent with D50's
"per-turn derived view, not a persisted artifact".

---

## 7. Risks & trade-offs (named explicitly)

- **Uncalibrated reranker scores.** ms-marco logits aren't 0–1; any knowledge score floor is a
  guess until traffic. Mitigation: floor **off** by default, gate by count; revisit (OQ-R2). Trade:
  possible junk knowledge injection vs. wrongly dropping good hits — we chose the former (cheaper to
  detect in evals).
- **Embedding-model skew between offline index and online query.** Silent recall degradation if the
  index was built with a different model. Mitigation: stamp `embedding_model` on vectors, skip on
  mismatch (§2). Trade: a model swap requires a full reindex — accepted (rare, offline).
- **`assemble()` signature change** ripples to `agent_loop.py` + `app.py`. Reversible (optional
  arg, `None` = today's behaviour). Trade: one coordination point vs. a second parallel
  context-assembly entry point (rejected — two entry points drift).
- **neo4j as a hard dependency for the real path.** Slice 1 deliberately avoids it (fake index) so
  the brick ships and de-risks the D71 clients first; the swap seam is the `VectorIndex` protocol.
- **User memory deferred.** 03 lists user memory in the pre-injection, but no user store exists.
  `NullUserMemoryProvider` ships it empty — honest degradation, no fabricated defaults. The seam is
  in place for when the store lands.

---

## 8. Open questions (safe interim defaults chosen — not blocking)

- **OQ-R1 recall_k / top-k / rerank cutoffs** → interim `recall_k=30`, `top_k_blueprints=3`
  (fixed by 03), `top_k_knowledge=3`; all `RuntimeSettings` tunables. (03 open-question:
  "reranker model/threshold and k … tune empirically".)
- **OQ-R2 knowledge score floor** → **off** (uncalibrated logits); add only if evals show noise.
- **OQ-R3 `searchKnowledge` returns chunks vs. summarized facts** (02 open-question) → interim
  **reranked chunks** `{id, text, score, title}`; finalize with the knowledge store in Slice 2.
- **OQ-R4 user-memory store** → deferred; `NullUserMemoryProvider` (empty) until the store exists.
- **OQ-R5 embedding/reranker model ids** → default to the mock models
  (`all-mpnet-base-v2` 768-dim; `ms-marco-MiniLM-L-6-v2`) via settings; real endpoint ids TBD (D71).
- **OQ-R6 offline reindex trigger** → Slice 2 ships a manual/batch indexing job; event-driven
  reindex-on-write is a Track B concern (blueprint/knowledge promotion).
- **OQ-R7 neo4j graph+vector schema** (04 §Storage stub) → **must be specified as the first task of
  Slice 2**; shared prerequisite of read [B] and Track B write.
- **OQ-R8 (M4) retrieved-block token budget** → the rendered pre-injection block is **not yet
  counted against the D46 history token budget** (`context/budget.py`). Slice 1 partially mitigates
  unbounded prompt growth via `render.py`'s per-field length caps (`_MAX_FIELD_CHARS=500` /
  `_MAX_CHUNK_CHARS=2000`), but **full budget accounting** (reserve/charge the retrieved block, trim
  under pressure) is **deferred to Slice 2** — it couples to the neo4j corpus sizing.
- **OQ-R9 (L5) blueprint scope filter has no `scratch.` exemption — by design.** Unlike the trail
  filter (`context/scope_filter.py`, which exempts `scratch.`-prefixed session tables per D64/D80),
  `retrieval/scope_filter.py` applies a plain `uses ⊆ column_scope` subset check with **no scratch
  exemption**: a **blueprint must never reference a scratch/session-scoped table** (blueprints are
  reusable warehouse-analysis templates written offline, not session artifacts). If Slice 2's
  authored blueprint corpus ever needs scratch USES, this is the deliberate place to revisit.

### 8.1 Slice-1 trust-boundary as-built note (H2 — render sanitisation)

The retrieved block is interpolated into a **system-role** message. `retrieval/render.py` now
**structurally sanitises** every interpolated field before emitting it: whitespace controls
(`\n\r\t\v\f`) collapse to single spaces, other C0/C1 control characters (incl. NUL) are dropped,
and each field is length-capped (500 chars per card field, 2000 per knowledge chunk). This prevents
retrieved TEXT from forging message STRUCTURE (fake `## System` sections / injected instructions at
system privilege), while keeping the block deterministic (resume renders byte-identically, §6). The
**content itself remains trusted** — it comes from the offline, leakage-gated corpus (blueprint
intents / promoted knowledge). **Slice 2 must reconfirm this posture** when Track-B *learned* (and
therefore less-trusted) content begins writing the corpus: at that point content-level treatment
(not just structural sanitisation) may be required.

---

## 12. Doc/code updates on adoption (NOT done this pass — for the implementing developer)

Because file discipline limited this pass to writing this one design doc, the following are
**recommended** edits to make when Slice 1 is built, not changes made now:

- **`observability/tracing.py`** — add `recall_span` (`CHAIN`) and `rerank_span` (`RERANKER`)
  helpers + export them; reuse the existing `embedding_span`. Extend `__all__`.
- **`config.py` (`RuntimeSettings`)** — add the §3.4 fields (reranker_*, retrieval_*, neo4j_*).
  (The developer may already be adding reranker_* alongside `HttpRerankerClient`.)
- **`context/assembly.py`** — add optional `retrieval: RetrievalPipeline | None` ctor dep;
  `assemble(...)` gains `user_message: str` + `user_id: str | None`; prepend the rendered retrieved
  system message when `retrieval` is set; `AssembledContext` may gain
  `retrieved_counts` for the span. Byte-identical when `retrieval is None`.
- **`loop/agent_loop.py`** — thread the turn's `user_message`/`user_id` into `assemble(...)`; hold
  the turn-local `RetrievedContext` memo so per-round-trip rebuilds don't re-embed.
- **`app.py`** — construct `RetrievalPipeline` (wire `HttpEmbeddingClient` + `HttpRerankerClient` +
  `Neo4jVectorIndex` **only when configured**, else leave `retrieval=None` → Phase-0 parity) and
  pass it to `ContextAssembler`.
- **`docker-compose.integration.yml`** — the embedding (18003) + reranker (18004) mocks (developer
  is already wiring these); Slice 2 adds a neo4j service for the retrieval↔neo4j Layer-2 tests.
- **Docs (DECISIONS/OPEN-QUESTIONS/TRACEABILITY/03/WORKLOG)** — record a new decision for the
  `VectorIndex` seam + degrade policy (candidate **D86**), close the 03 §"reranker/embedding" and
  OPEN-QUESTIONS §Retrieval open items that this design settles (recall_k, cutoffs, store=neo4j via
  D60, degrade paths), move the neo4j-schema OQ to "Slice-2 first task", and add a WORKLOG entry.
```
