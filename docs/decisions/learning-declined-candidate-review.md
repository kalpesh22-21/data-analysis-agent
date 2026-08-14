# Declined-candidate review — making "fail-to-review" true

**Status: BUILT** (2026-08-13), to the design sketch below. Decided by the Lead after
the deductions-ratio investigation (below). Where the implementation lives:
`consumer.py::_persist_declined_for_review` (the route + its two conditions),
`candidate/decline.py` (what persists), `inbox/completion.py` (the human completion →
full re-validation → the normal stage pipeline), and the `needs_parameterization` status
across the candidate store, the inbox service and `ui/static/inbox.html`.

The two OPEN QUESTIONS below are still open: `missing_rule` does NOT route to review
(its §7 count question is unsettled), and the hint-vocabulary work remains deferred.

## The evidence that forced this document

One session — *"Which department has the highest ratio of total deductions to total
earnings?"* (`sd8f2a14f21db4dcaae5ed02a266aa1a7`, learning content hash
`efd3ec30…`) — was processed **three times** (2026-08-11, and twice on 2026-08-13,
the last on current code with the hint machinery live and tracing on). All three runs
agreed on merit and all three produced **nothing**:

| Gate | Verdict, every run |
|---|---|
| Triage | keep (K1), blueprint hint |
| Judge (prior-art, gpt-5.5) | `new` / `existing-plus-delta`, `covered_by: bp-total-earnings-by-department` — *"the closest artifact covers total earnings only; this adds deduction aggregation, a ratio, ordering for highest ratio"*. Proceeded at 0.86–0.93 |
| The SQL itself | proven live — it is the exact query that answered the runtime turn correctly |
| Extraction / D97 totality | **declined, terminal, after 2 corrective rounds — every time** |

Final decline (traced 2026-08-13, Phoenix `learning-loop`, extract span 20:12:20):

```
totality_violation: candidate.payload.parameterization has no entry for 2 literal
predicate(s) of the accepted SQL …
  - total_earnings != '0'   — no catalog rule declares this predicate
  - register_type IN ('DDUCT', 'EARN' …)
```

Six corrective attempts across three runs failed on the same two predicates. When a
model fails the *same nameable fix* six times, the model stops being the suspect:

- **`total_earnings != '0'`** guards a **derived alias** (the ratio's own computed
  denominator). No catalog rule can ever declare it; no slot binds it. The only legal
  role is `inline` + why — which the hint text never suggests, and which may also be
  tripping the non-empty-`why` check via the known placeholder-serialization habit
  (unverified; the corrected payloads are not recorded anywhere).
- **`register_type IN ('DDUCT','EARN')`** spans **two** catalog rules
  (`gross_earnings` = 'EARN', `employee_deductions` = 'DDUCT'). The correspondence
  check compares whole member sets, so *no single rule can ever validate* against the
  merged IN. The hint's per-member pairing offers exactly the citations that cannot
  pass. The fix a human would state in one breath — "split the predicate, or declare a
  slot, or inline it" — is not something the hint can currently say.

And the sharpest fact: `extractor/validation.py`'s stated doctrine is that a decline
"routes to review, never a bad landing" — but a terminal decline after corrections
**writes nothing durable**. No candidate doc, no inbox entry, no audit record beyond a
span (which, until 2026-08-13, was usually not exported at all). A blueprint the
corpus demonstrably wants **evaporated three times**. That is a false reject of a true
positive: the pipeline's precision is intact, its recall is being eaten by a form that
sometimes has no valid way to be filled in.

## The decision

**Build "fail-to-review" for merit-passed, parameterization-declined candidates.**
A candidate that (a) the judge ruled worth extracting (`proceeded`) and (b) died on
`totality_violation` / parameterization after its corrective rounds should be
**persisted to the review queue with its decline attached**, not discarded. A human
fills in the two parameterization entries in thirty seconds and the blueprint lands.

The alternative considered — extending the hint vocabulary so the model can succeed
(inline suggested for derived-alias predicates; split/slot suggestion for multi-rule
INs; verify the empty-`why` placeholder theory) — is **deferred, not rejected**. It is
worth doing eventually, but it hardens a channel that will always have a tail of
unfillable forms; the review route makes the tail survivable regardless.

## Design sketch (for the future builder)

1. **Trigger**: extraction outcome `declined` where the final reason is
   `totality_violation` (or `rule_predicate_mismatch`) AND the pre-extraction judge
   verdict was `proceeded`. Merit-failed declines (`no_evidence`, `no_acceptance`,
   `unrewritable_sql`, judge-dropped) keep today's behaviour — they are *supposed* to
   die.
2. **What persists**: the last corrected candidate payload (best attempt), the FULL
   decline detail (predicates + hints, the same text the model saw — already
   sanitized by `_flattened`/`_quoted`), `correction_history`, the judge verdict +
   `covered_by`, and the accepted-SQL evidence refs. Status: a NEW candidate status
   (e.g. `needs_parameterization`) so the inbox can render it distinctly from
   `in_review` — the reviewer's task is *complete the form*, not *judge the idea*.
3. **Leakage still gates**: the leakage scan runs on the persisted statement/SQL
   exactly as for accepted candidates. A decline must not become a side door around
   the entity scan.
4. **Human completes → normal pipeline resumes**: on reviewer completion the
   candidate re-enters validation (full re-validation, same as the corrective turn's
   contract) and proceeds through generalize → leakage → dedup → writer. No bypass.
5. **Telemetry**: `learning.extract.outcome` gains `declined_to_review`
   (distinct from `declined`); the D97 §7 counts must NOT be inflated —
   `needs_parameterization` candidates are their own number, which is precisely the
   metric that measures how often the form is unfillable.
6. **Bound it**: only the final decline of a session persists (not one per corrective
   round), and `CandidateStore.supersede(content_hash)` applies as usual so a
   re-processed session replaces its stale review item.

## Open questions for the revisit

- Should `missing_rule` (terminal, no hint) also route to review? Leaning yes — same
  merit-passed logic — but it needs the §7 count kept separate. **Still open**: the
  built route takes `totality_violation` and `rule_predicate_mismatch` only.
- Does the inbox (UI Slice 2, currently dormant) need a new card type, or does the
  existing verify/promote surface stretch? **Answered: a new card.** The row carries a
  decline block and a completion form; the detail is withheld at the wire unless the
  persisted leakage verdict is a clean `pass`.
- The empty-`why` placeholder theory: cheap to verify by recording corrected-payload
  *shapes* (D25: keys only, no values) on the extract span before building anything.

## Related

- `docs/decisions/release-1/04-evidence-validators.md` (derived-guard doctrine),
  `learning-prior-art-and-promotion-plan.md` (verify→promote flow the review item
  joins), the detected-issues stack memory (§G/§H), and the 2026-08-13 silent-skip
  consumer fix (the reason this session's three runs are reconstructable at all).
