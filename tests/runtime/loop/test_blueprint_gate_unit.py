"""Unit tests for `loop/blueprint_gate.py::BlueprintGate` — the
getBlueprint-before-runBlueprint gate's STATE and DECISION, exercised through its
own interface rather than through a whole scripted turn.

These are the fast, exhaustive complement to `test_blueprint_definition_gate.py`,
which stays the proof that the loop wires the decision to the right effects: the
refusal reaching the trail, the executor never running, `_maybe_start_summary`
being skipped, the resume path never passing through the gate at all. Here we pin
the decision itself — the two sets and the difference between them, the seeding
rules, the deduped-`getBlueprint` case, the fold's timing, and the exact refusal
and event payload.

THE ONE ASYMMETRY WORTH STATING UP FRONT, because every test below turns on it:
`check_run_blueprint` consults the COMMITTED set only. Staging (from either site)
does NOT make an id runnable in the same batch — `commit_round` does, and it runs
after the batch drains.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.dispatch.denial_mapping import classify_denial
from data_agent.runtime.loop.blueprint_gate import (
    BLUEPRINT_DEFINITION_NOT_READ_CODE,
    BLUEPRINT_DEFINITION_NOT_READ_EVENT,
    BlueprintGate,
)

BP_A = "bp-active-headcount-by-department"
BP_B = "bp-average-salary-by-department"


class _Recorder:
    """Records every `(event, payload)` the gate emits, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def payloads(self, event: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.events if name == event]


def _gate() -> tuple[BlueprintGate, _Recorder]:
    """A gate with its round already begun — the loop always calls `begin_round`
    before dispatching a batch, so a test that skipped it would be testing a state
    the production path never reaches."""
    recorder = _Recorder()
    gate = BlueprintGate(recorder)
    gate.begin_round()
    return gate, recorder


# --- the gate's basic decision ----------------------------------------------


def test_unread_blueprint_is_refused() -> None:
    gate, recorder = _gate()
    refusal = gate.check_run_blueprint({"id": BP_A})
    assert refusal is not None
    assert refusal.status == "error"
    assert refusal.tool_name == "runBlueprint"
    assert refusal.error_code == BLUEPRINT_DEFINITION_NOT_READ_CODE
    assert recorder.names() == [BLUEPRINT_DEFINITION_NOT_READ_EVENT]


def test_refusal_names_the_blueprint_and_the_fix() -> None:
    """`denial_detail` is the model-facing half: `context/budget.py::_render_entry`
    renders `denial_detail or classify_denial(code).user_message` and never
    `user_message`, and the denial table sees only the code — so the detail is the
    ONLY place the specific blueprint can be named. Both fields carry it, and both
    say what to do next."""
    gate, _ = _gate()
    refusal = gate.check_run_blueprint({"id": BP_A})
    assert refusal is not None
    assert refusal.denial_detail == refusal.user_message
    detail = refusal.denial_detail
    assert detail is not None
    assert BP_A in detail
    assert f"getBlueprint('{BP_A}')" in detail
    # Registered in the denial table for the case where the detail is ever absent.
    assert classify_denial(BLUEPRINT_DEFINITION_NOT_READ_CODE).code == (
        BLUEPRINT_DEFINITION_NOT_READ_CODE
    )


def test_refusal_is_retryable_and_determined_empty_provenance() -> None:
    """`retryable` because the fix is one `getBlueprint` away, and the turn
    continues. `provenance=frozenset()` (determined-EMPTY, never `None`) because
    `_compute_turn_provenance_union` is fail-closed: a `None` here would collapse the
    whole turn's union and drop the user's own answer from every later replay."""
    gate, _ = _gate()
    refusal = gate.check_run_blueprint({"id": BP_A})
    assert refusal is not None
    assert refusal.retryable is True
    assert refusal.provenance == frozenset()
    assert refusal.result_preview is None
    assert refusal.result_full is None


def test_refusal_event_payload_is_exactly_the_three_pinned_keys() -> None:
    """D25: the blueprint id is corpus-authored and allowlisted in
    `observability/tracing.py`; no SQL and no question text reaches the span."""
    gate, recorder = _gate()
    gate.check_run_blueprint({"id": BP_A, "slot_bindings": {"department": "Engineering"}})
    assert recorder.payloads(BLUEPRINT_DEFINITION_NOT_READ_EVENT) == [
        {
            "tool_name": "runBlueprint",
            "blueprint_id": BP_A,
            "reason": "no_get_blueprint_this_turn",
        }
    ]


