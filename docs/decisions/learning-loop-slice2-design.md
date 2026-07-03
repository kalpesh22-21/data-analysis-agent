# Learning-loop Slice 2 — SessionSummary loader + triage gate + `learning_audit` provisioning

**Status:** BUILT (2026-07-03) — Layer-1 172 tests + Layer-2 live (audit RBAC/TTL + consumer S2 vs real Couchbase); reviewed (REQUEST CHANGES → 3 HIGH inference bugs fixed → APPROVE); full suite 1591 passed.

**Slice-3 forward note (non-blocking, from review):** the confirmation negation guard is
**backward-only** — "looks right? no it doesn't" / "looks right, no wait" still score
`explicit_confirm` (overstates signal *strength* for S3; cannot flip triage keep/skip since row-4
defaults to `no_correction`→K1 regardless). Rare phrasing; leave for the D100 telemetry-tuning loop.
**Scope:** Track B, Slice 2 (S2) — the layer BENEATH the S3 extractor. It replaces the Slice-1 no-op
consumer `_do_work` seam with **load → triage → (skip | hand-off)**, and provisions the D95
`learning_audit` store + a tested `evidence_ref` KV client. It does **not** design the extractor
(that is [learning-loop-extractor-design.md](learning-loop-extractor-design.md), S3); S2 produces
exactly the typed `SessionSummary` S3 consumes and stands up the audit store S3 first writes to.

**Non-goals (later slices, named so the S2 boundary is exact):** the grounded extractor + candidate
envelopes (S3); generalize / AST rewrite / static-validate (S4); leakage gate (S5); dedup (S6);
writers + review inbox (S7); promotion scheduler (S9). S2 stops at "a triaged, normalized session is
handed to a stub extractor seam that only traces `would-extract`."

