# UI Slice 2 — Review-inbox full-vertical contract

**Status:** Contract-locked (no minted Dxx — reuses D58a pre-gate, D5/D57 scope, D25/D17 redaction, D29 negative signal, D101 store).
**Scope:** Three fan-out work items behind one seam — (1) fix the `global_knowledge` landing gap, (2) an inbox HTTP service wrapping `ReviewInbox.list/approve/reject/retract`, (3) a minimal static inbox review page. Dev-flag gated, OFF by default, phase-0/dormant.
**Reconciled vocabulary:** `docs/08-ui.md` § "Review inbox". Builders build against *this file only*.

Code lives under `src/data_agent/`; all paths below are relative to it unless prefixed with `ui/` or `tests/`.

---

## 0. Decisions (locked — do not re-litigate)

| # | Decision | Rationale (one line) |
|---|---|---|
| Hosting | **Option A — a new small FastAPI inbox service** mounting the write plane once; the UI BFF (`ui/server.py`) serves the page and proxies `/api/inbox/*` to it (mirrors the BFF→runtime `/turn` proxy). | The BFF's charter is "these responsibilities and ONLY these" (`ui/server.py:2-16`) and it must hold *only* chat-session JWTs; injecting a neo4j+couchbase+MCP+embedding write plane into it couples reviewer infra to the chat lifecycle and muddies the persona split. A dedicated process keeps the reviewer surface structurally separate and mirrors the existing runtime↔BFF split. |
| Evidence quotes | **Defer raw-quote display for phase-0.** The UI shows the redacted `payload_view` + the `evidence_refs` identifiers and their count — never the quotes. | Raw quotes live only in `learning_audit`, reachable by KV key (`evidence_refs`); surfacing them needs a *new* `learning_audit` read path (a new read tool). Out of scope, and it re-opens the exact exposure the inbox redaction (D17) closes. |
| Reviewer auth | **Lightweight gate: env flag + shared-secret reviewer token header.** No reviewer role/JWT claim, no `column_scope` mint. | There is no reviewer/admin role in the codebase — every runtime caller is a warehouse-query session authed by a `column_scope` JWT + `X-Session-Id` (`runtime/app.py:109-132`, `auth/jwt_verify.py:60`). A reviewer has *no* warehouse scope, so minting a session token for them is wrong. A real reviewer role/JWT claim is **future work.** |

---

## 1. Sub-task 1 — landing fix (GATING PREREQUISITE)

**The bug.** An approved `global_knowledge` candidate is stamped `validated` in the `learning_candidates` KV store but is **never landed** into Neo4j `:KnowledgeChunk`, so it never becomes recallable — the flagship inbox action ("approve → retrievable") is a silent no-op. In `apply_human_decision`, the non-replayable `else` branch (`promotion/scheduler.py:414-418`) falls through to the shared `approved = replace(env, status=VALIDATED); store.put(approved)` at **`scheduler.py:419-420`** with **no landing call**. Recall reads `:KnowledgeChunk` (`retrieval/vector_index.py:167-174`), which only the offline fixture seed (`scripts/seed_neo4j_corpus.py`) ever writes.

**The fix reuses the type-agnostic land-then-status machinery.** `_land_and_promote` (`scheduler.py:544-608`) already strips entity payload, stamps drift, and calls `self._landing_writer.land(...)` — it is not blueprint-specific. The only scheduler change is routing the else-branch through it for `global_knowledge` when a landing writer is wired, and HOLDING honestly when it is not.

### 1.1 Ordered file-change list

