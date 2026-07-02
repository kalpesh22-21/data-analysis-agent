# Layer-3 conformance completion — design

**Status:** BUILT (Session 14) — **Slices 1+2 complete; the full Layer-3 conformance suite is
12/12 Playwright-green (RUN_E2E 13/13), satisfying the D68 release gate for the first time.** Slice 1
wired the demo launcher's retrieval pipeline + the 4 runBlueprint scenarios; Slice 2 added the 3
original Phase-0 scenarios (mid-session scope narrowing D44, PII span inspection D25, restart
durability against live Couchbase D45) behind default-off, monotonic-narrowing test seams. See the
Session-14 D68-gate note in TRACEABILITY.md.
**Branch context:** `phase0/provenance-extractor`.
**Scope:** Turn every currently-red Layer-3 (Playwright) conformance scenario green, so the
D68 release gate (the *full* Layer-3 burndown) is satisfiable for Phase 0. Seven red scenarios
across two families:

- **4 runBlueprint scenarios (D89):** fast-path execution, no-silent-verification (D56),
  approval pause/resume (D45/D59b), ask→clarify→resume for a slot (D49).
- **3 deferred Phase-0 scenarios:** mid-session scope narrowing (D44), observability + PII span
  inspection (D25), pause/resume durability across a runtime restart (D45).

The existing **5 green** scenarios (progress D61, clarify/resume, scope denial D57, parser
fail-closed D63, budget-cap D47) MUST stay green — they are the regression floor.

---

## 0. TL;DR — one-line answers to the design questions

- **Q1 (demo-launcher retrieval wiring):** Inject a `RetrievalPipeline` built from a **seeded
  `FakeVectorIndex` + `FakeEmbeddingClient`** into `create_app(retrieval=...)` with
  `retrieval_enabled=True` (deterministic, hermetic, no neo4j/embedder); extend `DemoMCPClient`
  to **content-route `runQuery` off the SQL text statelessly** (matching *both* the `sql` and
  `query` arg keys) so the blueprint's node/domain/grain probes return canned results with **no
  FIFO to drift** across the long-lived server.
- **Q2 (mid-session scope narrowing):** Add a **test-only, env-gated scope-override affordance**
  to the BFF (`POST /api/session/scope` re-mints the session's JWT with a narrower
  `column_scope`) — the BFF *still* mints server-side and the browser still never sees the JWT
  (D82/D5 intact); assert an earlier in-scope answer **drops from replay** after narrowing (D44).
- **Q3 (observability + PII span):** Wire an **in-process `InMemorySpanExporter`** in the demo
  launcher (test-only env toggle) plus a test-only `GET /_test/spans` dump endpoint on the
  runtime — **not** a real Phoenix container (heavier, non-hermetic, and unnecessary to prove the
  D25 invariant); the scenario drives a turn and asserts spans are non-empty and PII-clean.
- **Q4 (restart durability):** Point the demo launcher at the **already-running `l2-cb`
  Couchbase** (`CouchbaseSessionStore`, env-selected) for one durability scenario; the conftest
  **kills and relaunches the `:8000` runtime subprocess** mid-pause and asserts resume at
  `awaiting_node`. In-memory store stays the default for the other 10 scenarios.
- **Q5 (the 4 runBlueprint scenarios):** `DemoModelClient` grows trigger phrases that emit
  `runBlueprint` tool calls; `DemoMCPClient` content-routes the per-node `runQuery` + the D56
  grain probe (passing vs. fan-out-violating); approval + ask→clarify **reuse the existing
  ask-user chip/resume UI** unchanged.
- **Q6 (slicing):** **Slice 1** = retrieval wiring + the 4 runBlueprint scenarios (all
  in-memory, hermetic). **Slice 2** = the 3 infra-touching scenarios (scope-switch BFF, in-memory
  span exporter, Couchbase-backed restart). Slice 1 carries almost all the value and almost none
  of the regression risk.
