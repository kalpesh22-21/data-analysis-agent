# Knowledge review — assistant-edited knowledge, and a user fact promoted to global by a button

**Status:** DESIGNED 2026-09-01, building on `learning/blueprint-review-rework-design.md` (§C).
Two deliverables over the review inbox, both about the `global_knowledge` target, which until now
a reviewer could only approve or reject as-emitted.

| | what it adds | where |
|---|---|---|
| **K1** | edit a `global_knowledge` candidate under review, with or without the assistant | `learning/revise/knowledge.py`, `learning/inbox/knowledge_edit.py`, two inbox routes, the card |
| **K2** | list one user's private facts in the inbox and **promote** one to a `global_knowledge` candidate by one button | `ReviewInbox.promote_user_knowledge`, two inbox routes, the "User Knowledge" tab |

---

## A. What is already here, and the two gaps

* A `global_knowledge` candidate ALWAYS routes to `in_review` (D58a, `writer/routing.py`), lands in
  neo4j as `source=learning` on approve, and reaches the MCP canon through `verify` → `promote`
  (`promotion/mcp_export.py::_knowledge_doc`). Its payload is a CLOSED key set — exactly the five
  surfaces the S5 leakage gate scans (`leakage/gate.py::_ENTITY_FREE_SURFACES["global_knowledge"]`,
  pinned to `validation.py::_GLOBAL_KNOWLEDGE_KEYS` by a test).
* `user_knowledge` auto-commits into the per-user store (`learning/user/`, D17) and is DROPPED from
  the candidate store by `UserKnowledgeCommitStage`, so the inbox's "User Knowledge" tab has been
  empty since it was drawn. Nothing at runtime reads the per-user store either.
* `revise` / `complete` / `apply_revision` are blueprint-only: the completer is a
  `ParameterizationCompleter`, the reviser proposes parameterization entries. A knowledge candidate
  with a wrong statement has exactly one action — `reject` — which is the dead end §C.5 of the
  rework doc was written to remove for blueprints.

**Gap 1 (K1):** no write path into a knowledge candidate's payload.
**Gap 2 (K2):** no way to lift a fact a user taught the agent into the shared corpus.

---

## B. The invariant both deliverables keep

**There is ONE write path into a knowledge candidate's payload, and it re-adjudicates.**

`inbox/knowledge_edit.py::KnowledgeEditor.admit(env)` is that path. It is used by K1 (apply an
edit) and K2 (admit a promoted user fact) and does, in order:

1. **Intake validation** with the SAME reader the extractor uses at intake
   (`extractor/validation.py::_global_knowledge_payload`, exposed through a public
   `validate_payload(candidate_type, raw) -> Decline | None`). A payload that the extractor would
   have declined is refused here with the decline's sentence as the 422 detail. This is what makes
   the closed key set hold on the human path too: `definition`, `intent`, `user_id` — anything
   outside the five surfaces — is a 422 naming the key.
2. **Leakage scan, scan-only** — `leakage/gate.py::settle_entity_scan(stages, env, ctx)`, the
   helper the declined-candidate and completion paths already share. The verdict is STAMPED on the
   candidate; the gate's consequences (`_apply`) are NOT run. See §B.1 for why.
3. **Status `in_review`**, `route_reason` = `knowledge_edited` (K1) or `promoted_from_user` (K2).
   `route_candidate` would say `in_review` for this type regardless, so nothing is bypassed.