| # | File · anchor | Change |
|---|---|---|
| 1 | `learning/generalize/mapping.py` (mirror `blueprint_seed_from_candidate`, `:81-126`) | **BUILD** `knowledge_seed_from_candidate(env, *, id) -> KnowledgeSeed`. Pure function. Reads ONLY the entity-free knowledge surfaces (`statement`, `structured`, `related_terms`, `scope` — the set `leakage/gate.py:69-74` scans): `text` ← `statement` (optionally concatenated with `related_terms`/serialized `structured` for richer recall), `title` ← `scope` or a short summary, `doc_id` ← `env.candidate_id`, `id` ← the deterministic landing id, plus the new provenance/drift fields (row 2). Raise `ValueError` if `statement` is empty (never land an empty knowledge chunk). Never reads `evidence`, audit spans, or entity-bearing payload (D17). |
| 2 | `runtime/retrieval/corpus_loader.py` — `KnowledgeSeed` (`:97-106`) + `_UPSERT_KNOWLEDGE` (`:255-265`) | **ADD** `drift_status: str = "clean"`, `created_by: str = "seed"`, `source_candidate_id: str \| None = None` (mirror the blueprint side at `:84-85`, `:220-223`). Thread them into the knowledge write loop (`:932-942`, parallel to the blueprint loop `:911-931`) as `k.drift_status`, `k.created_by`, `k.source_candidate_id`. Defaults keep existing `knowledge.yaml` fixtures byte-identical (`created_by='seed'` was hardcoded in the Cypher at `:263` — move it to a parameter). Needed so retraction can stamp `drift_status`/`status` on the node. |
| 3 | `learning/promotion/landing.py` — `CorpusLandingWriter.land` (`:154-192`) | **BRANCH** on `env.type`: `blueprint` → existing `blueprint_seed_from_candidate` + `load_corpus(driver, embedder, [seed], [], ...)`; `global_knowledge` → `knowledge_seed_from_candidate` + `load_corpus(driver, embedder, [], [seed], ...)`. The last-gate entity defense `_assert_seed_entity_free` (`:111-130`, called at `:178`) must cover the knowledge seed's text fields — add a `_knowledge_haystack` sibling of `_seed_haystack` (`:91-108`) over `text`/`title`/serialized `structured`, and route the assert by type. |
| 4 | `learning/promotion/landing.py` — `_RETRACT_BLUEPRINT` (`:84-88`) + `update_status` (`:194-242`) | **BUILD** a `_RETRACT_KNOWLEDGE` Cypher sibling (`MATCH (k:KnowledgeChunk {id:$id}) SET k.status=$status, k.drift_status=$drift_status RETURN k.id`). Dispatch by `env.type` in `update_status`. `landing_id` (`:60-74`) is `bp::`-prefixed — add a parallel `knowledge_landing_id` (`kn::`-prefixed) or make the prefix type-driven, so blueprint and knowledge ids never collide and both MERGE idempotently. |
| 5 | `runtime/retrieval/vector_index.py` — `_KNOWLEDGE_RECALL_QUERY` (`:167-174`) | **ADD a recall-eligibility filter.** ⚠️ Unlike `_BLUEPRINT_RECALL_QUERY` (`:156-165`), the knowledge query has **no status filter today** — so a `_RETRACT_KNOWLEDGE` that only stamps `status=retired` is a **silent no-op** (the retired node still recalls). Add `AND coalesce(node.status,'validated')='validated'` (drift not applicable to knowledge; the `coalesce` default keeps existing fixture nodes recallable — the same COMPAT trick as `:151-155`). Without this line, retract does not pull knowledge from recall. |
| 6 | `learning/promotion/scheduler.py` — else-branch (`:414-420`) | **ROUTE** the `global_knowledge` approve through landing. In the non-replayable `else`: if `env.type == "global_knowledge"` and `self._landing_writer is not None` → `return await self._land_and_promote(env, DriftStamp(), action="approve", forbidden_spans=forbidden_spans)` (land FIRST, then `validated`); elif `self._landing_gate_blocks()` (`:535-542`) → `return CandidateDecision(..., "hold", reason="approve_blocked_landing_unavailable")` (fail honestly — no fake validate). Only fall through to the existing direct-validate at `:419-420` for genuinely non-landing targets (`schema_edit`) or a truly dormant dev config with `require_landing` off. `forbidden_spans` are already captured pre-strip at `:358`. |

**Recall needs no runtime change.** Once the node exists, both retrieval paths already read it — the auto-injection pipeline and the model-facing `searchKnowledge` tool (`retrieval/tools.py:282-318`) both go through `_KNOWLEDGE_RECALL_QUERY`.

### 1.2 Test plan (sub-task 1)

