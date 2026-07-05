# S9 Activation — promotion scheduler end-to-end (design)

Status: PROPOSED (no code). Activates the S9 promotion scheduler's three deferred
stubs (`_DeferredWarehouseProbe`, `_DeferredDependencyResolver`, and the missing
corpus-landing writer) so a validated candidate actually promotes AND lands in the
neo4j retrieval corpus (becomes recallable). This is the learning loop's FIRST
write-into-retrieval capability — the design is safety-first and fail-closed.

Grounding read (reused, not reinvented): `runtime/blueprint/executor.py`
(`_verify` / `_grain_probe_sql` / `_map_grain_columns` / `_unpack_grain_probe`),
`runtime/retrieval/corpus_loader.py::load_corpus` + `BlueprintSeed`,
`learning/generalize/mapping.py`, `runtime/retrieval/vector_index.py::Neo4jVectorIndex`,
`runtime/mcp/real_client.py`, `clickhouse-api/app/token_service.py` (`POST /token`),
`learning/promotion/{scheduler,replay,models}.py`, `learning/factory.py`,
`docs/decisions/learning-loop-contracts-design.md` §11 + `…-wave3-wiring-design.md`.

---

## 0. The store topology this brick sits on (load-bearing context)

Two DIFFERENT stores, easy to conflate:

- **Couchbase `learning_corpus`** = the dedup hard-key + `hit_count` store
  (`CouchbaseBlueprintCorpus`, keyed by `canonical_key`). Already real; it is the
  `HitCountReader`. NOT the retrieval corpus.
- **neo4j** = the recall/retrieval corpus (`Neo4jVectorIndex` recall +
  `corpus_loader.load_corpus` write). A blueprint becomes *recallable* only when it
  is MERGE-upserted here. **This is the landing target.**

So the landing writer writes to **neo4j**, not to the Couchbase corpus. The
`hit_count` that gates promotion lives in Couchbase; the artifact that makes the
blueprint retrievable lives in neo4j. The two share one identity (see §3.2).

---

## 1. WarehouseProbe (real)

### 1.1 Reach decision — MCP-via-blueprint-scope vs direct ClickHouse  ← the central call

**Recommendation: run the golden-replay grain probe through the adopted MCP
`runQuery` path, under a JWT minted per-blueprint scoped to the blueprint's own
`uses`.** Reject the direct-ClickHouse alternative.

Rationale (parity is the whole point of D98/D56):

| | MCP-via-scope (RECOMMENDED) | Direct ClickHouse |
|---|---|---|
| D57 column-scope enforcement | REUSED verbatim (MCP is the boundary) | bypassed — must re-implement scope teeth |
| Oracle parity with live `runBlueprint` | identical `runQuery` → identical `COUNT(*),COUNT(DISTINCT)` path → a replay that passes offline behaves identically live | a second SQL execution path that can silently diverge from the live oracle |
| Cost | needs an offline JWT mint + an MCP client in the scheduler process | simpler (one CH connection), no token dance |
| Failure surface | mint / MCP-denial / MCP-down all fail-closed to `probe_unavailable` | fewer moving parts, but scope-honesty is now the scheduler's problem, not the MCP's |

The direct path is *simpler*, but it moves the D57 scope boundary from the MCP
(where it is proven) into the scheduler (in-boundary/trusted, but now re-asserting a
security invariant by hand). The parity payoff — "offline replay green ⇒ live run
green, over the exact same enforced path" — is exactly what the golden replay exists
to buy, and it is only true if the probe travels the MCP `runQuery` choke point. The
one-time cost of offline minting is bounded and reversible.

### 1.2 How the grain-probe SQL is built (NO duplication)

`golden_replay` already binds the frozen template into `replay_sql` and passes
`(replay_sql, grain_columns)` to `probe.run`. The probe must return the
`verify_result` triple `(row_count, distinct_grain_count, columns)`. It builds that
exactly as `BlueprintExecutor._verify` does today:

1. `_map_grain_columns(replay_sql, grain_columns)` → declared grain → output-column
   names (fail-closed `None` on an ambiguous/absent map ⇒ the probe returns
   `distinct_grain_count=None`, which `verify_result` already treats as
   grain-unverifiable, never a false pass).
2. `_grain_probe_sql(replay_sql, mapped_cols)` → the AST-built
   `SELECT COUNT(*) AS __bp_n, COUNT(DISTINCT <grain>) AS __bp_d FROM (<replay_sql>)`.
3. dispatch that through `ToolDispatcher.dispatch("runQuery", …, credentials)`;
   `_unpack_grain_probe` → `(total, distinct)`.
4. `columns`: dispatch the bound `replay_sql` with a small `limit` and read the
   `columns` header from the runQuery result (values discarded — see §1.4). This
   yields the result-signature shape `verify_result` needs.
