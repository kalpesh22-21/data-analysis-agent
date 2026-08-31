# Blueprint review rework — readable cards, LLM-assisted editing, a parameterization judge

**Status: BUILT** (2026-08-28) — §B, §C and §D-1. **§D-2 remains a proposal pending the §D.0
measurement**, and no code for it exists. Where the implementation lives:

| Part | Code | Tests |
|---|---|---|
| §B the card | `ui/static/inbox.html` (`renderPayload` and the renderers under it), `InboxItem.template_parts` | `tests/ui/test_inbox_card_render.py`, `tests/learning/inbox/test_template_parts.py` |
| §C the reviser | `learning/revise/` (`schema`, `prompt`, `diff`, `engine`), `ReviewInbox.propose_revision`, `POST /inbox/{id}/revise`, the BFF allowlist | `tests/learning/revise/` |
| §D-1 the judge | `learning/paramjudge/` (`schema`, `prompt`, `judge`, `stage`), `ParamJudgeRecord` in `audit/judgement.py`, `CandidateEnvelope.param_judge` | `tests/learning/paramjudge/` |

Three placements differ from what this document originally specified, all forced by import
direction rather than taste, and each is recorded at its site: `ParamAssessment` lives in
`audit/judgement.py` beside `CoverageAssessment` (the envelope imports that module), the record
lives there too (`audit/store.py` types its port on it), and the judge is its own package rather
than `judge/parameterization.py`.

**Size:** L · **Depends on:**
[learning-loop-s4-generalize-design.md](learning-loop-s4-generalize-design.md) (the five static
checks, the rewrite), [learning-declined-candidate-review.md](learning-declined-candidate-review.md)
(`needs_parameterization`, the completion path), [ui-slice2-inbox-contract.md](ui-slice2-inbox-contract.md)
(the card, the actions, the XSS posture) · **Blocks:** nothing — all three are additive

> **The one-line claim.** A reviewer today is shown a blueprint as one undifferentiated
> `JSON.stringify(payload_view, null, 2)` and, if it needs fixing, is asked to hand-author a JSON
> array into a textarea. Both halves of that are the same mistake: the review surface speaks the
> storage format instead of the domain. §B gives the card a shape, §C gives the edit a language,
> and §D **proposes** a model call for the one parameterization question the five deterministic
> checks cannot reach — not *"should this literal have been a slot?"*, which is what was asked for
> and is the cheap half, but its dangerous inverse: ***should this slot have been frozen?*** A
> blueprint with too few slots is narrow. One with too many is wrong.

---

## A. What is already here

This document is mostly about **not rebuilding things**, so the inventory comes first.

| Thing | Where | State |
|---|---|---|
| Reviewer cards | `ui/static/inbox.html:1301` `renderItem` | Exist. Header + summary + evidence + actions are already structured; **only the payload is a dump** (`:1355–1361`) |
| Type tabs, status queues | `inbox.html:571`, `:695` | Four types, five statuses. No change needed |
| Human parameterization edit | `renderCompleteForm` (`:1160`), `POST /complete` | Exists, and works — the surface is a raw-JSON textarea (`:1172`, *"parameterization entries (JSON array)"*) |
| The re-validation engine | `learning/inbox/completion.py::ParameterizationCompleter` | Exists. Merges entries → `to_candidate` (totality walk included) → the same write-router stages |
| **Frozen-date check** | `generalize/validate.py:196` `check_no_frozen_date_literal` | **Already built.** `REASON_DATE_LITERAL = "frozen_date_literal"` |
| Totality (every literal classified) | S3 extractor, D97 | Already built. A literal predicate with no `parameterization` entry declines |
| An LLM judge with the right discipline | `learning/judge/judge.py` `CoverageJudge` | Exists — fail-open, forced tool, audit-record-first, shadow mode. **The pattern to copy** |

⚠ **The "no hardcoded dates" requirement is already deterministic and must NOT be given to the
judge.** `check_no_frozen_date_literal` walks the template AST, finds every date-shaped string
literal, and fails the candidate when one has no comparison node above it — the exact incident it
was written for (`toDateTime64('2026-08-28 00:00:00', 6)` pasted into a `DATE_DIFF`). It is free,
it runs on every candidate, and its reason tag is what S7 routes on. A second enforcer for the
same rule would disagree with the first one eventually, and would spend a model call to say what a
regex already said. This mirrors [09 §B](release-1/09-answer-judge.md)'s ruling on `markdown_table`.

So of the three asks, **one is a pure frontend change, one is a new engine with an existing
validation path underneath it, and one is a genuinely new model call with a much narrower
mandate than it first appears** (§D.2).

---

## B. Deliverable 1 — the card

### B.1 The site

`renderItem` builds an `<li>` and appends, in order: header, summary, **payload**, evidence, scan,
dedup, actions. Only the payload block changes:

```js
// inbox.html:1355 — today
var payload = document.createElement("pre");
payload.textContent = stringifyPayload(item.payload_view);
```

becomes a dispatch on `item.type`, with the dump surviving as the fallback leg:

```
renderPayload(item)
  blueprint        → renderBlueprintCard(item)
  global_knowledge → renderKnowledgeCard(item)
  user_knowledge   → renderKnowledgeCard(item)
  schema_edit      → renderSchemaEditCard(item)
  (anything else)  → the current <pre> dump, verbatim
```

