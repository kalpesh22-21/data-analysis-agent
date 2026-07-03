# Learning-loop infra spine — Slice 1 design

**Status:** BUILT (2026-07-02) — Layer-1 98 tests + Layer-2 live (7 Redis, 6 Couchbase incl. the
full sweeper→Redis→consumer round-trip against real infra); reviewed (REQUEST CHANGES → fixed →
APPROVE), full suite 1517 passed.
**Scope:** Track B, Slice 1 — the transport + lifecycle spine only. NO extractor, writers,
leakage gate, dedup, or promotion (later slices). The dead-letter, idempotency, and kill-switch
paths ARE real and tested now.

**Slice-2 forward notes (non-blocking, from review):**
1. **Dead-letter source of truth = the session `learning_status` flag, NOT the `learning:jobs:dead`
   stream.** On a crash between the `*→dead_letter` CAS and `finalize_dead_letter`, recovery
   ack-skips the reclaimed message without writing the dead-stream entry — so the stream can
   under-count. The session flag is authoritative; Slice-2 dead-letter tooling must read it.
2. **MAXLEN-approximate trim edge:** a message could theoretically be trimmed before consumption
   while its session sits `queued` — only reachable under a ~100k+ backlog. Add a janitor / comment
   when backlog pressure becomes real.
3. The `enqueue_without_dedup_mark()` test seam lives on the production queue classes (inert,
   documented) — fold into a test-only subclass if it ever grows.
