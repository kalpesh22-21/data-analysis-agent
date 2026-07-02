# Table-Intermediate DAG Execution — the F2 Scratch-Write Side-Channel + Runtime Materialize-and-Join

**Status:** Proposed (design only; nothing built this pass). Cross-repo brick, three slices proposed.
**Brick:** the deferred **F2** boundary of the `runBlueprint` brick — a node that outputs a *table*
which a downstream node `JOIN`s. Blocked since D89 on the absence of a scratch-*write* surface.
**Spans two repos:** `clickhouse-api` (the MCP + the new privileged write side-channel) and
`data-analysis-agent` (the runtime executor that materializes and rewrites).
**Precedent it builds on:** `runblueprint-design.md` (§F2 boundary, §2.4 scalar-only, `_has_table_intermediate`),
D89 (runBlueprint as-built), the existing `clickhouse-api` write path (`app/admin_ingest.py`), the
existing scratch **read** isolation (`app/sqlparse/provenance.py` + `app/service.py`, D64/D69).
**Owning spec:** `04-blueprints.md` (§Execution step 4 table branch, the convergence-DAG example),
DECISIONS D19/D20/D21 (scratch side-channel), D64 (scratch session isolation), D59a (scalars→params /
tables→scratch JOIN), D5/D57/D63/D80 (read-plane enforcement), D45/D55 (pause/resume + scratch
reconnect), D69 (scratch columns session-gated), D89 (runBlueprint boundary).

---

## 0. Ground truth this design relies on (verified by reading both repos)

The scary framing — "a WRITE path into the warehouse DB" — is **materially softened by what already
exists**. Three findings reshape the risk and the build:

- **G1 — the privileged write primitives already exist and are hardened.** `clickhouse-api/app/admin_ingest.py`
  already builds a one-off ClickHouse client (`build_admin_client`, *not* the read-only singleton, no
  `readonly_settings`) and does `CREATE TABLE` / `DROP TABLE` / batched `INSERT` — with a strict
  safe-identifier regex (`_SAFE_IDENTIFIER_RE`, `validate_identifier`), a ClickHouse-type whitelist
  (`_ALLOWED_TYPE_RE`, `validate_ch_type`), string-literal escaping (`_escape_sql_string`), and — the
  load-bearing part — **native bulk insert** (`client.insert(data=rows, column_names=…)`), which passes
  rows as native Python objects to the driver, **never as SQL text**. This is exactly the D19 "privileged
  side-channel, not an agent tool" primitive set, today reachable only via the CSV-shaped, admin-key
  `/admin/ingest` route. **The scratch-write surface is a re-shaping of proven code, not net-new DDL/DML.**

- **G2 — the scratch *read* isolation (D64/D69) is fully built and tested; only the write half is missing.**
  `app/sqlparse/provenance.py` already knows the `scratch` database, validates `s_<session_id>_*`
  ownership (`_validate_scratch_name` → `ScratchSessionError`), and `app/service.py`'s `run_query` /
  `sample_rows` already: (a) session-gate scratch tables via the injected `X-Session-Id`, (b) reject
  foreign/malformed scratch names as `SCRATCH_SESSION_VIOLATION`, (c) **exclude** scratch columns from
  the column-scope allowlist (D69/OQ-4 — scratch is session-gated, not scope-gated). So the moment a
  `scratch.s_<sid>_*` table *exists*, a downstream `runQuery` that `JOIN`s it is **already** correctly
  enforced. The runtime side does not have to invent read enforcement; it inherits it.

- **G3 — the runtime seam is clean and the transport already carries the session binding.** The executor
  already isolates the boundary in one function, `_has_table_intermediate` (executor.py:1088), which
  today returns `UNSUPPORTED`. `RealMCPClient._headers` already sends `Authorization: Bearer <jwt>` +
  `X-Session-Id: <session_id>` on every call; `RuntimeCredentials` already carries `session_id`. A
  materialize-and-join step drops into `_execute_dag` between the producing node's dispatch and the
  consuming node's dispatch, and a new scratch client reuses the exact same credential bundle.

Further verified, non-blocking:
- `run_query`/`sample_rows` treat scratch columns as **session-gated, not scope-gated**, so a
  scratch-JOIN's provenance union naturally excludes scratch columns (the D69/OQ-4 filter:
  `if not db_tbl.startswith("scratch.")`). Provenance stays honest and fail-closed.
- The MCP can host a **non-tool** HTTP route behind the same auth (`mcp.custom_route(...)` already used
  for `/health` and the OAuth PRM), so the write endpoint can share `JWTAuthMiddleware` — inheriting JWT
  validation **and** the `current_session_id` binding — while never being advertised as an MCP tool.
- D89(d) already fail-closes a **scalar** intermediate that returns `!=1` row / wrong shape *before* the
  consumer runs. Table intermediates get the analogous structural guard (§Q3), plus the terminal D56
  grain gate still guards the final answer.