# --- seeding from the persisted trail ---------------------------------------


def test_prior_definition_read_is_committed_immediately() -> None:
    """A PERSISTED entry is by construction from an earlier round-trip, so the model
    has already seen its result — it needs no staging, and must satisfy the gate on
    the very first batch of a resumed window."""
    gate, recorder = _gate()
    gate.observe_prior_definition_read(BP_A)
    assert gate.check_run_blueprint({"id": BP_A}) is None
    assert recorder.events == []


def test_seeding_one_blueprint_does_not_admit_another() -> None:
    gate, _ = _gate()
    gate.observe_prior_definition_read(BP_A)
    assert gate.check_run_blueprint({"id": BP_B}) is not None


def test_seed_normalizes_the_id_the_same_way_the_check_does() -> None:
    """`clean_blueprint_id` strips — so a trail entry whose arg carries whitespace
    still satisfies a clean `runBlueprint`. Keying the two sides on the same
    normalization is why the gate calls the shared helper rather than re-deriving
    one."""
    gate, _ = _gate()
    gate.observe_prior_definition_read(f"  {BP_A}  ")
    assert gate.check_run_blueprint({"id": BP_A}) is None


def test_seed_ignores_a_missing_or_non_string_id() -> None:
    """A `getBlueprint` entry with no usable id seeds nothing — and, crucially, does
    not blow up the trail walk that feeds it."""
    gate, _ = _gate()
    gate.observe_prior_definition_read(None)
    gate.observe_prior_definition_read(123)
    gate.observe_prior_definition_read("")
    gate.observe_prior_definition_read("   ")
    assert gate.check_run_blueprint({"id": BP_A}) is not None


# --- staging vs committing: the fold's timing -------------------------------


def test_same_batch_get_then_run_is_still_refused() -> None:
    """THE CENTRAL RULE. A `[getBlueprint(x), runBlueprint(x)]` pair in ONE response
    does not pass: the result of that `getBlueprint` does not reach the model until
    the next round-trip, so at the moment of the run the model is still blind to the
    SQL — the whole failure the gate exists to stop."""
    gate, recorder = _gate()
    gate.note_definition_in_context(BP_A)
    refusal = gate.check_run_blueprint({"id": BP_A})
    assert refusal is not None
    assert recorder.names() == [BLUEPRINT_DEFINITION_NOT_READ_EVENT]


def test_staged_id_runs_after_the_fold() -> None:
    """…and the refusal costs exactly the round-trip the model owed anyway: once the
    batch drains and `commit_round` folds, the next batch runs it."""
    gate, recorder = _gate()
    gate.note_definition_in_context(BP_A)
    gate.commit_round()
    gate.begin_round()
    assert gate.check_run_blueprint({"id": BP_A}) is None
    assert recorder.events == []


def test_batched_shape_costs_two_round_trips_for_two_deliverables() -> None:
    """`[getBlueprint(a), getBlueprint(b)]` then `[runBlueprint(a), runBlueprint(b)]`
    — 2 round-trips for 2 deliverables, not 2 per deliverable. The gate is turn-
    scoped, not per-blueprint-serialized, and this is the shape that proves it."""
    gate, recorder = _gate()
    gate.note_definition_in_context(BP_A)
    gate.note_definition_in_context(BP_B)
    gate.commit_round()

    gate.begin_round()
    assert gate.check_run_blueprint({"id": BP_A}) is None
    assert gate.check_run_blueprint({"id": BP_B}) is None
    assert recorder.events == []


def test_begin_round_drops_uncommitted_staging() -> None:
    """A batch that never reaches its fold (a budget ceiling breaking out of the
    window, say) leaves nothing behind: the next round starts from the committed set
    alone. The staged set is per-response by construction."""
    gate, _ = _gate()
    gate.note_definition_in_context(BP_A)
    gate.begin_round()
    gate.commit_round()
    assert gate.check_run_blueprint({"id": BP_A}) is not None


