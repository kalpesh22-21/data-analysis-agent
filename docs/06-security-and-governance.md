# 06 — Security & Governance

## Identity and injection

Every UI request carries `session_id` (UI-generated), a **JWT**, and a **column-level scope**. These
are **injected by runtime code at dispatch** into every tool call. They are:

- **Not declared** in any model-visible tool schema.
- **Not present** in the model's context (no leakage, no token cost).
- The model calls `runQuery(sql)`; the runtime runs `runQuery(sql, jwt, scope, session_id)`.

### Token acquisition and interim scope (D82)

The JWT is obtained at login: the **UI's backend** calls `token_service`'s guarded `POST /token` (or an external IdP in production) to mint a signed JWT for the session. The browser receives but does not sign tokens.

**Interim posture — `column_scope: []` (D82/D80):** every token is currently minted with an **empty `column_scope`**, which D80 defines as allow-all. Per-user column entitlements are **not yet wired**, so **column-level enforcement is effectively a no-op until entitlements are populated** — this is an explicit interim, not the end state. Row-level tenant isolation (`SQL_tenant`/`user_name`) is enforced separately at the warehouse and is unaffected. The MCP parser (D57/D62) and D63 fail-closed + D64 scratch isolation **still fire** on every request even with an allow-all token, so cross-session scratch access and unparseable SQL remain blocked.

`session_id` travels as an unsigned `X-Session-Id` header (D81), separate from the JWT. The backend forwards both to the MCP on every turn.

### Why this is a security property
The model never holds the token, so it **cannot forge or escalate scope**. Enforcement is uniform —
the same injection applies to `runQuery` calls made **inside** `runBlueprint`, so column-level access
holds even within blueprint DAGs.

### How the MCP enforces it (D57)
Injection attaches the scope; **enforcement is the MCP parsing the SQL** (option 2a). On every
`runQuery`, the MCP runs the **D52 ClickHouse-dialect parser** over the query, extracts its
**referenced columns** (`USES` semantics — same derivation as the D44 trail-provenance set, so a
derived `AVG(gross_pay)` is gated even though no raw payroll column is returned), and **rejects the
query when referenced columns ⊄ scope**. The parser is therefore the **live security boundary** — a
fourth D52 consumer and the only one gating *live* access (see [09](09-infrastructure.md) §SQL
parser). We rejected pushing scope down to ClickHouse roles (`SET ROLE`, engine-enforced) because
scope arrives as an MCP input, not a DB identity. **Trade-off — the parse-failure fork:** because the
model emits arbitrarily exotic ClickHouse SQL, a parse failure forces a choice between rejecting a
legitimate query and running it unchecked. **Resolved (D63): fail-closed + alert — reject the query,
never run it unchecked.** A rejected legit query is recoverable (surfaced via §Graceful denial, user
can rephrase / falls back to the raw loop); a fail-open leak is not. The false-reject rate is
measurable pre-launch via the `system.query_log.columns` oracle (D62) and driven down by parser
coverage — never by relaxing to fail-open.

## Column-level scope at every layer

Scope shapes the candidate set identically across layers:

| Layer | How scope applies |
|---|---|
| Retrieval | Pre-filter out blueprints whose **transitive** `(table,column)` dependency set ⊄ scope. |
| Thin cards | Only in-scope blueprints are surfaced — restricted reports' existence isn't leaked. |
| `runBlueprint` | Internal `runQuery` calls carry injected scope. |
| Raw `runQuery` | Injected scope is the **hard enforcement boundary** for **new** queries — enforced **in the MCP by parsing the SQL** (D57). |
| **Replayed session trail** | Past tool results are **provenance-tagged** and **re-filtered against current scope every turn** — see §Mid-session scope changes (D44). |

**Defense-in-depth:** retrieval pre-filter = "don't surface what you can't use"; injected scope =
"can't execute outside scope even if something slips through." The graph filter is **never** the only
gate (stale edges, partial-column cases, scope changing mid-session).

### Mid-session scope changes (D44)

Scope is sent per-request, so it can **narrow mid-session** (access revoked, role change). The
injected `runQuery` scope guards only **new** queries — but the Couchbase session trail holds **past
results** (PII rows) that are replayed into model context every follow-up turn
([01-architecture.md](01-architecture.md)). Without re-filtering, a narrowed scope would still leak
previously-authorized data on replay.

Fix (**provenance-filter**, the surgical option):
- Every stored tool result carries a **column-provenance set** = the columns it **referenced**
  (`USES` semantics, not just columns returned — so an `AVG(gross_pay)` is gated even though it
  returns no raw payroll column). Computable for every data-returning tool (raw `runQuery` via SQL
  parse, `runBlueprint` via `USES` edges, `sampleRows` via the table's columns).
- **Fail-closed (D52):** if the SQL parser can't determine a `runQuery`'s referenced columns, the
  entry has **no provenance** and is **dropped from replay** — an undeterminable query is never
  assumed in-scope.
- **Every turn**, before context assembly, drop any trail entry whose provenance ⊄ the **current**
  request scope. In-scope conversation context is preserved; only now-forbidden entries vanish.
- This is the **replayed-trail layer** of the scope table above — the same "scope shapes the
  candidate set at every layer" principle, extended to history. The `runQuery` hard gate (new
  queries) + provenance-filter (replayed history) together close both paths.
- **Ordering (D50):** the provenance-filter runs **before** D46's history compaction, so the
  summarizer never sees out-of-scope entries and the (provenance-less) prose summary is safe by
  construction. See [03-context-and-retrieval.md](03-context-and-retrieval.md) §Ordering.