---

## One-line answers (Q1–Q7)

- **Q1 (write surface):** A new **non-tool** `POST /scratch/materialize` route on the MCP service,
  behind the same `JWTAuthMiddleware` (JWT + `X-Session-Id`), executing via a **scratch-only privileged
  ClickHouse credential** (GRANT on `scratch.*` only); it CREATEs `scratch.s_<injected-session_id>_<token>`,
  native-bulk-INSERTs the rows, sets a table TTL, returns the table name — the session_id comes from the
  header, **never the body**, so cross-session writes are impossible by construction.
- **Q2 (runtime):** In `_execute_dag`, after a table-output node's `runQuery` returns, the runtime calls
  the side-channel with those rows (native data, never SQL), gets back a runtime-controlled scratch
  identifier, and **AST-rewrites** the consumer's `feeds_from` table placeholder to `scratch.s_<sid>_<token>`
  — name is runtime-controlled, rows are data, neither is model text.
- **Q3 (security):** Blast radius is bounded to the `scratch` schema by the credential GRANT; isolation
  is by header-injected session_id at write **and** the D64 parse at read; a blueprint cannot smuggle a
  write because the endpoint is not an agent tool and the runtime only materializes an already-scope-checked
  node result; orphans TTL away (D20/D45); the credential can never touch the warehouse.
- **Q4 (executor):** Flip `_has_table_intermediate` from reject→materialize; the F2 boundary shrinks to
  **cross-session (never), oversized intermediates (row/byte cap → raw loop), and non-tabular**; D56 verify
  stays on the terminal; the materialize call is inside the one `runBlueprint` tool call (budget = 1);
  pause/resume **re-introduces D55 scratch-reconnect** (the one real new cost F2 previously bought us out of).
- **Q5 (slices):** Slice 1 = the `clickhouse-api` scratch-write side-channel + tests (L1 + L2 vs real
  ClickHouse); Slice 2 = the runtime materialize-and-join + executor flip + a seed blueprint + live e2e;
  Slice 3 = pause/resume scratch-reconnect (D55) + Layer-3 convergence-DAG conformance.
- **Q6 (cross-repo):** `clickhouse-api` ships the endpoint; `data-analysis-agent` ships the client+executor;
  the contract is the `/scratch/v1/materialize` request/response shape below, versioned by a path prefix +
  a `scratch_api` capability line on `/health`; the Layer-2 stack wires the endpoint URL like `MCP_URL`.
- **Q7 (recommendation):** Build Slice 1 (low marginal risk — reuses G1, unblocks). **Gate Slice 2/3 on a
  concrete table-intermediate blueprint existing** — if none does, the scalar-converging subset already
  covers the reports and this is YAGNI (see §Recommendation).

---

## Q1 — The scratch-write surface

### 1.1 Where it lives — a non-tool route on the MCP, not a 7th read tool

The six data-plane tools stay read-only (D4/D80); this is a **separate privileged side-channel** (D19,
*"not an agent tool"*). It is **not** an MCP tool: it is never registered with `@mcp.tool`, never appears
in `list_tools`, and the agent LLM never sees or can invoke it. It rides as a custom HTTP route on the
**same MCP Starlette app** so it inherits the one thing that matters — the `JWTAuthMiddleware` that binds
`current_session_id` from `X-Session-Id` (`mcp_server.py:515`). That gives the write path the *identical*
session binding the read path uses for D64, with zero new session plumbing.

```
POST  {MCP_HOST}/scratch/v1/materialize     # create + load, one call (the executor's need)
POST  {MCP_HOST}/scratch/v1/drop            # explicit best-effort drop (end-of-DAG cleanup)
```
(A `/scratch/v1/` prefix + a `scratch_api: "v1"` line on `/health` is the contract version, §Q6.)

### 1.2 The three operations, collapsed to two calls

- **Create + load (one atomic `materialize`).** The executor already holds the producing node's rows in
  memory (the `runQuery` result). So the surface does not need a separate "create then stream" dance for
  Phase-1 sizes: **one call** takes `{columns, rows}`, CREATEs `scratch.s_<sid>_<token>`, native-bulk-INSERTs,
  sets a TTL, returns the table name. This mirrors `admin_ingest.ingest(mode="create")` but session-named
  and JSON-shaped instead of admin-key + CSV.
- **Drop (best-effort).** `runBlueprint` calls `/scratch/v1/drop` on the tables it created when the DAG
  finishes cleanly (courtesy GC). It is **best-effort** — a missed drop is covered by the table TTL, so
  a crash never leaks (D20/D45).
- **TTL is the real cleanup** — every scratch table is created with a ClickHouse `TTL now() + INTERVAL
  <SCRATCH_TTL>` (a wall-clock table TTL) so orphans from a crash/abandoned pause GC themselves with no
  external sweeper (D20 "TTL'd", D45 "orphaned scratch TTLs away").