**The unknown-type leg is not defensive boilerplate; it is the correctness argument for the whole
change.** A typed renderer shows the fields it knows about, so a payload key nobody anticipated
becomes invisible rather than ugly. The fallback guarantees that a candidate type added later is
merely unpretty, never silently truncated — and §B.4 keeps the same guarantee for the types that
*do* have a renderer.

### B.2 The blueprint card

| Section | Source | Notes |
|---|---|---|
| Intent | `payload.intent` | The headline. Today the card's `<h3>` is the *routing reason* (`fail_to_review`), which says why it is in the queue, not what it is |
| Template | `payload.generalization.sql_template` | `{slot}` tokens rendered as chips (§B.3) |
| Slots | `payload.parameterization[]` where `role == "slot"` | Table: name · type · `binds_to` · required · `optional_pattern` · `enum_values` |
| Frozen filters | `role == "inline"` | Value + the model's `why`. **The single most important new section** — see §D.2 |
| Rule filters | `role == "rule"` | `rule_id` + locator |
| Uses | `generalization.uses`, `uses_rules` | Collapsed list, count in the summary line |
| Result | `payload.result_signature` | Shape columns, grain, invariants |
| Checks | `generalization.static_validation` | Five pass/fail pills + the `reason` tag when `outcome == "fail_to_review"` |
| Raw JSON | the whole `payload_view` | `<details>`, collapsed, **exactly the current `stringifyPayload` output** |

Rendering the inline predicates *next to* the slot table is what makes an over-inlined blueprint
visible at a glance — a `region = 'NA'` sitting under "frozen filters" with an unconvincing `why`.
**This is the real fix for the over-inlining complaint**, and §D.2 explains why: the automated
repair for it is the risky direction, so the defect is best handed to a human who can see it for
free here, and who can do the one thing the pipeline cannot — ask a new question that produces a
blueprint with the flexibility this one's SQL never had.

### B.3 Slot chips, safely

`payload_view` is server-redacted but still untrusted, and the contract is `textContent` only,
never `innerHTML` with server data (`ui-slice2-inbox-contract.md` §4). Chips are therefore built
by splitting the template string and appending alternating text nodes and `<span class="slot">`
elements — no string concatenation into markup at any point.

The split needs the slot-token regex, which is `runtime/blueprint/template.py:35`:

```python
_SLOT_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
```

⚠ **Re-spelling it in JS forks a definition that two layers depend on.** Two options, and the
second is preferred:

1. Mirror the regex in `inbox.html` with a parity test that asserts the JS and Python token sets
   agree over a fixture corpus. Cheap, but the test has to keep a JS engine around.
2. **Have the wire projection ship the tokenization**: `InboxItem` gains
   `template_parts: [{"text": "SELECT … WHERE department = "}, {"slot": "department"}, …]`,
   produced in `inbox/models.py` by the same `SLOT_TOKEN` the runtime uses. The browser then does
   no parsing at all, which is also the stronger XSS posture.

Option 2 costs one derived field on a projection that is already derived-not-stored, and removes
the fork rather than testing it. Take it — **with one condition that is easy to get wrong and is
a D17 leak if you do.**

⚠ **`template_parts` MUST be tokenized from the REDACTED view, never from `env.payload`.**
`payload_view` is `entity_free_payload_view(env)`; the raw payload is not. A template is not
slots-only — every `role="inline"` predicate keeps its **literal value** in the template body
(`record_type = 'EARNING'`), and an inline literal can be entity-bearing. Deriving the new field
from the raw payload would ship those literals to a browser through a field added specifically to
be safer, bypassing the redaction the `<pre>` beside it respects. Concretely: tokenize
`payload_view["generalization"]["sql_template"]`, not `env.payload[...]`, and assert it in a test
(§G) rather than in a comment.

The general shape of the mistake: **a derived field inherits the trust level of its source, not of
its sibling.** Any future addition to `InboxItem` that reaches into `env.payload` re-opens this.

### B.4 Not losing fields

A typed renderer that silently omits an unrecognised key is a worse dump than the dump. The
blueprint renderer therefore computes `Object.keys(payload_view)` minus the keys it consumed, and
if the remainder is non-empty renders it as an "other fields" block above the raw `<details>`.
This is the same instinct as the unknown-type fallback, one level down, and it means adding a
payload key upstream can never quietly stop being reviewable.

### B.5 The `needs_parameterization` card is the common case

The queue this rework exists for holds candidates whose `generalization` is the **fail-to-review**
one (`builder.fail_to_review_generalization`) — there is no `sql_template`, no `uses`, and the
`static_validation` carries the failing reason. Every section above must render from absent data
without throwing, and the card must lead with the decline detail (which today is already rendered
by `renderDecline`, above the actions). Concretely: the template section is replaced by the
**accepted SQL's uncovered predicates as named in `decline.detail`**, because on this queue that
text *is* the form.

### B.6 Test impact

`tests/ui/test_inbox_markup.py` asserts on `data-testid="inbox-payload-view"`. Keep that testid on
the `<pre>` **inside** the new `<details>`, so the existing assertions keep pointing at the raw
JSON they were written for, and add new testids (`inbox-bp-intent`, `inbox-bp-template`,
`inbox-bp-slots`, `inbox-bp-inline`, `inbox-bp-checks`, `inbox-raw-toggle`) for the new sections.