- **Q7 (scripted-double vs live):** Keep the **deterministic-doubles posture** (fakes for model +
  MCP + vector index; real JWT verify, real BFF, real runtime, real UI). Only Slice 2's restart
  scenario adds a genuinely-live dependency (Couchbase), because durability *cannot* be
  demonstrated on an in-memory store — the others need no live infra.

---

## 1. The structural blocker, stated precisely

`scripts/run_ui_runtime.py::build_demo_app()` calls `create_app(...)` with **no `retrieval`
argument** and default `RuntimeSettings` (`retrieval_enabled=True` by default, but `neo4j_url`
unset and no embedder). Tracing `app.py`:

- `retrieval` stays `None` (line 239–285: the neo4j branch needs `neo4j_url` **and** an
  embedder; the demo has neither) → `active_retrieval = None` (line 292).
- With `active_retrieval is None`, `_build_agent_loop` wires **only** `resolveValues`; the three
  read tools **and `runBlueprint` are never registered** (line 351–396), and
  `blueprint_executor` stays `None`.
- Net: the demo runtime advertises no blueprint fast path. A model that emits `runBlueprint` gets
  the loop's unwired-tool path (`RUN_BLUEPRINT_UNAVAILABLE`). **No blueprint scenario can go green
  through this launcher as built.**

Everything else the four runBlueprint scenarios need already exists and is Layer-1/2-proven:
`RunBlueprintTool`, `BlueprintExecutor` (single-node + DAG + approval pause/resume), the D56
verify gate, the ask-user chip UI (`ui/static/index.html` renders `pending_question.options` as
`data-testid="ask-user-option"` chips and resumes via `/api/turn/resume`). The gap is **purely
demo-launcher wiring + scripted doubles**, not runtime capability.

---

## 2. Decisions

### D-L3-1 — Retrieval via seeded in-process fakes, injected through `create_app(retrieval=...)`

`create_app` already accepts a fully-constructed `retrieval: RetrievalPipeline` and honors it
verbatim (`app.py` line 239: "An injected `retrieval` ... is honored as-is and never rebuilt").
The demo launcher will build:

```
FakeEmbeddingClient()                     # deterministic hash vectors, no network
FakeVectorIndex(entries=[...], details={  # seeded blueprint corpus
    "headcount_by_dept":     BlueprintDetail(... sql_template + result_grain ...),  # fast-path
    "headcount_by_dept_bad": BlueprintDetail(... wrong-grain ...),                  # D56 negative
    "avg_tenure_by_dept":    BlueprintDetail(... slot with no default ...),         # ask→clarify
    "headcount_with_approval": BlueprintDetail(... approval node ...),              # D45 approval
})
RetrievalPipeline(embedding_client=fake_embed, vector_index=fake_index,
                  reranker=None, user_memory=NullUserMemoryProvider(), ...)
```

and pass `retrieval=that_pipeline` to `create_app`, leaving `retrieval_enabled=True` (default).
That flips `active_retrieval` non-`None`, which registers the three read tools + `runBlueprint`
+ constructs the `BlueprintExecutor` over the **same per-request `ToolDispatcher`** — i.e. the
blueprint's inner `runQuery` calls flow through the identical D57/D64/D5/provenance choke point as
production. Nothing in `app.py` changes.

**Trade-off:** the `FakeVectorIndex` recall path is exercised (embed→recall→`get_blueprint`), but
recall *ranking quality* is not — acceptable, because Layer-3 asserts **behavior**, not retrieval
relevance (that is Layer-2's `Neo4jVectorIndex` live tests). The alternative (point the demo at
the live `l2-neo4j`) would make the suite non-hermetic, require a seed step, and couple green-ness
to container health — rejected. **Rationale:** the existing 5 green scenarios already use scripted
doubles (`DemoModelClient`/`DemoMCPClient`); a seeded `FakeVectorIndex` is the same posture for the
recall seam and keeps the whole suite deterministic and infra-light.

### D-L3-2 — `DemoMCPClient` content-routes `runQuery` off SQL text, statelessly