| Layer | Test | Assertion |
|---|---|---|
| Unit (pure fn) | new `tests/learning/.../test_knowledge_mapping.py` | `knowledge_seed_from_candidate` maps `statement`→`text`, stamps `created_by="learning"` + `source_candidate_id`, raises on empty `statement`, and reads no entity-bearing field. |
| Unit (fake) | **UPDATE** `tests/learning/promotion/test_human_review_and_guards.py::test_human_approve_knowledge_promotes_without_replay` (`:61-75`) | Currently only asserts `status=="validated"`. Wire the scheduler with a `FakeLandingWriter` (`tests/learning/promotion/helpers.py:139-185`) + `require_landing=True`; assert the approved `global_knowledge` env appears in `writer.landed` (i.e. `land()` was called) **before** the store shows `validated`. Add a sibling asserting that with **no** landing writer wired the approve HOLDS `approve_blocked_landing_unavailable` (not a fake validate). |
| Live | **CLONE** `tests/integration/test_learning_corpus_landing_live.py` | Land a `global_knowledge` candidate via the real `CorpusLandingWriter` → recall it back through `Neo4jVectorIndex.recall(kind="knowledge")`. Second land of the same id MERGEs one node. Skip-guarded on `NEO4J_TEST_URI` + `EMBEDDING_TEST_URL`; stack = `neo4j` + `embedding-api` from `docker-compose.integration.yml` (couchbase not needed for a pure land→recall test). |

---

## 2. Inbox HTTP API

Wraps `ReviewInbox` (`learning/inbox/inbox.py:65-131`). All routes live in the inbox service (Option A). All are gated (see §4): flag off → 404; missing token → 401; bad token → 403.

| Method · path | `ReviewInbox` method | Success | Errors |
|---|---|---|---|
| `GET /inbox` | `list(limit=100)` (`inbox.py:78-81`) | `200 {"items": [InboxItem…], "count": n}` | — |
| `POST /inbox/{id}/approve` | `approve(id)` (`inbox.py:93-109`) | `200 {ActionResult}` | `404` unknown id · `409` illegal transition / approve **held** (carries hold reason) · `503` landing plane unavailable (degraded, §6) |
| `POST /inbox/{id}/reject` | `reject(id)` (`inbox.py:111-117`) | `200 {ActionResult}` | `404` unknown id · `409` not `in_review` |
| `POST /inbox/{id}/retract` | `retract(id)` (`inbox.py:119-130`) | `200 {ActionResult}` | `404` unknown id · `409` not `validated` |

**Error mapping.** `InboxTransitionError` (`inbox.py:41-42`) → `404` when its message contains `not found`, else `409` (illegal current status, or an approve that HELD — e.g. failed replay, unresolved `depends_on`, landing unavailable; the reason string is surfaced verbatim). A missing landing plane on `approve` of a landing target → `503` (see §6). Flag/token errors are enforced *before* the inbox is touched.

### 2a. `InboxItem` wire shape — the exact projection

Pinned from `learning/inbox/models.py:61-87`. These are the **real** fields — the projection carries **no `status`** (every listed item is `in_review` by construction of `list()`, `inbox.py:80`) and **no `confidence`/`drift`** (they exist on `CandidateEnvelope` but are *not* projected today).

```json
{
  "candidate_id": "candidate::<hash>::0",
  "type": "global_knowledge",
  "reason": "knowledge_pre_gate",
  "summary": "entity-free one-liner (intent | statement)",
  "payload_view": { "...": "redacted payload — entity spans → \"[redacted]\"" },
  "evidence_refs": ["audit-kv-key-1", "audit-kv-key-2"],
  "entity_scan": { "result": "pass", "hits": [ {"field": "...", "kind": "...", "span": ""} ], "scanner": "..." },
  "dedup": { "canonical_key": "sha256:…", "matched_id": "…", "action": "…" },
  "created_at": "2026-07-09T12:00:00+00:00"
}
```

