"""The getBlueprint-before-runBlueprint gate — its refusal AND the window-scoped
`BlueprintGate` that owns the decision (Release 1).

THE RULE. `runBlueprint(id=X)` is refused unless this TURN already holds a successful
`getBlueprint(X)` the model has ALREADY SEEN. A blueprint card carries `intent`, `slots`,
`resolves` and `result_grain` — and NO SQL — so without this the model routes on an
authored prose `intent` string and can run, and confidently report, an analysis that
measures something else entirely.

The loop keeps the EFFECTS (trail persistence, the dispatch-chain short-circuit, budget
control flow); this owns the state and the decision, and emits its own event because
nothing observable happens between the emission and the refusal's use.

It imports `ToolResult`, `ToolObserver` and `clean_blueprint_id` — the last so the gate
keys on the SAME normalization of a model-supplied id the rest of the turn uses. So this
is NOT the stdlib leaf `read_guard.py` is, and does not need to be: nothing outside the
loop package imports it.
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
    """The refusal for `runBlueprint(id=X)` where X was never expanded with a successful
        `getBlueprint` earlier in this turn.

        Module-private: `BlueprintGate.check_run_blueprint` is the only caller. The ERROR CODE
        is public (`BLUEPRINT_DEFINITION_NOT_READ_CODE`) because the learning summary loader,
        the denial table and the e2e tests all key on it.

        The D56 grain gate cannot catch what this catches: it verifies that the result SHAPE
        matches the declared `result_grain`, never that the blueprint answers the question
        that was asked. Reading the definition is the only step that can.

        RETRYABLE, and the fix is one call away, so the turn continues. It batches:
        `getBlueprint` for several ids in one response, `runBlueprint` for all of them in the
        next, so an N-deliverable request costs 2 round-trips, not 2N.

        `denial_detail` NAMES THE BLUEPRINT and the exact fix, because
        `context/budget.py::_render_entry` builds the model-facing text from
        `entry.denial_detail or classify_denial(entry.error_code).user_message` and never from
        `ToolResult.user_message`, and the denial table sees only the code. D25:
        `blueprint_id` is corpus-authored, never user content, so naming it is safe.

        `provenance=frozenset()` (determined-empty): this refusal reads no warehouse data, and
        `_compute_turn_provenance_union` is fail-closed, so a `None` here would collapse the
        turn's union and drop the user's own answer from every later turn's replay. It is
        deliberately NOT added to `context/assembly.py::_STALE_CROSS_TURN_ERROR_CODES` — the
        only model-authored text on the entry is the `slot_bindings` in its `args`, which a
        SUCCESSFUL `runBlueprint` entry already replays cross-turn.
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

          - `_definitions_read`: ids whose definition the model has ALREADY SEEN — the only
            set the membership test consults.
          - `_expanded_this_round`: ids expanded during the CURRENT response batch, which
            become readable to the model only on the NEXT round-trip. Held apart until
            `commit_round`.

        WINDOW-SCOPED, and seeded from the persisted trail for the same two reasons the read
        guard is: the D45 per-round-trip context rebuild would otherwise reset the memory on
        every `send_turn`, and a budget-window continue enters a fresh `_run_loop_body` with
        an empty in-memory set, refusing a model for work it had already done.

        TURN-SCOPED. A `getBlueprint` from an EARLIER turn does not satisfy the gate: context
        is rebuilt per turn and trimmed by `fit_request_to_budget`, and the rule is about what
        the model can read RIGHT NOW. The accepted cost is one extra `getBlueprint` per
        follow-up turn. The caller's trail walk is what enforces the scope.

        THE GATE EMITS ITS OWN EVENT, unlike the read guard's guarded event: nothing
        observable happens between the refusal being built and the loop routing it.
    """

    def __init__(self, observer: ToolObserver) -> None:
        self._observer = observer
        self._definitions_read: set[str] = set()
        self._expanded_this_round: set[str] = set()

    def observe_prior_definition_read(self, raw_id: Any) -> None:
        """Seed from ONE prior `getBlueprint` trail entry of this turn (the caller's walk is
                already filtered to `turn_index` + `status == "ok"`, and to the tool name — so
                that one walk feeds both this gate and the read guard).

                Seeded straight into the COMMITTED set, not the staged one: a persisted entry is
                by construction from an earlier round-trip, so the model has seen its result.

                `status == "ok"` is the whole predicate — no `found` check. The result body sits
                behind a D46 KV pointer, so checking would cost a store round-trip per entry, and
                a `{found: false}` expansion cannot make a `runBlueprint` succeed anyway. The
                gate's job is "did you look", not "did you find".
        """
        blueprint_id = clean_blueprint_id(raw_id)
        if blueprint_id is not None:
            self._definitions_read.add(blueprint_id)

    def begin_round(self) -> None:
        """Start a response batch: clear the staged set, which is per-response by
        construction (`commit_round` folds it at the end of the batch)."""
        self._expanded_this_round = set()

    def note_definition_in_context(self, raw_id: Any) -> None:
        """Stage one blueprint id whose definition the model HOLDS as of this batch, so it
                becomes runnable from the next round-trip.

                ONE method for the loop's two staging sites, because they are the same fact: a
                SUCCESSFUL `getBlueprint` in this batch (whose result the model reads next
                round-trip), and a `getBlueprint` DECLINED by the repeated-read guard (being
                deduped means the definition is already in the model's context). Without the
                second a deadlock is reachable: expand, run refused for an unrelated reason,
                re-expand defensively, get a data-free marker, never satisfy the gate again.

                NOTE the deduped case is the OPPOSITE of the finalization evidence rule, which
                REJECTS the same marker. The two gates ask different questions — "does the model
                have the definition?" versus "did work actually happen?" — and a dedup answers yes
                to the first and no to the second.

                Staging the deduped id rather than committing it is not a weakening: a decline
                proves the id was served earlier THIS TURN, so it is already committed in every
                case except a duplicate pair inside ONE batch — where staging is precisely
                correct, because the model has still seen neither result.

                Takes the RAW argument value, not a cleaned id: `clean_blueprint_id` runs here so
                the staged key is provably the same normalization the membership test uses. A
                `None`/non-string id stages nothing.
        """
        blueprint_id = clean_blueprint_id(raw_id)
        if blueprint_id is not None:
            self._expanded_this_round.add(blueprint_id)

    def check_run_blueprint(self, args: Any) -> ToolResult | None:
        """The gate itself: the refusal for this `runBlueprint` call, or `None` to let it
                through. Emits `loop_blueprint_definition_not_read` on refusal.

                THE MEMBERSHIP TEST CONSULTS THE COMMITTED SET ONLY — see `commit_round` for why
                a same-response `[getBlueprint(x), runBlueprint(x)]` pair must NOT pass.

                Two ways to pass without ever having read the definition, both intentional: *args*
                is not a dict or carries no usable `id` (nothing to gate; the call fails on its own
                merits in the executor), and the CALLER declines to ask — `runBlueprint` with no
                wired handler is never gated, since "expand it first" would send the model after a
                tool that cannot help it, and a mid-DAG checkpoint RESUME never reaches the
                dispatch site at all, so a resumed blueprint is structurally ungated rather than
                exempted by a branch here.
        """
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
        """Fold the batch's expansions into the committed set, once the batch has DRAINED.

                Ids expanded in THIS response become runnable from the NEXT one, deliberately not
                mid-batch: the result of a `getBlueprint` issued in this response does not reach
                the model until the next round-trip, so a `[getBlueprint(x), runBlueprint(x)]`
                pair in one message would satisfy a mid-batch fold while the model was still blind
                to the SQL — the whole failure the gate exists to stop. The refusal costs exactly
                the round-trip the model owed anyway, and the BATCHED shape is unaffected.
        """
        self._definitions_read |= self._expanded_this_round