The blueprint executor issues its inner queries via
`ToolDispatcher.dispatch("runQuery", {"sql": <sql>, "limit": ...})` — note the **`sql` key**,
whereas the model-emitted `runQuery` in `DemoModelClient` uses the **`query` key**. `dispatch`
forwards `model_args` verbatim to `call_tool` (`tool_dispatcher.py` line 198). So the extended
`DemoMCPClient.call_tool` must read the SQL from **whichever of `sql`/`query` is present** and
route on its content.

The launcher is a **long-lived, multi-session** process, so a FIFO of scripted `runQuery`
responses would drift (session A's second turn consuming session B's first response). The existing
`DemoMCPClient` already solved this for the denial scenarios by **content-routing, not queueing**.
Extend the same override to recognize the blueprint fixtures by **sentinel table/column names
embedded in the seeded `sql_template`s**, and return canned results **purely as a function of the
SQL shape** (no per-instance mutable state):

| Inner query shape (matched substring) | Canned result | Drives |
|---|---|---|
| node SQL over `demo.headcount_by_dept` | `{columns:[department,n], rows:[[Sales,3],...], row_count:3}` | fast-path |
| grain probe `COUNT(*), COUNT(DISTINCT …)` over the good node | `[[3, 3]]` (total==distinct → PASS) | fast-path verify |
| node SQL over `demo.headcount_bad` (fan-out fixture) | `{... row_count:12}` | D56 negative |
| grain probe over the bad node | `[[12, 3]]` (total≠distinct → FAIL) | D56 no-silent |
| DISTINCT-domain probe over `demo.dept_dim.department` | `[[Sales],[Engineering],[Support]]` | slot resolution |
| approval-node upstream scalar query | `[[42]]` (single cell) | D45 approval |

Because routing is a pure function of the SQL string, **any number of sessions/turns replay
identically** — the same "survives unbounded demo turns" property the current doubles guarantee.
This is demo plumbing only; the real MCP derives these from ClickHouse (Layer-2 live-proven).

**Verification note for the builder:** confirm the exact key the real MCP `runQuery` schema uses
(`sql` vs `query`) when finalizing; the safe implementation reads `args.get("sql") or
args.get("query")` and matches on that, so it is correct regardless.

### D-L3-3 — `DemoModelClient` grows blueprint trigger phrases

Add content routes (same "route off first user message, decide on message shape" pattern the class
already uses — indefinitely replayable, no queue):

| First-message phrase | Emits | Second call (resume / tool result present) |
|---|---|---|
| `"headcount by department"` | `runBlueprint(id="headcount_by_dept", slot_bindings={"dept":"Sales"})` | final answer echoing the verified result |
| `"bad headcount"` | `runBlueprint(id="headcount_by_dept_bad", slot_bindings={"dept":"Sales"})` → VERIFY_FAILED | falls back to a raw `getTableSchema` → graceful answer (never the withheld rows) |
| `"average tenure"` | `runBlueprint(id="avg_tenure_by_dept", slot_bindings={})` → slot pause | on resume (2 user msgs) re-emit `runBlueprint(id=..., slot_bindings={"dept":<answer>})` → final answer |
| `"approve headcount"` | `runBlueprint(id="headcount_with_approval", slot_bindings={"dept":"Sales"})` → approval pause | on resume re-enter executor at `awaiting_node`; final answer after approve |

For the **no-silent-verification** route the model must, on seeing the `runBlueprint` *error*
tool-result (VERIFY_FAILED), do exactly what production does: **fall back to the raw loop** and
produce an answer that does **not** contain the withheld rows. The scripted double models this by
emitting a `getTableSchema` follow-up (or a plain final answer) — the point the test asserts is
that the fan-out numbers (`12`) never reach the DOM and no error banner appears.

### D-L3-4 — Mid-session scope narrowing: an env-gated BFF re-mint endpoint

