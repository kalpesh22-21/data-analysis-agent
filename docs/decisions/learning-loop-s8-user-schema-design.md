# Learning-loop Slice 8 — user-knowledge store + schema-edit PR bot (design)

**Status:** BUILT (Track B, Wave-1). Two contract-independent writers (Contract
targets §5/§8 of
[learning-loop-contracts-design.md](learning-loop-contracts-design.md)); both
payloads are Locked at S3 and carry no blueprint enrichment. Builds only in
`learning/user/` + `learning/schema_edit/` (+ matching test dirs); edits no shared
file (its own `user/config.py`, not the shared `learning/config.py`).

**Locks it builds on:** [D17] (user_knowledge auto-commits, scoped to the user;
the only entity-bearing target), [D18] (schema_edit never auto-commits),
[D53](DECISIONS.md#blueprints) (schema edits open a bot PR; human MERGE is the
gate), [D95](DECISIONS.md#memory--learning) (dedicated access-controlled store +
scoped RBAC — the audit-store pattern), [D101] (candidate store pattern),
[D102](learning-loop-contracts-design.md#9-new-decision) §11.3 (user_knowledge
auto-commit runs as a `CandidateStage` with `control="drop"`).

---

## Part A — per-user knowledge store (`learning/user/`)

### A.1 The store (mirrors D95 / D101)

A dedicated, access-controlled store behind a `UserKnowledgeStore` Protocol, with
a memory fake + a Couchbase impl — exactly the audit/candidate port shape:

| File | Role |
|---|---|
| `store.py` | `UserKnowledgeStore` Protocol + `UserKnowledgeAccessError` (the RBAC boundary) |
| `memory_user_store.py` | `InMemoryUserKnowledgeStore` — Layer-1 fake |
| `couchbase_user_store.py` | `CouchbaseUserKnowledgeStore` — own `Cluster` authed as `user_knowledge_writer` against its own keyspace (bucket/scope/collection); import-guarded like `couchbase_audit_store` |
| `config.py` | `UserKnowledgeStoreConfig` — dedicated settings (kept out of the shared `learning/config.py` to avoid a cross-track edit) |
| `models.py` | `UserKnowledgeRecord` — the committed per-user row |

**RBAC boundary (D95-style), load-bearing because this is the ONE entity-bearing
store.** The store models a Couchbase RBAC role scoped to exactly one KEYSPACE
(`bucket`.`scope`.`collection`): `open_keyspace(k)` returns the store iff `k` is the
grant, else raises `UserKnowledgeAccessError`. `list_for_user(user_id)` is the only read
surface and is always `user_id`-scoped — no cross-user surface. The Couchbase impl
authenticates as a keyspace-scoped user and its `list_for_user` is a
`user_id`-parameterized N1QL query against the three-part keyspace.

> AMENDED (shared-bucket layout). This was originally a BUCKET-scoped grant and
> `open_bucket(b)`. The deployment may now put all five stores in ONE bucket separated by
> named scopes (`pcm_iwant`.`user`.`knowledge` beside `pcm_iwant`.`learning`.`audit`),
> and there a bucket-name comparison is vacuous — it passes for the audit, candidate and
> corpus keyspaces, i.e. exactly the boundary this guard exists to defend. The guard and
> the N1QL keyspace are both built from all three parts now. `open_bucket` was renamed,
> not aliased: a vacuous guard that still looks like a guard is worse than none. The
> bucket-per-store layout is unchanged — its scope/collection are `_default`/`_default`,
> which is the same collection `bucket.default_collection()` always returned.

**Idempotency (D17).** `mint_record_id(user_id, candidate_id)` →
`userknow::<user_id>::<candidate_id>`; a re-commit UPSERTs the same key.

### A.2 The auto-commit stage

`UserKnowledgeCommitStage` (`CandidateStage`, writer position). For a
`user_knowledge` candidate it projects the envelope into a `UserKnowledgeRecord`,
auto-commits it to the per-user store (scoped to the fact's `user_id`, D17), and
emits **`control="drop"`** — so the enriched envelope NEVER reaches the candidate
holding store / review inbox (the per-user store is its home, per D102 §11.3).
Every other type passes through (`control="continue"`).

Payload tolerance: reads the Locked `user_knowledge` fields (`user_id`,
`statement`, optional `fact_type`/`structured`) and the fixture's `scope` — a
superset, robust to either spelling.

---

## Part B — schema-edit PR bot (`learning/schema_edit/`)

### B.1 What it does (D18/D53)

`schema_edit` is the highest-stakes target — it grounds `getTableSchema` for ALL
users, so it is **never auto-committed** (D18). `SchemaEditPRStage` (`CandidateStage`,
writer position) turns a `schema_edit` candidate into a **branch + YAML-patch PR**
via an INJECTED git client, gated by INJECTED CI checks (schema lint +
`explainQuery` dry-run), then routes the candidate to human review
(`status=in_review`) — the human MERGE of the PR is the real gate (D53). It writes
NOTHING to the catalog.

| Injected seam | Protocol | Test double |
|---|---|---|
| PR authoring | `GitPullRequestClient.open_pull_request(spec) -> PullRequestResult` | scripted `ScriptedGitClient` (records specs, no network) |
| CI gate | `SchemaEditChecks.run(patch) -> CheckResult` (lint + dry-run) | `AllPassChecks` default / scripted `FailingChecks` |

**The real GitHub/git wiring is deliberately DEFERRED** — this slice builds only
the seam. There is NO default git client (a missing client is a loud wiring error,
never a silent no-op that could resemble an auto-commit).

### B.2 Flow

```
CI passes -> open PR (branch + patch) -> payload.schema_edit_review{pr_opened:true, pr_url,…}
          -> status=in_review, control=route_inbox
CI fails  -> NO PR (no red branch)   -> payload.schema_edit_review{pr_opened:false, reason:fail_to_review}
          -> status=in_review, control=route_inbox
```

Either path routes to human review and NEVER auto-commits. Branch id is
deterministic (`learning/schema-edit/<content_hash>`) so a re-run targets the same
branch. The PR result is recorded ADDITIVELY under `payload.schema_edit_review`
(no new envelope field). Every non-`schema_edit` candidate passes through.

Payload tolerance: `SchemaEditPatch.from_payload` accepts the fixture shape
(`edit_kind`/`target_catalog`/`proposed_yaml`) and the Locked field names
(`edit_type`/`target`/`patch`).

---

## Tests (Layer-1)

- **User store:** RBAC boundary (`open_keyspace` denies every non-granted keyspace,
  including sibling scopes in the SAME bucket),
  per-user scoping (`list_for_user` returns only that user's rows, no cross-user
  surface), deterministic/idempotent record id, auto-commit + `control="drop"`,
  pass-through for non-user targets, doc round-trip.
- **Schema-edit PR bot:** opens the PR via the injected client (asserted, never a
  real GitHub call) and routes to review — NOT an auto-commit; PR body carries
  provenance + a "NOT auto-merged" notice; a failed CI gate opens no PR yet still
  routes to review; pass-through for non-schema_edit; patch parsing tolerance.

## Frozen-contract notes / assumptions

- Both payloads are used exactly as Locked (05 §user_knowledge / §schema_edit);
  no field added to the frozen envelope — the PR ref is additive **within**
  `payload`.
- `UserKnowledgeStoreConfig` lives in-module rather than in the shared
  `learning/config.py` to keep the slice from co-editing a cross-track file; the
  composition root can promote those knobs into the shared settings when the real
  stores are wired.
- The git/CI seams are S8-local DI seams (like S3's `ModelClient`), not frozen
  cross-stage contracts.