### 1.3 Schema, naming, and type derivation

- **Naming:** `scratch.s_<session_id>_bp_<uuid4hex>`. The `s_<session_id>_` prefix is built **from the
  `X-Session-Id` header** inside the endpoint — never from the request body — so the caller cannot name a
  foreign session's table. The `bp_<uuid>` suffix is server-generated (collision-free, and it means two
  materializations in one DAG never clash). This is byte-compatible with the read-side
  `_validate_scratch_name` prefix check (`s_<session_id>_…`), so the very table this endpoint writes is one
  the D64 read gate will accept **for this session only**.
- **Columns/types:** the request body carries `columns: [{name, type}]`. Two honest options for `type`:
  1. **(default) runtime-inferred** — the runtime infers a ClickHouse type per column from the result
     values, reusing the proven `admin_ingest.infer_column_type` logic (Int64 → Float64 → DateTime →
     Date → String, Nullable-wrapped on any empty). Robust, already battle-tested.
  2. **(future) upstream-declared** — if a later `getTableSchema`/runQuery ever returns column *types*,
     use them verbatim. Not available today (`runQuery` returns `{columns, rows}`, no types).
  Every `type` is re-validated server-side against `validate_ch_type` (the existing whitelist) — an
  un-whitelisted type is rejected, never interpolated. Every `name` passes `validate_identifier`.
  **Trade-off (OQ-A):** inference can mistype an all-NULL column as `Nullable(String)`; a JOIN key that
  is really an Int lands as String → the downstream JOIN either coerces or misses. Mitigation: the
  producing node's template should `CAST` join keys to a stable type, and the seed blueprint declares it.

### 1.4 Write auth — who may call vs. what capability is exercised

Two distinct things, deliberately separated (this is the crux of "privileged side-channel"):
- **Who may call (client auth):** the **same JWT + `X-Session-Id`** the read plane uses. The trusted agent
  backend already forwards both (`RealMCPClient._headers`). No new client secret. The JWT authenticates
  *who*; the header supplies the *session binding* that names the table.
- **What capability is exercised (server privilege):** a **distinct, server-side-only ClickHouse credential**
  (`SCRATCH_CH_USER`/`SCRATCH_CH_PASSWORD` config) GRANTed `CREATE/INSERT/DROP/SELECT` on **`scratch.*` only**
  — never the warehouse databases. The runtime never sees or holds this credential; it lives in the MCP
  deploy config. So "privileged" = the server-side scratch-only grant + the non-tool nature, **not** a
  privileged client token.