| Field | Source | Wire type / note |
|---|---|---|
| `candidate_id` | `InboxItem.candidate_id` | string |
| `type` | `.type` | `blueprint \| global_knowledge \| user_knowledge \| schema_edit` (`extractor/models.py:20`); `user_knowledge` never appears (auto-committed, not reviewed) |
| `reason` (category) | `.reason` — re-derived via `derive_inbox_reason` (`writer/routing.py`) | one of `InboxReason` (`learning/inbox/models.py`): `knowledge_pre_gate \| schema_edit \| leakage_near_miss \| blueprint_sampled \| dedup_conflict \| suppressed_duplicate \| fail_to_review \| needs_parameterization \| hand_authored`. **Re-derived, never stored** — cannot drift from the routing decision. `suppressed_duplicate` is the deterministic (`action: "increment"`) match against a **live** learning artifact, kept in review so a human can revise a delta; `dedup_conflict` stays the SOFT near-miss (`conflict` / `merge`). |
| `summary` | `.summary` | entity-free one-liner (`_summary_of`, `models.py:36-41`) |
| `payload_view` | `.payload_view` — `entity_free_payload_view` (`candidate/redaction.py:91-94`) | **redacted** dict; entity spans replaced with `"[redacted]"` (D17). The UI renders this **verbatim** and must never expect raw entity values. |
| `evidence_refs` | `.evidence_refs` | array of KV keys (identifiers only — **not quotes**, §0 evidence decision) |
| `entity_scan` | `.entity_scan` (`LeakageVerdict`) | serialize via `.to_doc()`; `span`s are blanked (`""`) so no raw value re-leaks |
| `dedup` | `.dedup` (`DedupVerdict \| None`) | serialize via `.to_doc()` or `null` |
| `created_at` | `.created_at` | ISO-8601 string (the reviewer timestamp) |

**AMENDMENT — three fields have been ADDED since this table was written**, and the JSON
above is a snapshot rather than the live shape. The authoritative list is
`learning/inbox/service.py::_inbox_item_to_wire` plus its exact-shape test
(`tests/learning/inbox/test_inbox_service.py::test_list_returns_exact_wire_shape`); this
table is not re-derived from either, so prefer them.

| Field | Added by | Wire type / note |
|---|---|---|
| `status` | ui-inbox-type-archive | the envelope status as a plain string. The claim above that the projection carries no `status` is no longer true — the SAME projection now serves the `?status=rejected` archive and the Phase-3 `?status=validated` listing, so the row has to say which one it is. |
| `verified` | Phase-3 inbox | bool. Distinguishes a human-VERIFIED validated learning node (promotable) from an auto-landed one. `false` for every review-queue and archive row. |
| `score` | plan §4 (promotion policy) | object — `{score, novelty, groundedness, session_quality, novelty_measured, quality_measured, groundedness_measured}`, all floats/bools. The review-queue ORDER (`novelty × groundedness² × session-quality`, descending, arrival order breaking ties). The three axes and the three `*_measured` flags ship alongside the composite deliberately: the queue is sorted by it, so a client that could not see the components would be rendering an order it cannot explain, and an unmeasured axis must render as *unknown* rather than as a low score. Only the `in_review` listing is ranked — the archive stays newest-first and the validated listing stays oldest-first. |

**`ActionResult` (approve/reject/retract response).** A minimal status projection — deliberately **not** the full envelope (no payload echoed back; the UI just refreshes the list):

```json
{ "candidate_id": "…", "type": "global_knowledge", "status": "validated", "reason": null }
```

`status` is the new store status after the transition (`validated` / `rejected` / `retired`); `reason` is `null` on success and the hold reason on a `409`.

---

## 3. Hosting + auth (Option A)

```
browser ──(same origin)──► BFF ui/server.py ──proxy /api/inbox/*──► inbox service ──► ReviewInbox ──► write plane
   page GET /inbox            serves inbox.html            attaches X-Reviewer-Token           (Couchbase / Neo4j / MCP / embedding)
```

- **Env flag** `REVIEW_INBOX_ENABLED` (mirror `UI_TEST_AFFORDANCES`, `ui/server.py:175`). Gates **both** the BFF page route + proxy **and** the inbox service's own routes (defense in depth). When unset/≠`"1"` → every inbox route raises `HTTPException(404)` — the surface does not exist.
- **Reviewer token.** The inbox service requires header `X-Reviewer-Token`, compared **constant-time** to env `REVIEWER_TOKEN`. Missing → `401`; mismatch → `403`. This is **not** a `column_scope` JWT and the service never calls the token mint — a reviewer has no warehouse scope. The BFF holds `REVIEWER_TOKEN` in env and attaches it server-side on the proxy hop (exactly as it holds the issuer key at `ui/server.py:42`); the browser never sees it. (A per-reviewer token / real JWT claim is future work.)
- **Browser reach.** The BFF serves `ui/static/inbox.html` at `GET /inbox` (add a `FileResponse` route mirroring `index()` at `ui/server.py:120-122`) and proxies `POST/GET /api/inbox/*` to the inbox service (a JSON, non-streaming sibling of `_proxy_stream`, `ui/server.py:215-250`). Same origin ⇒ no CORS.

