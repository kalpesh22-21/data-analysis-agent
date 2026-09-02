# Learning-loop Slice 7 — review-inbox + writers

**Status:** BUILT (Track B, Wave 1). Implements the terminal write-router
`CandidateStage` + the review-inbox projection and its human transitions against the
frozen Wave-0 contracts ([learning-loop-contracts-design.md](learning-loop-contracts-design.md)
§4, §7, §11.5) — it does not re-decide them.

Modules: `src/data_agent/learning/writer/`, `src/data_agent/learning/inbox/` ·
Tests: `tests/learning/writer/`, `tests/learning/inbox/`.

---

## 1. The writer stage

`WriterStage` (`writer/stage.py`, `stage_id="writer"`) runs LAST in the pipeline. It
reads the upstream verdicts (`entity_scan` from S5, `dedup` from S6,
`generalization.static_validation` from S4) plus the candidate `type` and advances the
consumer-owned `status`, returning the pipeline `control` the consumer honors
(`route_inbox` → persist at `in_review`; `continue` → auto-land at `candidate`). It
mutates only `status` (D102 additivity — `status` is a consumer-pipeline-owned field).

### Routing rules (`writer/routing.py`, precedence top-wins)

1. **`global_knowledge` / `schema_edit` → ALWAYS `in_review`** (human pre-gate; never
   auto-retrievable — D58a/D18). Reasons `knowledge_pre_gate` / `schema_edit`.
2. blueprint, `static_validation.outcome == "fail_to_review"` → `in_review`, reason
   `fail_to_review` (un-rewritable, reviewed not dropped — D52/D97).
3. blueprint, `dedup.action ∈ {conflict, merge}` (a SOFT near-miss) → `in_review`,
   reason `dedup_conflict` ("soft conflict/variant" — never auto-append, §3).
   **AMENDED (blueprint-review-rework slice):** a hard-key `increment` against a **live**
   artifact now DOES reach the writer and routes to `in_review` under the reason
   `suppressed_duplicate` — the hit count is still incremented, and the row is kept so a human
   can revise a genuine delta. An `increment` against a **terminal** artifact, and
   `redundant_with_canon`, are still dropped at S6 and never arrive. The writer also maps
   `redundant_with_canon → suppressed_duplicate` as fail-closed defence-in-depth for a
   rehydrated verdict (`DedupVerdict.from_doc` does not validate `action`).
4. blueprint, settled `entity_scan.result != "pass"` (a leakage near-miss) → **ALWAYS**
   `in_review`, reason `leakage_near_miss` (100% of near-misses, never sampled out — D58b).
5. clean blueprint, **sampled** (`blueprint_inbox_sample_rate = 0.10` provisional) →
   `in_review`, reason `blueprint_sampled` (D58b audit sample).
6. clean blueprint, not sampled → `candidate` (auto-land; retrievable after S9).
7. `user_knowledge` / unknown → pass through unchanged (S8 auto-commits + drops
   `user_knowledge` upstream; the writer never owns it).

The sample coin is an **injected decider** (`sampler`) so tests are deterministic and
no global RNG/settings is read in the stage. `route_candidate` checks the
inbox-forcing conditions (2–4) BEFORE the sample, so a near-miss is never sampled out.

`derive_inbox_reason(env)` is the single source of truth for the `InboxItem.reason`,
used by BOTH the writer (to decide) and the inbox projection (to label) — the label
can never drift from the decision. It needs no sampling flag: a clean blueprint only
reaches `in_review` via the sampled path, so it derives `blueprint_sampled`.

## 2. The review inbox

The inbox is **NOT a second store** — it is a projection over `learning_candidates`
(D101) queried by `status == "in_review"`, plus a typed reviewer view.

### `InboxItem` (`inbox/models.py`, Contract D §4)

Derived from an envelope via `InboxItem.from_envelope`: `candidate_id`, `type`,
`reason` (re-derived), `summary` (entity-free one-liner — `intent` or `statement`),
`payload_view` (entity-free-where-required payload copy), `evidence_refs` (KV keys into
`learning_audit`), `entity_scan` (the settled S5 `LeakageVerdict`; a pre-S5 `pending`
self-check is represented as an empty `pass` view — the inbox never asserts a finding
S5 did not settle), and `dedup` (the S6 verdict / collision, if any).

### `ReviewInbox` (`inbox/inbox.py`) — the human transitions

The ONLY caller-driven transitions in the write router. Each is guarded (fail-loud
`InboxTransitionError` on an illegal current status — a mis-routed action never
silently mutates a candidate):

| action | transition | notes |
|---|---|---|
| `approve` | `in_review → validated` | knowledge becomes retrievable / blueprint confirmed. Entity spans stripped first (below). `schema_edit` opens the D53 PR — the human MERGE is the real gate (out of scope here). |
| `reject` | `in_review → rejected` | archived as a **NEGATIVE signal, NOT a delete** — the row stays in the store for the S9 learner (D29). |
| `retract` | `validated → retired` | a post-promotion pull (leak/drift). The physical index removal + D25 exposure trace are **S10** (§11.4). |

**Entity-strip on promotion (D17).** `approve` moves a candidate toward a global,
retrievable state, so before it is stamped `validated` the entity-bearing leakage
`span`s are blanked from `entity_scan` (the field/kind survive for reviewer context).
The invariant "no entity span crosses into a global store" is enforced at this status
boundary, not deferred to the physical promoter.

## 3. Tests (Layer-1)

- `S7-knowledge-schema-human-pregate` —
  `test_all_global_knowledge_and_schema_edit_route_to_in_review`: EVERY
  `global_knowledge`/`schema_edit` candidate routes to `in_review` (never `candidate`,
  never auto-retrievable) even with a clean `entity_scan` and an anti-sampling sampler.
- `S7-reject-is-negative-signal` — `test_reject_marks_rejected_not_deleted`: reject
  ⇒ `status=rejected`, the row remains fetchable and surfaces via `list_by_status`,
  and leaves the `in_review` projection.
- Blueprint auto-land / sampled / fail_to_review / dedup_conflict / leakage_near_miss
  routing; approve→validated with span-strip; retract→retired; transition guards; the
  in_review projection + reason labelling — all against `envelopes_each_reason.json`.

## 4. Open items / ambiguity

- **`blueprint_inbox_sample_rate` / band thresholds** are provisional knobs (§11.5),
  injected at the composition root, not hard-coded.
- **`merge` routing.** The task spec calls out `dedup.action=conflict → inbox`; §3
  also mandates soft `merge` is never auto-appended. The writer therefore routes BOTH
  `merge` and `conflict` (`layer=soft`) to the inbox under the frozen `dedup_conflict`
  reason ("soft conflict/**variant**"), reconciling the two.
- **Wiring is deferred.** Per §7, registering `WriterStage` (and `DedupStage`) into the
  consumer's `stages` tuple is a one-line composition-root change made once when the
  contracts land — this slice builds the stages/inbox in their own modules and does NOT
  edit `consumer.py`.