def test_commit_round_is_idempotent_and_keeps_earlier_commits() -> None:
    """The fold is a union, not an assignment — an id committed two batches ago
    survives a later batch that expanded nothing."""
    gate, _ = _gate()
    gate.note_definition_in_context(BP_A)
    gate.commit_round()

    gate.begin_round()
    gate.commit_round()
    gate.commit_round()
    assert gate.check_run_blueprint({"id": BP_A}) is None


def test_staging_an_unusable_id_stages_nothing() -> None:
    gate, _ = _gate()
    gate.note_definition_in_context(None)
    gate.note_definition_in_context({"id": BP_A})
    gate.commit_round()
    gate.begin_round()
    assert gate.check_run_blueprint({"id": BP_A}) is not None


# --- the deduped `getBlueprint` (the read-guard coupling) -------------------


def test_deduped_definition_counts_once_committed() -> None:
    """A `getBlueprint` DECLINED by the repeated-read guard still satisfies the gate:
    being deduped means the definition is already in the model's context, which is
    exactly what the gate asks. The loop stages it through the same method a
    successful expansion uses, so it is admitted from the next batch.

    This is the OPPOSITE of 04 condition 5, which REJECTS the same guard marker as
    completion evidence — "does the model have the definition?" and "did work
    actually happen?" are different questions, and a dedup answers yes to the first
    and no to the second."""
    gate, _ = _gate()
    # Round 1: the dedup path stages the id (the real serve was an earlier
    # round-trip of this turn's trail, or an earlier round of this window).
    gate.note_definition_in_context(BP_A)
    gate.commit_round()

    gate.begin_round()
    assert gate.check_run_blueprint({"id": BP_A}) is None


def test_defensive_re_expand_after_an_unrelated_refusal_does_not_deadlock() -> None:
    """The deadlock the dedup fold exists to prevent, in full: expand → run refused
    for an unrelated reason (a missing slot, refused downstream of this gate) →
    re-expand defensively, which the read guard dedups into a data-free marker →
    run again. Without the fold the id would never re-enter the gate's sets and the
    model could never satisfy it."""
    gate, _ = _gate()
    gate.note_definition_in_context(BP_A)  # round 1: real getBlueprint
    gate.commit_round()

    gate.begin_round()  # round 2: run passes the gate, fails downstream
    assert gate.check_run_blueprint({"id": BP_A}) is None
    gate.note_definition_in_context(BP_A)  # round 3's defensive re-expand, deduped
    gate.commit_round()

    gate.begin_round()
    assert gate.check_run_blueprint({"id": BP_A}) is None


# --- calls the gate deliberately lets through -------------------------------


def test_non_dict_args_are_not_gated() -> None:
    """No id to gate. The call fails on its own merits in the executor rather than
    being refused for a rule it cannot satisfy."""
    gate, recorder = _gate()
    assert gate.check_run_blueprint(None) is None
    assert gate.check_run_blueprint("bp-active-headcount") is None
    assert gate.check_run_blueprint([{"id": BP_A}]) is None
    assert recorder.events == []


def test_missing_or_blank_id_is_not_gated() -> None:
    gate, recorder = _gate()
    assert gate.check_run_blueprint({}) is None
    assert gate.check_run_blueprint({"id": None}) is None
    assert gate.check_run_blueprint({"id": ""}) is None
    assert gate.check_run_blueprint({"id": "   "}) is None
    assert gate.check_run_blueprint({"id": 7}) is None
    assert recorder.events == []


def test_repeated_refusals_each_emit_their_own_event() -> None:
    """A `[runBlueprint(x), runBlueprint(x)]` batch is refused twice — the gate holds
    no per-round refusal memo, and each refused call gets its own trail entry, so
    each gets its own span."""
    gate, recorder = _gate()
    assert gate.check_run_blueprint({"id": BP_A}) is not None
    assert gate.check_run_blueprint({"id": BP_A}) is not None
    assert recorder.names() == [
        BLUEPRINT_DEFINITION_NOT_READ_EVENT,
        BLUEPRINT_DEFINITION_NOT_READ_EVENT,
    ]


def test_run_blueprint_id_is_normalized_before_the_membership_test() -> None:
    """The check side of the same normalization the seed side uses: a padded id in
    the model's `runBlueprint` args matches a clean committed one."""
    gate, _ = _gate()
    gate.observe_prior_definition_read(BP_A)
    assert gate.check_run_blueprint({"id": f" {BP_A}\n"}) is None