5. return `ProbeResult(row_count=total, distinct_grain_count=distinct, columns=cols)`.

**Do NOT copy these three helpers.** They are currently module-private in
`executor.py`. Promote `_grain_probe_sql`, `_map_grain_columns`, `_unpack_grain_probe`
into a shared `runtime/blueprint/grain_probe.py` and have BOTH the executor and the
offline probe import them, so the offline oracle can never drift from the live one.
This extraction is mechanical (move + re-export) and is the first task of Slice 1.

### 1.3 Offline JWT minting scoped to the blueprint's `uses`

`clickhouse-api/app/token_service.py` exposes `POST /token`, guarded by a static
`TOKEN_ISSUER_API_KEY`, that mints an RS256 JWT with an explicit `column_scope` list
(the exact `["db.table.column", …]` grammar the MCP enforces). The offline mint, per
replayed blueprint:

```
POST {TOKEN_SERVICE_URL}/token
  Authorization: Bearer {TOKEN_ISSUER_API_KEY}
  body: { "user_name": "learning-scheduler",
          "column_scope": <blueprint.uses>,      # exactly the declared footprint
          "ttl_seconds": <short, e.g. 300> }
```

- **No `session_id`** in the mint body → NO `sid_hash` claim is stamped → the MCP
  applies no session binding, so the probe can pass an arbitrary `X-Session-Id`
  (the offline probe is not a user session). The real client already sends both
  headers; only the token's scope claim is load-bearing here.
- The minted token → `RuntimeCredentials(session_id=<synthetic>, jwt=<token>,
  column_scope=frozenset(blueprint.uses))` → the same dispatcher path the executor
  uses. Because `column_scope == uses`, the replay reads only within the blueprint's
  declared footprint (D89 scope-honesty), and the MCP's D57 teeth are the backstop
  if the mint is ever wrong.
- The mint client is a thin `httpx` POST (mirror `runtime/mcp/scratch_client.py`),
  wired into the probe. A short TTL bounds token exposure; no caching needed at
  Phase-1 cycle rates (revisit as a perf-only concern).

**Where does `blueprint.uses` come from at promote time?** — see Open Question OQ-1
(a blocker): the replay currently references only `sql_template` + `result_grain` +
slot types, never a `uses` list. Both the token scope AND the landed `BlueprintSeed`
(§3) need it.

### 1.4 Fail-closed behavior (D98 — never a value, never a raise)

- `probe.run` may raise on ANY failure — token-mint HTTP error, MCP denial
  (`COLUMN_SCOPE_VIOLATION` etc.), MCP unreachable, malformed result. That is
  correct: `golden_replay` already catches every `probe.run` exception →
  `ReplayOutcome(passed=False, reason="probe_unavailable")`, so both the cron scan
  and the human-approve path HOLD cleanly (no auto-promotion, no 500). The real
  probe simply raises where the stub raised.
- The probe returns **only** counts + column names. It never reads or returns a
  row value (step 4 discards rows; the grain probe returns aggregates). This is the
  `S9-probe-never-returns-value` invariant (D98/D17 — no value oracle).

---

## 2. DependencyResolver (real)

### 2.1 What a `depends_on` ref actually is

Per `extractor/models.py::CandidateHeader.depends_on` — *"sibling candidate ids this
one is blocked on (§7)"*. A `depends_on` entry is the **candidate_id of a sibling
candidate** (e.g. a blueprint that needs a `schema_edit(add_rule)` emitted in the
same session — the D35 missing-rule pairing). It is NOT a raw schema rule string or
a neo4j node id.

### 2.2 Resolution query — recommend candidate-store status, not a neo4j probe

The entrypoint docstring speculatively labels this "neo4j `depends_on` resolution",
but given the ref is a candidate id, the natural, no-new-infra resolver reads the
**same candidate store** the scheduler already holds:

```
CandidateStoreDependencyResolver.is_resolved(ref) ->
    env = await store.get(ref)                       # keyed get by candidate_id
    return env is not None and env.status == VALIDATED
```

A dependency is "resolved" once its OWN candidate reaches `validated` — which for a
blueprint dep means it landed (§3), and for a `schema_edit` means a human approved it
through the inbox (D18 — the human approve only fires post-PR-merge, so `validated`
implies the catalog rule is live). This reuses the store the scheduler is already
pinned to (no split-brain risk), and is trivially Layer-1 testable with the in-memory
store.

Reject the neo4j-probe alternative for Slice 1: it needs a candidate_id → landed-id
mapping plus a separate catalog-liveness query, and duplicates state the candidate
store already holds authoritatively.

### 2.3 Fail-closed default

