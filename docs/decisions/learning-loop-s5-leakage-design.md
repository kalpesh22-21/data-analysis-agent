# Learning-loop Slice 5 — the leakage gate (design)

**Status:** BUILT (Track B, Wave-1). Implements Contract B
([learning-loop-contracts-design.md](learning-loop-contracts-design.md) §2) — the
authoritative `LeakageVerdict` writer. Builds only in `learning/leakage/` +
`tests/learning/leakage/`; edits no shared file (no `consumer.py`, no
`observability.py`, no `config.py`).

**Locks it builds on:** [D58](DECISIONS.md#memory--learning) (leakage gate /
knowledge pre-gate / kill-switch), [D17] (global stores stay entity-free),
[D25] (trace posture — no PII on spans), [D101] (candidate holding store),
[D102](learning-loop-contracts-design.md#9-new-decision) (the `CandidateStage`
seam + additive `entity_scan` verdict).

---

## 1. What S5 is

A `CandidateStage` (§7.1 seam) that runs over each freshly-`extracted` GLOBAL
candidate and writes the authoritative `LeakageVerdict` into `envelope.entity_scan`,
overwriting S3's preliminary `pending` self-check. It is the hard guarantee that
the entity-free stores (neo4j blueprints, the knowledge RAG index) never receive an
entity (D17). It emits one `GUARDRAIL` span per scanned candidate (D58/D25).

**The gate is the WRITER of the settled verdict** — so it never `from_doc`s the
inbound `entity_scan` (that would parse S3's un-settled `pending` sentinel as a
`LeakageVerdict` and fail loud by design, per the base's `is_settled` guard). It
only writes.

## 2. Two-layer scan

| Layer | Module | What it catches |
|---|---|---|
| regex / NER | `entities.py` | KNOWN entity shapes: `employee_code` (letter + ≥4 digits), `dept_code` (leading-zero 3–4 digit), `date` (ISO / 19xx-20xx year), `person` (two Capitalized words), `region` (EMEA/APAC/LATAM/NA/US/EU whole-words). Pure, deterministic, ordered. |
| LLM semantic | `scanner.py` (INJECTED) | What no pattern anticipated, **plus a classification** (`clean` / `leak` / `user_fact`) that drives reroute vs. reject/quarantine. Scripted double in tests — NEVER a real LLM. Defaults to the null (regex-only) scanner. |

**Scanned fields (Contract B):** blueprint → `payload.intent` +
`payload.result_signature` (canonically JSON-serialized so a nested dict is
scannable); global_knowledge → `payload.statement`. The `scanned_fields` tuple on
the verdict audits exactly what was read; `scanner` records the layer provenance
(`regex+ner` or `regex+ner+llm`).

## 3. Verdict decision matrix

```
no hits AND classification==clean                 -> pass
classification == user_fact                       -> reroute
hits AND candidate_type == global_knowledge       -> reject
hits (blueprint, no user_fact signal)             -> quarantine
```

| Verdict | Status write | Control | Side effect |
|---|---|---|---|
| `pass` | unchanged (`extracted`) | `continue` | verdict stamped; downstream writer auto-lands it |
| `reroute` | `rejected` (the global) | `route_inbox` | spawns a linked `user_knowledge` candidate (`depends_on` this one) into the candidate store |
| `quarantine` | `quarantined` | `route_inbox` | held for human (S7 routes the near-miss to the inbox — not this slice) |
| `reject` | `rejected` | `route_inbox` | terminal negative signal (audit posture, D101) |

**Why `route_inbox` for a `rejected`/`quarantined` verdict.** The four
`StageControl` values are the consumer's *persistence + flow* directives, not
inbox semantics. `route_inbox` is the only one that means "persist the enriched
envelope AND stop THIS candidate's pipeline" (`continue` would also run S6/S7 on a
rejected item; `drop` would not persist it; `halt` stops the whole batch). The
**`status` field — not the control — determines inbox visibility**: S7 projects the
inbox by `status`, so a `rejected` candidate persisted via `route_inbox` never
appears there. This keeps the terminal candidate durable for audit (D101) without
inventing a fifth control.

## 4. The reroute spawn

`reroute` = "the entity is a legitimate per-user fact, not a leak." S5 spawns a NEW
`user_knowledge` `CandidateEnvelope` carrying the entity-bearing statement,
`depends_on` the (now-rejected) global candidate, keyed deterministically off the
global id (`<id>::rerouted-userk`) so a re-run UPSERTs (idempotent). It is
persisted at `status=extracted` via the injected candidate store; S8's
`user_knowledge` auto-commit is its downstream home. The global one is marked
`rejected` — the pattern behind the entity may still be re-learned as an
entity-free global fact in a later session (D17: "global = entity-free; user = may
carry entities").

## 5. Applicability

`user_knowledge` (entity-bearing by design) and `schema_edit` (human-gated) are NOT
the gate's remit — it passes them through untouched (`control="continue"`, no
verdict written). Only `blueprint` + `global_knowledge` are scanned.

## 6. Observability

`spans.py::leakage_span` — a `GUARDRAIL` span (`learning.leakage`) emitted per
scanned candidate. D25-safe: the ONLY attributes are the verdict label, the hit
COUNT, the scanned-field labels, and the scanner string — never an entity span or
payload text. Kept in the leakage module (not the shared `observability.py`) so
this slice co-edits no cross-track file.

## 7. Tests (Layer-1)

- `S5-leakage-blocks-entity` — the leaking blueprint (intent carries `E12345` +
  `2025`) never passes; with no `user_fact` signal it quarantines; the clean
  blueprint passes. A hard entity in a `global_knowledge` statement rejects.
- `S5-reroute-to-user-knowledge` — a scripted `user_fact` classification reroutes:
  a linked `user_knowledge` candidate is spawned (`depends_on` the global) and the
  global is rejected.
- Guard tests: the gate never reads the pending self-check as a verdict; non-global
  targets pass through; scanner provenance is recorded; a consumer-integration test
  proves the stage composes in the real §7.1 seam and persists a settled verdict.

## 8. Frozen-contract notes / assumptions

- The `LeakageResult` literal, `EntityHit` shape, and `is_settled` guard are used
  exactly as frozen; S5 adds no field.
- The semantic-scanner seam (`SemanticEntityScanner`) is S5-local (not a frozen
  cross-stage contract) — it is an internal DI seam for the injected LLM, analogous
  to the S3 extractor's `ModelClient`.
- The reroute spawn uses the candidate store (the only durable seam available to a
  stage). If a later slice prefers S5 to hand the spawned fact directly to S8's
  per-user store, that is an additive change; today it persists at `extracted` and
  lets S8's auto-commit stage pick it up.