4. **Guarded put** (`ParameterizationCompleter._guarded_put`'s rule, lifted into a shared helper):
   the row must still be at the status it was read at, or nothing is written.

The stamped scan is what the existing guards then read. ⚠ **CORRECTED after building — the first
draft of this section described a guard this system does not have.** What is actually true
(`promotion/scheduler.py::_entity_scan_is_actionable`, pinned by two tests in
`tests/learning/inbox/test_user_knowledge_promote.py`):

```python
if not LeakageVerdict.is_settled(scan): return False   # unsettled ⇒ never approvable
if scan.get("result") == "pass":        return True
return bool(entity_spans(env))                          # a LOCALIZED finding IS approvable
```

So a settled `reject`/`quarantine` **whose hits carry spans can be approved by a human**, and
`strip_entity_bearing` removes those spans on the way to `validated`. The predicate is derived
from the OPERATION, not from the verdict word: the strip and the landing tripwire both consume
`entity_spans(env)`, so an empty span set disables both layers — which is why an unsettled scan
and a settled finding that localizes nothing are refused together, and a localized one is not.
`attest_scan` does **not** feed this guard at all; it clears `inbox/models.py::_leakage_cleared`,
which governs the card's withheld text and the blueprint assistant.

What therefore holds for K1/K2 is narrower than "cannot be approved", and it is still enough: an
unscanned or unlocalized promotion cannot be approved at all, a localized one has its entities
stripped before landing, and the card withholds the flagged text until a human attests. Nothing
here adds an approval shortcut, and no guard was changed to make this slice work.

### B.1 Why scan-only rather than the full gate

The mined path runs the full `LeakageGateStage.process`, whose `_decide` says a hard entity in a
`global_knowledge` payload is a TERMINAL `reject` (`_HARD_REJECT_TYPES`), and whose `reroute`
commits a per-user fact scoped to the session user. Both are right for an unattended pipeline and
wrong for these two paths:

* K2's whole input is an entity-bearing user fact. Running the full gate would reject every
  promotion on arrival — the button would only ever produce an archive row.
* K1's editor is a reviewer, not the session user. A `reroute` would commit a fact into the
  ORIGINAL user's private store on a reviewer's keystroke, with nothing to retract it.
* On both paths a human is already holding the candidate. The fail-to-review decision
  (`learning-declined-candidate-review.md`) made this exact call for the parameterization form:
  when a person is in the loop, "hold with the verdict visible" beats "discard".

So: the verdict is settled and stamped (a `reject` verdict is stamped as `reject`), the row stays
`in_review`, and `approve` stays refused until the text is clean. The terminal reject is replaced
by a hold that only a human can clear — the same shape, one gate later.

---

## C. K1 — editing a knowledge candidate

### C.1 The reviser (`learning/revise/knowledge.py::KnowledgeReviser`)

Mirrors `BlueprintReviser` mechanically — one forced-tool model turn, `asyncio.timeout`,
fail-soft to a proposal with a `reason`, a span — and differs in what it proposes.

* **Tool** `propose_knowledge`, properties = EXACTLY the five surfaces (`statement` required,
  `knowledge_type`, `related_terms: [str]`, `structured: {str: str}`, `scope`) plus `rationale`.
  A test pins the tool's property names to `_ENTITY_FREE_SURFACES["global_knowledge"]` so a
  surface added to the gate is added to the tool, and vice versa.
* **Closed set at parse time.** Any other key at any depth → `ForbiddenKnowledgeEditError`
  (subclass of `ValueError`, surfaced as 422 verbatim, like `ForbiddenTemplateEditError`). The
  reviewer learns the system has no such field; the assistant does not get a second try at it.
* **Brief** = the current five fields, the settled scan's hits as `field (kind): span` — the
  assistant has to SEE the entity to remove it, and this text stays server-side — plus the
  reviewer's `feedback` (capped at `MAX_FEEDBACK_CHARS`), the entity-free rule stated once, and
  the same "prefer the smallest change" instruction the blueprint prompt uses.
* **The proposal is scanned before it leaves.** The reviser runs the gate's `scan` on a throwaway
  envelope carrying the proposed payload. If the verdict is not `pass`, the proposal is NOT
  returned: the 200 carries `reason` naming `field (kind)` — never the span — and empty fields.
  This is the withholding rule the card obeys (`_leakage_cleared`), applied to the one surface
  that could otherwise hand the browser fresh entity-bearing text. It also means the
  `propose_revision` refusal ("scan not cleared ⇒ no assistant") does NOT apply here — the
  assistant is offered precisely BECAUSE the scan did not clear; what is refused is a draft that
  still leaks.
* **Diff.** Per-field before/after rows (`field, before, after, kind ∈ {unchanged, changed, added,
  removed}`) so the card can render what changed. `before` is REDACTED (`[withheld]`) for any
  field the current scan flagged — same rule as the payload view.

Wire shape of the proposal (`KnowledgeProposal.to_wire`):

```json
{"payload": {"statement": "...", "knowledge_type": "...", "related_terms": [], "structured": {}, "scope": "..."},
 "rationale": "...", "reason": "", "diff": [{"field": "statement", "kind": "changed", "before": "[withheld]", "after": "..."}]}
```

Empty `payload` + non-empty `reason` = "the assistant had no suggestion", a 200.

### C.2 Routes

| route | body | does |
|---|---|---|
| `POST /inbox/{id}/revise_knowledge` | `{feedback: str}` | K1 proposal. WRITES NOTHING. 404 unknown, 409 not `in_review` or not `global_knowledge`, 422 forbidden key, 503 no reviser. |
| `POST /inbox/{id}/apply_knowledge` | `{payload: {…five surfaces…}}` | `KnowledgeEditor.admit`. 404 / 409 as above, 422 intake decline (its sentence), 409 race, 503 no editor (no write plane). Returns `_completion_result`-shaped `{candidate_id, status, outcome: "edited", entity_scan: {result, hits: [{field, kind}]}}`. |

`apply_knowledge` is the ONLY write; the reviewer may send the assistant's payload verbatim, a
hand-edited one, or one typed from scratch. `revise_knowledge` shares `LEARNING_REVISE_ENABLED`,
the model and the API key with the blueprint reviser (`service.py::_build_reviser` builds both, or
neither).

The edit is recorded ADDITIVELY on the envelope, NOT in the payload (the payload's key set is
closed): `env.route_reason = "knowledge_edited"` and a new envelope field
`knowledge_edit: {applied_at, previous_statement_sha256, edits: int}` — a digest, never the
previous text, for the same reason `sql_rewrite` carries a digest.

### C.3 The card

The `global_knowledge` card on the `in_review` queue gains, under the payload sections:

* an **edit form** — the five fields (`statement` textarea; `knowledge_type`, `scope` inputs;
  `related_terms` one per line; `structured` as key/value rows) prefilled from `payload_view`
  (redacted fields prefilled EMPTY, with a note saying why), **Apply** → `apply_knowledge`;
* an **assistant block** — feedback textarea, **Ask the assistant** → `revise_knowledge`; the
  proposal renders its diff rows and its rationale, and **Use this draft** fills the form (it does
  not apply — the reviewer still presses Apply, the two-step §C.3 of the rework doc);
* a `reason` from either route renders inline in words, never as an error banner.

Testids: `inbox-kn-form`, `inbox-kn-field-<name>`, `inbox-kn-apply`, `inbox-kn-revise`,
`inbox-kn-feedback`, `inbox-kn-proposal`, `inbox-kn-diff`, `inbox-kn-use-draft`,
`inbox-kn-reason`. The existing `inbox-payload` / `genericSections` rendering is unchanged.

Only `in_review` shows the form. `validated` knowledge is already a neo4j node; editing it means
re-landing, which is a different slice (§G).

---

## D. K2 — a user fact, promoted

### D.1 The read surface, and the D17 amendment it is

`GET /inbox/user_knowledge?user_id=<id>&limit=100` → `UserKnowledgeStore.list_for_user`. The
store's per-user read is the ONLY read used; no cross-user listing is added to the Protocol. The
reviewer names the user. The `user_id` is required — an empty one is a 400, never "all users".

This IS a change to D17's "surfaced only in that user's context": the reviewer role now sees a
named user's private facts, on the same privileged, token-guarded surface that already shows
audit quotes, trial-run rows and unredacted SQL. Recorded here as the deliberate exception. The
records are returned as-is (they are entity-bearing by contract) and are never indexed, scored,
or written back by this route.

Each record on the wire: `record_id, user_id, statement, fact_type, scope, structured,
committed_at, provenance{source_session, source_trace}`, plus `promotion: {candidate_id, status} |
null` — whether this record already has a promoted candidate, read from the candidate store by
the deterministic id below.

### D.2 The button

`POST /inbox/user_knowledge/promote` body `{user_id, record_id}` → `ReviewInbox.promote_user_knowledge`.

1. `store.get(record_id)`; 404 if absent **or if `record.user_id != user_id`** — a record id is
   guessable (`userknow::<user>::<candidate>`), and the user id in the body is what the reviewer
   was looking at; the two must agree.
2. Candidate id is DETERMINISTIC: `candidate::userpromote::<sha256(record_id)[:32]>`, minted by
   `candidate/models.py::mint_promoted_candidate_id`. A second press finds the existing row and
   returns it (200, `already: true`) — no duplicate, no status move, whatever status it is at.
3. Build the `global_knowledge` envelope from the record:
   * `payload` = `{statement: record.statement, knowledge_type: record.fact_type or
     "user_fact", structured: record.structured if it is an object else omitted}` (⚠ CORRECTED:
     an earlier draft said "an object of strings" and no such check exists — see §F.1.b).
     `scope` is NOT carried: on a user record it is the literal `"user"`, and on a knowledge chunk
     it becomes the node title (`knowledge_seed_from_candidate`).
   * `source_session`/`source_trace`/`evidence_refs` from the record's provenance — the
     evidence trail survives the hop.
   * `content_hash = "userpromote::" + record_id` — its own namespace, so a session re-extraction's
     `supersede(content_hash)` can never sweep it.
   * `extractor_rationale = "promoted from user knowledge by a reviewer"`, `proposed_action =
     "promote_user_knowledge"`, `confidence = 1.0` (a human chose it), `depends_on = ()`.
   * `revalidation` = a minimal `ValidationSnapshot` whose summary carries `user_id`,
     `session_id`, `content_hash` and nothing else, so `StageContext.summary.user_id` is the
     OWNER on every later re-run (the field is entity-free and never projected to the wire).
     ⚠ **CORRECTED after building:** this snapshot is IN-PROCESS ONLY. `ValidationSnapshot.
     from_doc` returns `None` unless `sql_by_ref` and `evidence` are both non-empty — a
     derived-from-the-reads guard — and a promoted candidate has neither, so it rehydrates as
     `None` from Couchbase (it survives `InMemoryCandidateStore`, which holds objects). The
     guard was NOT weakened. Nothing reads the field today: the editor needs it only to build
     `StageContext`, and `settle_entity_scan` ignores `ctx`. A future stage that depends on the
     owner id needs a third legal shape in `from_doc`, not a caller that fakes one.
4. `KnowledgeEditor.admit(env)` — §B. The scan will usually NOT pass (the fact was rerouted to
   the user store because it carried an entity); the row lands `in_review` with the verdict
   stamped, the card withholds the flagged text, and K1's assistant is the next click.

Response: `{candidate_id, status, already: bool, entity_scan: {result, hits: [{field, kind}]}}`.

### D.3 The tab

"User Knowledge" stops being a filter over the candidate list (it never matched anything) and
becomes its own view: a `user_id` input + **Load**, then one card per record — statement,
fact type, scope, structured, committed_at, session — with **Promote to global knowledge**. After
a promotion the card shows `in review as <candidate_id>` and the button is disabled; a record
whose `promotion` is non-null loads that way. The Global Knowledge tab then shows the candidate.

Testids: `inbox-uk-user-id`, `inbox-uk-load`, `inbox-uk-list`, `inbox-uk-card`,
`inbox-uk-promote`, `inbox-uk-promoted`.

### D.4 Route placement — a shadowing trap, named

`/inbox/{candidate_id}/promote` already exists. `POST /inbox/user_knowledge/promote` MUST be
declared BEFORE the parametrised routes in both the inbox service and the BFF (the service already
does this for `/inbox/mint/*`), and a test in each place posts to the new path and asserts the
candidate route did not answer (a candidate named `user_knowledge` must not be looked up).

---

## E. Wiring

* `ReviewInbox(..., knowledge_editor=None, knowledge_reviser=None, user_store=None)` — three new
  optional collaborators, absent by default, each refusing loudly on use (503) rather than
  silently doing less.
* `KnowledgeEditor(store, stages)` is built beside the completer from the SAME store and the SAME
  stage tuple (`_build_completer` already assembles both); no completer ⇒ no editor.
* `_build_reviser` returns both revisers or neither. Same switch, model, key, timeout, tracer.
* The inbox process needs a `UserKnowledgeStore`: the Couchbase one from `user/config.py::
  UserKnowledgeStoreConfig` when configured (the consumer already wires it this way in
  `factory.py::build_learning_consumer`), else `InMemoryUserKnowledgeStore` (offline: empty
  listings, promote 404s). The offline posture is logged.
* BFF (`ui/server.py`): `revise_knowledge` and `apply_knowledge` join `_INBOX_ACTIONS`,
  `_INBOX_BODY_ACTIONS`, and `revise_knowledge` joins the model-action set the existing invariant
  test covers. Two new proxies: `GET /api/inbox/user_knowledge?user_id=` (validated non-empty,
  forwarded as a query param) and `POST /api/inbox/user_knowledge/promote` (bounded JSON body),
  both declared before `/api/inbox/{candidate_id}/{action}`.

No new settings. No change to the extractor, the gate, the router, landing, or promotion.

---

## F. Tests

Layer-1, no infra.

* **Reviser:** tool properties == gate surfaces (pin); forbidden key ⇒ `ForbiddenKnowledgeEditError`;
  a draft that still scans dirty is withheld (reason names field+kind, payload empty, span absent
  from the wire); timeout/exception/unusable ⇒ reason; diff rows redact flagged `before`.
* **Editor:** intake decline ⇒ 422 with the reader's sentence; extra key ⇒ 422 naming it; scan is
  settled and stamped, status `in_review`, route_reason set, `knowledge_edit` digest recorded,
  previous text absent from the doc; race ⇒ 409, nothing written; no stages wired ⇒ `pending`
  sentinel stamped (approve then refuses).
* **Promote:** deterministic id, second press ⇒ `already`; foreign `user_id` ⇒ 404; record fields
  land where §D.2 says; `scope` not carried; `content_hash` namespace; owner `user_id` on the
  snapshot; the promoted candidate lists under `global_knowledge` `in_review`.
* **Guards that must still hold** (⚠ corrected per §B): `approve` refused on a promoted candidate
  whose scan is UNSETTLED, and on a settled finding that localizes no span; a settled finding WITH
  spans stays approvable and is stripped at landing — the pre-existing asymmetry, pinned by test
  rather than changed. `_leakage_cleared` withholds the card text until `attest_scan`.
* **Routes:** shadowing test (§D.4) in service and BFF; BFF allowlist invariant; `user_id`
  required.
* **UI markup:** the new testids exist; the form posts a JSON body to `apply_knowledge`; the tab
  fetches `/api/inbox/user_knowledge?user_id=`; no HTML sink (existing test covers the file).

---

## F.1 Decisions forced by the review (2026-09-02)

**F.1.a — an unscanned K2 promote is REFUSED, not degraded.** The review found the posture that
breaks §B's promise: a deployment with real `USER_KNOWLEDGE_*` credentials but an unreadable
catalog builds no completer, hence a `KnowledgeEditor` with NO stages, hence a `pending` scan on a
promoted fact — which `inbox/models.py::_leakage_view` renders to the card as `pass`. The row is
still unapprovable, but a raw per-user fact is listed on the shared queue under a green verdict.

K1 and K2 differ here and the difference decides it. An unscanned K1 edit is degraded but honest —
the reviewer typed the text and can see what they typed. K2's input is entity-bearing BY
CONSTRUCTION: the fact went to the per-user store precisely because it named someone. So
`promote_user_knowledge` refuses with 503 when its editor cannot scan, and — belt as well as
braces — the listing withholds any `knowledge_edited`/`promoted_from_user` row whose scan is
unsettled, so a row stored that way by any future path still fails closed.

**F.1.b — `structured` stays permissive at intake and strict in the reviser, and the two are
different populations.** The review is right that they disagree: `validate_payload` accepts a
nested `structured`, while the reviser refuses one at any depth. That is not drift to be flattened
in either direction. The reviser governs what a MODEL may author into a payload; intake governs
what an existing candidate may CARRY, including one the mined path produced. Tightening intake
would decline candidates the loop handles correctly today — the gate scans string leaves at any
depth (`_collect_text`) and `knowledge_seed_from_candidate` lands only those scanned leaves, so a
nested dict of strings is both scanned and landed, and a numeric leaf is neither. What was
genuinely wrong is the PROSE: §D.2's "object of strings" and two docstrings claimed a check that
does not exist. Those are corrected; the code is not.

**F.1.c — a rejected promotion stays a dead end, and that is the point.** A second press on a
record whose candidate a colleague rejected returns `already: true, status: "rejected"`, with the
button disabled. Making it re-promotable would hand any reviewer a one-click way to re-open work
another reviewer deliberately closed (D29's rule, from the other direction). The cost is real and
is now named rather than discovered: that user fact cannot be proposed globally again through this
surface. The tab says so in words instead of showing the same "in review as …" phrasing a live
promotion gets.

## G. Deferred, named

1. Editing a `validated` (landed) knowledge node — needs re-landing through `landing.land` and a
   verify reset; not this slice.
2. A "my knowledge" panel in the CHAT UI so a user can propose their own fact without a reviewer
   naming their id — the D17-clean alternative to §D.1. Same backend (`promote_user_knowledge`
   with the session user), different page.
3. Editing `user_knowledge` records themselves (the private fact, in place).
4. Runtime recall of the per-user store — still unread by the agent; unrelated to this slice but
   the reason "promote to global" is currently the only way a user fact affects an answer.
5. **A live end-to-end Load/Promote test.** `tests/e2e/_seeded_inbox_app.py` builds its inbox with
   no user store and no knowledge editor, so the listing route 503s there. The e2e file's updated
   assertions cover the panel's empty state only, and they are UNVERIFIED — nobody has run them
   under `RUN_E2E=1`. Seeding that app with an in-memory user store and an editor is the unlock.
6. **A payload key can be permitted by intake and not scanned by the gate.** `as_text` coerces a
   number, so `scope: 7` passes `validate_payload`, which only ANSWERS — it never returns a
   normalized payload, and `admit` stores the dict verbatim. `gate._collect_text` walks string
   leaves only, so that surface never appears in `scanned_fields`. Harmless today (no entity kind
   can be spelled as a bare number, and `knowledge_seed_from_candidate` lands only scanned string
   leaves) and pre-existing on the extraction path, so it is NOT a defect of this slice — but it
   makes "every key intake permits is a surface this gate scans" one step weaker than it reads.
   The honest fix is a validator that returns the coerced payload rather than a verdict about it.
   Pinned by a test so it cannot quietly get worse.
7. **`promotion` is a both-or-neither pair, and nothing pins it.** The card renders its promotion
   block only when `candidate_id` is truthy, so a `promotion` object carrying a `status` and no id
   would render no text AND leave the Promote button live — a second press on a fact that already
   has a candidate. Not reachable today: `list_user_knowledge` builds the two together or emits
   `null`. But it is a coupling across the service/page boundary that no test on either side
   holds, and the failure is silent in the direction that writes.
8. **The reviewer token is the only authorization on the per-user read.** The BFF holds
   `REVIEWER_TOKEN` server-side and applies no per-request auth to `/api/inbox/*` — a pre-existing
   posture, but §D.1 widens what it exposes to a named user's raw facts. Per-reviewer identity on
   this surface, and an audit line naming which reviewer read which user, are both unbuilt.