- No `depends_on` ⇒ trivially resolved (existing `_deps_resolved` short-circuit).
- Ref present but the candidate is missing (`get` → `None`), or its status is
  anything other than `validated` ⇒ `False` (unresolved → HOLD). Never fail-open:
  an unverifiable dependency is always "not resolved". This is the
  `S9-resolver-fail-closed` invariant. The scheduler already fails closed when the
  resolver itself is absent (`_deps is None` → `False`).

Hardening flagged (OQ-2): if we later want "the rule is *actually* in the catalog"
rather than "its candidate is validated", add a catalog-registry probe as a second
AND-clause. Not needed for Slice 1.

---

## 3. Corpus-landing writer

### 3.1 Trigger — on the `→ validated` edge, land BEFORE the status write

Landing happens on exactly the two edges that produce `validated`:
`_advance_candidate` (auto) and `apply_human_decision` approve-blueprint (human).
Extract a single helper so the invariant lives in one place (mirrors the single
`apply_human_decision`):

```
async def _land_and_promote(env, drift) -> outcome:
    seed = blueprint_seed_from_candidate(env)      # generalized fields ONLY (§3.3)
    try:
        await self._landing.land(seed)             # neo4j MERGE via load_corpus
    except Exception:
        return self._hold(env, "landing_failed")   # stays candidate; retry next cycle
    await self._store.put(replace(env, status=VALIDATED, drift=drift))
    return CandidateDecision(..., action="promote"/"approve", ...)
```

**Order is load-bearing: land first, then write `validated`.** A crash between the
two re-lands next cycle (idempotent MERGE — harmless) and re-writes `validated`. The
reverse order would risk a `validated` candidate that never landed — invisible to
retrieval, the exact silent gap this brick exists to close. So "not landed ⇒ not
validated" becomes a promotion guard.

### 3.2 Candidate → `BlueprintSeed` mapping + identity

Reuse `blueprint_from_generalization(payload, generalization, id=…)` to get a runtime
`Blueprint`, then project it onto `BlueprintSeed` (a parallel `blueprint_seed_from_candidate`
in `generalize/mapping.py`):

| `BlueprintSeed` field | source |
|---|---|
| `id` | the S6 corpus artifact id (keyed by `canonical_key`) — see identity below |
| `intent` | `payload.intent` (generalized) |
| `uses` | the blueprint footprint (OQ-1) |
| `slots_summary` | rendered from `slots` |
| `resolves/slots/uses_rules/sql_template/composes/result_grain` | the S3/S4 generalization (same fields `blueprint_from_generalization` maps) |
| `status` | `"validated"` (only validated candidates land) |
| `drift_status` | from the fresh `DriftStamp` |

**Identity / idempotency:** the neo4j node `id` MUST be deterministic per canonical
blueprint so a re-promotion MERGEs in place rather than duping. Use the **same id the
S6 `CorpusArtifact` already carries** (derived from `canonical_key`) so the Couchbase
hit-count artifact and the neo4j retrieval node share one identity. `load_corpus`
MERGEs by `id` (`_UPSERT_BLUEPRINT`), so re-landing is idempotent by construction —
the `S9-landing-idempotent` invariant. If no `canonical_key` exists (S6 never ran →
count 0, so the count path can't promote anyway; only human approve could reach here),
fall back to a stable id derived from `candidate_id` (OQ-3).

### 3.3 Safety

- **Only `validated` candidates land** — landing is invoked strictly on the
  `→ validated` edge, after every guard (entity_scan pass, static ok, deps resolved,
  replay pass, hit-count ≥ T or human approve).
- **Entity-stripped (D17):** the mapping reads ONLY generalized fields (template,
  resolves, result_grain, intent, slots) — never `evidence`, audit spans, or any
  entity-bearing payload. The human path already calls `strip_entity_bearing` before
  `validated`; the auto path must apply the same strip on this edge (today it only
  *checks* `entity_scan` is clean). Add `strip_entity_bearing` to `_land_and_promote`
  so both edges land an entity-free seed, plus a defense-in-depth assertion that the
  seed carries no entity fields. This is `S9-landing-only-validated-entity-stripped`.
- **Model-parity:** `land()` calls `load_corpus(..., model_id=settings.embedding_model,
  ensure_schema=False)` — the SAME embedding model the corpus was seeded with.
  `check_model_parity` (already inside `load_corpus`, first statement of the write
  txn) raises `CorpusLoadError` on a mismatch → caught by `_land_and_promote` → HOLD
  (never a mixed-model index). `ensure_schema=False` because the seed load already
  created constraints/indexes; the scheduler is a writer, not a provisioner.
- **Landing failure** → `hold(env, "landing_failed")`, candidate stays `candidate`,
  retried next cycle. No `landed` sub-state (over-engineering); the land-then-status
  ordering + idempotent MERGE already gives at-least-once-safe, exactly-once-effect
  landing. `S9-not-landed-stays-candidate`.

### 3.4 Writer construction

`CorpusLandingWriter(driver, embedding_client, *, model_id, database)` wraps
`load_corpus`. It reuses the `Neo4jVectorIndex` async driver (the resolver — if it
ever needs neo4j — and the landing writer share ONE driver, created once per process,
closed on shutdown). The embedding client is the real `HttpEmbeddingClient` (the same
D71 endpoint the online recall path embeds against — parity by construction).

---

## 4. Wiring (`run_learning_scheduler.py` + factory)

Config-gated exactly like the consumer factory's all-or-nothing rule (§2 of
`factory.py`): the promotion WRITE plane (probe + resolver + landing) is a UNIT.
Either every real port is present, or the scheduler stays on today's fail-closed
stubs (dormant, auto-promotes nothing).