D82's model is: the BFF mints exactly one JWT per session at `/api/session` and the browser never
sees it. The obstacle to the D44 scenario is that there is **no way to switch `column_scope`
between turns**. Add a **test-only** BFF endpoint, active only when `UI_TEST_AFFORDANCES=1`:

```
POST /api/session/scope   { "session_id": ..., "column_scope": ["demo.headcount_by_dept.department", ...] }
```

It re-calls the token service server-side with the narrower `column_scope`, replaces
`_SESSIONS[session_id]`, and returns `{"ok": true}`. **D82/D5 stay intact:** the BFF is still the
only holder of the JWT; the browser still never receives it; the endpoint just lets the *test
harness* drive a scope change that a real product would drive from an identity provider. Guarding
it behind `UI_TEST_AFFORDANCES` means the production BFF never exposes it.

**Scenario shape (`D44-scope-narrows-drops-replay`):**
1. Turn 1 (wide scope, `column_scope=[]` = allow-all): ask a question that surfaces an in-scope
   result; assert the answer/context renders.
2. Playwright calls `POST /api/session/scope` narrowing scope to exclude the payroll column.
3. Turn 2: ask a follow-up that would replay turn 1's context; assert the earlier payroll-derived
   content is **absent** from the rendered page (D44 fail-closed replay filter drops the
   now-out-of-scope prior assistant message).

The teeth already exist server-side (`ContextAssembler` + `scope_filter`, and the adversarial
replay test `tests/runtime/provenance/test_fail_closed_replay_adversarial.py`); this scenario is
the end-to-end UI proof over the real BFF.

### D-L3-5 — Observability + PII: in-memory span exporter, not a Phoenix container

`app.py` calls `tracing.configure_tracing(otlp_endpoint=settings.otlp_endpoint, ...)`. The demo
never sets `otlp_endpoint`, so no spans are exported. Two options:

- **(A) Real Phoenix container** in the Layer-3 stack + assert spans arrive via its query API.
- **(B) In-process `InMemorySpanExporter`** wired by the demo launcher under a test env toggle,
  plus a **test-only `GET /_test/spans`** endpoint on the runtime that dumps the captured spans'
  names + kinds + attributes (keys AND stringified values). Dumping the *values* is deliberate
  and load-bearing: the D25 invariant under test is "no attribute VALUE carries a cell/JWT/SQL
  literal", which can only be asserted by inspecting the values — a keys-only dump could not prove
  it. Safe because the route is env-gated + test-only (registered only when the in-memory exporter
  is injected); it is never present on the production HTTP surface.

**Choose (B).** The D25 invariant under test is "**spans are emitted and carry no cell
values/JWT/PII**" — an in-memory exporter proves exactly that, hermetically, with no extra
container, no network, and no flaky collector readiness. A real Phoenix adds operational surface
(a 12th service, a query API to poll, startup races) for zero additional assurance about the
invariant. `configure_tracing` should grow a small seam to accept an injected exporter (or the
launcher installs an `InMemorySpanExporter` via a test-only settings flag); the `/_test/spans`
route is registered only when that flag is set.

**Scenario shape (`D25-spans-emitted-pii-clean`):** drive one ordinary turn; `GET /_test/spans`;
assert (a) at least one AGENT + one TOOL span exist, and (b) no span attribute value matches the
JWT, a raw SQL string, or a cell value — reusing the assertions from
`tests/runtime/observability/test_redaction.py` at the Playwright layer. A follow-up: assert a
`GUARDRAIL` span appears on the leakage path if we drive one.

### D-L3-6 — Restart durability: Couchbase-backed launcher + subprocess bounce

Durability across a *process* restart cannot be shown on `InMemorySessionStore` (it loses state by
design). This one scenario needs a real, out-of-process store. The `l2-cb` Couchbase service
already exists in `docker-compose.integration.yml` and `scripts/couchbase-init.sh` seeds it.