### Graceful denial
If a blueprint or query touches columns outside scope (edge cases past the pre-filter), the access
layer denies it. The executor catches access-denied and surfaces it cleanly ("this report needs
payroll access") rather than failing opaquely. Retrieval results legitimately differ by user scope.

## Entity-agnostic governance

Global stores (**blueprints, global knowledge**) must contain **no entity-specific data** — no
person names, department names/codes, employee codes, or specific dates. Enforcement:

- The learning-loop **leakage gate** scans every global candidate before commit (LLM + regex).
- Literals are lifted to **slots** (blueprints) or generalized patterns (knowledge).
- Entity-bearing facts are rerouted to **user knowledge** (personal, scoped), which is allowed to
  carry entities.

**The gate is probabilistic — so global stores get a human/detection backstop (D58):** validation
never re-scans for entities and `searchKnowledge` once returned candidates immediately, so the gate's
false-negative rate would equal the cross-user leak rate with no checkpoint. Therefore:
- **`global_knowledge` requires human review *before* it is retrievable** (D58a) — `searchKnowledge`
  returns only human-approved statements (free-text prose is the hardest to guarantee entity-free).
- **`blueprint` candidates require human review by default** (D58b) — all mined blueprints and all
  leakage near-misses go to the review inbox; leaked artifacts are **retracted** (pull from index;
  the D25 `GUARDRAIL` trace identifies who was exposed).
- **`LEARNING_ENABLED=false`** (D58c) halts all write-back instantly without a deploy.

## PII and the scratch schema

- Uploaded external datasets often contain PII. Scratch tables are **session-scoped**, **access-scoped**,
  and **TTL'd** (auto-dropped).
- Scratch contents and external-upload sessions **must not** feed the entity-agnostic global stores
  (see external-data handling in [07-external-data.md](07-external-data.md)).

### Scratch isolation — own-session-only (D64)

Scratch tables are named `scratch.s_<sessionId>_<file>`, but a naming convention is **not** an access
boundary. The D57 column-scope parser gates warehouse **columns** against the user's scope; it does
**not** say which *session* may read a given scratch table, and uploaded scratch tables aren't in the
column-scope model at all. Without a separate check, nothing would stop session A's `runQuery` from
`JOIN scratch.s_<sessionB>_…` — and uploaded files are often the most sensitive PII.

Enforcement reuses the **same MCP-side SQL parse** as column scope (one mechanism, consistent with
D57 — scope/`session_id` arrive as MCP inputs, not ClickHouse roles):

- On every `runQuery`, the MCP uses the D52/D62 `sqlglot` parse — already run to extract referenced
  columns — to also enumerate **referenced tables**. Any table in the `scratch` database **must match
  `s_<injected session_id>_*`**; a reference to a foreign or malformed scratch name is **rejected**.
- The model never sees or supplies `session_id` ([D5](decisions/DECISIONS.md)), so it cannot forge a
  cross-session reference.
- **Fail-closed (D63):** an unparseable query is already rejected, so it can never run an
  unverifiable scratch reference unchecked. Rejections surface via §Graceful denial.
- **Defense-in-depth (optional, not the boundary):** the privileged ingestion side-channel creates
  scratch tables under a known session and *may* additionally grant ClickHouse access only to that
  session's identity — but per D57 the **MCP parse is the live boundary**.

Scratch isolation is thus a **session boundary** (own-session-only), orthogonal to and stacked with
the **column boundary** (D57, in-scope columns only) and the **replayed-trail boundary** (D44).

## PII and the session store (D44)

The Couchbase session trail is the **other** raw-PII home (it stores tool **results** = HR rows), so
it gets an explicit retention policy consistent with the scratch TTL above:

- **Single document TTL** (Couchbase native expiry): the whole session doc — trail, results, and all
  — auto-expires after `SESSION_TTL`. No partial-purge job; simplest path.
- **Constraint:** `SESSION_TTL` **must exceed** the learning-loop completion window, since the loop
  reads the trail after session close ([05-memory-and-learning.md](05-memory-and-learning.md)). The
  durable learned artifacts (entity-free candidates, user knowledge) outlive the doc by design — the
  raw trail is not the system of record after learning runs.
- Trade-off accepted: PII cell-values dwell for the full TTL even after the learning loop has consumed
  the session (no early value-purge). `SESSION_TTL` value is an open parameter.

## Rule enforcement (two paths, one definition)

Catalog rules (`active_employee`, `settled_pay_only`, …) are **defined once** in the Semantic
Catalog `rules.yaml` ([09-infrastructure.md](09-infrastructure.md)), but **enforced along two paths**
with different trust properties:

| Path | How the rule is applied | Trust |
|---|---|---|
| **Blueprint** (`uses_rules`) | The AST-rewrite stage injects the rule predicate deterministically. | Strong — not model-dependent. |
| **Raw agent loop** | The model reads the rule text in the `getTableSchema` YAML and writes it into its own SQL. | Weaker — model judgment; the rule can be omitted or misapplied. |

This asymmetry is acceptable (the raw loop is the fallback, not the fast path) but explicit: a rule is
a **correctness convention** in the raw loop and a **deterministic guarantee** only inside validated
blueprints. It is **not** an access-control boundary in either case — column-level scope (above) is.

## Read-only data path

The agent path is read-only: MCP blocks INSERT/UPDATE/DELETE/DDL/table functions and enforces time
limits + row caps. Writes to scratch happen via a **separate privileged ingestion side-channel**,
never an agent tool.

---

**Status:** Locked
**Open questions:**
- ~~Scope representation/format passed by the UI (claim shape in JWT vs. separate scope object).~~ **RESOLVED by D79b:** `column_scope` is a signed JWT claim (a JSON list of `database.table.column` triples).
- Scratch TTL duration and cleanup ownership.
