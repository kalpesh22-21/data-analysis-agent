# Rebasing onto the review-inbox / minting branch

You forked before `learning/blueprint-review-rework` and now have to reconcile. This is what
changed, what will break loudly, what will change **silently**, and how to resolve the conflicts
you will actually hit.

Five commits, base `8c1b4cb`:

| commit | what it changes |
|---|---|
| `ed9a1c2` | S4: frozen date literals + slot-name identifier guard |
| `7066282` | the review card, the LLM assistant, the parameterization judge, tracing |
| `1918dd5` | leakage attestation + re-validation snapshots |
| `dc76811` | queue pager + trial run |
| `dda8d75` | minting, reviewer-supplied tokens, table intermediates |

---

## Read this first: three silent behaviour changes

These do **not** fail loudly. If your fork touches the same areas, they will change what your
code does without changing whether it compiles.

### 1. `{name}` inside a string literal is no longer a slot

`SLOT_TOKEN` is a raw-text regex and used to fire **anywhere** — including inside quoted text,
where the braces are data. `WHERE note LIKE '%{cfg}%'` was rewritten to `'%:cfg%'`: a different
string constant, carried into the template, with the walk and the entries agreeing about the
corrupted value so nothing downstream could notice.

Slot tokens are now scoped out of strings and comments (`_SKIP_OR_SLOT` in
`runtime/blueprint/template.py`). This affects **every** reader:

```python
referenced_slots(sql)        # ← fewer names now, if any lived in a string
parse_template(sql)          # ← no longer corrupts string constants
bind_template(sql, ...)      # ← will not demand a value for a quoted "{token}"
```

**If your fork copied `SLOT_TOKEN` and did its own `.sub`/`.findall`, you have the old bug.**
Switch to `sub_slot_tokens` / `slot_tokens_outside_strings` / `iter_slot_tokens`, all now public
from `template.py`.

### 2. A hand-authored candidate never auto-lands

`writer/routing.py::route_candidate` gained a branch: any candidate whose
`revalidation.authored` is True routes to `in_review` / `hand_authored` instead of the auto-land
path. Precedence is unchanged — it sits **below** every defect reason, so `fail_to_review`,
`dedup_conflict`, `leakage_near_miss` and an unsettled scan all still win.

Mined candidates are unaffected: `authored` defaults False on every snapshot built
`from_summary` or rehydrated `from_doc`.

### 3. Scratch no longer moves the inferred default database

`_infer_default_db` skips the `scratch` db. Registering a scratch schema used to push the key
count to two, collapsing the inference to the hardcoded `dbpcm_warehouse` — so one node's own
registration changed how its **sibling** nodes' bare table names qualified. Only bites on a
single-database catalog that is not `dbpcm_warehouse`.

---

## Breaking changes — these fail loudly

### `ReviewInbox.approve` now requires a token

```python
# before
await inbox.approve(candidate_id)
# after
await inbox.approve(candidate_id, token=reviewer_pasted_token)
```

Approving runs the golden replay — a real warehouse query — and this plane no longer mints
authority for anything a human triggers. Blank or whitespace raises
`InboxTransitionError("approve_needs_token: …")`. **There is no fallback**, deliberately: both
paths return the same shape, so a substitution would let a reviewer believe they had proven
something about their own access.

Over HTTP, `POST /inbox/{id}/approve` now takes a body `{"token": "…"}` and `approve` is in the
BFF's `_INBOX_BODY_ACTIONS`.

**Fixing your tests:** most call sites just need a token. If your inbox has no MCP transport
(offline/test wiring), `_probe_for` falls back to the wired probe with a WARNING — so a token
string of any value works. To assert against a specific probe, inject `probe_factory`.

### `trial_run` takes a token, not a tenant

The tenant-selector allowlist is **gone**. Removed entirely:

| removed | replacement |
|---|---|
| `learning_trial_tenants` (settings) | — |
| `ReviewInbox.trial_tenants()` | — |
| `GET /inbox/trial_tenants`, `/api/inbox/trial_tenants` | — |
| `TrialRunResult.tenant` | — |
| `trial_run(..., tenant="LABEL")` | `trial_run(..., token="<pasted JWT>")` |

New refusal reasons: `no_token` (nothing pasted). The old `unknown_tenant` is gone.

### Changed signatures

```python
extract_column_provenance(sql, catalog_schema, *, session_id=None,
                          declared_scratch=frozenset())          # new kwarg
PromotionScheduler.apply_human_decision(env, decision, *, probe=None)
ReviewInbox(store, *, ..., minter=None, mcp_client=None, probe_factory=None)
build_promotion_write_plane(..., minter=None, mcp_client=..., probe_factory=None)
builder._provenance_uses(templates, catalog_schema, *, per_node=None)
```

All additive with defaults — existing positional calls keep working.

### Removed, no replacement

`ReviewInbox.minting_available`, `MintResult.needs_work`, `MintRequest.submitted_by` — all had
zero readers.

---

## New public API you should use instead of copying

```python
# runtime/blueprint/template.py
SCRATCH_DB                      # was three private copies; use this one
sub_slot_tokens(sql, repl)      # string/comment-aware substitution
slot_tokens_outside_strings(sql)
iter_slot_tokens(sql)           # (name, start, end) — for slicing a template

# learning/revise/schema.py   (shared with learning/mint)
ENTRIES_SCHEMA                  # the parameterization-entry JSON Schema
coerce_entries(raw)             # normalization: unquotes locators, drops nameless slots
forbidden_keys_anywhere(value)  # the template-edit sweep
```

`coerce_entries` in particular: it encodes two live-model failures (a `locator.value` copied
**with** its SQL quotes, and a placeholder slot `{"name":"","type":""}`). A second implementation
re-learns both, and only in whichever caller a live model happens to hit first.

---

## New data field

`ValidationSnapshot.authored: bool = False` — a **third** provenance state beside
`reconstructed`. They make opposite claims about the same walk:

- `reconstructed=True` → the SQL was rebuilt **from the candidate's own entries**, so the first
  totality walk is circular and proves nothing.
- `authored=True` → the SQL came from **outside** the entries (a person typed it), so the walk is
  the strongest check that candidate ever faces.

Never coalesce them. `from_doc` reads both with `False` defaults, so old documents rehydrate
unchanged.

---

## The conflict surface, ranked

| file | lines | how to resolve |
|---|---|---|
| `ui/static/inbox.html` | +2026 | **take ours, re-apply yours by hand.** Almost everything is new render functions. Two invariants: no `innerHTML`/`outerHTML`/`insertAdjacentHTML`/`document.write`/`eval` anywhere (a test pins it), and the queue is a **pager** now, so only one card is visible — code that assumed all rows are in the DOM needs rethinking. |
| `learning/inbox/inbox.py` | +600 | new verbs (`propose_revision`, `apply_revision`, `attest_scan`, `trial_run`, `mint_*`) plus the `approve` signature change. Mostly additive. |
| `learning/inbox/service.py` | +582 | new routes and request models. If you added routes, watch ordering: the mint routes are declared **before** `POST /inbox/{candidate_id}/{action}` because `mint/schema` reads as `candidate_id="mint"`. |
| `learning/revise/schema.py` | +401 | if you copied the entry schema, delete your copy and import `ENTRIES_SCHEMA`. |
| `runtime/blueprint/executor.py` | +373 | ⚠ table-intermediate pause/resume durability. **Not authored or reviewed by the rest of this branch** — flagged in `dda8d75`'s message. Treat it as unreviewed. |
| `sqlparse/provenance.py` | — | small but security-sensitive. See below. |

Whole new packages — no conflicts, just new imports: `learning/mint/`, `learning/paramjudge/`,
`learning/revise/`, `learning/generalize/reconstruct.py`.