Add a `build_promotion_write_plane(...)` factory alongside `build_promotion_plane`
that takes the injected infra clients (MCP `ToolDispatcher`, token-mint client, neo4j
driver, embedding client) and returns the real `WarehouseProbe`,
`CandidateStoreDependencyResolver`, and `CorpusLandingWriter`. The scheduler gains a
new injected `landing_writer` param.

The entrypoint gate (in addition to today's `candidates_ready` + `corpus_ready`):

```
write_plane_ready = bool(
    runtime.mcp_url and runtime.neo4j_url and runtime.neo4j_username
    and runtime.embedding_api_url and TOKEN_SERVICE_URL and TOKEN_ISSUER_API_KEY
)
if write_plane_ready:  build real probe + resolver + landing writer
else:                  keep _DeferredWarehouseProbe + _DeferredDependencyResolver,
                       landing_writer=None  (auto-promotion stays DORMANT)
```

**Gating invariant (critical):** auto-promotion of a blueprint must be enabled ONLY
when BOTH the real probe AND the landing writer are present — a real probe with no
landing writer would validate a blueprint that never becomes recallable (the silent
gap). So the scheduler treats `landing_writer is None` as "blueprint auto-promotion
disabled" (blueprint holds `landing_unavailable`), symmetric with the deferred probe.

Config to add (OQ-4): `TOKEN_SERVICE_URL` + `TOKEN_ISSUER_API_KEY` (the offline mint
credentials). Recommend `RuntimeSettings` (they are runtime/MCP-adjacent, next to
`mcp_url`/`jwks_url`), read by the scheduler entrypoint alongside `LearningSettings`.

---

## 5. Test slugs (TRACEABILITY-style)

| slug | invariant | layer |
|---|---|---|
| `S9-probe-never-returns-value` | the real probe returns only `(row_count, distinct_grain_count, columns)`; no row value ever leaves it (D98) | L1 (fake dispatcher returning counts) |
| `S9-probe-scoped-to-uses` | the minted token's `column_scope == blueprint.uses`; a probe reading outside `uses` is DENIED and holds `probe_unavailable` | L1 for mint-args; **L2** for real MCP D57 enforcement |
| `S9-probe-oracle-parity` | the offline probe builds the grain SQL via the SHARED helper — byte-identical to the executor's live probe | L1 (assert same builder output) |
| `S9-resolver-fail-closed` | missing / non-`validated` dependency ⇒ `is_resolved=False`; only `validated` ⇒ `True`; no-deps ⇒ trivially resolved | L1 (fake store) |
| `S9-landing-only-validated-entity-stripped` | landing fires only on the `→validated` edge; the seed is built from generalized fields only and carries no entity payload | L1 |
| `S9-landing-idempotent` | re-promoting the same canonical blueprint MERGEs to ONE node (no dupe); deterministic id from `canonical_key` | L1 for the id; **L2** (real neo4j) for the MERGE |
| `S9-landing-model-parity` | landing stamps `settings.embedding_model`; a mismatched model raises `CorpusLoadError` → HOLD | **L2** (real neo4j) |
| `S9-not-landed-stays-candidate` | a landing failure holds `landing_failed`; the candidate is NOT written `validated` | L1 (landing writer raises) |
| `S9-autopromote-gated-on-landing` | `landing_writer is None` ⇒ blueprint auto-promotion stays dormant | L1 |

Layer-2 (real infra) is needed for: real MCP D57 scope enforcement, the true neo4j
MERGE idempotency, model-parity raise, and a real token mint round-trip.

---

## 6. Open questions (resolve before build) + slice split

### Blockers
- **OQ-1 (BLOCKER): where does `blueprint.uses` come from at promote time?** Both the
  token `column_scope` (§1.3) and the landed `BlueprintSeed.uses` (§3.2) need the
  blueprint's `db.table.column` footprint, but `golden_replay` / the generalization
  today reference only `sql_template` + `result_grain` + slot types. Options: (a) S4
  already computes and carries `uses` on the generalization/payload — confirm and
  read it; (b) derive the footprint from the bound `replay_sql` via sqlglot at
  promote time (the same table.column extraction `corpus_loader._assert_template_reads_within_uses`
  performs). Recommend (a) if S4 carries it; else (b) as a shared helper. **Must be
  answered first — it is on the critical path for both the probe and the writer.**

### Important
- **OQ-2:** resolver semantics — is "dependency candidate == `validated`" a strong
  enough definition of "the schema rule is live", or do we need a catalog-registry
  probe? Recommend candidate-store status for Slice 1; add a catalog probe only if a
  `schema_edit` can reach `validated` before its rule is actually usable.
- **OQ-3:** landed-node identity when `canonical_key` is absent (human-approved,
  S6-never-ran blueprint). Recommend a stable `candidate_id`-derived id; confirm no
  collision with the S6 artifact id namespace.
- **OQ-4:** which settings object carries `TOKEN_SERVICE_URL` / `TOKEN_ISSUER_API_KEY`
  — `RuntimeSettings` (recommended) or `LearningSettings`?

### Nice to know
- Token mint caching / TTL tuning (perf only; not correctness).
- Whether the scheduler should reuse the app's `Neo4jVectorIndex` driver instance or
  own its own (it is a separate process, so it owns its own — noted).

### Recommended slice split (the brick is large — two slices)
- **Slice 1 — probe + resolver (de-risk the central JWT/MCP decision first).**
  Extract the shared `grain_probe.py` helper; build the real `WarehouseProbe`
  (MCP `runQuery` + offline mint) and `CandidateStoreDependencyResolver`; add the
  token-service config + mint client. Auto-promotion STAYS gated off (no landing
  writer yet → `landing_unavailable` hold), but the real replay gate can now be
  exercised end-to-end at Layer-2 against real ClickHouse. Resolves OQ-1, OQ-2, OQ-4.
- **Slice 2 — landing writer + full activation.** `blueprint_seed_from_candidate`
  mapping, `CorpusLandingWriter`, the `_land_and_promote` edge (land-then-status,
  entity-strip, idempotent MERGE, model-parity), the `build_promotion_write_plane`
  factory, and the entrypoint gate that flips auto-promotion + landing on together.
  Resolves OQ-3.
```

---

## 7. Slice 1 — AS BUILT (probe + resolver + shared grain probe)

Status: BUILT (branch `phase0/provenance-extractor`). The golden-replay gate is now
REAL; auto-promotion-into-retrieval stays gated off (no landing writer — Slice 2).

### 7.1 What landed
- **`runtime/blueprint/grain_probe.py`** — the shared D56 grain-probe builder
  (`map_grain_columns`, `build_grain_probe_sql`, `unpack_grain_probe`), moved
  verbatim out of `executor.py`. The executor now imports them and keeps its private
  aliases (`_grain_probe_sql`/`_map_grain_columns`/`_unpack_grain_probe`), so its call
  sites + tests are byte-unchanged (338 blueprint tests still green). A Layer-1 test
  asserts `executor._grain_probe_sql is grain_probe.build_grain_probe_sql` (function
  IDENTITY) — the "can't drift" guarantee is now structural, not aspirational.
- **`learning/promotion/warehouse_probe.py::MCPWarehouseProbe`** — injected an
  `MCPClient` + a `TokenMinter` + a synthetic session-id factory. `run(sql, *,
  grain_columns, column_scope)` mints a `uses`-scoped JWT, runs the replay SQL through
  MCP `runQuery` to read the OUTPUT column signature (rows DISCARDED, D98), and — when
  a grain is declared — runs the shared grain probe for `(total, distinct)`. It NEVER
  returns a value; ANY mint/MCP/parse failure RAISES (→ `golden_replay` holds
  `probe_unavailable`).
- **`learning/promotion/token_minter.py`** — `TokenMinter` Protocol + `HttpTokenMinter`
  (thin httpx `POST /token`, mirrors `scratch_client.py`).
- **`learning/promotion/dependency_resolver.py::CandidateStoreDependencyResolver`** —
  `is_resolved(ref)` = the sibling candidate exists AND `status == VALIDATED`; missing
  / non-validated / store-error ⇒ `False` (fail-closed).
- **Config:** `RuntimeSettings.token_service_url` + `.token_issuer_api_key` (OQ-4:
  RuntimeSettings, as recommended). The entrypoint reads them + `mcp_url`.
- **Gating:** `PromotionScheduler` gained `landing_writer` + `require_landing` (both
  default OFF, so today's 422 callers are unchanged). When `require_landing` is set and
  no `landing_writer` is wired, a blueprint that passes EVERY guard (incl. the real
  replay gate) HOLDS `landing_unavailable` on the auto edge and
  `approve_blocked_landing_unavailable` on the human-approve edge. The entrypoint sets
  `require_landing = write_plane_ready` with `landing_writer=None`. Non-blueprint
  human-gated targets are unaffected.

### 7.2 Deviation from §1.3 — the mint is session-BOUND, not session-LESS
§1.3 assumed a session-LESS mint (no `sid_hash`) so the probe could send an arbitrary
`X-Session-Id`. The live MCP runs `require_sid_binding=true`: an unbound token + an
`X-Session-Id` header is rejected 403 `SESSION_BINDING_MISMATCH` (proven by
`test_mcp_scope_live.py::test_unbound_token_with_session_header_rejected`), and
`RealMCPClient` ALWAYS sends the header. So the probe mints a token BOUND to its OWN
synthetic session id and sends that SAME id — the probe owns both (no hijack surface),
and the scope claim stays exactly `uses`. `TokenMinter.mint` therefore takes a keyword
`session_id`. This is the only deviation; the scope-honesty and fail-closed posture are
unchanged. (Confirmed end-to-end at Layer-2: the grain probe returns 5 rows / 5 distinct
against `dbpcm_warehouse.employee`, and an out-of-`uses` column is denied
COLUMN_SCOPE_VIOLATION by the real D57 teeth.)

### 7.3 Contract change
`WarehouseProbe.run` gained a keyword `column_scope: tuple[str, ...]` (the blueprint's
`uses`); `golden_replay` passes `column_scope=gen.uses` (OQ-1: `generalization.uses`,
populated by S4 and carried on the fixture — used directly, no sqlglot re-derivation).
The four in-repo fake probes accept it (default `()`), so nothing else changed.

### 7.4 Tests
Layer-1 (26 new): shared grain-probe helper + oracle-parity identity
(`S9-probe-oracle-parity`); the real probe with a fake MCP + fake minter
(`S9-probe-never-returns-value`, `S9-probe-scoped-to-uses` mint-args half, plus
mint/MCP-failure → `probe_unavailable`); the resolver (`S9-resolver-fail-closed`); the
landing gate (`S9-promotion-gated-on-landing`, both edges + the replay-still-runs +
the require_landing=False regression guard). Layer-2 (`test_warehouse_probe_live.py`,
`MCP_TEST_URL`-guarded, RUN GREEN against the live stack): the real grain probe
end-to-end + the out-of-`uses` column denied by the real D57 scope.

### 7.5 Review fixes (APPROVE-WITH-FIXES fold-ins)
- **Empty-`uses` backstop (BLOCKER).** An empty `uses` would mint an ALLOW-ALL token
  (D80b) — the learning plane must NEVER do that. Two gates: `golden_replay`
  short-circuits BEFORE any mint/MCP call to a clean `no_uses_scope` HOLD (not the
  misleading `probe_unavailable`); `HttpTokenMinter.mint` independently RAISES
  `TokenMintError` on an empty scope (defense-in-depth). Tag
  `S9-empty-uses-refuses-allow-all` (asserts ZERO MCP/mint calls). The three contrived
  `SELECT 1 AS c` / empty-`uses` blueprint fixtures in `envelopes_each_reason.json`
  were made HONEST (real column-reading template + matching `uses`); their
  `canonical_ast_norm` (the S6 dedup-key input) was left untouched.
- **Landing gate fails loudly on a non-None writer.** Slice 1 has no land-then-promote
  sequence, so keying the gate on writer PRESENCE would let a caller passing any object
  promote a `validated`-but-unrecallable blueprint. `PromotionScheduler.__init__` now
  refuses a non-None `landing_writer` (typed `None`) with a `ValueError` until Slice 2.
- **Stale "session-less" comments corrected** to match the actual session-BOUND mint.
- **`_REPLAY_COLUMN_PROBE_LIMIT` kept at 1 (finding):** the MCP's `runQuery` `limit` is
  declared `ge=1, le=10000` (`clickhouse-api/app/mcp_server.py`), so `limit: 0` is
  REJECTED by request validation — it is not a "no limit" sentinel. 1 is the minimum;
  D98 is already satisfied (the single fetched row never reaches `ProbeResult`/logs).

---

## 8. Slice 2 — AS BUILT (landing writer + full activation)

Status: BUILT (branch `phase0/provenance-extractor`). The loop's FIRST write into what
gets RECALLED is live: on `candidate → validated` a validated blueprint LANDS into the
neo4j retrieval corpus (becomes recallable), then its `validated` status is written.
Auto-promotion-into-retrieval is now FULLY ON when every real port is configured.

### 8.1 The candidate → `BlueprintSeed` mapping + deterministic id
- **`generalize/mapping.py::blueprint_seed_from_candidate(env, *, id)`** — the parallel
  projection to §3.2. It REUSES `blueprint_from_generalization` to VALIDATE + normalize
  the structure (the same parse the executable blueprint takes), then projects the
  generalized, entity-free fields onto a `BlueprintSeed`: `intent`←payload,
  `uses`/`sql_template`/`uses_rules`/`result_grain`←generalization, `resolves`/`slots`/
  `composes`←the S3 plan (via two extracted helpers `_slot_docs`/`_compose_docs` now
  shared by BOTH `blueprint_from_generalization` and the seed map, so the landed seed
  and the executable blueprint can never drift), `status="validated"`, `drift_status`←
  the fresh stamp. It reads ONLY generalized fields — never `evidence`, audit spans, or
  entity-bearing payload (D17). Raises `ValueError` on a candidate with no
  `generalization` (fail-closed).
- **Deterministic id (`promotion/landing.py::landing_id`)** — `f"bp::{canonical_key}"`
  from the S6 dedup identity, so a re-promotion MERGEs the SAME neo4j node (idempotent
  by construction — `load_corpus` MERGEs by `id`). Fallback `f"bp::{candidate_id}"` when
  no canonical_key exists (a human-approved blueprint that never ran S6, OQ-3). Both
  forms SHARE the `bp::` prefix; disjointness rests on the SHAPE of the suffix — a
  `sha256:`-shaped canonical key vs. a `candidate::`-shaped candidate id — so the two
  never collide. The fallback has a semantic-dupe window (two candidates for the same
  canonical blueprint that never ran S6 land as two nodes); `CorpusLandingWriter.land`
  WARNs whenever it lands under the fallback so that window is observable. A re-map of
  the same candidate is BYTE-identical (`BlueprintSeed` frozen-dataclass equality).

### 8.2 The land-then-status invariant + crash-safety
- **`promotion/landing.py::CorpusLandingWriter`** — wraps the reused
  `runtime/retrieval/corpus_loader.load_corpus([seed], [], model_id=…, ensure_schema=
  False)` over an INJECTED neo4j async driver + the real `HttpEmbeddingClient` (the same
  D71 endpoint online recall embeds against — parity by construction). `load_corpus`
  embeds the `intent`, MERGE-upserts by the deterministic id, and enforces model-parity
  (`check_model_parity`, first statement of the write txn → `CorpusLoadError` on a
  mismatch). `ensure_schema=False`: the scheduler is a WRITER, not a provisioner (§3.3).
- **Entity-strip DEFENSE (last gate, D17).** Before ANY embed/neo4j write, the writer
  asserts no SETTLED S5 entity span leaked into the generalized seed
  (`_assert_seed_entity_free`) — on the normal path the candidate is stripped at
  validation (spans blanked ⇒ no-op); this is the tripwire for a bug/adversarial path
  where an un-stripped envelope reaches the global write. A detection RAISES
  `LandingEntityError` (never lands).
- **`scheduler.py::_land_and_promote(env, drift, *, action)`** — the SINGLE land-then-
  status sequence on the `→ validated` edge, shared by the auto (`_advance_candidate`)
  and human-approve paths. Order is load-bearing: `strip_entity_bearing` (idempotent —
  the human path already stripped at Guard 2; the auto path only CHECKED entity_scan) →
  `landing_writer.land(env)` → CAS `status = validated`. Invariant **"not landed ⇒ not
  validated"**: a landing failure returns a HOLD `landing_failed` and the candidate
  stays `candidate`/`in_review` (never a half state); a crash BETWEEN land and the
  status write leaves the candidate un-promoted and the next cycle re-lands idempotently
  (MERGE) then writes status. The Slice-1 constructor refusal of a non-None
  `landing_writer` is REMOVED; the param is typed `LandingWriter | None` (a new port
  Protocol in `promotion/models.py`).

### 8.3 The factory + flip-on wiring
- **`factory.py::build_promotion_write_plane(...)`** — the fully-activated write plane as
  a UNIT (§4): it takes the injected infra clients (MCP `runQuery` transport, token
  minter, neo4j driver, embedding client + model id) and wraps them into the three ports
  (`MCPWarehouseProbe` + `CandidateStoreDependencyResolver` + `CorpusLandingWriter`),
  then delegates to `build_promotion_plane` with `require_landing=True`. It constructs NO
  infra clients itself (that stays in the entrypoint). With a real writer present the
  `landing_unavailable` hold CLEARS.
- **`scripts/run_learning_scheduler.py`** — `write_plane_ready` now additionally requires
  `neo4j_url` + `neo4j_username` + `embedding_api_url` (the landing writer's ports)
  alongside the Slice-1 MCP/token config. When ALL are present the entrypoint builds the
  neo4j driver + embedding client and calls `build_promotion_write_plane` (FULL
  auto-promotion + neo4j landing ACTIVE); missing ANY port ⇒ the deferred stubs with
  `landing_writer=None, require_landing=False` (dormant, fail-closed). The driver is
  closed on shutdown.

### 8.4 Tests
- **Layer-1 (17 new/updated):** the mapping + deterministic id + identical re-map
  (`S9-land-only-validated`, `test_landing_writer.py`); the entity-strip defense — an
  entity in the seed → `LandingEntityError`, ZERO embed/neo4j calls
  (`S9-land-entity-strip-defense`); land-then-status ordering, a crash-after-land-before-
  status re-landing idempotently, and a landing failure holding `landing_failed` on BOTH
  edges (`S9-land-then-status-idempotent`, `S9-landing-failure-holds`,
  `test_land_then_status.py`); the full `candidate→validated` happy path (real replay-
  pass + hit_count≥T + a fake landing writer ⇒ landed BEFORE the status flip). The
  Slice-1 gated test's obsolete "non-None writer refused" case is replaced by a real-
  writer-lands-then-promotes regression.
- **Layer-2 (`test_learning_corpus_landing_live.py`, NEO4J_TEST_URI + EMBEDDING_TEST_URL-
  guarded, RUN GREEN against the live l2 stack):** land a validated candidate via the
  REAL writer then RECALL it through `Neo4jVectorIndex.recall` with byte-exact `uses`
  (recallable end-to-end); a second land of the same canonical_key MERGEs to exactly ONE
  `:Blueprint` node (idempotency).

### 8.5 Deviations from the Slice-2 plan
- **`LandingWriter.land` takes the ENVELOPE (+ `forbidden_spans`), not a pre-built
  seed.** §3.1 sketched `land(seed)` with the seed built in the scheduler; the writer
  instead owns the whole map→defense→write pipeline (`land(env, *, forbidden_spans)`),
  keeping the mapping + the last-gate defense colocated in the one module that performs
  the global write (a cleaner single responsibility; the scheduler stays status-only,
  D102). No behavior change.
- **The entity defense asserts against PRE-strip spans passed by the scheduler, not
  spans re-derived at the writer.** The strip blanks `entity_scan` (`span=""`), so
  re-deriving spans at the writer (as the first Slice-2 cut did) made the tripwire a
  provable no-op on BOTH wired edges (review BLOCKER). Fixed: the scheduler captures
  `redaction.entity_spans(pre_strip_env)` — on the auto edge before `_land_and_promote`,
  on the human edge before Guard 2's strip — and passes them to `land`. The writer
  RAISES `LandingEntityError` if any survives into the seed; the tripwire now fires
  through the real scheduler path (proven by a test that regresses the strip to a no-op
  and still gets a `landing_failed` hold). It never silently re-redacts.
- **Fresh drift stamped BEFORE landing (review S1).** `_land_and_promote` now applies
  the fresh `drift` to the env before the land (`replace(strip_entity_bearing(env),
  drift=drift)`), so the landed seed's `drift_status`, the crash-retry re-land, and the
  status write all carry the SAME fresh stamp — never the stale pre-promotion
  `unchecked`/`suspect`.
- **Provenance stamp (review S3).** A loop-landed node carries `created_by="learning"`
  + `source_candidate_id` (vs. a hand-authored `created_by="seed"`), so incident
  response can list/remove everything the loop landed. `BlueprintSeed.created_by`
  defaults to `"seed"` and `source_candidate_id=None` (which sets no property), so
  existing fixture nodes are byte-identical.
- **The status write is a plain store upsert, not a CAS.** S9 assumes a SINGLE
  promotion writer (the cron scan and the human-approve path both serialize through this
  scheduler over the shared store, §7.2); a second concurrent writer is out of scope and
  would need a compare-and-set. The comments were corrected from the overstated "CAS".

### 8.6 KNOWN GAP — corpus retraction (S9-activation Slice 3, next-slice obligation)

**This slice makes a demoted/broken blueprint stay recallable.** A drift-suspect
demotion (`_recheck_validated`) or a user-correction (`apply_user_correction`) writes
`status = candidate` back to the Couchbase candidate store but NEVER touches the landed
neo4j node, and `_BLUEPRINT_RECALL_QUERY` has NO status/drift filter (it selects on the
vector index only). So a blueprint that was demoted for drift-suspicion or corrected by a
user REMAINS fully recallable AND runnable from the retrieval corpus — the landing write
is currently one-way.

**S9-activation Slice 3 — corpus retraction (REQUIRED follow-on):**
- a demote / reject / user-correction MUST write back to (or remove) the landed neo4j
  node — e.g. flip `b.status`/`b.drift_status` on the node, or DETACH DELETE it;
- AND `recall` MUST filter on the landed `status`/`drift_status` (only
  `status='validated'` + non-`suspect` drift is recallable) so a stale node cannot be
  retrieved even between a demotion and its write-back.

Until Slice 3 lands, a demoted blueprint remains recallable — a KNOWN, tracked gap, not a
silent one. It is NOT built here (this slice is the forward WRITE path only).