> **Footnote, unrelated to the deliverable.** `redaction.py:39`'s docstring says entity spans are
> *"removed from all string leaves"*; the body substitutes `_REDACTED = "[redacted]"` (`:21`). The
> code is right and `inbox.html:557`'s comment is right; the docstring is stale. One-line fix.

---

## C. Deliverable 2 — LLM-assisted editing

### C.1 The invariant that makes this safe

**The model edits the `parameterization` array. It never emits SQL.**

`sql_template` is not authored — it is *derived*, by AST rewrite from the accepted SQL that
actually ran (`generalize/rewrite.py::rewrite_sql_to_template`), with the parameterization array
saying which literals become holes. That derivation is what makes `explain_ok`,
`binds_to_subset_uses` and `read_only_select` mean anything: they check a provenance chain back to
a query the warehouse really answered.

If a model writes the template, all five checks silently change subject — they start validating
model prose. The rewrite's own `_check_rewritten` / `_recheck_inline_literals` guards exist
precisely to prove the rewritten form still *is* the accepted query, and there is nothing to prove
that about a freshly-generated string.

So the revise engine's output schema **has no `sql_template` field**, and a response carrying one
is rejected rather than ignored — the difference matters, because ignoring it would let a model
believe it had changed something it had not.

### C.2 The engine

New package `src/data_agent/learning/revise/`:

```
revise/
  engine.py   BlueprintReviser — brief → model → validated entries
  prompt.py   SYSTEM_PROMPT + blueprint_brief()
  schema.py   the forced tool + the guard on what comes back
```

**What the model is shown.** The brief carries the intent and notes; the current
`parameterization` array; the **accepted SQL** from `env.revalidation.sql_by_ref`; the decline
block or judge feedback; the reviewer's free-text feedback; the catalog rule ids in force
(`known_rules`); and the columns of the tables the SQL touches, so a proposed `binds_to` is
groundable rather than invented.

⚠ **The engine reads the FULL payload and the raw accepted SQL, not the redacted `payload_view`.**
The literal values are the entire subject — a model asked to re-role `department = '0420'` while
being shown `department = '[redacted]'` has nothing to reason about. This is not a new trust
boundary: the S3 extractor is already handed the same accepted SQL from the same session. The
asymmetry to state plainly in the code is *the human sees the redacted view; the model sees what
the extractor saw*, and the engine therefore runs **server-side in the inbox service**, never in
the BFF and never with anything from the browser other than the feedback string.

**The forced tool** (`propose_parameterization`), mirroring `judge/schema.py`'s posture — every
guard derived from what downstream reads, not from field names:

| Field | Downstream read | Guard |
|---|---|---|
| `entries` | walked by `to_candidate`'s totality check; each item indexed by `locator`/`role` | a `list` of `dict`; length capped; anything else is a rejection |
| `replace` | selects `_merged_parameterization`'s append-vs-replace branch | strict `bool`, defaulting **False** (append) — the safer branch, since append leaves valid classifications intact |
| `rationale` | rendered to the reviewer, logged | `str`, flattened to one line, capped |

Per-entry field validation is deliberately **not** re-implemented here. `to_candidate` already
owns it, it is the same validator the extractor's output faces, and a second vocabulary for the
same mistake is what `ui/server.py:595`'s proxy docstring warns against.

### C.2.1 ⚠ "One engine, two callers" is a claim to check, not a win to assume

The reviser has two callers — the UI (§C.3) and the judge loop (§D.3) — and it is tempting to
present that as reuse. **Look at the populations before believing it:**

| | UI caller | Judge caller |
|---|---|---|
| Status | `needs_parameterization` | passed all five static checks |
| Has a `sql_template`? | **No** — `fail_to_review_generalization` | Yes |
| Parameterization | incomplete (that is the defect) | complete (the roles are the defect) |
| The brief's core | the decline detail + the uncovered predicates | the judge's finding + the rewritten template |
| Failure being fixed | *a literal nobody classified* | *a literal classified wrongly* |

Those are **disjoint candidate sets with different inputs**. What they genuinely share is the
output contract — a `parameterization` array that `to_candidate` will walk — and the guard on it.
What they do not share is the prompt.

So the honest factoring is: **`schema.py` is shared, `prompt.py` is two functions**
(`completion_brief` and `critique_brief`), and `engine.py` is a thin caller of whichever brief it
was handed. If implementation finds itself branching on caller inside one brief builder, that is
the signal the module is two modules wearing one name — split it then, and do not defend the
shared name.

The reuse that *is* load-bearing and must not be forked: both paths land on
`ParameterizationCompleter` → `to_candidate` → `build_write_router_stages`. One validation path,
whatever produced the entries.

### C.3 Propose, then apply — two steps, one write path

```
POST /inbox/{id}/revise   {feedback: "region should be a slot, not frozen"}
  → 200 {proposal: {entries: [...], replace: false}, rationale: "...", diff: {...}}
     (nothing has been written)

POST /inbox/{id}/complete {entries: [...], replace: false}     ← the EXISTING route
  → the existing ParameterizationCompleter → to_candidate → the write-router stages
```