---

## `provenance.py` — read before you touch it

Two changes, both narrow, both fail-closed by default.

**`declared_scratch`** exempts a *named* `scratch.<name>` from the D64 session-ownership check.
The check asks "does this caller own the table they are READING"; a blueprint template's
`scratch.emp_earnings` names an intermediate the blueprint produces itself, so no session owns it
and nothing is materialized until the DAG runs. The parameter defaults empty — **every runtime
caller is byte-identical.** Only `builder._provenance_uses` passes it.

Two properties hold this together, and both have tests:

1. a name **in** the set is exempt;
2. a name **not** in it still raises `ScratchSessionError`, even when other names are declared.

If you refactor the membership test, #2 is the one that catches an inversion — nothing else
would.

**Do not extend `declared_scratch` to a new caller without the caller-side filter.** The
exemption is "skip, full stop", not "skip when there is no session". `builder._declared_scratch`
refuses two shapes before declaring anything: a name matching `^s_.+_.+$` (looks like a real
materialized table) and a name colliding with a catalog table (the alias map keys by **bare**
table name, so `scratch.payroll` shadows `dbpcm_warehouse.payroll` and every warehouse column
gets attributed to scratch — then dropped, producing `uses=()` with `outcome="ok"`, which is
fail-open). A second caller inherits neither guard.

---

## HTTP surface added

```
GET  /mint                        the authoring page
GET  /inbox/mint/schema           tables + columns the form may offer
POST /inbox/mint/prior_art        duplicate check, reads only
POST /inbox/mint                  submit
POST /inbox/{id}/revise           ask the assistant (writes nothing)
POST /inbox/{id}/apply_revision   apply a proposal (replace-only)
POST /inbox/{id}/trial_run        execute with reviewer-chosen values
POST /inbox/{id}/attest_scan      record a false-positive judgement
```

Mirrored on the BFF under `/api/inbox/…`. `mint` and `revise` are **model-backed** and need the
long read budget (`_INBOX_MODEL_PATH_SEGMENTS`); the default 10s CRUD timeout abandons them
mid-call and reports the service unreachable.

---

## Settings

**Added:** `learning_mint_model`, `learning_mint_timeout_seconds` (150.0 — must stay **below** the
BFF's 180s hop budget), `learning_revise_*`, `learning_param_judge_*`.

**Removed:** `learning_trial_tenants`.

⚠ Settings use `extra="ignore"`. A field read via `getattr` but never **declared** is silently
ignored — `LEARNING_MINT_MODEL` did nothing at all until it was declared. If you add a setting,
declare it.

---

## Traps that cost time on this branch

- **`git merge --squash` is a real 3-way merge.** It conflicts against already-squashed history.
  To set a tree exactly: `git reset --hard <tip>` then `git reset --soft <prev>`.
- **A test asserting a defect fails when you fix the defect.** Several here are
  `xfail(strict=True)` for exactly that reason — they flip to XPASS and demand attention rather
  than sitting as silent passes. If one goes XPASS after your rebase, you fixed something.
- **`outcome == "completed"` is the completer's verdict, not `static_validation`.** A composite
  test asserting only the DAG shape passed for weeks against candidates that had declined.
- **The MCP is a separate repo** (`clickhouse-api`) with its **own copy** of `provenance.py`. It
  has no `declared_scratch` and re-checks with a real session. Nothing in this repo can weaken
  it — and nothing here should try.

---

## Verifying your rebase

```bash
uv run ruff check src/ tests/ ui/
uv run pytest -q                 # 7124 passed, 225 skipped, 0 xfailed
```

If the UI is wired, the fastest end-to-end confidence is: mint a single blueprint at `/mint`,
trial it with a pasted token, approve → verify → promote. That exercises the draft, the totality
walk, the write router, the replay and the landing gate in one pass.