**Construction recipe.** The inbox service builds its `ReviewInbox` from the fully-wired write plane exactly as `scripts/run_learning_scheduler.py:129-193` does (which today builds and **discards** `_inbox`): `build_promotion_write_plane(...)` (`learning/factory.py:305-348`) returns `(scheduler, inbox)` wiring `CouchbaseCandidateStore`, `CouchbaseBlueprintCorpus` (hit counts), the MCP client + token minter (blueprint golden-replay), and the Neo4j driver + embedding client (landing). The service holds the returned `inbox`. An **offline dev mode** swaps in `InMemoryCandidateStore` (`candidate/memory_candidate_store.py`) with the default (unwired) scheduler — list/reject/retract work; landing-approve does not (§6).

---

## 4. Inbox UI page

A new static page `ui/static/inbox.html` — vanilla JS + CSS custom properties, no framework, matching `ui/static/index.html` (CSS vars `:root{--color-*/--space-*}` at `:8-26`, `data-testid` hooks, semantic a11y). Served by the BFF at `GET /inbox`; calls the BFF's `/api/inbox/*`.

**Per item** (renders the `InboxItem` fields, all already redacted):
- Header: `reason` as the category label + `type` badge.
- Body: `summary`, then `payload_view` rendered **verbatim** (pretty-printed JSON or a field list) — the page **never** expects or reconstructs raw entity values; the redaction token `"[redacted]"` shows where a value was.
- Evidence: an `evidence_refs` **count** + the ref identifiers (e.g. "2 evidence refs") — **no quotes** (§0).
- `entity_scan` finding (field + kind) as a quiet attention marker; `dedup.canonical_key` when present.

**Three action buttons** — `approve` / `reject` / `retract` (`data-testid="inbox-approve|reject|retract"`):
- Each shows a **confirm** step before firing (a destructive/promotion action).
- On click: **disable all three** for the item, POST the action, then on success **remove the item** and refresh the list (a validated/rejected/retired item is no longer `in_review`). `retract` is only shown for items reachable as `validated` (in practice the list is all `in_review`, so `retract` is typically hidden/disabled here — surface it only if a validated-item view is added later).
- On `4xx/5xx`: re-enable the buttons and show the error banner (reuse the error styling at `index.html`; a `409` shows the hold/transition reason, a `503` shows "approve-that-lands is unavailable in this deployment").

**Empty state:** `GET /inbox` → `count:0` renders "Inbox is empty — nothing awaiting review" (`data-testid="inbox-empty"`).

---

## 5. Degrade behavior (fail honestly)

| Deployment | list / reject / retract | approve |
|---|---|---|
| **Full write plane** (`build_promotion_write_plane`, all ports set) | work | works; `global_knowledge` lands then validates; `blueprint` runs replay+landing |
| **Offline dev** (`InMemoryCandidateStore`, default scheduler, no landing writer) | **work** | **refused with `503`** for any landing target (`global_knowledge`, replayable `blueprint`) — the service reports "landing plane unavailable"; it does **not** fake a `validated`. reject/retract still mutate the store honestly. |

The service exposes a `GET /inbox/health` returning `{"write_plane": "full" \| "offline"}` so the page can pre-disable approve and show a "read-only review" banner in offline mode rather than letting the reviewer click into a `503`.

---

## 6. Feasibility (GREEN / YELLOW / RED)