**`complete` stays the only write path.** The revise route is a proposal generator with no store
access; the human is still the committer, and the thing they commit goes through the identical
re-validation a hand-typed array goes through. `completion.py`'s docstring states the rule this
preserves: *"The one thing a human is trusted with is CONTENT, never the checks."* A model is
trusted with less than a human, so it certainly does not get a shortcut around them.

`diff` is computed server-side (entry added / removed / role changed / slot retyped) so the card
can show a slot-level before-and-after rather than two JSON blobs, which would reproduce the
original complaint inside the fix.

**Wiring:** add `"revise"` to `_INBOX_ACTIONS` (`ui/server.py:136`) and `_INBOX_BODY_ACTIONS`
(`:143`). The body cap (`INBOX_BODY_MAX_BYTES`, 256 KiB) already applies. The BFF stays
body-agnostic — it forwards verbatim and propagates the service's status, exactly as it does for
`complete` and `promote`; note that the revise response is a *third* body shape on that route, so
`_proxy_inbox`'s "must never assume one of those two shapes" comment needs updating to say three.

### C.4 ⚠ First cut is `needs_parameterization`-only, and the reason is structural

`ParameterizationCompleter.complete` opens with:

```python
if env.revalidation is None:
    raise CompletionUnavailableError("… carries no re-validation snapshot …")
```

`ValidationSnapshot` is written by `build_declined_envelope` **at decline time only**. A candidate
that sailed through S3 and sits at `in_review` has `revalidation is None`, so there is nothing to
re-validate a revision against — no accepted SQL, no session summary (the live one is an
in-process value dropped when extraction ended).

Editing an `in_review` blueprint therefore requires snapshotting the validation context on **every**
candidate, not just declined ones. That is a real option and probably the right eventual answer,
but it is a store-shape change with an entity-bearing cost (`sql_by_ref` holds raw literals, which
is why the snapshot lives in the access-controlled store), and it drags the re-run through dedup
and leakage with different routing consequences. **Deferred, named here so it is not re-derived.**

The first cut covers the queue where the need is sharpest and the machinery already exists.

---

## D. Deliverable 3 — the parameterization judge

### D.0 ⚠ Why this section is a proposal and not a design

**The documented failure mode of this pipeline is refusing good work, not admitting bad work.**

[learning-declined-candidate-review.md](learning-declined-candidate-review.md) is a case study of
exactly that: one session — *"Which department has the highest ratio of total deductions to total
earnings?"* — processed **three times**, passing triage and the coverage judge on every run, whose
SQL *"is the exact query that answered the runtime turn correctly"*, producing **nothing** all
three times after six corrective rounds. The candidate that reaches the end of this pipeline has
already survived triage, a coverage judge, the D97 totality walk and five static checks. It is a
survivor of a gauntlet with a demonstrated over-refusal problem.

Adding a sixth gate that can discard is therefore a bet on a base rate **nobody has measured**:

- *How many blueprints reach the judge at all?* (i.e. pass all five static checks)
- *Of those, how many would it flag — and of the flags, how many are real?*
- *How many of those were going to be seen by a human anyway?* A `blueprint_sampled` candidate
  routes to `in_review`, where a reviewer looks at it for free. Auto-discard upstream of that
  removes cards the existing sampling was already catching, and pays a model call to do it.

§D.4 answers *"how do we make a discard auditable"*. That is the wrong first question. The right
one is *"does this judge agree with humans often enough to be allowed to act"*, and it is
answerable only with data that does not exist yet.

**So the deliverable is split, and only the first half is designed here:**

| Phase | What ships | Gate to the next phase |
|---|---|---|
| **D-1 — measure** | The stage, the verdict schema, the audit record, the card marker. `shadow_mode=True`, **hard-wired**, no discard code path at all | A week of records read by a human, with the flag rate and the agree-with-reviewer rate written down |
| **D-2 — act** | The repair loop and auto-discard, per §D.4 | Not designed until D-1 reports |

Auto-discard remains the chosen end state (decided 2026-08-28); §D.4 records what it must satisfy.
It is not phase 1. **Do not implement a discard path in D-1** — a dormant discard branch guarded by
a flag is how a bar nobody validated ends up live after a config change.

### D.1 Placement

A new `ParameterizationJudgeStage`, inserted into the frozen write-router order
(`factory.py:362`, `build_write_router_stages`):

```
GeneralizeStage → [ParameterizationJudgeStage] → LeakageGateStage → DedupStage → … → WriterStage
```

**After generalize**, because the judge should see the rewritten template and the resolved
`uses`, not just the plan. **Before leakage**, for two reasons: a candidate about to be discarded
should not consume an entity scan, and the revise loop mutates the payload — settling a scan and
then changing what it was settled about is exactly the bug `completion.py::_still_declined` had to
add a re-settle to fix (*"THE SCAN IS RE-SETTLED, because the payload CHANGED"*).

**It runs only when the five static checks passed.** A `fail_to_review` candidate already has a
deterministic complaint with a reason tag; paying a model to add a second opinion about a
candidate that is already routed is waste, and would risk the two disagreeing about why. The
judge's whole population is *candidates that passed every mechanical check and are still bad.*

