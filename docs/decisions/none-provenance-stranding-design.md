# `ok`+`None` Provenance Stranding — Silent-Hang Fix Design (D94)

**Status:** DESIGNED (Session 15, D94). Companion to [DECISIONS.md](DECISIONS.md)
(D44 / D47 / D56 / D57 / D61 / D63 / D89). No `src/` written by this doc — it is
the buildable spec the backend developer implements against. Scope: three parts —
(1) break the retry-until-budget-cap loop with a non-data-bearing sentinel,
(2) emit a diagnostic observer event, (3) a seed/load-time catalog-skew warning
plus a record correction about where the *production* safety actually lives.

---

## 1. Problem restatement

A tool result with `status="ok"` but `provenance=None` is **silently stranded**,
causing a silent retry-until-budget-cap hang with **no root-cause signal**.

### Root cause — a catalog/extractor SKEW, not a data leak

The MCP call **succeeds** (the MCP's own sqlglot parse + scope-enforcement passed),
so the `ToolResult` keeps `status="ok"`. The runtime then runs its **independent**
sqlglot re-parse against `CatalogHandle.schema` to compute D44 provenance, and that
re-parse fails:

- **raw-loop `runQuery`** — `runtime/provenance/capture.py:86`: a
  `ProvenanceExtractionError` is swallowed → provenance `None`. `:94`:
  `sampleRows`/`getTableSchema` against an uncatalogued table → `None`.
- **`runBlueprint`** — `blueprint/executor.py::_union_provenance` (~412-413,
  ~882-886) poisons the whole union to `None` if **any** inner `runQuery` returns
  `None`; `RunBlueprintTool._execute` (`blueprint/tool.py:134-144`) then emits
  `status="ok"` + `provenance=None`.

Both paths persist an `ok`+`None` entry to the trail (`agent_loop.py:903-915`).

### Why it hangs

Context is rebuilt from the trail **every model round-trip**
(`_build_canonical_messages` `agent_loop.py:465-510,787`) via
`ContextAssembler.assemble` (`context/assembly.py:140-142`), which calls
`scope_filter.filter_trail`. In `filter_trail` (`scope_filter.py:109-118`) a
current-turn entry is exempted from the strict D44 drop **only when
`entry.status != "ok"`** — a deliberate, PII-safe, status-gated design (module
docstring `:27-45`): a non-`ok` entry carries no result rows, so surfacing it
leaks nothing, whereas an `ok` entry is data-bearing.

An `ok`+`None` entry is therefore **not** exempt. It falls through to
`is_entry_in_scope` → `is_provenance_in_scope` (`:58-78`), where
`if provenance is None: return False` (`:67-68`) drops it **unconditionally**.
The stranded entry never re-enters context → the model re-emits the identical
tool call → the loop burns iterations to `_max_budget_windows` →
`loop_paused_budget_cap` / `loop_hard_ceiling_stop` (`agent_loop.py:921-952`).
Those are the **only** signals; nothing attributes the hang to stranded
provenance.

### What is and isn't at stake

This is **fail-closed** — no data leaks. `is_provenance_in_scope` correctly
refuses to surface a result whose columns it cannot prove in-scope. The bug is
the **silent hang + zero diagnosis**, not a leak. Every fix below must preserve
the fail-closed invariant: **no data-bearing content is ever surfaced for an
out-of-scope or undetermined-provenance entry, under any scope including
narrow/empty.**

---

## 2. Part 1 — Break the loop with a non-data-bearing sentinel

### Chosen architectural home: `ContextAssembler.assemble` (assembly layer)

**Recommendation: option (ii) — detect and inject one layer up, in
`context/assembly.py`, NOT in `scope_filter.py`.**

Justification (one line): `scope_filter` is a pure, heavily-unit-tested PII gate
with a single invariant ("never surface out-of-scope data") and **no observer
handle** — grafting a "surface a synthetic marker" concern into it mixes two
responsibilities, complicates its adversarial test matrix, and still can't emit
the Part-2 event; `assemble` already owns the observer, already calls
`filter_trail`, and can cheaply diff the drop set, so the sentinel lives where the
rendering and the observer already are while `filter_trail` stays a byte-identical
pure gate.

### Detection predicate (covers BOTH paths by construction)

After `filter_trail` returns `in_scope`, `assemble` computes the **stranded set**:

```
entry is stranded  ⟺  entry.turn_index == current_turn_index
                       AND entry.status == "ok"
                       AND entry.provenance is None
                       AND entry not in in_scope     (i.e. it was just dropped)
```

Because both the raw-loop `runQuery` and the `runBlueprint` paths produce exactly
`ok`+`None`, a single predicate covers both — no path-specific code. The
`blueprint_id` (present on the `runBlueprint` entry, absent/`None` on a raw
`runQuery`) is carried only into the diagnostic payload (Part 2), never into the
sentinel body.

### The sentinel — a synthetic tool result whose RESULT payload is data-free

For each stranded entry, `assemble` injects a **synthetic tool-role message keyed
to that entry's `tool_call_id`** into the rendered message list, whose **content is
the fixed sentinel string ONLY**:

> **Sentinel text (exact):**
> `result withheld: provenance could not be determined for this call, so its result cannot be shown. Do not retry the identical call — it will be withheld again. Try a different query or approach, or ask the user.`

Rendering it as the **tool message for the stranded `tool_call_id`** (rather than a
free-floating assistant note) matters: the model emitted a `tool_call`, so the
canonical message sequence needs a matching tool result for that id every
round-trip. Supplying the sentinel as that result is what actually **breaks the
loop** — the model sees a definitive "this call is withheld, don't repeat it"
answer in the exact slot where it was otherwise re-inferring "I have no result,
call again."

### Two-sided data contract (decided during review — do NOT "tighten" this)

The sentinel splits into two sides with **different** data contracts, and
conflating them reopens the hang:

- **The synthetic TOOL-RESULT message content = the fixed data-free sentinel string
  ONLY.** It carries ZERO warehouse result data: no `result_preview`, no
  `result_full`, no result columns/cells. (This is the invariant that was always
  the point.)
- **The synthesized ASSISTANT `tool_call` that pairs with it DOES replay the
  model's own original `args`** (the SQL/params it authored this turn). This is
  **required for correlation**: without the args the assistant side renders
  `runQuery({})`, and in a **multi-call turn** the model cannot tell *which* query
  was withheld, so it re-emits it and re-strands — reintroducing the exact hang D94
  fixes. The args are therefore intentionally retained, not stripped.

**Why replaying the args is PII-safe** (three reasons, so a future reader does not
strip them and reopen the hang):

1. `args` are the model's **OWN current-turn output**, generated causally **before**
   the withheld result existed — they cannot contain the withheld result's data.
2. The runtime **already** replays full current-turn args (SQL, including column
   names) for DENIED (`status != "ok"`) entries via `budget.py::_render_entry`;
   replaying the withheld-provenance call's args is consistent with that
   pre-existing, accepted contract.
3. The provenance / column-scope gate protects warehouse **RESULT DATA**
   (values/cells outside the caller's column scope), **not** the model-authored
   query text.

### Shape (two changes the implementer must make)

- The injected sentinel is a dict with keys
  `{role, tool_call_id, tool_name, args, withheld_sentinel, content}` — `args`
  carries the model's own replayed call arguments (per the contract above);
  `content` is the fixed sentinel string.
- The verbatim-render discriminator is the **explicit `withheld_sentinel` flag**
  (a boolean the renderer checks), **not** the presence of a `content` key — so a
  data-bearing entry can never be mistaken for a sentinel, and vice-versa.

### Constraints satisfied

- **(a) NO RESULT payload.** "No data payload" scopes specifically to the **RESULT**
  payload of the withheld tool result: no `result_preview`, `result_full`, result
  columns, or cell values ever reach the sentinel content — it is a fixed string
  built outside the data-bearing render path, PII-safe under **any** scope
  (empty/narrow). The **paired assistant call intentionally retains the model's own
  `args`** for correlation, which is PII-safe for the three reasons above (own
  causally-prior output; parity with the accepted denied-entry arg replay in
  `budget.py::_render_entry`; the scope gate guards result data, not query text).
- **(b) Current-turn only.** The predicate requires
  `entry.turn_index == current_turn_index`. A **cross-turn** `ok`+`None` entry stays
  dropped as history (it is not exempt, gets no sentinel) — the retry loop is a
  *within-turn* phenomenon, and a prior turn's undetermined result is correctly
  gone. This preserves cross-turn D44 unchanged.
- **(c) Both paths.** Covered by the single predicate above.

### Invariant preserved

`filter_trail` and `assemble` **still never surface any data-bearing content** for
an out-of-scope or undetermined-provenance entry. The sentinel is not the entry's
data replayed — it is a purpose-built, data-free marker that says *"a result
existed but its provenance is undetermined, so it is withheld."* The strict D44
drop of the real entry is unchanged; `filter_trail` stays a pure gate with the
same signature and the same test matrix.

---

## 3. Part 2 — Diagnostic observer event

### Event name: `loop_result_withheld_provenance`

### Emitted from: `ContextAssembler.assemble` (via the observer it already receives)

Since Part 1 lives in `assemble` (which is forwarded the per-request observer,
`assembly.py:127`), the event fires there, using the established convention
`self._observer("loop_result_withheld_provenance", {…})`
(`ToolObserver = Callable[[str, dict], None]`).

### Payload (non-sensitive — NO data)

```
{
  "tool_name":    <str>,          # "runQuery" | "runBlueprint" | "sampleRows" | "getTableSchema"
  "turn_index":   <int>,          # current turn
  "tool_call_id": <str>,          # opaque model-supplied id, not sensitive
  "blueprint_id": <str | None>,   # set on the runBlueprint path, None for raw runQuery
  "reason":       "provenance_undetermined"
}
```

No SQL, no columns, no cell values, no scope token — safe under D25/D61.

### De-duplication (avoid per-round-trip spam)

`assemble` runs on **every** round-trip, so a naive emit would fire the event once
per remaining budget iteration for the same stranded call. Fire it **at most once
per `tool_call_id` per turn** by threading a turn-local `withheld_call_ids: set[str]`
memo through `assemble`, reset each turn and owned by `AgentLoop` — the exact same
turn-local memo pattern already used for `retrieval_memo`
(`assembly.py:122-125`). Emit only when a stranded `tool_call_id` is first seen.

### Telemetry-only (no UI surface)

This is a **diagnostic/telemetry** event — its purpose is to make the hang
attributable in Phoenix, not to drive UI. It therefore needs **no**
`progress.py` change (no `_SHAPE_ALLOWLIST` payload keys, no `_STEP_LABELS` label);
`to_progress_event` will simply not forward it, which is correct.

> **If** the team later decides to surface it in the UI, the minimal follow-up is:
> add the payload keys (`tool_name`, `turn_index`, `tool_call_id`, `blueprint_id`,
> `reason`) to `_SHAPE_ALLOWLIST` and a step label to `_STEP_LABELS` in
> `observability/progress.py`. Not done now — reject astronautics.

---

## 4. Part 3 — Seed/load-time catalog-skew warning + record correction

### Home: the OFFLINE seed/load path — `retrieval/corpus_loader.load_corpus`

The runtime `VectorIndex` protocol (`retrieval/vector_index.py:36-60`) exposes only
`recall()` and `get_blueprint(id)` — there is **no enumerate-all-blueprints**
method, so a runtime `create_app` startup check has no enumeration hook. The
natural home is the **offline** path, which has every blueprint's `uses` at
seed/load time and can load a `CatalogHandle` to cross-check:

- `corpus_loader.load_corpus` (`797-892`) already iterates every `BlueprintSeed`
  and validates internal `uses` consistency (`_validate_blueprint_uses` `296-310`),
  but **never** cross-checks `uses` against the catalog.
- `scripts/seed_neo4j_corpus.py` (blueprint list at `52`) is the invocation site
  that can supply a `CatalogHandle` via `load_catalog_handle()`.

### The check

For each loaded blueprint, for each `(db, table)` in `BlueprintSeed.uses`, if a
`CatalogHandle` is available and `handle.is_catalogued(db, table)` is **False**,
log a **loud WARNING** naming the blueprint id and the missing `db.table`(s).

### Decision: SOFT (logged warning), NOT a hard `CorpusLoadError`

Lean soft. A blueprint may **legitimately** reference tables absent from a given
catalog snapshot — a partial dev warehouse, a reduced dev catalog, or tables
catalogued in a different environment. Hard-failing the whole corpus load on that
would block loading a valid corpus against an incomplete dev catalog: brittle and
wrong. So:

- **Skew found** → `WARNING` per blueprint, enumerating missing `db.table`(s).
  Loading proceeds.
- **No `CatalogHandle` supplied** → skip the check silently (it is an optional
  dev-time aid, not a load precondition).
- `CorpusLoadError` stays **reserved** for genuine corpus-internal-consistency
  failures (the existing loud-fail), unchanged.

### Record correction (also carried into D94 in DECISIONS.md)

**Production protection against this catalog/extractor skew is
(MCP-fails-closed) + (both catalogs in agreement) — NOT load-time catalog
validation.** The real defense against a leak is that the **MCP itself** parses
and scope-enforces every `runQuery` and fails closed (D57/D63), and that its
introspection-built enforcement schema and the runtime's `CatalogHandle` YAML
**agree**. When they disagree, you get this stranded-provenance hang — which is now
**diagnosable** (Parts 1+2) and **pre-warned at seed time** (Part 3) — but the
*safety* (no leak) was never dependent on load-time validation. The seed-time check
is a **dev-time early-warning aid only**, never the prod safety mechanism.

---

## 5. Invariants preserved (summary)

1. **No RESULT-data leak, any scope.** No warehouse **result** data
   (`result_preview`/`result_full`/columns/cells) is surfaced for an out-of-scope
   or undetermined-provenance entry, including under a narrow/empty `column_scope`.
   The sentinel's **content** carries zero result data. (The paired assistant call
   replays the model's own `args` — PII-safe: own causally-prior output, parity
   with the accepted denied-entry arg replay in `budget.py::_render_entry`, and the
   scope gate guards result data, not query text.)
2. **`filter_trail` stays a pure gate.** Same signature, same PII invariant, same
   Layer-1 test matrix — no observer, no sentinel logic inside it.
3. **Cross-turn D44 unchanged.** A prior-turn `ok`+`None` (or any out-of-scope)
   entry is still dropped with no sentinel and no event.
4. **Status-gated exemption unchanged.** Current-turn non-`ok` entries keep their
   existing exemption; the sentinel is an *additional* path only for current-turn
   `ok`+`None`.
5. **Fail-closed remains the default.** If sentinel injection itself cannot be
   built for an entry, the entry stays dropped (no data surfaced) — the loop-break
   is best-effort on top of a still-safe drop.

---

## 6. Test matrix (what QA must cover)

| # | Scenario | Assertion |
|---|----------|-----------|
| 1 | Current-turn `ok`+`None` (raw `runQuery`) | A sentinel tool message for that `tool_call_id` appears in assembled context; it contains the exact sentinel text and **no** SQL / column / cell / preview content. |
| 2 | Same, PII-safety under narrow/empty scope | The injected context contains no data-bearing bytes from the stranded entry under an empty scope AND a narrow scope — byte-inspection. |
| 3 | Sentinel breaks the retry loop | With the sentinel present, the model (cassette/fake) does **not** re-emit the identical tool call; the loop terminates before `_max_budget_windows` instead of hitting `loop_paused_budget_cap` / `loop_hard_ceiling_stop`. |
| 3b | Multi-call turn correlation | In a turn with ≥2 tool calls where ONE strands, the paired assistant `tool_call` replays that call's own `args` (not `{}`), so the model correlates the withheld marker to the exact query and does not re-emit it; verify the un-stranded call's result is unaffected. |
| 4 | `runBlueprint` `ok`+`None` path | Same sentinel behavior via the `_union_provenance`→`None` path; the Part-2 payload carries the `blueprint_id`. |
| 5 | Cross-turn `ok`+`None` still dropped | A prior-turn `ok`+`None` entry gets **no** sentinel and **no** event; it stays dropped as history. |
| 6 | Diagnostic event fires once | `loop_result_withheld_provenance` is emitted with the correct non-sensitive payload, and **at most once per `tool_call_id` per turn** across repeated round-trip rebuilds (memo de-dup). |
| 7 | Event payload PII-safe | No SQL, columns, cell values, scope token, or JWT in the payload (D25/D61 parity). |
| 8 | `filter_trail` unit parity | Existing `filter_trail`/`is_provenance_in_scope` Layer-1 tests are byte-unchanged — the gate itself did not move. |
| 9 | Seed skew warning fires | Loading a corpus whose blueprint `uses` a table absent from the supplied `CatalogHandle` logs a WARNING naming blueprint id + missing `db.table`; load still succeeds (no `CorpusLoadError`). |
| 10 | Seed check optional | With no `CatalogHandle` supplied, `load_corpus` runs unchanged and emits no skew warning. |
| 11 | Non-`ok` current-turn exemption unchanged | A current-turn denied/errored entry is still surfaced by the existing exemption (no regression, no sentinel path taken). |

---

## 7. Build handoff summary

- **Part 1 home:** `ContextAssembler.assemble` (`context/assembly.py`) — inject a
  sentinel tool message for each current-turn `ok`+`None` stranded entry, keyed to
  its `tool_call_id`. `scope_filter.filter_trail` is **not** touched.
- **Sentinel data contract (two sides):** the TOOL-RESULT `content` = the fixed
  sentinel string ONLY (no `result_preview`/`result_full`/columns/cells); the paired
  ASSISTANT `tool_call` DOES replay the model's own `args` (required for multi-call
  correlation — PII-safe: own causally-prior output, parity with denied-entry arg
  replay in `budget.py::_render_entry`, scope gate guards result data not query text).
- **Sentinel dict shape:** keys `{role, tool_call_id, tool_name, args, withheld_sentinel, content}`;
  the verbatim-render discriminator is the explicit `withheld_sentinel` flag, NOT the
  presence of a `content` key.
- **Sentinel text:** `result withheld: provenance could not be determined for this call, so its result cannot be shown. Do not retry the identical call — it will be withheld again. Try a different query or approach, or ask the user.`
- **Part 2 event:** `loop_result_withheld_provenance`, emitted from `assemble`,
  telemetry-only (no `progress.py` change), deduped once-per-`tool_call_id`-per-turn
  via a turn-local memo threaded like `retrieval_memo`.
- **Part 3 warning:** SOFT (logged WARNING, not `CorpusLoadError`) in
  `corpus_loader.load_corpus`, cross-checking `BlueprintSeed.uses` against an
  optional `CatalogHandle`. Record correction: prod safety = MCP-fails-closed +
  catalogs-in-agreement, not load-time validation.