**Locks this builds on:** [D26–D31](DECISIONS.md#memory--learning) (two processes; reference-not-
transcript; the D27 loader/normalizer; D31 shared-header/`accepted_signal`), [D34](DECISIONS.md#blueprint-extraction-payload)
(lift-only-accepted-SQL; `accepted_signal` mandatory for a blueprint), [D46](DECISIONS.md#context-budget--loop-guardrails)
(the FULL tool I/O trail via `result_full_ref`), [D51](DECISIONS.md#blueprint-dedup--resolvers) /
[D95](DECISIONS.md#memory--learning) (evidence snapshot → the dedicated `learning_audit` bucket;
entity-free global stores), [D58c](DECISIONS.md#memory--learning) (kill-switch), [D72](DECISIONS.md#extensibility)
(learning loop read-only w.r.t. request-path data), [D96](DECISIONS.md#memory--learning) (the Slice-1
spine + the `_do_work` seam), [D17](DECISIONS.md#memory--learning) (global = entity-free).
**New (this doc):** [D99](DECISIONS.md#memory--learning) (the `accepted_signal` deterministic
inference contract) and [D100](DECISIONS.md#memory--learning) (Phase-1 triage is deterministic
heuristics, not a cheap-LLM call).

**Aligns to existing typed models** so the S3 build maps cleanly: `SessionDoc` /
`TurnMessage` / `TrailEntry` in [`src/data_agent/runtime/session/models.py`](../../src/data_agent/runtime/session/models.py);
the store seam `SessionStore` + `write_full_result` in [`store.py`](../../src/data_agent/runtime/session/store.py);
the consumer `_do_work` seam in [`src/data_agent/learning/consumer.py`](../../src/data_agent/learning/consumer.py);
the S3 `EvidenceRef{turn_ref:int, tool_call_ref:str, quote}` header in
[learning-loop-extractor-design.md](learning-loop-extractor-design.md) §2 — S2's `SessionSummary`
must preserve `turn_index` (→ `turn_ref`) and `tool_call_id` (→ `tool_call_ref`) so S3's evidence
refs resolve.

---

## 0. TL;DR — the four load-bearing commitments

1. **`SessionSummary` (§1)** — a **deterministic, pure** normalizer of `SessionDoc` (+ full results
   via `result_full_ref`, D46) into exactly what triage + S3 need: per-turn user NL, tool calls with
   SQL/args/status/provenance, blueprint usages (accepted/corrected), askUser Q&As, failed→fixed SQL
   pairs, and the **inferred `accepted_signal`**. No LLM. Entity-bearing but strictly in-boundary.
2. **`accepted_signal` is INFERRED from the trail (§2, D99)** — the session model has **no thumbs/
   rating field**, so acceptance is derived deterministically: `no_correction` (default when a
   successful answer has no trailing correction), `explicit_confirm` (a confirmation-lexicon match),
   and `None` (no acceptance — errored / corrected-and-unfixed / greeting-only). `thumbs_up` is a
   valid enum value but is **never emitted** in Phase 1 (no capture surface exists yet). **Bias:
   conservative — ambiguous ⇒ `None`.**
3. **Triage is deterministic heuristics, not a cheap-LLM call (§3, D100)** — a cheap, deterministic,
   testable keep/skip gate runs BEFORE any extractor cost. Most sessions `skip` (terminal `done`,
   nothing enqueued downstream). LLM triage is a named later refinement, not Phase 1.
4. **`learning_audit` is provisioned + a tested client stood up, but S2 writes NO evidence (§4)** —
   the bucket, its own RBAC user, and the `AuditStore` KV client (mint / snapshot / read / retention)
   are delivered and Layer-2-validated against real Couchbase, but the **first real evidence write is
   S3's** (there are no candidates to cite yet). "Build the tooling, don't wire it prematurely."

---

## 1. The `SessionSummary` — deterministic normalizer (D27)

`load_session_summary` is a **pure function** of `(SessionDoc, full-results-loaded-via-D46)`. No LLM,
no network beyond the read-only full-result fetch, no mutation of the session (D72). It is the D27
loader/normalizer made concrete.

### 1.1 Typed schema

```python
# src/data_agent/learning/summary/models.py   (Slice 2)

AcceptedSignal = Literal["no_correction", "thumbs_up", "explicit_confirm"]
# NOTE: "thumbs_up" is declared (S3's header uses the same enum) but the S2 loader
# NEVER emits it — there is no rating capture surface yet (D99). The loader emits
# only "no_correction" | "explicit_confirm" | None.

@dataclass(frozen=True)
class ToolCallSummary:
    turn_index: int                        # → EvidenceRef.turn_ref
    tool_call_ref: str                     # = TrailEntry.tool_call_id → EvidenceRef.tool_call_ref
    tool_name: str                         # runQuery | runBlueprint | askUser | getTableSchema | ...
    args: dict[str, Any]                   # verbatim (SQL lives at args["sql"] for runQuery, etc.)
    sql: str | None                        # convenience: extracted SQL for runQuery/explainQuery, else None
    status: str                            # "ok" | "denied" | "error"
    error_code: str | None
    provenance: frozenset[tuple[str, str]] | None   # carried VERBATIM (in-boundary; may be None=undetermined)
    result_columns: tuple[str, ...]        # SHAPE only — from full result if loaded, else result_preview
    result_row_count: int | None
    result_full_ref: str | None            # the D46 pointer as recorded on the TrailEntry
    full_result_loaded: bool               # True iff the D46 full result was fetched (§1.4)

@dataclass(frozen=True)
class TurnSummary:
    turn_index: int
    user_nl: str | None                    # the role=="user" message content for this turn (None if none)
    assistant_text: str | None             # the role=="assistant" answer for this turn (None if none)
    tool_call_refs: tuple[str, ...]        # tool_call_ids produced in this turn, in order

@dataclass(frozen=True)
class BlueprintUsage:
    tool_call_ref: str                     # the runBlueprint call
    blueprint_id: str | None               # from args
    status: str                            # "ok" | "denied" | "error"
    outcome: Literal["accepted", "corrected"]   # inferred (§2.4)

@dataclass(frozen=True)
class AskUserExchange:
    question_tool_call_ref: str            # the askUser call
    question: str                          # args of askUser (the question text)
    answer: str | None                     # the NEXT user message content (None if session ended unanswered)
    answer_turn_index: int | None

@dataclass(frozen=True)
class FailedFixedSql:
    failed_tool_call_ref: str              # a runQuery/runBlueprint with status in {"error","denied"}
    failed_sql: str | None
    fixed_tool_call_ref: str               # the SUBSEQUENT status=="ok" runQuery in the same session
    fixed_sql: str | None

@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    user_id: str                           # carried from the LearningJob reference
    scope_ref: str                         # scope id/hash (D25/D30) — NEVER raw scope
    trace_id: str
    content_hash: str                      # carried for provenance / idempotency continuity
    turns: tuple[TurnSummary, ...]
    tool_calls: tuple[ToolCallSummary, ...]
    blueprint_usages: tuple[BlueprintUsage, ...]
    askuser_exchanges: tuple[AskUserExchange, ...]
    failed_fixed_sql: tuple[FailedFixedSql, ...]
    accepted_signal: AcceptedSignal | None   # None ⇒ no acceptance detected (§2)
```

### 1.2 Normalization rules (deterministic)

- **Turns** — group `messages` by `turn_index`; per turn pick the (at most one) `role=="user"` and
  the (at most one) `role=="assistant"` message; attach the `tool_call_id`s whose `TrailEntry`s carry
  that `turn_index`, in trail order.
- **Tool calls** — one `ToolCallSummary` per `TrailEntry`, preserving `tool_call_id`, `tool_name`,
  `args`, `status`, `error_code`, and `provenance` **verbatim** (the frozenset is copied, not
  re-derived — S2 never re-parses SQL for provenance; that is the request path's job). `sql` is a
  convenience extraction of `args.get("sql")` for `runQuery`/`explainQuery` (and the underlying SQL
  for `runBlueprint` is left in `args`, not lifted here — lifting is S3/S4).
- **Result shape** — `result_columns`/`result_row_count` come from the loaded **full result** when
  present (D46, §1.4), else fall back to `result_preview` (columns + row_count). S2 carries **shape
  only**; it does not copy full result rows into the summary (they stay behind `result_full_ref`).
- **Entity policy** — `user_nl`, `assistant_text`, and `args` (SQL literals) are **entity-bearing**.
  This is fine: the loader is IN-boundary and pre-leakage-gate. **Invariant:** the `SessionSummary`
  never leaves the learning process except as (a) an `evidence_ref` snapshot into `learning_audit`
  (S3, access-controlled) or (b) entity-FREE derivatives after the S5 leakage gate. S2 itself emits
  neither — it hands the in-memory summary to the stub extractor and drops it.

### 1.3 Determinism

Given the same `SessionDoc` and the same full-result bytes, `load_session_summary` returns an
identical `SessionSummary`. No timestamps of its own, no randomness, no ordering ambiguity (all
collections are built in trail/turn order). This makes every §2/§3 rule unit-testable at Layer 1 with
`InMemorySessionStore` fixtures.

### 1.4 Full-result loading (D46) — needs a new read-only store method

The loader must read the **full** tool result for shape/inspection (D46's "full tool I/O trail"),
which lives at `result::<uuid>` in the `session_results` collection. The `SessionStore` today has
`write_full_result` but **no read-back**. S2 adds a **read-only** method:

```python
# SessionStore Protocol (store.py) — additive, READ-ONLY (D72)
async def read_full_result(self, session_id: str, result_full_ref: str) -> dict[str, Any] | None:
    """Return the full tool result at *result_full_ref* (a `result::<uuid>` key), or None if
    absent/expired. Read-only: never mutates the session or the result doc (D72)."""
```

- `CouchbaseSessionStore.read_full_result` — `self._results.get(result_full_ref)`; `None` on
  `DocumentNotFoundException` (a TTL-expired result is a tolerated `None`, `full_result_loaded=False`).
- `InMemorySessionStore.read_full_result` — dict lookup in `self._results[session_id]`.

**Graceful degradation:** a missing full result is **not** fatal — the loader sets
`full_result_loaded=False` and falls back to `result_preview` shape. (The D96 §6 TTL invariant makes
this rare — the session and its results share `SESSION_TTL` and outlive the transport hop — but the
loader must not crash on a purged result.)

---

## 2. `accepted_signal` inference rules (D99) — the contract

**Problem.** D31/D34 make `accepted_signal ∈ {no_correction, thumbs_up, explicit_confirm}` a
mandatory field of a blueprint candidate, but `SessionDoc` has **no thumbs/rating field**. So
acceptance must be **inferred from the trail**. These rules are a contract (D99) because a
false-positive acceptance manufactures a blueprint candidate off an answer the user never accepted.

### 2.1 Definitions

- **Answer turn** — a turn `t` that has an `assistant_text` AND at least one `status=="ok"`
  `runQuery`/`runBlueprint` tool call in that turn. (An assistant turn with no successful data tool
  call is chat, not an answered question.)
- **Final answer turn** — the highest-`turn_index` answer turn. If none exists, the session answered
  nothing successfully.
- **Trailing user messages** — `role=="user"` messages with `turn_index > final_answer_turn`.
- **Correction phrase** (deterministic lexicon `CORRECTION_PHRASES`) — normalized-substring match on
  a curated set: `"no,"`, `"actually"`, `"i meant"`, `"that's wrong"`, `"that is wrong"`,
  `"not "`, `"should be"`, `"incorrect"`, `"wrong"`, `"instead"`. (Lexicon is versioned in code and
  covered by tests; it is intentionally small + high-precision, not exhaustive.)
- **Confirmation phrase** (`CONFIRMATION_PHRASES`) — `"yes"`, `"correct"`, `"perfect"`, `"exactly"`,
  `"that's right"`, `"that is right"`, `"thanks, that"`, `"looks right"`, `"great, thanks"`.
- **Correction event** — either (a) a trailing user message matching a `CORRECTION_PHRASE`, or (b) a
  **failed→fixed SQL pair** (§2.4) whose fix is followed by a new answer (the user drove a re-run).

### 2.2 The decision table

Evaluate top-to-bottom; first match wins.

| # | Condition | `accepted_signal` |
|---|---|---|
| 1 | No final answer turn exists (nothing successfully answered) | `None` |
| 2 | A correction event occurs **after** the final answer turn (the last answer was itself corrected and not re-answered) | `None` |
| 3 | A trailing user message after the final answer matches a `CONFIRMATION_PHRASE` (and none matches a `CORRECTION_PHRASE`) | `explicit_confirm` |
| 4 | A final answer turn exists AND no trailing user message is a correction (either no trailing user message at all — the session went idle after the answer — or the trailing messages are non-corrective) | `no_correction` |
| 5 | Ambiguous (a trailing message matches BOTH a confirmation and a correction phrase, or any case not cleanly resolved above) | `None` (conservative — §2.3) |

**`thumbs_up` is never row-selected** in Phase 1: there is no rating capture surface. The enum value
exists only so S2 and S3 share one `AcceptedSignal` type; the loader's output range is
`{no_correction, explicit_confirm, None}`.

### 2.3 The conservative bias (why ambiguous ⇒ `None`)

The failure modes are asymmetric. A **false negative** (a genuinely-accepted session scored `None`)
costs one un-learned blueprint — recoverable, because the same pattern recurs and D48 hit-count/
recurrence will catch it later. A **false positive** (an unaccepted/corrected answer scored
`no_correction`) manufactures a candidate off a wrong answer; even though `candidate ≠ validated`
(D29) contains the blast radius, it pollutes the review inbox and the dedup store. So the bias is
**keep-out on doubt**: when the rules do not cleanly resolve, emit `None`, and triage (§3) skips the
session for the blueprint target (a correction/askUser signal can still keep it for other targets).

### 2.4 Failed→fixed SQL pairing (feeds both `accepted_signal` and triage)

Scan `tool_calls` in trail order. For each `runQuery`/`runBlueprint` with `status ∈ {"error",
"denied"}`, pair it with the **next** `status=="ok"` `runQuery` in the same session as a
`FailedFixedSql`. A fixed pair is (a) a `correction event` input for row 2/4 above and (b) an
independent triage KEEP signal (§3 K2 — a failure that was fixed teaches a lesson / a `resolves`
mapping). A failed query with **no** subsequent success is **not** a fix — it contributes to "nothing
successfully answered" (row 1).

### 2.5 Blueprint usage outcome (`accepted` vs `corrected`)

A `runBlueprint` call is `corrected` if it is (a) `status != "ok"`, or (b) followed by a correction
event referencing its turn; else `accepted`. This is the negative-signal input the extractor uses to
learn *from* a blueprint that misfired (05 §"Clarification answers as learning signal").

---

## 3. Triage gate (D100) — deterministic heuristics, keep/skip

Triage answers "**is there anything worth learning?**" cheaply, BEFORE any extractor cost (05 §274:
"most sessions teach nothing; skip them cheaply before paying for extraction").

### 3.1 Recommendation: deterministic heuristics first (not a cheap-LLM triage)

05's data-flow labels triage a "cheap LLM" step. **S2 recommends replacing that, for Phase 1, with a
deterministic-heuristics gate** (D100), because:

1. **Cheaper** — zero token cost on the ~majority of sessions that skip; an LLM triage pays per
   session even to say "nothing here."
2. **Deterministic + testable** — every keep/skip is a pure function of the `SessionSummary`, so it
   is Layer-1 unit-testable with exact fixtures (an LLM gate is only statistically testable and adds a
   flaky external dependency to the hottest path).
3. **Matches the repo's philosophy** — the whole spine (D96) and the extractor's fail-to-review
   valves (D52/D97) are "deterministic gate first, LLM only where it must classify." Triage is a
   filter, not a classifier; it does not need an LLM.
4. **Reversible** — an LLM triage refinement can layer ON TOP later (as a second-stage filter over
   the sessions the heuristics KEEP, to trim false-keeps) without changing the transport or the S3
   contract. It is a named later refinement, not Phase 1.

The cost of heuristics is **recall risk** (a subtly-teachable session the predicates miss is skipped
silently). Mitigation: the predicates are deliberately **permissive** (keep on any positive signal),
and the observable `skip` trace event (§3.4) carries the reason code so skip-rate/precision can be
tuned from telemetry before an LLM stage is justified.

### 3.2 KEEP predicates (any one ⇒ keep)

| Slug | Predicate | Why it teaches something |
|---|---|---|
| **K1** | `accepted_signal != None` AND ≥1 `status=="ok"` `runQuery`/`runBlueprint` | An accepted, successful query is a liftable blueprint (D34). |
| **K2** | `len(failed_fixed_sql) >= 1` | A failure that was fixed teaches a lesson / a `resolves` mapping (global-knowledge or blueprint refinement). |
| **K3** | ≥1 `AskUserExchange` with a non-empty `answer` | A resolved clarification is a `resolves` fact / user-knowledge signal (05 §Clarification answers). |
| **K4** | ≥1 `BlueprintUsage` with `outcome == "corrected"` | A misfired blueprint is a negative signal worth capturing. |

### 3.3 SKIP predicates (keep NONE ⇒ skip)

A session is **skipped** exactly when none of K1–K4 hold. The canonical skip classes (reason codes on
the trace event) are:

- `skip_no_tool_calls` — greeting-/chat-only session (no data tool calls at all).
- `skip_all_failed` — every `runQuery` failed and none was fixed; nothing verified, no lesson pair.
- `skip_no_acceptance` — a successful query but `accepted_signal == None` and no K2–K4 signal
  (a trivial one-shot the user neither accepted nor corrected — nothing to lift).
- `skip_other` — keep predicates all false for any other reason (catch-all).

### 3.4 Observable outcome

Triage returns a typed verdict; the consumer acts on it (§5):

```python
@dataclass(frozen=True)
class TriageVerdict:
    decision: Literal["keep", "skip"]
    reason: str                      # a K1..K4 slug (keep) or a skip_* reason code
    target_hints: tuple[str, ...]    # {"blueprint","global_knowledge","user_knowledge","schema_edit"} — HINTS only, S3 authoritative
```

- **skip** ⇒ emit a `learning.triage` span with `decision="skip"`, `reason=<skip_*>`; the consumer
  proceeds to CAS `processing → done` (terminal) and **enqueues nothing downstream**. A skipped
  session is fully accounted for and never re-processed (idempotent `done`, D96).
- **keep** ⇒ emit `learning.triage` with `decision="keep"`, `reason=<K#>`, `target_hints`; the
  consumer hands the `SessionSummary` to the **stub extractor seam** (§5). `target_hints` are a cheap
  first guess (e.g. K1⇒`blueprint`, K3⇒`user_knowledge`+`global_knowledge`); S3's classifier is
  authoritative and may ignore them.

---

## 4. Provisioning the `learning_audit` store (D95)

D95 already **locks** the store (dedicated Couchbase bucket, own RBAC, KV-by-`evidence_ref`,
TTL-retained, entity-bearing snapshots never inlined into global stores). S2 **executes** that
provisioning — it makes **no new decision here**; it builds what D95 decided. The config fields
(`learning_audit_bucket`, `learning_audit_ttl_seconds`) reserved-but-unread in Slice 1 become
**read** in S2.

### 4.1 Bucket + RBAC (mirrors `scripts/couchbase-init.sh`)

- **Bucket** `learning_audit` — a **separate** Couchbase bucket on the same cluster (`l2-cb`), NOT a
  collection in `agent_sessions`. A separate bucket buys the **independent retention clock** (sessions
  TTL at 7 d; audit must survive the review-inbox dwell of days-to-weeks) and the **independent RBAC
  boundary** D95 requires.
- **RBAC user** `learning_audit_writer` — a distinct Couchbase user scoped to `learning_audit`
  **only** (`data_writer[learning_audit]` + `data_reader[learning_audit]`), with **no** grant on
  `agent_sessions`, neo4j, or the warehouse. (A read-only `learning_audit_reader` for the future
  review-inbox UI is declared in the init script but not consumed until S7.)
- **Provisioning script** `scripts/learning-audit-init.sh` — mirrors `couchbase-init.sh`: wait for
  REST, `bucket-create learning_audit --bucket-ramsize 256`, `user-manage --set` for the writer/reader
  roles. **KV-only** access (get/put by key) means **no GSI is needed** — omit the N1QL indexes the
  sessions bucket needs. Idempotent (`|| echo … (ok)` on every step). Wired into the Layer-2 stack the
  same way `couchbase-init.sh` is.

### 4.2 The `AuditStore` client (KV, seamed)

Mirrors the `SessionStore` port pattern (a Protocol + a real Couchbase impl + an in-memory fake):

```python
# src/data_agent/learning/audit/store.py
class AuditStore(Protocol):
    def mint_evidence_ref(self, session_id: str) -> str: ...
    async def snapshot(self, ref: str, snapshot: EvidenceSnapshot) -> None: ...   # KV upsert + TTL
    async def read(self, ref: str) -> EvidenceSnapshot | None: ...                # for the review UI (S7)

@dataclass(frozen=True)
class EvidenceSnapshot:                 # ENTITY-BEARING — lives ONLY in learning_audit
    evidence_ref: str
    session_id: str
    trace_id: str
    turn_ref: int                       # SessionSummary turn index
    tool_call_ref: str                  # D46 tool_call_id
    quote: str                          # the entity-bearing turn/tool-call quote (D51)
    snapshotted_at: str                 # ISO-8601
```

- **`evidence_ref` shape** — `evidence::<session_id>::<uuid4>`. The ref itself is **non-entity-
  bearing** (a session id is non-PII per D25 + an opaque uuid); it is the KV key the entity-free
  global candidate carries (D95 — "candidates carry only `evidence_ref`, never the snapshot").
  `mint_evidence_ref` is a pure key mint (no I/O), so S3 can mint→attach→snapshot deterministically.
- **`CouchbaseAuditStore`** — its own `Cluster` authenticated as `learning_audit_writer` against the
  `learning_audit` bucket (`_default._default`); `snapshot` = `collection.upsert(ref, snapshot.to_doc(),
  UpsertOptions(expiry=timedelta(seconds=learning_audit_ttl_seconds)))`; `read` = `collection.get`,
  `None` on `DocumentNotFoundException`.
- **`InMemoryAuditStore`** — dict-backed fake, same semantics, for Layer-1 wiring tests.
- **Retention** — every `snapshot` write carries `expiry = LEARNING_AUDIT_TTL_SECONDS` (default 90 d),
  the D95 floor `audit_TTL ≥ max_candidate_lifetime`. Unlike the session-lifecycle transitions (which
  `preserve_expiry` so as not to re-arm the session clock), audit writes SET the audit TTL fresh —
  the audit clock is independent by design.

### 4.3 RECOMMENDATION: S2 provisions + tests the client, but writes NO evidence

**S2 stands up the bucket + RBAC + a Layer-2-tested `AuditStore` client, and wires it into the
consumer's DI — but the consumer/stub-extractor does NOT call `snapshot`.** Rationale (consistent with
D95's "first evidence writes come with the extractor" and the repo's "build tooling, don't wire
prematurely"):

- There is **nothing to snapshot** in S2 — evidence is *cited by candidates*, and candidates are S3's
  output. A `snapshot` call in S2 would have no `EvidenceRef` to write.
- The client is nonetheless **fully proven** in S2 by **direct** Layer-2 tests (mint / snapshot /
  read round-trip / retention / RBAC boundary), so S3 inherits a de-risked, working store rather than
  co-developing it under extractor pressure.
- A Layer-1 test asserts the S2 consumer path performs **zero** `snapshot` calls (the client is
  injected but dormant) — encoding "provisioned, not yet wired" as an executable invariant.

---

## 5. Consumer integration — replacing the no-op `_do_work`

The Slice-1 `_process` flow is unchanged AROUND `_do_work`; only the seam's body changes. Today
(`consumer.py`): `get_session_with_cas` → idempotency check → CAS `queued→processing` →
`_do_work(delivered)` → CAS `processing→done` (fresh hash) → `XACK`.

### 5.1 The new `_do_work`

`_do_work` receives the **already-loaded** `doc` (avoid a second read; keep the hash source
consistent with MEDIUM-3's fresh-hash-from-`doc`). New signature:
`async def _do_work(self, doc: SessionDoc, delivered: DeliveredJob) -> None`.

```
_do_work(doc, delivered):
    summary = load_session_summary(doc, store, job=delivered.job)   # §1 — READ-ONLY (D72)
    verdict = triage(summary)                                       # §3 — deterministic
    if verdict.decision == "skip":
        emit learning.triage(skip, reason)            # nothing enqueued downstream
        return                                        # outer _process CAS→done makes it terminal
    emit learning.triage(keep, reason, target_hints)
    await self._extract_stub(summary, verdict)        # §5.2 stub extractor seam
    return                                            # outer _process CAS→done
```

The loader's full-result reads (§1.4) and triage are **read-only**; the ONLY writes remain the two
lifecycle transitions in `_process` (D72). Whether triage says keep or skip, the session still ends
`done` — "processed" means "triaged," and a kept session's downstream candidates are S3's concern, not
a new lifecycle state in S2.

### 5.2 The stub extractor seam (the S3 plug-in point)

```python
async def _extract_stub(self, summary: SessionSummary, verdict: TriageVerdict) -> None:
    """S2 stub: the CLEARLY-MARKED plug-in point S3 replaces. Emits a
    `learning.extract` span with outcome='would_extract' + target_hints and
    returns. Writes NOTHING (no candidate, no evidence snapshot). S3 replaces
    this body with the grounded extractor (learning-loop-extractor-design.md)."""
    self._emit_extract_stub(summary.session_id, verdict.target_hints)
```

`self._audit` (the injected `AuditStore`) is available but **unused** here in S2 (§4.3).

### 5.3 DI wiring

`LearningConsumer.__init__` gains three injected collaborators (all defaulted so Layer-1 tests can
pass fakes): a `summary_loader` (defaults to the module `load_session_summary`), a `triage` callable
(defaults to the module `triage`), and an `audit: AuditStore` (dormant in S2). The entrypoint
`scripts/run_learning_consumer.py` composes the real `CouchbaseAuditStore` from settings.

### 5.4 Preserved Slice-1 invariants (explicit checklist)

| Invariant | How S2 preserves it |
|---|---|
| **Kill-switch gates BEFORE this work (D58c)** | `run_once` still reads `learning_enabled()` fresh and returns before `XREADGROUP` when disabled — the loader/triage never run while disabled. Unchanged. |
| **CAS state machine (D96)** | The `queued→processing` and `processing→done` CAS transitions still bracket `_do_work`; the loader/triage sit strictly inside the existing processing window. Unchanged. |
| **Idempotency (D96 §5)** | The `done`+same-`content_hash` re-delivery short-circuit is untouched; a re-delivered already-triaged session still `dedup_skip`s. Fresh hash still recorded at `done`. |
| **Dead-letter (D96)** | `_handle_dead_letter` / reclaim path is untouched; a loader/triage exception is a normal processing failure → no `XACK`, message stays in the PEL → reclaimed → dead-lettered after N. |
| **D72 read-only** | The loader READS the session doc (already in hand) + full results (`read_full_result`, new but read-only); it never mutates the session. The only writes remain the lifecycle flag. |
| **Reference-not-transcript (D30)** | The message is still the reference envelope; the transcript is re-read from the live doc, never carried on the stream. |

---

## 6. Test matrix (Layer 1 + Layer 2)

Layer 1 = fakes (`InMemorySessionStore`, `InMemoryAuditStore`, in-memory queue). Layer 2 = real
Couchbase (`learning_audit` + `session_results`) from `docker-compose.integration.yml`. Every row maps
to a TRACEABILITY slug (§8).

### 6.1 Loader — normalization + `accepted_signal` inference (Layer 1)

| # | Case | Slug |
|---|---|---|
| L1 | `SessionDoc → SessionSummary` maps turns/tool_calls/args/status/**provenance verbatim**; `tool_call_ref`/`turn_index` preserved for S3 evidence refs | `S2-loader-normalization` |
| L2 | `accepted_signal = no_correction` — a successful answer with no trailing user message | `S2-accepted-signal-inference` |
| L3 | `accepted_signal = no_correction` — a successful answer with a trailing NON-corrective user message | `S2-accepted-signal-inference` |
| L4 | `accepted_signal = explicit_confirm` — trailing confirmation-lexicon match | `S2-accepted-signal-inference` |
| L5 | `accepted_signal = None` — final answer corrected (correction phrase after the answer) | `S2-accepted-signal-inference` |
| L6 | `accepted_signal = None` — nothing successfully answered (all queries failed / chat-only) | `S2-accepted-signal-inference` |
| L7 | `accepted_signal = None` — ambiguous (confirmation AND correction phrase) → conservative | `S2-accepted-signal-inference` |
| L8 | `thumbs_up` is NEVER emitted by the loader (range = {no_correction, explicit_confirm, None}) | `S2-accepted-signal-inference` |
| L9 | failed→fixed SQL pair detected; a failed query with no later success is NOT a fix | `S2-loader-failed-fixed-pair` |
| L10 | askUser Q&A paired with the next user message (unanswered → `answer=None`) | `S2-loader-askuser-pairing` |
| L11 | blueprint usage `outcome` = accepted vs corrected | `S2-loader-blueprint-usage` |
| L12 | full result loaded via `result_full_ref` (D46); missing/expired result → `full_result_loaded=False`, preview fallback, no crash | `S2-loader-full-result-d46` |
| L13 | **D72 read-only** — the session doc + result docs are byte-identical before/after `load_session_summary` | `S2-loader-readonly-d72` |
| L14 | determinism — same doc twice → identical `SessionSummary` | `S2-loader-normalization` |

### 6.2 Triage — keep/skip decisions (Layer 1)

| # | Case | Slug |
|---|---|---|
| T1 | K1 keep — accepted successful query | `S2-triage-deterministic-keep-skip` |
| T2 | K2 keep — failed→fixed pair present | `S2-triage-deterministic-keep-skip` |
| T3 | K3 keep — answered askUser exchange | `S2-triage-deterministic-keep-skip` |
| T4 | K4 keep — corrected blueprint usage | `S2-triage-deterministic-keep-skip` |
| T5 | skip `skip_no_tool_calls` — greeting-only session | `S2-triage-deterministic-keep-skip` |
| T6 | skip `skip_all_failed` — every query failed, no fix | `S2-triage-deterministic-keep-skip` |
| T7 | skip `skip_no_acceptance` — successful query, `accepted_signal=None`, no K2–K4 | `S2-triage-deterministic-keep-skip` |
| T8 | verdict carries the reason code + `target_hints` (K1⇒blueprint, K3⇒user/global) | `S2-triage-deterministic-keep-skip` |

### 6.3 Audit client — mint / snapshot / retention / RBAC (Layer 2, real Couchbase)

| # | Case | Slug |
|---|---|---|
| A1 | `mint_evidence_ref` yields unique `evidence::<session_id>::<uuid>` keys; pure (no I/O) | `S2-audit-store-provisioned` |
| A2 | `snapshot` then `read` round-trips an `EvidenceSnapshot` faithfully (real Couchbase) | `S2-audit-store-provisioned` |
| A3 | retention — a `snapshot` written with a short test TTL is gone after expiry; the default write uses `LEARNING_AUDIT_TTL_SECONDS` | `S2-audit-store-provisioned` |
| A4 | RBAC boundary — `learning_audit_writer` can read/write `learning_audit` but is DENIED on `agent_sessions` | `S2-audit-rbac-boundary` |
| A5 | (Layer 1) the S2 consumer path performs ZERO `snapshot` calls (client dormant — §4.3) | `S2-audit-not-wired-in-s2` |

### 6.4 Consumer integration (Layer 1 + Layer 2)

| # | Case | L1 | L2 | Slug |
|---|---|---|---|---|
| C1 | load→triage→**skip** → session ends `done`, nothing enqueued downstream, `learning.triage(skip)` traced | ✔ | ✔ | `S2-consumer-load-triage-integration` |
| C2 | load→triage→**keep** → stub extractor emits `learning.extract(would_extract)`, session ends `done` | ✔ | ✔ | `S2-consumer-load-triage-integration` |
| C3 | kill-switch still halts BEFORE load/triage (disabled ⇒ no `XREADGROUP`, no loader run) | ✔ | ✔ | `D58c-learning-kill-switch-halts-writes` |
| C4 | idempotency unchanged — re-delivery of `done`+same-hash → `dedup_skip`, loader NOT run | ✔ | ✔ | `D30-idempotent-by-content-hash` |
| C5 | **D72 end-to-end** — after a full consume the ONLY session change is `learning_status`(+hash); messages/trail/results untouched | ✔ | ✔ | `S2-loader-readonly-d72` |
| C6 | full result loaded via `result_full_ref` during a live consume (real `session_results`) | — | ✔ | `S2-loader-full-result-d46` |
| C7 | a loader/triage exception → no `XACK`, message stays in PEL → reclaim → dead-letter after N (D96 preserved) | ✔ | ✔ | `D30-dead-letter-after-n` |

---

## 7. New decisions

- **[D99](DECISIONS.md#memory--learning)** — `accepted_signal` is INFERRED deterministically from the
  trail (no rating field exists): `no_correction` default on a clean successful answer,
  `explicit_confirm` via a confirmation lexicon, `None` on error/correction/ambiguity;
  `thumbs_up` declared-but-never-emitted in Phase 1; conservative bias (ambiguous ⇒ `None`).
  *Concretizes D34/D31; genuinely new because the session model carries no acceptance signal.*
- **[D100](DECISIONS.md#memory--learning)** — Phase-1 triage is a **deterministic-heuristics** keep/
  skip gate (K1–K4 keep predicates), NOT the "cheap LLM" 05 sketched; LLM triage is a named later
  refinement layered over the KEEP set. *Refines 05's data-flow; genuinely new because it changes the
  triage mechanism.*

The `learning_audit` provisioning is **not** a new decision — it executes [D95](DECISIONS.md#memory--learning)
verbatim (bucket / RBAC / KV-by-`evidence_ref` / retention). The "S2 stands up the client but writes
no evidence" posture is D95's own "first evidence writes come with the extractor" + the
[hooks: build-not-wire](../decisions/DECISIONS.md#extensibility) convention.

---

## 8. TRACEABILITY additions (Slice-2 invariants)

New rows (Status `⛔ not-built` → `🟡 unit-green` as Layer-1 lands → `✅ green` when the Layer-2 legs
pass), added under the existing "Track-B learning-loop infra spine" section of
[TRACEABILITY.md](TRACEABILITY.md):

`S2-loader-normalization`, `S2-accepted-signal-inference` (D99), `S2-loader-failed-fixed-pair`,
`S2-loader-askuser-pairing`, `S2-loader-blueprint-usage`, `S2-loader-full-result-d46` (D46),
`S2-loader-readonly-d72` (D72), `S2-triage-deterministic-keep-skip` (D100),
`S2-audit-store-provisioned` (D95), `S2-audit-rbac-boundary` (D95), `S2-audit-not-wired-in-s2` (D95),
`S2-consumer-load-triage-integration` (D100/D96). The existing `D58c-learning-kill-switch-halts-writes`,
`D30-idempotent-by-content-hash`, and `D30-dead-letter-after-n` rows are re-exercised (C3/C4/C7) but
need no new slug.

---

## 9. Build task list (file-by-file, for the backend developer)

**New — SessionSummary loader (`src/data_agent/learning/summary/`):**

1. `models.py` — the §1.1 dataclasses (`SessionSummary`, `TurnSummary`, `ToolCallSummary`,
   `BlueprintUsage`, `AskUserExchange`, `FailedFixedSql`) + the `AcceptedSignal` type.
2. `lexicon.py` — `CORRECTION_PHRASES` / `CONFIRMATION_PHRASES` frozensets (§2.1), with a normalizer
   (lowercase, strip) helper.
3. `loader.py` — `load_session_summary(doc, store, *, job) -> SessionSummary`: normalization (§1.2),
   full-result hydration via `store.read_full_result` (§1.4), `accepted_signal` inference (§2.2), the
   failed→fixed pairing (§2.4), askUser pairing, blueprint-usage outcome (§2.5). **Pure + read-only.**

**New — triage (`src/data_agent/learning/triage/`):**

4. `triage.py` — `TriageVerdict` + `triage(summary) -> TriageVerdict` implementing K1–K4 keep (§3.2)
   and the `skip_*` reason codes (§3.3) + `target_hints`.

**New — audit store (`src/data_agent/learning/audit/`):**

5. `models.py` — `EvidenceSnapshot` (§4.2) + `to_doc`/`from_doc`.
6. `store.py` — `AuditStore` Protocol + `mint_evidence_ref` (pure key mint).
7. `couchbase_audit_store.py` — `CouchbaseAuditStore` (own `Cluster` as `learning_audit_writer`;
   `snapshot` upsert with the audit TTL; `read`).
8. `memory_audit_store.py` — `InMemoryAuditStore` fake.

**Modify — session store (read-only D46 fetch):**

9. `runtime/session/store.py` — add `read_full_result(session_id, result_full_ref) -> dict | None` to
   the `SessionStore` Protocol (READ-ONLY, D72).
10. `runtime/session/couchbase_store.py` — implement via `self._results.get(...)`, `None` on
    `DocumentNotFoundException`.
11. `runtime/session/memory_store.py` — implement via the `self._results` dict.

**Modify — consumer + config + observability:**

12. `learning/consumer.py` — replace `_do_work` with the §5.1 load→triage→skip/keep flow; change its
    signature to receive the loaded `doc`; add `_extract_stub` (§5.2); add `summary_loader`/`triage`/
    `audit` to `__init__` DI (defaulted); keep every §5.4 invariant intact.
13. `learning/config.py` — start READING `learning_audit_bucket`/`learning_audit_ttl_seconds` (drop
    the "unread" note); add `learning_audit_username` / `learning_audit_password` /
    `learning_audit_connection_string` fields for the `CouchbaseAuditStore`.
14. `learning/observability.py` — add `triage_span(decision, reason, target_hints)` and
    `extract_stub_span(target_hints)` helpers (CHAIN; shape-only attrs, D25 — never transcript/quote
    content).

**Modify — entrypoint + infra:**

15. `scripts/run_learning_consumer.py` — compose `CouchbaseAuditStore` + the loader/triage into the
    consumer.
16. `scripts/learning-audit-init.sh` — NEW: provision the `learning_audit` bucket + `learning_audit_writer`
    (+ declared `learning_audit_reader`) RBAC user; KV-only (no GSI); idempotent (§4.1).
17. `docker-compose.integration.yml` — run `learning-audit-init.sh` against `l2-cb` alongside
    `couchbase-init.sh` so the Layer-2 audit tests have the bucket + user.

**Tests:**

18. `tests/learning/summary/test_loader.py`, `test_accepted_signal.py` — §6.1 rows L1–L14.
19. `tests/learning/triage/test_triage.py` — §6.2 rows T1–T8.
20. `tests/learning/audit/test_memory_audit_store.py` — §6.3 A5 + fake round-trip.
21. `tests/integration/test_learning_audit_store_live.py` — §6.3 A1–A4 (real Couchbase, skip-guarded).
22. `tests/learning/test_consumer_slice2.py` — §6.4 C1–C5, C7 (Layer 1).
23. `tests/integration/test_learning_consumer_slice2_live.py` — §6.4 C1–C7 legs needing real infra.

**Docs:**

24. `DECISIONS.md` — add D99 + D100 (§7).
25. `TRACEABILITY.md` — add the §8 rows.
26. `learning-loop-infra-design.md` §15 — repoint the Slice-2 line to this doc; `learning-loop-extractor-design.md`
    §1 already assumes S2's `SessionSummary` + provisioned audit store — verify the field names match.
27. WORKLOG entry when S2 is built.