Plan: when `DEMO_SESSION_STORE=couchbase`, `build_demo_app()` constructs
`CouchbaseSessionStore(settings)` (pointed at `l2-cb`) instead of `InMemorySessionStore`. The
conftest fixture (which already owns the `:8000` subprocess lifecycle) gains a restart-capable
variant for this scenario:

**Scenario shape (`D45-pause-resume-survives-restart`):**
1. Launch runtime (Couchbase store). Send `"approve headcount"` → approval pause; the checkpoint
   is persisted to Couchbase before yielding (D45).
2. Playwright/conftest **terminates and relaunches the `:8000` subprocess** (fresh process, same
   Couchbase). The browser page is untouched (the pending ask-user UI is still shown).
3. Click **approve** → `/api/turn/resume` hits the fresh runtime, which CAS-consumes the
   Couchbase checkpoint and resumes the executor at `awaiting_node`; assert the answer renders and
   no error banner.

This is the **only** scenario that adds a live dependency. Isolate it (own class, own
Couchbase-backed fixture) so a Couchbase outage fails exactly one test, never the other ten.

**Trade-off:** it makes this scenario slower and infra-coupled. Accepted because the D45 invariant
is *specifically* about surviving a real restart — a mock cannot prove it (the README already
concedes this).

### D-L3-7 — Posture: doubles by default, live only where the invariant demands it

| Seam | Slice 1 (10 scenarios) | Slice 2 restart scenario |
|---|---|---|
| Model provider | `DemoModelClient` (fake) | `DemoModelClient` (fake) |
| ClickHouse MCP | `DemoMCPClient` (fake) | `DemoMCPClient` (fake) |
| Vector index / corpus | seeded `FakeVectorIndex` | seeded `FakeVectorIndex` |
| Embedder | `FakeEmbeddingClient` | `FakeEmbeddingClient` |
| Session store | `InMemorySessionStore` | **`CouchbaseSessionStore` (live `l2-cb`)** |
| JWT verification | **real** (`l2-token` JWKS) | **real** |
| BFF + runtime + UI | **real** | **real** |
| Spans | in-memory exporter (Slice 2) | — |

Honest statement for the traceability matrix: Layer-3 proves the system **behaves per spec**
through the real UI/BFF/runtime with deterministic doubles at the model+warehouse+recall seams;
data *correctness* over real ClickHouse/neo4j stays Layer-2 (live) and Layer-4 (eval). The one
exception is restart durability, which is genuinely live against Couchbase because that is the
only faithful proof.

---

## 3. Slice plan

### Slice 1 — Retrieval wiring + the 4 runBlueprint scenarios (hermetic, in-memory)

**Goal:** flip the structural blocker and take the 4 runBlueprint scenarios green with zero new
infra and zero risk to the current 5.

**Build:**
- `scripts/run_ui_runtime.py`: build the seeded `FakeVectorIndex` + `FakeEmbeddingClient` +
  `RetrievalPipeline`; pass `retrieval=` to `create_app`. Seed 4 `BlueprintDetail` fixtures
  (good, wrong-grain, slot-pause, approval).
- `DemoMCPClient`: extend `call_tool` to content-route `runQuery` (both `sql`/`query` keys) for
  node/domain/grain-probe queries (D-L3-2 table), statelessly.
- `DemoModelClient`: add the 4 blueprint trigger phrases (D-L3-3 table).
- `tests/e2e/test_conformance.py`: 4 new test classes.

**Doubles:** all existing + seeded `FakeVectorIndex`/`FakeEmbeddingClient`. No live infra beyond
the already-required `l2-token`.