| Component | Rating | Reason |
|---|---|---|
| `knowledge_seed_from_candidate` mapping | **GREEN** | Pure function mirroring `blueprint_seed_from_candidate`; payload fields exist (`gate.py:69-74`); unit-testable. |
| `KnowledgeSeed` + `_UPSERT_KNOWLEDGE` provenance/drift fields | **GREEN** | Mechanical mirror of the blueprint side (`corpus_loader.py:84-85,220-223`); optional-defaulted → no fixture migration. |
| `CorpusLandingWriter.land` type-branch + entity defense | **GREEN** | `load_corpus` already accepts knowledge seeds; only a type switch + a `_knowledge_haystack` for the D17 last gate. |
| scheduler else-branch routing through `_land_and_promote` | **GREEN** | `_land_and_promote` is type-agnostic (`scheduler.py:544-608`); the change is a 3-line route + an honest HOLD. |
| `_RETRACT_KNOWLEDGE` **+ knowledge recall status filter** | **YELLOW** | Cross-file dependency a builder can miss: `_KNOWLEDGE_RECALL_QUERY` (`vector_index.py:167-174`) has **no status filter**, so retract-by-stamp is a silent no-op until that `WHERE` line is added. Both changes must ship together. |
| Inbox service — list / reject / retract | **GREEN** | Thin wrapper over `ReviewInbox`; works over `InMemoryCandidateStore` with zero infra. |
| Inbox service — **landing-approve** | **YELLOW** | Requires the full couchbase+MCP+token+neo4j+embedding write plane (`build_promotion_write_plane`) **and** depends on sub-task 1. Honestly `503`s when not wired. Not RED — degrades cleanly. |
| Reviewer auth (flag + shared-secret) | **GREEN** | Direct mirror of the `UI_TEST_AFFORDANCES` 404 pattern (`ui/server.py:175`) + a constant-time header compare. |
| UI page | **GREEN** | Static, mirrors `index.html`; renders already-redacted fields. |

**No RED.** The one hard gate: "approve → retrievable" for `global_knowledge` is a *silent no-op* until sub-task 1 ships — so the inbox UI must not expose a landing-approve action against a deployment without the landing fix (the `503`/offline-mode banner enforces this).

---

## 7. Fan-out plan

Three disjoint work items; the only seam is this doc.

| Item | Files (disjoint) | Depends on |
|---|---|---|
| **1 · landing fix** (GATING) | `learning/generalize/mapping.py`, `learning/promotion/scheduler.py`, `learning/promotion/landing.py`, `runtime/retrieval/corpus_loader.py`, `runtime/retrieval/vector_index.py` + its tests | — |
| **2 · inbox service** | new `learning/inbox/service.py` (or a top-level `inbox_service/`), `learning/factory.py` reuse, a proxy block in `ui/server.py` | ReviewInbox (exists). Its **live approve→retrievable test** gates on Item 1. |
| **3 · UI page** | new `ui/static/inbox.html`, a serve route in `ui/server.py` | This doc's §2a/§4 wire shapes only — builds in parallel with 1 and 2. |

**Seams / order:** Items 1 and 3 are fully parallel. Item 2's *code* (routes + wiring) is parallel too, but its **approve→retrievable integration test** must land after Item 1. Integrate at the service boundary: Item 2 owns the HTTP↔`ReviewInbox` contract; Item 1 owns the `ReviewInbox.approve → recallable` behavior beneath it; Item 3 owns the browser↔`/api/inbox/*` contract. The cost lands at these seams — pin them here, not in code.

---

## 8. Test hooks

| Kind | Where | What |
|---|---|---|
| Unit — mapping | new `tests/learning/.../test_knowledge_mapping.py` | `knowledge_seed_from_candidate` field mapping + entity-free guard. |
| Unit — scheduler (fake) | **UPDATE** `tests/learning/promotion/test_human_review_and_guards.py:61-75` | Assert approved `global_knowledge` is handed to `FakeLandingWriter.land()` before `validated`; add the no-writer→HOLD sibling. |
| Unit — service (TestClient) | new `tests/learning/inbox/test_inbox_service.py` | Over `InMemoryCandidateStore`: list shape (§2a), flag-off→404, no-token→401, bad-token→403, unknown-id→404, illegal-transition→409, offline approve→503, reject/retract happy paths. |
| Live — docker | **CLONE** `tests/integration/test_learning_corpus_landing_live.py` | Land a `global_knowledge` candidate → recall via `Neo4jVectorIndex.recall(kind="knowledge")`; stack `neo4j` + `embedding-api` (`docker-compose.integration.yml`), skip-guarded on `NEO4J_TEST_URI` + `EMBEDDING_TEST_URL`. |
| Live — full plane (optional) | new integration test | Approve through the real `ReviewInbox` + `build_promotion_write_plane` → recall; adds `couchbase` to the stack. |

---

**Status:** Contract-locked. Sub-task 1 is the gating prerequisite for the approve action.