`include_target_specific=False` (the completion path) gets the stage too — a human-completed form
should face the same bar as an extracted one.

### D.2 ⚠ What the judge can and cannot ask for

Two ceilings, both of which invert the obvious reading of the requirement.

**Ceiling 1 — the rewrite can only punch a hole where a literal already exists.** A blueprint
generalized from `WHERE department = '0420' AND record_type = 'EARNING'` can have at most two
slots. If the judge asks for a `region` slot, there is no `region` predicate in the accepted SQL to
parameterize, and the only way to produce one is to *write SQL* — which §C.1 forbids, and which
would make the template stop being the query that ran. The judge can only **re-role literals that
are already there.**

**Ceiling 2 — and this is the one that reverses the design.** The requirement as stated
(*"not enough slots for flexibility"*) points at under-parameterization. But the two errors are not
symmetric, and the dangerous one is the opposite:

| Error | What it produces | Cost |
|---|---|---|
| **Over-inlined** — a varying dimension frozen (`region = 'NA'` inline) | A blueprint that is **narrow**. It answers its question correctly, for fewer cases | Reduced reuse. A recall miss |
| **Over-slotted** — a metric-defining literal turned into a slot (`record_type` slotted) | A blueprint that computes a **different metric** when the slot is filled differently, under an `intent` that still says "earnings" | **A confidently wrong answer at replay.** The D97 class |

An over-inlined blueprint is a smaller blueprint. An over-slotted one is a wrong one. So a judge
whose repair direction is `inline → slot` pushes toward the failure that manufactures wrong
answers, in order to fix a failure whose cost is a recall miss.

**The judge is therefore biased the other way**, and the finding classes are ranked accordingly:

| Class | Criterion | Weight |
|---|---|---|
| **A** | A slot that should be `inline` — the literal defines the metric, not the question. Evidence: the `intent` text names the thing the literal encodes (`record_type='EARNING'` under an intent about *earnings*) | **The primary target.** The only class that may ever justify a discard (§D.4) |
| **A** | `intent` describes something the template does not compute | Primary. The strongest checkable one — template, result signature and intent are all in hand |
| **B** | A slot named `x1`, or typed `entity` where `period` was meant | Advisory. A badly-named slot is unusable at recall, but it is not wrong |
| **C** | A varying dimension frozen as `inline` | **Advisory only, never grounds for a discard.** The original requirement, demoted — the repair is the risky direction, so it is proposed to a human and never enforced |
| — | Frozen absolute date in the template | **Already deterministic** — `check_no_frozen_date_literal`. Never give it to the judge (§A) |
| — | Every literal predicate classified | **Already deterministic** — the D97 totality walk |
| — | A slot for a predicate not in the accepted SQL | **Structurally impossible.** Ceiling 1 |
| — | Whether the blueprint duplicates the corpus | **Not this judge** — `CoverageJudge.adjudicate_candidate`, in the dedup stage |

Class C is the user-facing motivation for this whole section and it ends up the *weakest* finding
in it. That is the correct outcome, and the reason to state it here rather than discover it in
review: **the review surface (§B), not the judge, is the right fix for over-inlining.** A card that
puts frozen filters next to slots with their `why` text makes a suspicious inline visible for free,
and lets a human — who can add flexibility the SQL never had, by asking a new question — act on it.

### D.3 The loop (phase D-2)

```
generalize → judge
   ok      → continue
   revise  → BlueprintReviser (§C.2, feedback = the judge's own)
             → re-run generalize
                 static checks PASS → re-judge   (round += 1, ≤ max_rounds)
                 static checks FAIL → REVERT (see below), round += 1
   reject  → per §D.4
```

**⚠ The revert rule, which the first draft left undefined.** A revision can make the static checks
fail that previously passed — a new slot breaking `binds_to_subset_uses`, a re-role making the
template unrewritable. The candidate would then be `fail_to_review`, which §D.1 says the judge does
not run on, so it would exit the loop *sideways* into a path nothing specifies.

The rule: **a repair that breaks the static checks is a FAILED repair, not a new candidate.** The
pre-revision envelope is restored verbatim, the round is consumed, and the static failure's reason
tag is appended to the feedback for the next round (it is the most actionable thing the loop can
say: *"your last proposal produced `binds_to_not_subset`"*). Consequences, all deliberate:

- **A revision can never make a candidate worse.** The worst outcome of the whole loop is the
  candidate you started with. This is the property that makes the loop safe to enable before
  the judge itself is trusted.
- The five static checks stay the authority. The judge never routes a candidate to
  `fail_to_review`; only the deterministic checks do, and they do it on the original payload.
- A candidate that exhausts its rounds this way is, on the evidence, one the reviser cannot fix
  — which is a *stronger* argument against discarding it than a run of `revise` verdicts, since
  every proposal was rejected by a deterministic check rather than by a model.

The round counter and feedback strings **extend `DeclineBlock.corrections_attempted` /
`correction_history`** (`candidate/decline.py`) rather than adding a parallel pair of fields. Those
already exist to record "what was this candidate asked to fix, and how many times", they are
already carried across a re-queue, and the inbox already renders them. Note the semantic widening
to state in the code: today `corrections_attempted` counts what the **model** was asked (a human
completion deliberately does not increment it, per `_still_declined`); a judge round is a model
round, so it does increment.