**Page objects / testids (all already present in `ui/static/index.html` — no UI change needed):**
- fast-path: `progress-item` (runBlueprint emits `tool_dispatch_start/ok` → "running
  runBlueprint…"/"step complete: runBlueprint"), `answer` non-empty, `status` contains "done".
- no-silent-verification: `answer` non-empty and does **not** contain the fan-out number(s);
  `error-banner` hidden; assert the withheld rows never hit `body.inner_text()` (same
  leak-assertion technique as the scope-denial test).
- ask→clarify: `ask-user` visible, `ask-user-option` chips present, click → `ask-user` hidden →
  `answer` non-empty.
- approval: `ask-user` visible with `data-option="approve"` / `data-option="deny"` chips (reuses
  the budget-cap chip pattern exactly); click approve → `answer` renders, `status` "done".

**New testids required:** none. (Optional nice-to-have: render the blueprint `sql`/"Using
blueprint…" chip the docs mention — deferred; the UI's line-280 TODO. Scenarios assert on progress
+ answer, which the UI already surfaces.)

**Regression guard for the 5 green:** wiring `retrieval` changes the advertised tool list (from
the getTableSchema/runQuery pair to the full 12-tool schema). The 5 green scenarios route off the
**first user message** and their `DemoModelClient` branches are unchanged, so they still emit
`getTableSchema`/`runQuery`/`askUser` regardless of what else is advertised. Add an explicit
regression assertion: re-run the 5 existing classes unchanged in the same CI invocation. The
`DemoMCPClient` change is **purely additive** (new SQL-content branches; the two existing sentinel
denials and the `getTableSchema` queue fall through untouched).

### Slice 2 — The 3 infra-touching Phase-0 scenarios

**Goal:** close the remaining red without destabilizing Slice 1.

**Build:**
- **Scope narrowing:** `ui/server.py` gains env-gated `POST /api/session/scope` (D-L3-4);
  `test_conformance.py` gains `D44-scope-narrows-drops-replay`.
- **Observability:** `tracing.configure_tracing` seam for an injected `InMemorySpanExporter`;
  demo launcher installs it under a test flag + registers `GET /_test/spans`; test
  `D25-spans-emitted-pii-clean`.
- **Restart durability:** `build_demo_app()` selects `CouchbaseSessionStore` under
  `DEMO_SESSION_STORE=couchbase`; a restart-capable conftest fixture; test
  `D45-pause-resume-survives-restart` in its own class with its own fixture.

**Doubles vs live:** scope-switch + spans stay hermetic (fakes + in-memory exporter). Restart is
live against `l2-cb`.

**Page objects / testids:** scope narrowing reuses `answer` + `body.inner_text()` absence
assertions; spans assert via `httpx.get(:8000/_test/spans)` inside the test (no DOM); restart
reuses the approval chips from Slice 1.

**New testids required:** none.

**Regression guard for the 5 green + Slice 1:** the three new endpoints/exporters are **all
env-gated** (`UI_TEST_AFFORDANCES`, the tracing flag, `DEMO_SESSION_STORE`). With the flags unset
(the default the current 10 scenarios run under), the BFF/runtime are byte-identical to today.
Only the restart class opts into Couchbase; every other scenario keeps `InMemorySessionStore`.

---

## 4. The single biggest regression risk to the 5 green scenarios

**Wiring `retrieval` into the demo launcher changes the advertised tool schema and adds a
`ContextAssembler` retrieval pre-injection step to *every* turn — including the 5 green ones.**

Concretely: with `active_retrieval` non-`None`, `ContextAssembler` now runs the retrieval
pipeline (embed the question via `FakeEmbeddingClient` → `FakeVectorIndex.recall` → pre-inject) on
every turn, and emits `retrieval_start`/`retrieval` progress events (`progress.py` lines 48–49)
that did not exist before. Two failure modes for the green 5:

1. **Progress-count/shape drift.** `TestProgressStreaming` asserts `progress-item` count `!= 0`
   and the answer renders — robust to *extra* progress items. But any test that assumed a
   *specific* first/only progress step could break. (Current assertions are count-based, so this
   is low but non-zero.)
2. **A seeded blueprint accidentally matching a green scenario's question.** If `FakeVectorIndex`
   recall returns a blueprint card for e.g. "show me the columns" and the pre-injected context
   nudged a *real* model, behavior would shift — but the `DemoModelClient` is content-routed and
   **ignores injected context entirely** (it routes off the raw first user message), so the model
   double is immune. The residual risk is only in the *progress stream* and *pre-injection latency*,
   not in the routed decision.

**Mitigation:** (a) keep the seeded corpus's recall text disjoint from the 5 green trigger phrases
so `retrieval` returns 0 cards for them (shape `{blueprints:0, knowledge:0}`); (b) before merging
Slice 1, run the 5 existing classes as an explicit regression gate and confirm they stay green
with `retrieval` wired; (c) if progress drift bites, the fix is assertion-hardening (count/`not
have count 0`), not reverting the wiring. The `DemoModelClient`'s content-routing (not
context-consuming) design is what makes this risk manageable rather than fatal.