**Locks:** [D95](DECISIONS.md#memory--learning) (audit store), [D96](DECISIONS.md#memory--learning)
(the concrete spine). Builds against the already-Locked
[D26–D31](DECISIONS.md#memory--learning) (two processes; Redis Streams; reference-not-transcript;
`learning_status`), [D30](DECISIONS.md) (sweeper + flag), [D48](DECISIONS.md#blueprint-dedup--resolvers)
(single-writer-per-key), [D51](DECISIONS.md#blueprint-dedup--resolvers) (audit snapshot),
[D58c](DECISIONS.md) (kill-switch), [D72](DECISIONS.md) (read-only hooks), [D46](DECISIONS.md)
(full tool I/O trail).

---

## 1. What Slice 1 delivers

The moment a chat session goes idle, its learning job must (a) be **detected**, (b) be **claimed
exactly once**, (c) be **enqueued as a durable reference** (never the transcript), (d) be **consumed
exactly once**, (e) survive **crashes** (dead-letter after N attempts), and (f) **stop instantly**
when a leak is discovered (`LEARNING_ENABLED=false`). Slice 1 builds all six with a **no-op
consumer** standing in for the real triage/extractor (Slice 2). Everything downstream of "consumer
picks up a job" is a later slice; everything up to and including it is real, tested infrastructure.

Non-goals for Slice 1 (explicitly): no `SessionSummary` loader, no triage/extractor LLM calls, no
leakage gate, no candidate writers, no review inbox, no promotion scheduler, and **no evidence is
written to the audit store** (there is nothing to snapshot until the extractor exists — see §8).

---

## 2. Component / data-flow diagram

```
   ┌─────────────────────────────────────────────────────────────────────────┐
   │  session store (Couchbase, agent_sessions.sessions) — D22/D44/D45        │
   │  SessionDoc.learning_status ∈ {active, pending, queued, processing,      │
   │                                 done, dead_letter}                       │
   └───────────────┬───────────────────────────────▲─────────────────────────┘
       scan idle   │ CAS active→pending→queued      │ CAS queued→processing→done / →dead_letter
                   │                                │
        ┌──────────┴──────────┐            ┌────────┴──────────┐
        │   LEARNING SWEEPER   │            │  LEARNING CONSUMER │   ← two separate
        │   (periodic, script) │            │  (long-run, script)│     entrypoints,
        │   reads LEARNING_    │            │  reads LEARNING_    │     both traced to the
        │   ENABLED fresh/cycle│            │  ENABLED fresh/cycle│     Phoenix `learning-loop`
        └──────────┬──────────┘            └────────▲──────────┘       project (D23/D24)
                   │ XADD reference                 │ XREADGROUP `>` / XACK / XAUTOCLAIM
                   ▼                                │
        ┌──────────────────────────────────────────┴───────────────┐
        │  REDIS STREAMS (D30) — real infra, added to l2 compose    │
        │  stream  learning:jobs      group learning-workers        │
        │  dead    learning:jobs:dead                               │
        │  message = REFERENCE (D30): {session_id, couchbase_doc_id, │
        │    cas, user_id, scope_ref, trace_id, session_closed_at,   │
        │    content_hash}  — NEVER the transcript, NEVER raw JWT/scope│
        └───────────────────────────────────────────────────────────┘

  no-op consumer: read job → (idempotency check) → CAS queued→processing → emit trace
  event → CAS processing→done → XACK.  Slice-2 replaces the middle with the loader+triage.
```

Both processes run **in-boundary** (D24) and are **read-only w.r.t. the request-path data** (D72):
the only mutation they perform is advancing `learning_status` on the session doc (a lifecycle flag,
not request data). They are never model-callable tools (D26).

---

## 3. The `learning_status` state machine

Six states. `active` is the default already carried by `SessionDoc.learning_status` (models.py);
Slice 1 is the first code that transitions it. `done` and `dead_letter` are terminal.

```
active ──sweeper──▶ pending ──sweeper──▶ queued ──consumer──▶ processing ──consumer──▶ done
                                                                   │
                                                       consumer/reclaimer (N attempts)
                                                                   ▼
                                                              dead_letter
```

### Per-transition ownership + CAS strategy

Every transition is a **CAS-guarded** read-modify-write on the session doc, reusing the existing
`get_session_with_cas` + CAS-`replace` machinery (couchbase_store `_cas_mutate`, D45). The CAS is the
**single-writer-per-session** guarantee (the D48 single-writer-per-key spirit, key = `session_id`):
even with N sweeper replicas and M consumer replicas, exactly one worker wins each transition; losers
get `CASMismatchError` and skip. A transition also asserts the **expected `from` state** — a doc that
is no longer in the expected state (e.g. resumed to `active`, or already `queued` by a peer) is
skipped, not forced.

| # | Transition | Owner | Trigger | Guard |
|---|---|---|---|---|
| 1 | `active → pending` | **Sweeper** | `last_activity < now − idle_threshold` | CAS + assert `from==active`. This is the **claim**: it is what makes detection exactly-once. |
| 2 | `pending → queued` | **Sweeper** | `XADD` to `learning:jobs` succeeded | CAS + assert `from==pending`. Records `content_hash` on the doc (see idempotency). |
| 3 | `queued → processing` | **Consumer** | `XREADGROUP` delivered the message | CAS + assert `from==queued` (idempotency short-circuits a re-delivery of an already-`done` hash — §5). |
| 4 | `processing → done` | **Consumer** | no-op processing succeeded | CAS + assert `from==processing`, then `XACK`. |
| 5 | `* → dead_letter` | **Consumer / reclaimer** | delivery count `> LEARNING_MAX_DELIVERIES` | CAS (no `from` assertion — a poison job can die from `queued` or `processing`); `XADD` to dead stream + `XACK` original. |

**Crash-safety of the two-step sweeper claim.** Splitting `active→pending` (claim) from
`pending→queued` (enqueued) makes a sweeper crash *between* the CAS-claim and the `XADD` recoverable:
a session stuck in `pending` with no stream entry is re-detected by the next sweep cycle (which also
scans `pending` sessions older than `idle_threshold`) and re-enqueued — the `XADD` is idempotent by
`content_hash` (§5), so a crash that actually did `XADD` but died before the `pending→queued` CAS
does not double-enqueue. `pending` is therefore a recoverable intermediate, not a leak.

**Message `cas` is advisory.** The `cas` carried in the enqueued message (D30) is a best-effort
snapshot for the Slice-2 loader's optimistic read; it is stale the instant transition #2 bumps the
doc. The **authoritative** single-writer mechanism is the consumer's own fresh CAS at transition #3,
not the message `cas`. Slice 1's no-op consumer carries it through unused.

---

## 4. Redis Streams topology (D30)

| Thing | Value | Notes |
|---|---|---|
| Work stream | `learning:jobs` | `XADD` one entry per claimed session. Approximate-trimmed (`MAXLEN ~`) to bound memory. |
| Consumer group | `learning-workers` | Created idempotently at startup (`XGROUP CREATE … MKSTREAM`, ignore BUSYGROUP). |
| Consumer name | `LEARNING_CONSUMER_NAME` (default `worker-<host>-<pid>`) | Per-replica identity for the PEL. |
| Dead-letter stream | `learning:jobs:dead` | Terminal parking for poison jobs; not consumed by anything in Slice 1. |
| Delivery threshold `N` | `LEARNING_MAX_DELIVERIES` = **5** | Beyond N deliveries → dead-letter. |

**Enqueue (sweeper):** `XADD learning:jobs * <reference fields>`. The message is the D30
**reference envelope**, flat string fields:
`session_id`, `couchbase_doc_id` (= `session::<session_id>`), `cas`, `user_id`, `scope_ref`
(a scope **id/hash**, never the raw JWT or column_scope — D25/D30), `trace_id`,
`session_closed_at`, `content_hash`.

**Consume (consumer):** `XREADGROUP GROUP learning-workers <consumer> COUNT <batch> BLOCK <ms> STREAMS
learning:jobs >`. Process each, then `XACK learning:jobs learning-workers <id>`.

**Dead-letter / reclaim (consumer loop or a co-located reclaimer):** `XAUTOCLAIM learning:jobs
learning-workers <consumer> <min-idle-ms> 0` reclaims entries idle past
`LEARNING_RECLAIM_MIN_IDLE_SECONDS` (a crashed/slow consumer's un-ACKed messages) and re-delivers
them. When a reclaimed entry's delivery count (`XPENDING`) exceeds `N`, it is `XADD`ed to
`learning:jobs:dead`, `XACK`ed off `learning:jobs`, and its session CAS-marked `dead_letter`
(transition #5). A poison job thus never head-of-lines the stream.

---

## 5. Idempotency by `content_hash`

`content_hash` is the D28/D30 idempotency key: the same session-close must never be learned
twice, and a genuinely-changed session (new turns) must produce a new job.

**Definition (D96) — computed from the `SessionDoc`:**

```
content_hash = sha256_hex( canonical_json({
    "session_id": doc.session_id,
    "messages":   [(m.turn_index, m.role, m.content) for m in doc.messages],
    "tool_trail": [(e.turn_index, e.tool_call_id, e.tool_name,
                    canonical_json(e.args), e.status, e.error_code)
                   for e in doc.tool_trail],
}) )
```

Canonical JSON = sorted keys, UTF-8, no insignificant whitespace. **Deliberately excluded** (so the
hash is stable across the lifecycle transitions themselves and independent of storage/timestamp
noise): `learning_status` (it changes as the machine advances — including it would be circular),
`last_activity`/`created_at`/every `ts`, `result_full_ref` (a storage pointer, not content),
`result_preview` (a derived view of the full result), `pause_checkpoint`, `context_summary_cache`,
and all `provenance` sets (a re-parse artifact, not conversation content). What remains is exactly the
transcript the Slice-2 extractor will mine (messages + tool I/O identity — D46's "full tool I/O
trail").

**Enforcement points:**

1. **Enqueue (sweeper).** The computed hash is written onto the doc at transition #2 (`pending →
   queued`) as `learning_content_hash`, and carried in the message. A re-sweep of the same unchanged
   session recomputes the identical hash; the sweeper only enqueues sessions in `active`/`pending`
   (not `queued`/`processing`/`done`), so a completed session is not re-enqueued unless its content
   actually changes (which flips it back toward re-learning in a later slice — out of Slice-1 scope).
2. **Consume (consumer).** Before doing work, the consumer re-reads the doc: if it is already `done`
   **and** its recorded `learning_content_hash` equals the message's `content_hash`, this is a Redis
   **re-delivery of an already-processed job** → `XACK` and skip (no state change). This makes the
   at-least-once stream effectively exactly-once at the processing boundary.

---

## 6. The sweeper algorithm + idle / TTL relationship

```
every LEARNING_SWEEP_INTERVAL_SECONDS:
    if not learning_enabled():            # fresh read — §7
        emit `learning.sweep.skipped(disabled)`; continue
    cutoff = now − LEARNING_IDLE_THRESHOLD_SECONDS
    for (doc, cas) in store.scan_idle_sessions(status in {active, pending}, last_activity < cutoff):
        try:
            if doc.learning_status == "active":
                cas = store.transition_learning_status(id, "active", "pending", cas)   # claim
            hash = compute_content_hash(doc)
            msg_id = queue.enqueue(LearningJob.from_doc(doc, content_hash=hash))       # XADD
            store.transition_learning_status(id, "pending", "queued", cas, content_hash=hash)
        except CASMismatchError:
            continue        # a peer sweeper claimed it, or the session was resumed — skip
```

**Session-close = idle detection.** Slice 1 defines "closed" as *idle past a threshold*
(`LEARNING_IDLE_THRESHOLD_SECONDS`, default **1800 s / 30 min**) — resolving the "idle TTL value vs.
explicit end" open question in 05 for the idle half. Explicit end-of-session (a user "end" signal
marking `pending` immediately) is an additive Slice-2+ trigger; it does not change the transport.

**Resurrection safety.** A session that receives new activity after being scanned has its
`last_activity` (and CAS) bumped by the request path; the sweeper's CAS-claim then mismatches and the
session is skipped — a resumed conversation is never prematurely learned.

**`SESSION_TTL` vs. the learning window (the OPEN-QUESTIONS §Learning-loop item).** The message is a
**reference** — the Slice-2 consumer must re-read the still-live session doc to load the transcript.
So the doc must outlive the whole enqueue→process window:

> **Invariant (D96):** `SESSION_TTL > LEARNING_IDLE_THRESHOLD_SECONDS + P95(queue-dwell + processing)`.

With `SESSION_TTL = 604800` (7 d, config.py) and `idle_threshold = 1800` (30 min), the margin is
~7 days against a completion window measured in seconds-to-minutes — comfortably satisfied.
(Note the **longer-lived** concern — a candidate dwelling in the review inbox for *days* after the
session TTLs — is what the D51 evidence snapshot solves, not this window; see §8. Slice 1's window is
only the short transport hop, so no snapshot is needed yet.)

---

## 7. Kill-switch enforcement (D58c)

`LEARNING_ENABLED=false` must halt **both** enqueue and process, instantly, **without a deploy**, and
must **not** affect reads.

- **Read at runtime, not import time.** `RuntimeSettings` is `@lru_cache`d (config.py), which would
  freeze the flag at process start. The learning processes therefore read the switch through an
  **uncached** accessor `learning_enabled()` that reads the environment fresh **once per cycle** (top
  of each sweep cycle; before each `XREADGROUP` batch). Flipping the env var (or a mounted config)
  takes effect on the next cycle — no restart, no deploy.
- **Enforcement points (both halt):**
  - **Sweeper:** disabled ⇒ skip the entire cycle — no scan, no claim, no `XADD`. **No enqueue.**
  - **Consumer:** disabled ⇒ do **not** `XREADGROUP`/process; sleep and re-check. In-flight and
    future work simply **waits in the stream** (no message loss); when re-enabled the consumer
    resumes from where the group left off.
- **Reads unaffected — structurally.** The switch is consulted **only** inside the sweeper and
  consumer. The request-path read tools (`searchKnowledge`, `searchBlueprints`, `getBlueprint`,
  `runBlueprint`) live in `src/data_agent/runtime/` and never import the learning package or the flag,
  so already-published artifacts keep serving. A Layer-1 test asserts no request-path module
  references `LEARNING_ENABLED`; a Layer-2 test asserts `searchKnowledge` works with the switch off.

---

## 8. Audit / provenance store (D51 resolution → D95)

**Decision (locked now, regardless of when provisioned):** the dedicated, access-controlled
provenance/audit store is a **dedicated Couchbase bucket `learning_audit`** (NOT the existing
`agent_sessions.sessions` collection), keyed by `evidence_ref`, KV-accessed, TTL-retained.

**Why Couchbase (given the repo's existing infra):** the D51 posture is *"in-boundary, same PII
posture as the session store, own retention ≥ candidate lifetime, KV-by-ref."* Couchbase already
provides exactly that operational shape in this stack (KV get/put by key, native per-document TTL,
in-boundary, D24). neo4j and the knowledge vector index are **disqualified by construction** —
evidence quotes are entity-bearing and D17 forbids inlining entities into the entity-free global
stores. ClickHouse is the read-only data plane (D1) with the wrong access model and PII posture.
Redis is the ephemeral queue, not a durable retention store. A **separate bucket** (not just a
collection) buys an independent retention clock and an independent RBAC boundary from the session
lifecycle: sessions TTL at 7 d, but a candidate's evidence must survive the *review-inbox dwell*
(days-to-weeks), so the two cannot share a TTL.

**Access-control posture:** a distinct Couchbase RBAC user scoped to `learning_audit` **only** — not
the sessions bucket, not neo4j, not the warehouse. Write access: the Slice-2 extractor. Read access:
the review-inbox UI / audit tooling. The candidate envelopes in neo4j / the vector index carry
**only `evidence_ref`** (the KV key), never the snapshot — so the global stores stay entity-free
(D17) while audit stays durable (D51).

**Retention policy:** `LEARNING_AUDIT_TTL_SECONDS`, default **90 days** (`7776000`), a floor that must
be **≥ the maximum candidate lifetime** (max review-inbox dwell). Tunable; if review SLAs push
candidate lifetime past 90 d, raise this — the invariant is `audit_TTL ≥ max_candidate_lifetime`.

**Slice-1 vs Slice-2 — RECOMMENDATION: DEFER provisioning to Slice 2.** Slice 1 writes **no
evidence** (there is no extractor to snapshot anything). Standing up an access-controlled bucket,
RBAC user, and TTL that nothing writes to would be premature infrastructure. **Lock the decision now**
(this section + D95 — the load-bearing part is *that global stores never inline entities*, which must
be settled before any writer exists), and **provision the bucket + RBAC + the `evidence_ref` KV
client in Slice 2**, co-delivered with the extractor that first produces a snapshot. Slice 1 reserves
the config fields (`LEARNING_AUDIT_BUCKET`, `LEARNING_AUDIT_TTL_SECONDS`) unread, mirroring the repo's
"declare-but-don't-wire-until-needed" convention.

---

## 9. The queue Protocol / port seam

A thin port lets Layer-1 tests use an in-memory fake while Layer-2 validates the real Redis Streams
impl (the "real Redis now, but seamed" requirement).

```python
# src/data_agent/learning/queue.py
class LearningQueue(Protocol):
    async def ensure_group(self) -> None: ...                      # idempotent XGROUP CREATE MKSTREAM
    async def enqueue(self, job: LearningJob) -> str: ...          # XADD → message id
    async def consume(self, *, count: int, block_ms: int) -> list[DeliveredJob]: ...  # XREADGROUP >
    async def ack(self, message_id: str) -> None: ...              # XACK
    async def reclaim_stale(self, *, min_idle_ms: int, max_deliveries: int
                            ) -> list[DeliveredJob]: ...            # XAUTOCLAIM + dead-letter over N
```

- `RedisStreamsLearningQueue` (real, Slice 1) — `redis.asyncio`. Dead-letter (`XADD` to
  `learning:jobs:dead` + `XACK`) happens inside `reclaim_stale` when a delivery count exceeds
  `max_deliveries`; a dead-lettered job is reported so the consumer can CAS the session to
  `dead_letter`.
- `InMemoryLearningQueue` (Layer-1 fake) — dict + delivery counters, same semantics (PEL, redelivery,
  dead-letter threshold) so Layer-1 tests exercise the real state transitions.

`LearningJob` = the D30 reference envelope (a frozen dataclass); `DeliveredJob = (message_id, job,
delivery_count)`.

---

## 10. Observability (D23/D24/D25)

Both processes configure their own OTel `TracerProvider` with **`service.name = "learning-loop"`**
(the Phoenix "learning-loop" project), reusing `observability/tracing.py`'s `configure_tracing` with
that service name (and, for local dev/tests with no OTLP endpoint, the same no-op-provider behavior —
zero infra required to run).

| Span / event | Kind | Attributes (shape only — D25) |
|---|---|---|
| `learning.sweep` | CHAIN | `scanned`, `claimed`, `enqueued` counts; `disabled` bool |
| `learning.enqueue` | CHAIN | `session.id` (trace-grouping key), `content_hash`, `message_id` |
| `learning.consume` | CHAIN | `session.id`, `outcome ∈ {done, dedup_skip, dead_letter}`, `delivery_count` |
| `learning.disabled` | GUARDRAIL | which process halted (kill-switch trip) |

**Never** an attribute: the raw JWT, the raw `column_scope` (only the `scope_ref` id/hash travels —
D30), or any transcript/message/tool-result content. `content_hash` and `session_id` are non-PII and
are the audit/grouping keys (D25 groups traces by `session.id`). The whole job is a CHAIN per 05
§Observability.

---

## 11. Config / env vars

New `LearningSettings` (pydantic-settings, mirroring `RuntimeSettings`), plus one **uncached**
kill-switch accessor.

| Env var | Default | Meaning |
|---|---|---|
| `LEARNING_ENABLED` | `true` | D58c master switch. Read **uncached, per cycle**. |
| `LEARNING_REDIS_URL` | `redis://localhost:6379/0` | Redis Streams endpoint. |
| `LEARNING_JOBS_STREAM` | `learning:jobs` | Work stream. |
| `LEARNING_DEAD_LETTER_STREAM` | `learning:jobs:dead` | Dead-letter stream. |
| `LEARNING_CONSUMER_GROUP` | `learning-workers` | Consumer group. |
| `LEARNING_CONSUMER_NAME` | `worker-<host>-<pid>` | Per-replica consumer id. |
| `LEARNING_SWEEP_INTERVAL_SECONDS` | `60` | Sweep cadence. |
| `LEARNING_IDLE_THRESHOLD_SECONDS` | `1800` | Idle → "closed". Must keep the §6 TTL invariant. |
| `LEARNING_MAX_DELIVERIES` | `5` | Dead-letter threshold `N`. |
| `LEARNING_RECLAIM_MIN_IDLE_SECONDS` | `300` | `XAUTOCLAIM` min-idle for stuck PEL entries. |
| `LEARNING_BATCH_SIZE` | `10` | `XREADGROUP COUNT`. |
| `LEARNING_BLOCK_MS` | `5000` | `XREADGROUP BLOCK`. |
| `LEARNING_AUDIT_BUCKET` | `learning_audit` | **Reserved, unread in Slice 1** (D95, §8). |
| `LEARNING_AUDIT_TTL_SECONDS` | `7776000` (90 d) | **Reserved, unread in Slice 1** (D95, §8). |

`LEARNING_ENABLED` deliberately lives outside the `@lru_cache`d `RuntimeSettings`.

---

## 12. Test matrix (Layer 1 + Layer 2)

Layer 1 = fakes (`InMemorySessionStore`, `InMemoryLearningQueue`); Layer 2 = real Redis + real
Couchbase from `docker-compose.integration.yml`. Every row maps to a TRACEABILITY slug (§14).

| # | Invariant | L1 | L2 | Slug |
|---|---|---|---|---|
| 1 | Kill-switch halts **enqueue** (sweeper) AND **consume** (consumer); both processes | ✔ | ✔ | `D58c-learning-kill-switch-halts-writes` |
| 2 | Reads unaffected by kill-switch (`searchKnowledge` serves with switch off; request path never imports the flag) | ✔ (no-import assert) | ✔ (live read) | `D58c-kill-switch-reads-unaffected` |
| 3 | Idempotency by `content_hash`: a re-delivered / re-swept identical session processes once (dedup_skip + ACK) | ✔ | ✔ | `D30-idempotent-by-content-hash` |
| 4 | `content_hash` is deterministic, changes iff transcript content changes, ignores ts/result_ref/status/provenance | ✔ | — | `D96-content-hash-canonical` |
| 5 | Race-safe CAS transition = single-writer: two concurrent sweepers → one claim; two consumers → one `processing` | ✔ | ✔ | `D96-cas-single-writer-transition` |
| 6 | Sweeper picks **only** idle (`last_activity < cutoff`) active/pending sessions; fresh + resumed sessions skipped | ✔ | ✔ | `D30-sweeper-idle-detection` |
| 7 | Dead-letter after `N` delivery attempts: poison job → `learning:jobs:dead` + session `dead_letter`, stream unblocked | ✔ | ✔ | `D30-dead-letter-after-n` |
| 8 | `done` is terminal: a completed job's session ends in `done` with recorded `content_hash`; no re-enqueue of an unchanged done session | ✔ | ✔ | `D96-done-terminal` |
| 9 | Message is a **reference** (no transcript, no raw JWT/scope in the stream entry) | ✔ | ✔ | `D30-message-is-reference` |
| 10 | Enqueue→process happy path leaves the session doc at `done` and the stream fully ACKed (no PEL leak) | — | ✔ | `D96-spine-end-to-end-live` |

---

## 13. Slice-1 build task list (file-by-file)

**New package `src/data_agent/learning/`:**

1. `models.py` — `LearningJob` (frozen dataclass = the D30 reference envelope + `from_doc`),
   `LearningStatus` constants (`ACTIVE/PENDING/QUEUED/PROCESSING/DONE/DEAD_LETTER`) + a
   `VALID_TRANSITIONS` map, and `compute_content_hash(doc: SessionDoc) -> str` (§5 canonicalization).
2. `queue.py` — the `LearningQueue` Protocol + `DeliveredJob` (§9).
3. `redis_queue.py` — `RedisStreamsLearningQueue` (real; `redis.asyncio`; `ensure_group`, XADD,
   XREADGROUP, XACK, XAUTOCLAIM + dead-letter over N).
4. `memory_queue.py` — `InMemoryLearningQueue` (Layer-1 fake with the same PEL/redelivery/dead-letter
   semantics).
5. `state_machine.py` — `transition(store, session_id, expected_from, to, cas, content_hash=None)`
   helper wrapping the store's CAS transition + `VALID_TRANSITIONS` assertion.
6. `sweeper.py` — `LearningSweeper.run_once()` / `run_forever()` (§6 algorithm; kill-switch gate;
   two-step claim; enqueue).
7. `consumer.py` — `LearningConsumer.run_once()` / `run_forever()` (XREADGROUP → idempotency check →
   CAS queued→processing → **no-op** + trace event → CAS processing→done → XACK; reclaim +
   dead-letter; kill-switch gate).
8. `config.py` — `LearningSettings` (§11) + uncached `learning_enabled()`.
9. `observability.py` — `configure_learning_tracing()` (service.name="learning-loop") + the §10 span
   helpers (or thin reuse of `runtime/observability/tracing.py`).

**Session store extensions (`src/data_agent/runtime/session/`):**

10. `store.py` — add to the `SessionStore` Protocol:
    `scan_idle_sessions(statuses, last_activity_before, limit) -> list[tuple[SessionDoc, cas]]` and
    `transition_learning_status(session_id, expected_from, to, cas, *, content_hash=None) -> cas`.
11. `couchbase_store.py` — implement `scan_idle_sessions` via N1QL over the sessions collection
    (`WHERE learning_status IN $statuses AND last_activity < $cutoff`) and
    `transition_learning_status` via the existing `_cas_mutate` (assert `from`, set status +
    `learning_content_hash`).
12. `memory_store.py` — implement both against the dict-backed fake (emulated CAS).
13. `models.py` — add the additive `learning_content_hash: str | None = None` field to `SessionDoc`
    (+ `to_doc`/`from_doc`), defaulting `None` so existing docs round-trip unchanged.

**Entrypoints:**

14. `scripts/run_learning_sweeper.py` — compose `LearningSweeper` from settings + store + queue; run
    the periodic loop.
15. `scripts/run_learning_consumer.py` — compose `LearningConsumer`; run the blocking loop.

**Infra:**

16. `docker-compose.integration.yml` — add a `redis` service (`redis:7-alpine`, port 6379,
    healthcheck `redis-cli ping`) for the Layer-2 learning tests.

**Tests:** `tests/learning/` (Layer 1) for rows 1–9; `tests/integration/test_learning_*_live.py`
(Layer 2, skip-guarded on real Redis/Couchbase) for rows 1,2,3,5,6,7,8,10.

**Docs:** add TRACEABILITY rows (§14); this file is the design of record.

---

## 14. TRACEABILITY additions (Slice-1 invariants)

New rows carry Status `⛔ not-built` → `🟡 unit-green` as Layer-1 lands → `✅ green` when the Layer-2
legs pass. Slugs: `D58c-learning-kill-switch-halts-writes` (already a `⛔` row — moves to built),
`D58c-kill-switch-reads-unaffected`, `D30-idempotent-by-content-hash`, `D96-content-hash-canonical`,
`D96-cas-single-writer-transition`, `D30-sweeper-idle-detection`, `D30-dead-letter-after-n`,
`D96-done-terminal`, `D30-message-is-reference`, `D96-spine-end-to-end-live`.

---

## 15. Forward map — Slices 2..N (one line each; NOT designed here)

- **Slice 2 — Session loader + triage + audit store:** replace the no-op consumer with the D27
  loader→normalizer (`SessionSummary`) + cheap-LLM triage; **provision `learning_audit`** (D95) and
  the `evidence_ref` KV client (first evidence snapshots, D51).
- **Slice 3 — Grounded extractor:** RAG-grounded, structured-output extractor emitting typed
  candidate envelopes (D31), literals→slots, entity facts→user knowledge.
- **Slice 4 — Generalize + static-validate (blueprints):** lift→slots, `USES`/DAG,
  `explainQuery` dry-run, golden result-signature (D34–D36).
- **Slice 5 — Leakage gate:** regex/NER + LLM semantic scan; entity found → reroute/quarantine
  (D58/D17); `GUARDRAIL` span.
- **Slice 6 — Dedup / conflict:** the D48 canonical key + single-writer-per-`canonical_key` (over the
  same Redis) + soft embedding near-miss → review inbox.
- **Slice 7 — Writers + review inbox:** blueprint→neo4j candidate, global-knowledge→inbox (D58a),
  user-knowledge auto-commit, schema_edit→inbox.
- **Slice 8 — User store + schema-edit PR bot:** per-user knowledge store; D53 bot-authored
  `schema_edit` PRs with CI.
- **Slice 9 — Promotion scheduler:** the separate store-wide cron process (candidate→validated,
  drift/decay/retire) — the second D26 process.