### D.3.1 Why a third repair loop is justified

There are already two: the extractor's corrective rounds, and human completion. A third needs an
argument, not an assumption.

**The extractor's loop cannot take this critique, because the extractor never sees the artifact
the critique is about.** S3 produces the parameterization *plan*; the AST rewrite that turns it
into a template runs afterward, in S4. Every Class A finding in §D.2 is about the rewritten
template and its relationship to the intent — evidence that does not exist at the point the
extractor's corrective loop runs. Feeding the judge's feedback backward into S3 would mean
re-running extraction to re-derive a plan whose defect is only visible one stage later.

**The human loop is a different actor with a different trust level**, and §C.3 keeps it as the only
write path precisely so the two do not merge.

What is *not* justified is a third **vocabulary** — hence sharing `correction_history` above, and
sharing `BlueprintReviser` below, subject to the caveat in §C.2.1.

### D.4 Auto-discard — the phase D-2 preconditions

Decided (2026-08-28): after `max_rounds`, a still-failing candidate is **discarded**
(`control="drop"`), not routed to a human. **This is the target end state, not phase 1** (§D.0).

This is a deliberate departure from how every other refusal in this plane behaves, and
`judge/judge.py`'s docstring says why it needs compensating controls: *"A wrong KEEP costs one
extraction a human then sees; a wrong DROP is invisible."* With no human queue behind it, the
audit trail is the only thing standing between a systematically-wrong judge and a silently empty
corpus. So the CoverageJudge's discipline is not merely copied, it is tightened — and precondition
0, which the first draft of this document omitted, dominates the rest:

0. **The D-1 measurement exists and says the judge is right often enough** (§D.0). Preconditions
   1–5 make a wrong discard *reconstructable*; none of them makes it *rare*. Shipping 1–5 without
   0 is building an excellent flight recorder and no altimeter.