---

## 5. Documentation updates to enumerate (not made in this change)

- `docs/11-testing.md` — flip the 7 scenarios' Layer-3 status from 🟡 to green as each lands;
  update the "runBlueprint Layer-3 status (D89)" note; add the in-memory-exporter (not Phoenix)
  decision to the "Determinism of the LLM in e2e" / stack-topology sections (the topology diagram
  lists `phoenix` — annotate that Phase-0 Layer-3 uses an in-process exporter instead).
- `docs/decisions/TRACEABILITY.md` — update the Conformance-scenario Status column for D89 (the 4
  runBlueprint rows), D44, D25, D45 from 🟡 unit/Layer-2-green to Layer-3-green; keep the "not yet
  built" preamble accurate.
- `docs/decisions/DECISIONS.md` — add a short decision entry recording: (i) Layer-3 uses seeded
  `FakeVectorIndex`/`FakeEmbeddingClient` for the recall seam; (ii) the env-gated BFF scope-switch
  test affordance and why it preserves D82/D5; (iii) in-memory span exporter over a Phoenix
  container for the D25 Layer-3 proof; (iv) the Couchbase-backed restart scenario as the sole live
  dependency. Cross-reference D68/D44/D25/D45/D56/D89/D82.
- `tests/e2e/README.md` — move the "Deliberately DEFERRED (3)" section into "Covered", document the
  new env flags (`RUN_E2E`, `UI_TEST_AFFORDANCES`, `DEMO_SESSION_STORE`, the tracing flag) and the
  Couchbase prerequisite for the one restart test; note the seeded blueprint fixtures.
- `scripts/run_ui_runtime.py` module docstring + `scripts/run_ui.sh` — document the retrieval
  wiring, the blueprint trigger phrases (extend the trigger-phrase table), and the env toggles.
- `docs/08-ui.md` — note the (optional/deferred) "Using blueprint… + SQL shown" UI affordance the
  fast-path scenario would ideally assert, currently the `index.html` line-280 TODO; record that
  the Layer-3 fast-path assertion is scoped to progress + answer until that lands.

---

## 6. Open questions / deferred

1. **Real MCP `runQuery` arg key** (`sql` vs `query`): confirm when implementing D-L3-2; the
   defensive `args.get("sql") or args.get("query")` route is correct either way.
2. **"Using blueprint…" chip + SQL rendering** in the UI (docs/11 "Ask → fast path" asserts "SQL
   shown"): deferred behind the `index.html` line-280 TODO. Slice 1 asserts progress+answer only;
   a follow-up brick can add the richer result payload + a `data-testid="blueprint-chip"` /
   `data-testid="sql"` and tighten the fast-path assertion.
3. **`configure_tracing` injection seam** shape (accept an exporter arg vs. read a test settings
   flag): a small decision for the Slice-2 implementer; either keeps production untouched.
4. **Restart fixture mechanics** (terminate+relaunch the `:8000` Popen vs. a second app instance
   sharing Couchbase): recommend the subprocess bounce (most faithful to "a runtime instance
   restarted"), but a shared-store second instance is an acceptable, faster fallback if the bounce
   proves flaky in CI.
5. **Whether the restart scenario gates a release or runs informationally** (docs/11 open
   question #4): recommend gating, since D45 durability is a core safety invariant — but flag for
   the release-gate owner.
