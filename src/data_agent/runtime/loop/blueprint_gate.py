"""The getBlueprint-before-runBlueprint gate — its refusal AND the window-scoped
`BlueprintGate` that owns the decision (Release 1, recorded in
`docs/decisions/release-1/02-blueprint-card-enrichment.md`).

THE RULE. `runBlueprint(id=X)` is refused unless this TURN already holds a
successful `getBlueprint(X)` the model has ALREADY SEEN. A blueprint card carries
`intent`, `slots`, `resolves` and `result_grain` — and NO SQL — so without this the
model routes on an authored prose `intent` string and can run, and confidently
report, an analysis that measures something else entirely.

Extracted from `_run_loop_body` for the reason `read_guard.py` was: the state
(two sets), the membership test, the refusal and its event are one decision that
was spread across six sites of a 4000-line function, where the load-bearing
distinction between the two sets — staged-this-response versus committed — was
visible only to a reader who held all six in their head at once. The loop keeps
the EFFECTS (trail persistence, `_maybe_start_summary`, the dispatch-chain
short-circuit, budget control flow); this owns the state and the decision, and
emits its own event because nothing observable happens between the emission and
the refusal's use.

THREE IMPORTS, each unavoidable and each one-directional (nothing here imports
`agent_loop`, and `composite.answer_with_table`'s own edge back to it is
`TYPE_CHECKING`-only, so no cycle exists):

  - `ToolResult` — the refusal IS one; the loop routes it through the same
    trail/budget path as a dispatched result.
  - `ToolObserver` — the observer alias the loop already passes.
  - `clean_blueprint_id` — the single normalization of a model-supplied blueprint
    id, shared with `answerWithTable`'s designation path. Re-deriving it here is
    exactly the divergence that helper exists to prevent: the gate would key on a
    differently-normalized id than the one the rest of the turn uses.

So this is NOT the stdlib leaf `read_guard.py` is, and it does not need to be —
nothing outside the loop package imports it.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.composite.answer_with_table import clean_blueprint_id
from data_agent.runtime.dispatch.tool_dispatcher import ToolObserver, ToolResult

__all__ = [
    "BLUEPRINT_DEFINITION_NOT_READ_CODE",
    "BLUEPRINT_DEFINITION_NOT_READ_EVENT",
    "BlueprintGate",
]

BLUEPRINT_DEFINITION_NOT_READ_CODE = "BLUEPRINT_DEFINITION_NOT_READ"

# The gate's observer event. NAMED for the reason the answer-shape gate's two are:
# the `loop_` prefix is load-bearing rather than a convention —
# `observability/tracing.py::guardrail_observer` drops every event that lacks it,
# SILENTLY, so a misnamed event fires perfectly in every raw-recorder unit test and
# reaches production telemetry never. `blueprint_id` is pinned in that module's
# attribute allowlist; renaming either half here breaks the exported span.
BLUEPRINT_DEFINITION_NOT_READ_EVENT = "loop_blueprint_definition_not_read"


def _blueprint_definition_not_read(blueprint_id: str) -> ToolResult:
    """The refusal for `runBlueprint(id=X)` where X was never expanded with a
    successful `getBlueprint` earlier in this turn (the getBlueprint-before-run
    rule, recorded in `docs/decisions/release-1/02-blueprint-card-enrichment.md`).

    Module-private: `BlueprintGate.check_run_blueprint` is the only caller, and the
    refusal is meaningless without the membership test that decides to build it.
    The ERROR CODE is public (`BLUEPRINT_DEFINITION_NOT_READ_CODE`) because the
    learning summary loader, the denial table and the e2e tests all key on it.

    WHY THE GATE EXISTS. A blueprint card carries `intent`, `slots`, `resolves` and
    `result_grain` — and NO SQL. So the model has been choosing and running
    blueprints on the strength of an AUTHORED PROSE `intent` string; when that
    string misdescribes the query underneath it, the model runs the wrong analysis
    and reports it confidently. The D56 grain gate does not catch this: it verifies
    the result SHAPE matches the declared `result_grain`, never that the blueprint
    answers the question that was asked. Reading the definition is the only step
    that can, and it is the concrete practice the prompt's "Success is not proof of
    correctness" line implies for the blueprint route.

    RETRYABLE, and the fix is one call away — so the turn continues, the model
    expands the blueprint on the next round-trip and runs it on the one after. It
    may batch: `getBlueprint` for several blueprints in one response, `runBlueprint`
    for all of them in the next, so an N-deliverable request costs 2 round-trips,
    not 2N.

    `denial_detail` NAMES THE BLUEPRINT and the exact fix. It has to:
    `context/budget.py::_render_entry` builds the model-facing text as
    `entry.denial_detail or classify_denial(entry.error_code).user_message` and
    never from `ToolResult.user_message` (`TrailEntry` has no such field), and the
    denial table sees only the code, so its text can say "that blueprint" but never
    which one. The two strings are kept in step deliberately —
    `BLUEPRINT_DEFINITION_NOT_READ` is registered in `dispatch/denial_mapping.py`
    for the case where the detail is ever absent. D25: `blueprint_id` is
    corpus-authored, never user content, so naming it is safe.

    `provenance=frozenset()` (determined-empty), matching
    `_answer_table_blueprint_not_run` and `_finalization_blocked`: this refusal
    reads no warehouse data, and `_compute_turn_provenance_union` is fail-closed, so
    a `None` here would collapse the whole turn's union and drop the user's own
    answer from every later turn's replay. It is deliberately NOT added to
    `context/assembly.py::_STALE_CROSS_TURN_ERROR_CODES`: the only model-authored
    text on the entry is the `slot_bindings` in its `args`, and a SUCCESSFUL
    `runBlueprint` entry already replays exactly those cross-turn, so the refusal
    exposes nothing its successful twin does not.
    """
    detail = (
        f"You have not read blueprint '{blueprint_id}' in this turn, so you do not "
        "know what it actually measures — a card's intent line is authored prose, "
        f"the SQL is the analysis. Call getBlueprint('{blueprint_id}') first, then "
        "run it, and check what it does answers what was asked before you do."
    )
    return ToolResult(
        status="error",
        tool_name="runBlueprint",
        error_code=BLUEPRINT_DEFINITION_NOT_READ_CODE,
        retryable=True,
        user_message=detail,
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
        denial_detail=detail,
    )


class BlueprintGate:
    """The blueprint-definition gate's STATE and DECISION for one budget window.

    TWO SETS, and the distinction between them is the whole gate:

      - `_definitions_read`: ids whose definition the model has ALREADY SEEN — the
        only set the membership test consults.
      - `_expanded_this_round`: ids expanded during the CURRENT response batch,
        which become readable to the model only on the NEXT round-trip. Held apart
        until `commit_round`.

    WINDOW-SCOPED, and seeded from the persisted trail for the same two reasons the
    read guard is: the D45 per-round-trip context rebuild would otherwise reset the
    memory on every `send_turn`, and a budget-window `continue` resume enters a
    fresh `_run_loop_body` with an empty in-memory set — a model that expanded the
    blueprint before the cap would then be refused for work it had done.

    TURN-SCOPED, like every other memory in this release. A `getBlueprint` from an
    EARLIER turn does not satisfy the gate: context is rebuilt per turn and trimmed
    by `fit_request_to_budget`, so a definition fetched in turn 1 may have been
    trimmed out by turn 5, and the rule is about what the model can read RIGHT NOW.
    The cost is real and accepted — a follow-up turn that re-runs the same blueprint
    with a different slot value ("now just Engineering") pays one extra
    `getBlueprint` per turn. The caller's trail walk is what enforces the scope (it
    is already filtered to `turn_index` + `status == "ok"`).

    THE GATE EMITS ITS OWN EVENT, unlike the read guard's guarded event: nothing
    observable happens between the refusal being built and the loop routing it, so
    there is no append-then-emit ordering for the loop to own here.
    """

    def __init__(self, observer: ToolObserver) -> None:
        self._observer = observer
        self._definitions_read: set[str] = set()
        self._expanded_this_round: set[str] = set()

    def observe_prior_definition_read(self, raw_id: Any) -> None:
        """Seed from ONE prior `getBlueprint` trail entry of this turn (the caller's
        walk is already filtered to `turn_index` + `status == "ok"`, and to the tool
        name — `getBlueprint` is BOTH a guarded idempotent read and the thing this
        gate is keyed on, so that one walk feeds both observers).

        Seeded straight into the COMMITTED set, not the staged one: a persisted
        entry is by construction from an earlier round-trip, so the model has seen
        its result.

        `status == "ok"` is the whole predicate at the call site — no `found` check.
        The result body sits behind a D46 KV pointer, so reading it there would cost
        a store round-trip per entry, and a `{found: false}` expansion cannot make a
        `runBlueprint` succeed anyway (the executor re-fetches the definition and
        fails on its own merits). The gate's job is "did you look", not "did you
        find"."""
        blueprint_id = clean_blueprint_id(raw_id)
        if blueprint_id is not None:
            self._definitions_read.add(blueprint_id)

    def begin_round(self) -> None:
        """Start a response batch: clear the staged set, which is per-response by
        construction (`commit_round` folds it at the end of the batch)."""
        self._expanded_this_round = set()

    def note_definition_in_context(self, raw_id: Any) -> None:
        """Stage one blueprint id whose definition the model HOLDS as of this batch,
        so it becomes runnable from the next round-trip.

        ONE method for the loop's two staging sites, because they are the same fact:

          - a SUCCESSFUL `getBlueprint` in this batch — the definition arrives with
            this round's tool results, i.e. the model reads it next round-trip;
          - a `getBlueprint` DECLINED by the repeated-read guard — being deduped
            means the definition is already in the model's context (the guard's
            trim-aware exemption is what makes that true), which is exactly what the
            gate asks. Without this a deadlock is reachable: expand, run refused for
            an unrelated reason (a missing slot), re-expand defensively, get a
            data-free marker, and never satisfy the gate again.

        NOTE the deduped case is the OPPOSITE of 04 condition 5, which REJECTS the
        same marker as completion evidence. The two gates ask different questions —
        "does the model have the definition?" versus "did work actually happen?" —
        and a dedup answers yes to the first and no to the second.

        Staging the deduped id rather than committing it is not a weakening: a
        decline proves the id was served earlier THIS TURN, so it is already in
        `_definitions_read` (seeded from the trail, or folded by an earlier
        `commit_round`) in every case except a duplicate pair inside ONE batch —
        where staging is precisely the correct answer, because the model has still
        seen neither result.

        Takes the RAW argument value, not a cleaned id: `clean_blueprint_id` runs
        here so the staged key is provably the same normalization the membership
        test uses. A `None`/non-string id stages nothing (there is no id to gate)."""
        blueprint_id = clean_blueprint_id(raw_id)
        if blueprint_id is not None:
            self._expanded_this_round.add(blueprint_id)

    def check_run_blueprint(self, args: Any) -> ToolResult | None:
        """The gate itself: the refusal for this `runBlueprint` call, or `None` to
        let it through. Emits `loop_blueprint_definition_not_read` on refusal.

        THE MEMBERSHIP TEST CONSULTS THE COMMITTED SET ONLY — `_expanded_this_round`
        is deliberately not consulted. See `commit_round` for why a same-response
        `[getBlueprint(x), runBlueprint(x)]` pair must NOT pass.

        Two ways to pass without ever having read the definition, both intentional:

          - *args* is not a dict, or carries no usable `id`. There is no id to gate;
            the call fails on its own merits in the executor.
          - the CALLER declines to ask. `runBlueprint` with no wired handler is
            never gated (there is no blueprint stack at all, so "expand it first"
            would send the model after a tool that cannot help it, replacing the
            honest `RUN_BLUEPRINT_UNAVAILABLE` → raw-loop fallback with a loop), and
            a mid-DAG checkpoint RESUME never reaches the dispatch site at all — so
            a resumed blueprint is structurally ungated rather than exempted by a
            branch here. A resume is the continuation of an already-gated
            invocation, not a fresh decision to run a blueprint."""
        if not isinstance(args, dict):
            return None
        blueprint_id = clean_blueprint_id(args.get("id"))
        if blueprint_id is None or blueprint_id in self._definitions_read:
            return None
        refusal = _blueprint_definition_not_read(blueprint_id)
        self._observer(
            BLUEPRINT_DEFINITION_NOT_READ_EVENT,
            {
                "tool_name": "runBlueprint",
                # D25: corpus-authored, never user content. No SQL and
                # no question text is placed on the span.
                "blueprint_id": blueprint_id,
                "reason": "no_get_blueprint_this_turn",
            },
        )
        return refusal

    def commit_round(self) -> None:
        """Fold the batch's expansions into the committed set, once the batch has
        DRAINED.

        Ids expanded in THIS response become runnable from the NEXT one —
        deliberately not mid-batch. The rule is that the model has READ the
        definition, and the result of a `getBlueprint` issued in this response does
        not reach the model until the next round-trip: a `[getBlueprint(x),
        runBlueprint(x)]` pair in one message would satisfy a mid-batch fold while
        the model was still blind to the SQL, which is the whole failure the gate
        exists to stop. The refusal costs exactly the round-trip the model owed
        anyway, and the BATCHED shape is unaffected — `[getBlueprint(a),
        getBlueprint(b)]` then `[runBlueprint(a), runBlueprint(b)]` is still 2
        round-trips for 2 deliverables, not 2 per deliverable."""
        self._definitions_read |= self._expanded_this_round