This reconciles D19 ("privileged side-channel") with D5/D64 ("MCP receives scope as input; the parse is the
live boundary"): the *privilege* is a confined server-side grant; the *isolation* is the injected session_id
at write and the D64 parse at read. **Rejected alternative:** a separate admin-key/mTLS credential between
runtime and endpoint (like `/admin`'s `require_admin_api_key`). It adds a secret to rotate and does **not**
session-bind the caller, so the read-side D64 check would still be the only isolation — no gain, more ops.
Kept as OQ-B in case a deployment wants runtime↔scratch mutual-TLS as defense-in-depth.

### 1.5 Interplay with the extractor's existing D64 check

Nothing changes in the extractor. It already validates that a `runQuery` only references
`s_<injected-session_id>_*`. This design simply makes such a table *exist* (written under the same session_id),
so the pre-existing read gate lets this session — and only this session — `JOIN` it. The write endpoint and
the read extractor agree on the naming contract (`s_<session_id>_*`) by construction; the endpoint is the
producer, the extractor is the gate, and both derive the prefix from the same header-injected session_id.

---

## Q2 — The runtime side: materialize, then rewrite the JOIN

### 2.1 A `ScratchClient` (new, thin)

A new `runtime/mcp/scratch_client.py` (sibling to `RealMCPClient`) with one real method:
`materialize(columns, rows, *, jwt, session_id) -> str` (returns the scratch table name) and
`drop(table_name, *, jwt, session_id) -> None`. It POSTs to `/scratch/v1/materialize` with the same
header pair `RealMCPClient` sends. A `FakeScratchClient` (Layer-1) records calls and hands back a
deterministic `scratch.s_<sid>_bp_<n>` name so the executor path is testable with zero infrastructure —
exactly the `FakeMCPClient` pattern. It is injected into `BlueprintExecutor.__init__` as `scratch_client`
(defaulting `None` → table intermediates stay `UNSUPPORTED`, so the executor degrades cleanly if a deploy
has no scratch surface — reversible).

### 2.2 The materialize-and-join step in `_execute_dag`

Today's walk (executor.py:493) dispatches each node's SQL and, for a scalar-output node, extracts a single
cell to bind downstream. The table path adds, **after** a table-output node's successful dispatch:

1. **Structural guard (mirrors D89(d) scalar guard):** the producing node returned rows; enforce a
   **row cap** (`SCRATCH_MAX_ROWS`, config) and a **column cap** — an oversized intermediate → `ExecFailed(
   UNSUPPORTED)` → raw loop (never a runaway materialization). An empty result is allowed (an empty scratch
   table `JOIN`s to nothing — a legitimate "no rows" answer the terminal grain gate still validates).
2. **Materialize:** `name = await scratch_client.materialize(columns, rows, jwt=…, session_id=…)`. The
   `rows` are the untrusted result cells passed **as native data** (the endpoint bulk-inserts them, never
   string-interpolates — G1). Provenance of the **producing** node's query is already captured at its
   dispatch and folded into the union (unchanged).
3. **Rewrite the consumer's `feeds_from` table token → the scratch table (AST, injection-safe):** the
   consuming node's `sql_template` references its upstream by a **table placeholder** in FROM/JOIN position
   (the table analogue of the scalar `{consume}` placeholder — see §2.3). The executor parses the template
   AST and replaces that placeholder `exp.Table` node with `exp.Table(this=<token>, db="scratch")` where the
   token is the **runtime-controlled** `s_<sid>_bp_<uuid>` (a safe identifier the runtime built, never model
   text). Regenerate SQL from the AST. The scratch **name** is structural (runtime-owned identifier); the
   **rows** are data (native-loaded). At no point does model text or a result cell become SQL syntax (D10).
4. **Dispatch the consumer** through the same `ToolDispatcher.dispatch("runQuery", …)` choke point. Its SQL
   now reads `scratch.s_<sid>_bp_<uuid>` + its own warehouse tables → the D64 read gate validates scratch
   ownership, D57 scope-checks the warehouse columns, D69/OQ-4 excludes scratch columns from the scope union.
   **Provenance of the scratch-JOIN query** = its warehouse columns (scope-checked) ∪ (scratch columns
   *excluded* by the D69 filter). Fail-closed exactly as any node query.

### 2.3 The `composes`/`feeds_from`/`consumes` table-passing shape

`04-blueprints.md` already models a `table` output (`output: { flagged: table }`) and `consumes`. Phase-1
scalar passing binds `consumes: {placeholder: "$N.name"}` as a literal. Table passing extends `consumes` to
name a **table slot**: `consumes: { "$1": table }` meaning "node 1's table output is injected as the table
placeholder `{$1}` in my FROM/JOIN." The loader (Slice-A machinery, extended) validates that:
- a `table`-typed consume maps to a placeholder that appears **only in a table position** (FROM/JOIN),
  never in an expression/value position (a table token in a value slot is a load-time rejection);
- the producing node's declared `output` is `{name: table}` and it `feeds_from` correctly;
- the scope-honesty gate treats `scratch.{$N}` as a session-gated source (not requiring it in `uses`,
  matching the read-side D69/OQ-4 exclusion) — the consumer's **warehouse** columns still must be ⊆ `uses`.

### 2.4 Scope argument (verify it holds — the load-bearing claim)

**Claim:** table passing introduces no scope escalation. **Proof:** a scratch table is materialized **only**
from an already-dispatched node result. That producing query went through `ToolDispatcher.dispatch("runQuery")`,
so its columns were D57 scope-checked (or the whole node failed closed). Therefore every column value that
lands in scratch was data the session was **already permitted to read**. The downstream consumer reads
scratch (session-gated, D64) plus its own warehouse columns (independently D57 scope-checked). No path lets
scratch contain, or a JOIN surface, a column the session could not already read. The scratch table is a
**projection of in-scope data**, so downstream enforcement remains sound. (This is the table analogue of the
scalar argument in runblueprint-design §2.4: an intermediate is always a scope-checked node result, never
model-supplied.)

### 2.5 Provenance & D56 on a scratch-JOIN

- **Provenance:** the union spans the producing node's provenance (its warehouse columns) + the consumer's
  warehouse columns; scratch columns are excluded (D69/OQ-4), so the D44 replay filter checks exactly the
  real warehouse footprint. Fail-closed: any inner `None` poisons the union (unchanged `_union_provenance`).
- **D56 verify** stays on the **terminal** node (D89(b)). A materialized intermediate is not the terminal,
  so it is not grain-gated on its own — **but** a fanned-out intermediate would inflate the terminal join,
  and the terminal grain-integrity row-count probe catches that in the final result. The structural row/column
  cap (§2.2 step 1) is the intermediate's own guard against a runaway; the terminal grain gate is the
  correctness teeth. Honest limit: a subtle intermediate mis-grain that happens to leave the terminal
  distinct-grain count correct is not independently caught (same class of gap D89 accepts for measure-agg).

---

## Q3 — Security analysis (the write path, threat-modeled)

**Adversaries:** (1) the agent LLM (can pick any blueprint id, propose hostile slot values); (2) upstream
result cells (hostile *data* — may be PII or attacker-controlled strings from the warehouse/scratch).
**The trusted parties:** the runtime backend (holds the JWT, forwards the real session_id) and the MCP.

**Blast radius.** The privileged credential is GRANTed on **`scratch.*` only**. A total logic bug in the
endpoint — wrong table name, wrong SQL — still cannot CREATE/INSERT/DROP outside `scratch`. The warehouse
databases are physically outside the credential's reach. This is the single most important containment: the
"write into the warehouse DB" is, by grant, a write into a **disposable, session-partitioned scratch schema**,
never a warehouse table.

**Isolation (own-session-only), enforced twice.**
- **At write:** the table name's `s_<session_id>_` prefix is built from the `X-Session-Id` header inside the
  endpoint, never from the body. The model never sees or controls `session_id` (D5). So a caller cannot write
  another session's table.
- **At read:** the D64 parse gate on `runQuery` rejects any `scratch.*` reference not matching the injected
  session_id (`ScratchSessionError` → `SCRATCH_SESSION_VIOLATION`). Already built and tested.
- Two independent boundaries (write-naming + read-parse) must both fail for a cross-session leak — defense
  in depth.

**Can a blueprint smuggle an arbitrary write?** No. The write endpoint is **not an agent tool** — not
registered, not advertised, not in `list_tools`; the model cannot call it. Only the runtime materializes,
and only: (a) from an **already-scope-checked** node result, (b) into `scratch`, (c) with a
runtime-generated name, (d) rows loaded as native data. The model's only inputs are the blueprint id and
slot values (bound as literals). There is no request shape the model controls that reaches DDL/DML.

**Injection.** The table name is a runtime-built safe identifier (`validate_identifier` server-side too —
belt and suspenders). The rows are native-inserted (`client.insert(data=…)`), never concatenated into SQL
(G1). Column names/types are whitelisted server-side. A hostile cell value (`'; DROP TABLE …`) is inserted
as a literal string cell and, when later `JOIN`ed, is compared as data — never parsed as SQL.

**TTL / cleanup on crash.** Every scratch table carries a wall-clock ClickHouse TTL (`SCRATCH_TTL`). A crash
mid-DAG (non-paused) → the whole turn re-runs (read-only + idempotent path; materialize re-creates a **new**
uuid-named table) → the old table is orphaned → TTLs away. **Re-run is the compensation** (D45): there is no
warehouse write to undo, only scratch to GC. An abandoned pause expires via `SESSION_TTL` + the table TTL.
Confirmed consistent with D45's "orphaned scratch TTLs away."

**The privileged credential's scope.** Config-only, scratch-only GRANT. **Hard invariant:** the credential
must be provisioned with no warehouse grant. This is an ops/deploy invariant (a `GRANT` statement + a helm
secret) as much as a code one — see §Q7 invariant list.

**Threat-model summary — the hard invariants (all MUST be tested):**
1. The scratch credential cannot write (or read) any non-`scratch` database. (L2 test: attempt a warehouse
   CREATE/INSERT with the scratch credential → denied by ClickHouse grant.)
2. The written table name always begins `s_<X-Session-Id>_`; a body-supplied name/session is ignored.
3. A `runQuery` referencing another session's `scratch.*` is rejected (`SCRATCH_SESSION_VIOLATION`) — the
   existing D64 test extended to a *live-written* table.
4. Rows are native-inserted; a cell containing SQL metacharacters round-trips as data (create→JOIN→compare),
   never executes.
5. The write endpoint is absent from `list_tools` and unreachable as an MCP tool (the model can't call it).
6. An oversized intermediate (row/byte cap) fails closed to the raw loop, never a runaway materialization.
7. Orphaned scratch tables carry a TTL and are gone after it (L2 test with a short TTL).

---

## Q4 — Executor integration

- **Flip `_has_table_intermediate` from reject→handle.** Today (executor.py:1088) it returns True → `UNSUPPORTED`.
  It becomes: table intermediates are supported **iff** a `scratch_client` is wired **and** the producing node's
  output/consumer shape validates; otherwise still `UNSUPPORTED` → raw loop (reversible degrade).
- **The F2 boundary moves — what is STILL unsupported after this:**
  - **cross-session** intermediates — never (by design; not a feature).
  - **oversized** intermediates — beyond `SCRATCH_MAX_ROWS`/bytes → raw loop (a real cap, tunable OQ-C).
  - **non-tabular / streaming** producers — Phase-1 materializes an in-memory result set; a producer whose
    result exceeds the runQuery row cap cannot be fully materialized (it was already truncated) → raw loop.
  - **multi-hop table chains** (table → table → table) — supported topologically (each hop materializes),
    but each hop pays a create+load; a deep chain is allowed but flagged as a cost OQ-D.
- **D56 verify** unchanged — terminal-only (§2.5).
- **Budget/pause interplay.** The `materialize` side-channel call is *inside* the one `runBlueprint` tool
  call — like the domain probes and grain probe, it does **not** increment the model-facing `tool_calls_made`
  (still **1 per `runBlueprint`**, D89 budget rule preserved). It is extra latency, masked by D61 progress
  streaming ("materializing step 2…"). **Pause/resume re-introduces D55 scratch-reconnect** — the real new
  cost: if a DAG pauses (approval) *after* materializing an intermediate, the checkpoint must store the
  scratch table **ref** (name), and resume must reconnect to it. If the scratch TTL'd away during a long
  pause, resume **falls back to the raw loop** (never a wrong answer) — exactly D55/N8's best-effort reconnect,
  which the scalar-only Phase-1 had sidestepped. This is Slice 3.
- **D56 verify still on the terminal** (the brief's check): yes — a materialized intermediate does not change
  where the gate runs; the terminal node's grain probe is unchanged.

---

## Q5 — Slicing (each slice ships tests across layers)

### Slice 1 — `clickhouse-api` scratch-write side-channel (create/load/drop, session-gated, TTL)
**Scope:** the `/scratch/v1/materialize` + `/scratch/v1/drop` routes on the MCP app behind `JWTAuthMiddleware`;
the scratch-only privileged CH client (`build_scratch_client`, mirroring `build_admin_client` but from
server config, scratch-only); server-side re-validation (`validate_identifier`/`validate_ch_type`); the
`s_<session_id>_bp_<uuid>` namer (session_id from header); native bulk insert; table TTL; the `scratch`
database bootstrap (created once by the privileged credential on first use or at deploy). No runtime changes.
**Tests — Layer-1 (pure, no ClickHouse):** namer derives prefix from header only (body name ignored);
identifier/type validation rejects hostile column names/types; a cell with SQL metacharacters is passed to
`client.insert` as data (assert via a fake CH client that records `data=` vs any `.command(sql)`); missing
`X-Session-Id` → rejected. **Tests — Layer-2 (vs real ClickHouse):** create→insert→`SELECT` round-trip;
the scratch credential is **denied** a warehouse CREATE/INSERT (invariant #1); a short-TTL table disappears
after the interval (invariant #7); two materializations in one session get distinct names.
**Contract frozen at end of Slice 1** (the request/response shape below) so Slice 2 builds against it.

### Slice 2 — runtime materialize-and-join + executor flip + seed blueprint + live e2e
**Scope:** `runtime/mcp/scratch_client.py` (`ScratchClient` + `FakeScratchClient`); the `_execute_dag`
materialize step (§2.2); the AST table-placeholder rewrite (`blueprint/template.py` extension); flip
`_has_table_intermediate`; extend the loader/`models` for `table`-typed `consumes`; a **table-intermediate
seed blueprint** (the `04-blueprints.md` convergence DAG — dept_actuals + budget_targets → flag, or a
2-node table→JOIN). **Tests — Layer-1:** the rewrite substitutes a runtime-controlled scratch identifier
into FROM/JOIN, never a model string (adversarial upstream cell never becomes SQL); materialize is invoked
with a session-scoped name and native rows; oversized result → `UNSUPPORTED` → raw loop; no `scratch_client`
wired → `UNSUPPORTED`; provenance union excludes scratch columns. **Tests — Layer-2 (vs real MCP + scratch
endpoint + ClickHouse + neo4j):** the seed convergence blueprint executes end-to-end — producing node
materializes, consumer JOINs `scratch.s_<sid>_*`, terminal D56 grain gate runs, scope enforced; a
cross-session scratch JOIN is rejected live (invariant #3, on a real written table).

### Slice 3 — pause/resume scratch-reconnect (D55) + Layer-3 conformance
**Scope:** store the scratch table ref(s) in the `PauseCheckpoint` (additive, alongside the completed-node
records); on resume, reconnect — if the ref is gone (TTL'd), fall back to the raw loop (D55/N8); best-effort
`drop` of a DAG's scratch tables on clean completion. **Tests — Layer-2:** `D55-scratch-reconnect-on-resume`
(materialize → approval pause → resume → consumer JOINs the still-live scratch table, exactly-once); a
TTL-expired scratch on resume → clean raw-loop fallback (never wrong answer). **Tests — Layer-3
(conformance, the headline unlock):** the `04-blueprints.md` convergence-DAG runs as a table-intermediate
fast path (chip + progress + SQL shown); cross-session isolation holds; a pause/restart mid-DAG survives.

---

## Q6 — Cross-repo coordination

- **Ships in `clickhouse-api` (Slice 1):** the two routes, the scratch-only credential + GRANT, the namer,
  the TTL, the scratch-DB bootstrap. `clickhouse-api` owns the write privilege end-to-end; the runtime never
  holds a CH write credential.
- **Ships in `data-analysis-agent` (Slice 2/3):** `ScratchClient`, the executor materialize+rewrite, the
  seed blueprint, the checkpoint reconnect.
- **The contract between them** (frozen at Slice 1) — see the API shape below. It is the *only* coupling.
- **Versioning:** a `/scratch/v1/` path prefix + a `scratch_api: "v1"` field added to the MCP `/health`
  payload (the runtime can assert the capability at startup, like it does MCP reachability). This is
  lighter than `catalog_sha` because the scratch API is enforcement infra, not versioned catalog content —
  a breaking change bumps the path prefix (`/scratch/v2/`), and `/health` advertises what's live. **Rejected:**
  folding it into `catalog_sha` (couples an infra surface to catalog content — wrong axis).
- **Layer-2 wiring:** a `SCRATCH_API_URL` (defaulting to the MCP base + `/scratch/v1`) in the runtime config,
  threaded like `MCP_URL`; the Layer-2 harness brings up the MCP (which now serves the scratch routes) +
  ClickHouse with the scratch credential provisioned. One extra deploy artifact: the scratch CH credential
  secret (helm), GRANTed scratch-only.

### The scratch-write API contract (endpoint + params + auth)

```
POST /scratch/v1/materialize
  Headers:  Authorization: Bearer <jwt>          # same as read plane (who may call)
            X-Session-Id: <session_id>           # binds the table name (never from body)
  Body (application/json):
    {
      "columns": [ { "name": "<safe-ident>", "type": "<whitelisted-CH-type>" }, ... ],
      "rows":    [ [ <cell>, ... ], ... ]        # native JSON values; bulk-inserted as DATA
    }
  200 -> { "table": "scratch.s_<session_id>_bp_<uuid>", "row_count": <int> }
  4xx -> { "error": "...", "code": "SCRATCH_MATERIALIZE_REJECTED"     # bad ident/type/shape
                          | "SCRATCH_SESSION_MISSING"                 # no X-Session-Id
                          | "SCRATCH_TOO_LARGE" }                     # over the row/byte cap
  Auth: JWTAuthMiddleware (JWT validated); table name derived from X-Session-Id ONLY.
  Privilege: executed via the server-side scratch-only ClickHouse credential (GRANT scratch.* only).
  Side effect: CREATE scratch.s_<sid>_bp_<uuid> (...) ENGINE=MergeTree TTL now()+<SCRATCH_TTL>; INSERT rows.

POST /scratch/v1/drop
  Headers:  Authorization: Bearer <jwt>, X-Session-Id: <session_id>
  Body:     { "table": "scratch.s_<session_id>_bp_<uuid>" }   # must match caller's session prefix, else 403
  200 -> { "dropped": true }        # best-effort; TTL is the real guarantee
  403 -> { "code": "SCRATCH_SESSION_VIOLATION" }   # cross-session drop attempt
```

The `type` values are the **existing** `_ALLOWED_TYPE_RE` whitelist (Int/UInt/Float/String/Bool/Date/DateTime/
Nullable/LowCardinality). Row cells are JSON-native and inserted via `client.insert(data=rows,
column_names=[...])` — the injection boundary is the driver's native protocol, not SQL text.

---

## Q7 — Open questions (interim defaults) + the hard invariants that MUST be tested

| OQ | Interim default | Revisit when |
|---|---|---|
| **OQ-A — column type derivation** | runtime-inferred (reuse `admin_ingest.infer_column_type`); seed blueprint CASTs join keys to a stable type | runQuery returns column types, or a mistyped-JOIN-key bug appears |
| **OQ-B — client auth for the write route** | JWT + `X-Session-Id` (no separate secret); privilege is the server-side scratch-only grant | a deployment wants runtime↔scratch mTLS as defense-in-depth |
| **OQ-C — size cap for a materializable intermediate** | `SCRATCH_MAX_ROWS` (start at the runQuery row cap) → over-cap = raw loop | real report intermediates exceed it |
| **OQ-D — multi-hop table chains** | allowed (each hop materializes), flagged as a latency cost | a deep-chain blueprint is authored |
| **OQ-E — SCRATCH_TTL value** | a small multiple of a max pause window (must exceed a human approval pause) | D55 reconnect fallbacks become common (TTL too short) |
| **OQ-F — explicit drop vs TTL-only** | best-effort drop on clean finish + TTL as the guarantee | scratch storage pressure shows up |
| **OQ-G — scratch DB bootstrap** | privileged credential `CREATE DATABASE IF NOT EXISTS scratch` at deploy/first use | multi-tenant scratch-DB-per-tenant is needed |

**Hard security invariants (all MUST be tested — restated as the acceptance bar):**
1. The scratch credential can write/read **only** `scratch.*` — a warehouse CREATE/INSERT/SELECT with it is
   denied by the ClickHouse grant (L2).
2. The written table name always starts `s_<X-Session-Id>_`; a body-supplied name or session is ignored (L1).
3. A `runQuery` referencing another session's live-written `scratch.*` is rejected `SCRATCH_SESSION_VIOLATION`
   (L2, extends the existing D64 test to a real written table).
4. Rows are native-inserted; a cell with SQL metacharacters round-trips as data through create→JOIN→compare,
   never executes (L1 + L2).
5. The write route is not an MCP tool — absent from `list_tools`, unreachable by the model (L1/L2).
6. An oversized intermediate fails closed to the raw loop; no unbounded materialization (L1).
7. Orphaned scratch tables carry a TTL and are gone after it; a crash/abandoned-pause leaks nothing (L2).
8. The runtime never holds or forwards a ClickHouse write credential; the privilege is server-side only (L1
   asserts the runtime `ScratchClient` sends only JWT + session_id, no CH creds).

---

## Recommendation — build Slice 1; gate Slice 2/3 on a real need

**Build Slice 1.** Its marginal risk is low: it re-shapes already-hardened write primitives (G1), rides the
existing session-binding middleware (G3), and is validated against the already-built read isolation (G2).
It unblocks the F2 boundary and is independently useful (the same surface is what BYO-fact-table uploads,
D19/D20, will eventually use).

**Gate Slice 2/3 on a concrete table-intermediate blueprint existing.** The F2 deferral rationale
(runblueprint-design §0/F2) was explicit that **single-node + scalar-converging DAGs are a large, honest
subset** — "the canonical `avg_salary_by_dept` and most report blueprints are single-node or scalar-converging."
If no seed/needed blueprint actually requires a *table* intermediate today, wiring the runtime path is
**YAGNI**: it re-introduces the D55 scratch-reconnect complexity (the one real cost the scalar-only phase
deliberately bought us out of) for a capability nothing yet exercises. **So the trigger for Slice 2 is: a
real report whose DAG cannot be expressed as scalar-converging** (e.g. the convergence flag over two
independent table branches that must be row-joined, not scalar-compared). Until then, ship Slice 1 (the
surface + its invariants), keep `_has_table_intermediate` returning `UNSUPPORTED` when no `scratch_client`
is wired, and let the raw loop answer the rare table-intermediate question.

**What would make me say "don't build this now" entirely:** if (a) no blueprint needs table intermediates
**and** (b) the ops cost of provisioning + rotating a second (scratch-only) ClickHouse credential is not yet
worth carrying, then defer the whole brick — the scalar subset + raw-loop fallback is a correct, secure
product without it. The design above is the plan for *when* a table-intermediate blueprint lands; it is not a
reason to open a warehouse-adjacent write path before one does.

---

## Doc/code updates on completion (NOT done this pass — enumerated)

**Docs:**
- `04-blueprints.md` — §Execution step 4 table branch: flip "**DEFERRED (F2, D89)**" → **built** (gated on
  the scratch surface); note the `table`-typed `consumes` shape; note D55 reconnect applies again.
- `DECISIONS.md` — a new locked decision (**D90**) recording: the scratch-write side-channel (non-tool MCP
  route, JWT+X-Session-Id auth, scratch-only server credential), the materialize-and-join executor path, the
  scope-preservation argument (§2.4), the hard invariants (§Q7), and the F2 boundary shrink. Amends D89 (F2
  now buildable), extends D19/D20 (the side-channel is realized), D45/D55 (scratch reconnect returns), D59a
  (table passing built).
- `OPEN-QUESTIONS.md` — close the F2 scratch-write OQ; open OQ-A..G.
- `02-tools-and-api.md` — a note that a **non-tool** privileged scratch route exists (explicitly not one of
  the six read tools).
- `TRACEABILITY.md` — `D59a-table-intermediate-as-scratch-join`, `D64-cross-session-scratch-write-rejected`,
  `D55-scratch-reconnect-on-resume` move toward built as slices land.

**Code (net-new, additive):** `clickhouse-api`: `app/scratch_ingest.py` (session-named create/load/drop,
reusing `admin_ingest` validators) + the two `mcp.custom_route`s + `build_scratch_client` + config
(`SCRATCH_CH_*`, `SCRATCH_TTL`, `SCRATCH_MAX_ROWS`) + the scratch grant (helm). `data-analysis-agent`:
`runtime/mcp/scratch_client.py` (+ fake) + the `_execute_dag` materialize step + `blueprint/template.py`
table-placeholder rewrite + `_has_table_intermediate` flip + loader/`models` `table`-consume support + the
`PauseCheckpoint` scratch-ref additive field + a seed blueprint fixture.