1. **The audit record is written FIRST, and a failed write converts the discard into a proceed.**
   Verbatim the existing rule (`judge/judge.py`: *"THE DURABLE RECORD IS A PRECONDITION OF THE
   DROP"*). Non-negotiable — it is the whole compensating control.
2. **The record holds every round**, not just the last: each verdict, each feedback string, and
   the payload as of that round, plus session / trace / content hash. A discarded candidate must
   be fully reconstructable, because nobody will ever see it in a queue.
3. **Fail-open everywhere.** Unreachable model, timeout, malformed response, out-of-range
   confidence, invented verdict → `ok`, proceed, exactly as a deployment with no judge. The judge
   may only ever REMOVE work, on positive evidence.
4. **Shadow mode is the default**, and the ship-dark default for the whole stage is `enabled=False`
   (§E). Shadow runs everything, records `would_discard=true`, and forces the discard to False.
   (The rollout gate itself is precondition 0, not this flag — a shadow mode nobody reads is
   indistinguishable from a disabled stage.)
5. **The discard is loud.** A `learning.param_judge` span with the verdict, round count and
   content hash; a WARN log naming the content hash; a counter. An invisible drop plus a quiet log
   is how a broken bar goes unnoticed for a month.
6. **The discard requires a Class A finding** (§D.2). A run of `revise` verdicts carrying only
   Class B naming complaints and Class C over-inlining is not grounds for destroying a candidate
   whose SQL demonstrably answered a real turn — those are reasons a blueprint is *worse*, not
   reasons it is *wrong*.

A confidence bar (`learning_param_judge_discard_confidence`) gates the discard the same way
`post_drop_confidence` gates the coverage drop, and — as in `judge/schema.py` — an out-of-range
confidence is a **rejection, not a clamp**: `5.0` is a broken response, and clamping it to `1.0`
would turn a malfunction into the most confident discard the system can express.

⚠ **Interaction with `blueprint_sampled`.** Some validated blueprints already route to `in_review`
for a human to look at. For that slice, a discard replaces a free human check with a paid model
call and destroys the artifact the human would have judged. Whether the judge should defer
entirely on sampled candidates is a real question, and it is one the D-1 measurement can answer
directly — the sampled population is exactly where judge verdicts and human verdicts can be
compared on the same candidates. **That comparison is the primary output of phase D-1.**

### D.5 The verdict schema

One forced tool, `judge_parameterization`. Guards derived from the read, per house rule:

| Field | Read | Guard |
|---|---|---|
| `verdict` | equality-compared against a closed set; the audit dataset's `GROUP BY` key | member of `("ok", "revise", "reject")` — anything else would become its own bucket in every distribution query |
| `feedback` | handed to `BlueprintReviser` as prompt text; rendered in the audit record | `str`, one line, capped (~600 chars, matching `_MAX_REASON_CHARS`) |
| `confidence` | float-compared against the discard bar; serialized | real number in `[0.0, 1.0]`; never `bool` (an `int` subclass, so `True >= 0.9`); never NaN |
| `findings` | rendered on the card; **the Class A test gates the discard** (§D.4 precondition 6) | list of `{class, criterion, entry_index, note}`, capped. `class` ∈ `("A","B","C")` — a member test, since it gates an irreversible action; an unrecognised class is read as **C** (the weakest), never as A. An out-of-range `entry_index` drops that finding, not the verdict |

`verdict="revise"` with an empty `feedback` is treated as `ok`: a repair round with nothing to
repair against would burn a model call to re-derive the same complaint.

⚠ **`class` is not the model's to choose freely.** Letting a model self-report the severity that
authorizes a discard is the schema equivalent of asking it for permission. Two mitigations, both
cheap: the enum is described to the model in terms of *consequence* rather than rank (Class A is
*"filling this slot with another value answers a different question"*), and the down-cast on an
unrecognised value means every malformed severity fails toward advisory.

### D.6 The card shows the judge

**In D-1 this is not a nicety, it is the measurement's user interface.** A candidate the judge
flagged carries its verdict, findings and `would_discard` marker on the card, so a reviewer looking
at the `in_review` queue is — without doing anything extra — generating exactly the
judge-versus-human comparison §D.4 needs. That overlap is the cheapest way to get phase D-1's
primary output, and it is the argument for building the card marker in D-1 rather than deferring it
with the rest of the UI work.

In D-2 the same block grows a round history — `judged: revise → revise → ok (3 rounds)` — with the
feedback strings collapsed under the same `<details>` treatment as §B.

---

## E. Configuration

Naming mirrors the existing `learning_judge_*` block (`config.py:461`) so the two read as
siblings.

| Setting | Default | Phase | Why that default |
|---|---|---|---|
| `learning_param_judge_enabled` | `False` | D-1 | Ship dark. The stage is not built when off |
| `learning_param_judge_shadow_mode` | `True` | D-1 | In D-1 there is no non-shadow code path to switch to; the setting exists so D-2 does not have to invent it |
| `learning_param_judge_model` | `""` | D-1 | Empty ⇒ reuse the extractor's client, as `learning_judge_model` does |
| `learning_param_judge_timeout_seconds` | match `learning_judge_timeout_seconds` | D-1 | On expiry, fail open |
| `learning_param_judge_max_rounds` | **unset** | D-2 | See below |
| `learning_param_judge_discard_confidence` | **unset** | D-2 | See below |
| `learning_revise_enabled` | `False` | C | The UI edit's own switch. Separate from the judge's: it is human-gated and human-committed, so it can be enabled independently and much earlier |

⚠ **The two D-2 numbers are deliberately left unset, because the first draft's justifications for
them were decoration.** `max_rounds = 2` was anchored to the declined-candidate investigation's
"failed the same fix six times" — evidence about a *different* loop failing a *different* check,
which says nothing about this one. `discard_confidence = 0.85` was justified as sitting "between"
the coverage judge's two bars, which is a position on a number line, not a reason.

Both are outputs of the D-1 measurement, not inputs to it: rounds from the observed
revise-then-pass rate per round, and the confidence bar from the observed agreement rate at each
confidence decile. Picking them now would mean tuning the D-1 data to numbers chosen before it
existed.

⚠ `l2 dev-stack traps` applies twice over here: `LearningSettings` is `extra="ignore"`, so a typo'd
env var is swallowed silently, **and** a setting left unset reads as its default rather than as an
error. Every new setting needs a startup log line stating its effective value, and the D-2 pair
must fail loudly if the discard path is reached with them unset.

---

## F. Build order

```
B (card) ──────────────────────────────────────→ ship first, alone, no backend change
C.2 (revise engine) → C.3 (endpoint + UI) ──┐
D-1 (judge, shadow, no discard) ────────────┴──→ [MEASURE] ──→ D-2 (loop + discard)
```

**§B is genuinely independent and should land alone.** It needs no model, no new endpoint and no
config, and it makes everything after it reviewable — the judge's value proposition is legible only
on a card that already separates slots from frozen filters (§B.2). Per §D.2's closing note, §B is
also the *actual* fix for the over-inlining complaint that motivated §D.

**D-1 does not depend on §C.** A judge that only observes needs no reviser; the dependency appears
in D-2, where the repair loop calls one. Running D-1 and §C in parallel is the fastest honest
ordering, and it means the measurement starts accumulating while the edit surface is built.

⚠ **The gate between D-1 and D-2 is a human reading records, not a passing test suite.** It is the
one step in this plan that cannot be automated, and the one most likely to be skipped because the
code for D-2 will look ready.

---

## G. Tests

| Deliverable | Test | Where |
|---|---|---|
| B | Blueprint card renders every section from the S4 fixture | `tests/ui/test_inbox_markup.py` |
| B | A `fail_to_review` payload (no `sql_template`, no `uses`) renders without throwing | same |
| B | Unknown candidate type falls back to the raw dump | same |
| B | Consumed-keys accounting: an unexpected payload key surfaces in "other fields" | same |
| B | `template_parts` tokenization matches `SLOT_TOKEN` over the fixture corpus | `tests/learning/inbox/` |
| B | **`template_parts` on a candidate with an entity-bearing inline literal contains `[redacted]`, not the literal** | same — the D17 test §B.3 exists for |
| C | Response carrying `sql_template` is **rejected**, not ignored | `tests/learning/revise/` |
| C | `replace` defaults to append on a missing/non-bool value | same |
| C | `/revise` writes nothing — store untouched on success and on model failure | same |
| C | Proposed entries that fail the totality walk come back as `declined`, unchanged flow | `tests/learning/inbox/test_completion*.py` |
| C | `revise` is in the BFF allowlist and forwarded verbatim; oversize body → 413 | `tests/ui/test_inbox_proxy.py` |
| D-1 | Judge never runs on a `fail_to_review` candidate | `tests/learning/judge/` |
| D-1 | Every failure mode (timeout, malformed, bad verdict, NaN/out-of-range confidence) → proceed | same |
| D-1 | An unrecognised finding `class` is read as **C**, never as A | same |
| D-1 | **No code path in D-1 can return `control="drop"`** — asserted over the stage's whole verdict space, not just the shadow flag | same. §D.0's "do not implement a discard path" is only real if a test says so |
| D-1 | Stage order in `build_write_router_stages`, both callers | `tests/learning/test_factory*.py` |
| D-2 | **A failed audit write converts the discard into a proceed** | `tests/learning/judge/` — the load-bearing one |
| D-2 | A discard with no Class A finding does not happen, at any confidence | same (§D.4 precondition 6) |
| D-2 | **A revision that fails the static checks REVERTS to the pre-revision envelope** and consumes a round | same (§D.3). The property is *the loop cannot make a candidate worse* |
| D-2 | `max_rounds` honoured; `correction_history` holds every round | same |
| D-2 | Shadow mode records `would_discard` and never drops | same |

The existing `tests/learning/generalize/test_frozen_date_literal.py` (currently untracked in the
working tree) covers the deterministic date check and needs no change — it is the proof §A's
"don't give it to the judge" ruling rests on.

---

## H. Rejected alternatives

1. **Let the model rewrite `sql_template` directly.** Destroys the provenance chain the five
   static checks validate (§C.1). The checks would still pass — against a different subject.
2. **Give the frozen-date rule to the judge.** Already deterministic, already routed, already
   tagged (§A). Two enforcers for one rule disagree eventually.
3. **A judge that proposes new slots for predicates the accepted SQL never had.** Structurally
   impossible without generating SQL (§D.2). The honest version of that feature is a *new*
   blueprint from a new session, not a repair of this one.
4. **Apply the LLM revision directly instead of proposing it.** Would make the model a writer to
   the candidate store and put a second write path beside `complete`. The proposal/apply split
   costs one round trip and keeps `to_candidate` the single gate (§C.3).
5. **Show the model the redacted `payload_view`.** The literals are the subject; a model reasoning
   about `'[redacted]'` cannot re-role anything (§C.2).
6. **Discard without a durable record.** Rejected even though auto-discard itself was chosen — a
   drop nobody can reconstruct and nobody will see is unauditable by construction (§D.4).
7. **A generic JSON tree-viewer widget instead of typed renderers.** Prettier JSON is still JSON;
   it would not put inline predicates next to slots, which is the comparison the reviewer is
   actually there to make (§B.2).
8. **Shipping the discard behind a flag in phase D-1** (§D.0). A dormant destructive branch guarded
   by a default-off boolean is one config change away from live, and the flag will look validated
   because the surrounding code is tested. D-1 contains no discard path at all.
9. **Deriving `template_parts` from `env.payload` because "a template is mostly slots".** It is
   not: every `role="inline"` predicate keeps its literal (§B.3). This was in the first draft of
   this document and is the reason the D17 note is now a ⚠ and a test rather than a comment.
10. **Making the judge's repair direction `inline → slot`**, the obvious reading of the original
    requirement. It optimizes for reuse at the cost of correctness, in a system where the
    over-slotted failure produces confidently wrong answers and the over-inlined one produces a
    recall miss (§D.2, ceiling 2).
11. **Feeding the judge's critique back into the extractor's existing corrective loop** instead of
    adding a third repair loop. The extractor never sees the rewritten template, so the evidence
    every Class A finding rests on does not exist at that point (§D.3.1).

---

## I. Open questions

1. **Does `revise` need its own rate limit?** `/complete` is human-paced and free; `/revise` costs
   a model call per click, and nothing on the current inbox routes is rate-limited. A per-reviewer
   token bucket in the inbox service is the obvious answer; not designed here.
2. **Should the judge's `findings` feed the card's inline-predicate section directly** (marking the
   specific entry it objected to), or stay a flat list? The former is better UX and couples the
   card's DOM to the judge's schema. Leaning flat list for the first cut.
3. **Snapshotting `ValidationSnapshot` on every candidate** (§C.4) — the unlock for editing
   `in_review` blueprints. Needs its own decision on store size and the entity-bearing cost.
4. **What happens to a candidate discarded in shadow mode that a human later approves?** Today:
   nothing, correctly — shadow does not act. But the record exists, and reconciling "the judge
   would have discarded this; a human approved it" is the tuning signal for the bar, and nothing
   currently reads it. **This is phase D-1's primary output** (§D.4) and needs a reader built, not
   just records written — a measurement nobody has tooling to read is a measurement that will not
   happen.
5. **Should the judge defer entirely on `blueprint_sampled` candidates?** They route to a human
   anyway, so a discard there replaces a free check with a paid one (§D.4). Answerable from D-1.
6. **What is the actual flag rate?** Everything downstream of §D.0 is contingent on it. If the
   judge flags 2% of a population of ten candidates a week, the correct decision is to keep §B and
   §C and delete §D — and that outcome should be treated as a success of the measurement, not a
   failure of the design.
