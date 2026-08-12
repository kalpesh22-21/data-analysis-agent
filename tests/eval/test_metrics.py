"""The metric code is code (07 §F), so it gets its own tests.

Every case here is SYNTHETIC. That is deliberate for one metric in particular:
E.3's multi-intent detection rate is definitionally 1.0 in A1 and carries no
information there, so the only honest place to test the COMPUTATION is against
inputs that include the failures A1 cannot produce — a multi-intent request where
the model never initialized, and a single-intent request where it did.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.session.models import (
    REASON_CODES,
    AnalysisState,
    ResultPreview,
    TrackedIntent,
    TrailEntry,
)

from . import metrics


def _intent(
    intent_id: str,
    status: str,
    *,
    reason_code: str | None = None,
    evidence: str | None = None,
) -> TrackedIntent:
    return TrackedIntent(
        intent_id=intent_id,
        description=f"deliverable {intent_id}",
        status=status,
        evidence_tool_call_id=evidence,
        reason_code=reason_code,
    )


def _state(turn_index: int, *intents: TrackedIntent) -> AnalysisState:
    return AnalysisState(turn_index=turn_index, intents=tuple(intents))


def _entry(
    tool_call_id: str,
    tool_name: str,
    *,
    turn_index: int = 0,
    status: str = "ok",
    authoritative: bool = False,
    args: dict[str, Any] | None = None,
    row_count: int | None = None,
) -> TrailEntry:
    return TrailEntry(
        turn_index=turn_index,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=args or {},
        status=status,
        error_code=None,
        provenance=frozenset(),
        result_preview=(
            None
            if row_count is None
            else ResultPreview(
                columns=["x"], row_count=row_count, truncated=False, preview_rows=[]
            )
        ),
        result_full_ref=None,
        ts="2026-08-11T00:00:00Z",
        authoritative=authoritative,
    )


def _state_call(tool_call_id: str, bindings: list[dict[str, Any]], **kwargs: Any) -> TrailEntry:
    return _entry(
        tool_call_id, "updateAnalysisState", args={"intents": bindings}, **kwargs
    )


# ---------------------------------------------------------------------------
# E.1 — the scoped pending sweep
# ---------------------------------------------------------------------------


def test_pending_on_a_done_turn_is_a_violation() -> None:
    records = [
        metrics.TurnRecord("c", 0, "done", _state(0, _intent("i1", "pending")))
    ]
    assert metrics.pending_on_terminal_turns(records) == [("c", "i1")]


def test_hard_ceiling_is_terminal_too() -> None:
    records = [
        metrics.TurnRecord(
            "c", 0, "stopped_hard_ceiling", _state(0, _intent("i1", "pending"))
        )
    ]
    assert metrics.pending_on_terminal_turns(records) == [("c", "i1")]


def test_abandoned_pauses_and_cas_races_are_excluded() -> None:
    """05 §F.1's three legitimate paths to a `pending` state on the doc. An
    UNSCOPED assertion fails against every one of them — and therefore against any
    real store, which is the exact claim 05 §I exists to correct."""
    records = [
        metrics.TurnRecord("a", 0, "paused_ask_user", _state(0, _intent("i1", "pending"))),
        metrics.TurnRecord("b", 0, "paused_budget_cap", _state(0, _intent("i1", "pending"))),
    ]
    assert metrics.pending_on_terminal_turns(records) == []


def test_a_state_from_another_turn_is_history_not_a_violation() -> None:
    """The `live_analysis_state` rule, in the sweep: turn 1 finishing `done` while
    turn 0's abandoned state is still on the doc is not a dropped intent."""
    records = [
        metrics.TurnRecord("c", 1, "done", _state(0, _intent("i1", "pending")))
    ]
    assert metrics.pending_on_terminal_turns(records) == []


def test_terminal_statuses_are_the_real_turn_status_values() -> None:
    """A typo here would silently make the sweep vacuous, so it is pinned against
    the `TurnStatus` literal itself rather than against a copied list."""
    import typing

    from data_agent.runtime.loop.agent_loop import TurnStatus

    declared = set(typing.get_args(TurnStatus))
    assert metrics.TERMINAL_TURN_STATUSES | metrics.NON_TERMINAL_TURN_STATUSES == declared
    assert not (metrics.TERMINAL_TURN_STATUSES & metrics.NON_TERMINAL_TURN_STATUSES)


# ---------------------------------------------------------------------------
# E.2 — buckets derived from REASON_CODES, folded per intent
# ---------------------------------------------------------------------------


def test_buckets_are_derived_from_the_enum_not_a_hard_coded_list() -> None:
    """The draft named three of five, omitting `BUDGET_EXHAUSTED` and
    `USER_STOPPED`. Deriving costs nothing and means a later addition cannot go
    silently uncounted."""
    report = metrics.blocked_intent_report([])
    assert REASON_CODES <= set(report.buckets)
    assert len(REASON_CODES) == 5


def test_report_folds_per_intent_final_state() -> None:
    records = [
        metrics.TurnRecord(
            "c",
            0,
            "done",
            _state(
                0,
                _intent("i1", "completed", evidence="q1"),
                _intent("i2", "blocked", reason_code="NO_ACCESS", evidence="q2"),
                _intent("i3", "blocked", reason_code="ENFORCEMENT_EXHAUSTED"),
            ),
        )
    ]
    report = metrics.blocked_intent_report(records)
    assert report.tracked == 3
    assert report.completed == 1
    assert report.blocked == 2
    assert report.buckets["NO_ACCESS"] == 1
    assert report.buckets["ENFORCEMENT_EXHAUSTED"] == 1
    assert report.blocked_rate == 2 / 3


def test_the_same_intent_seen_on_two_turn_records_counts_once() -> None:
    """A turn spanning a pause produces several `TurnRecord`s sharing one state.
    Folding per `(case, turn, intent)` is what stops that from multiplying the
    counts — the same class of error as counting events instead of intents."""
    state = _state(0, _intent("i1", "blocked", reason_code="NO_ACCESS", evidence="q1"))
    records = [
        metrics.TurnRecord("c", 0, "paused_ask_user", state),
        metrics.TurnRecord("c", 0, "done", state),
    ]
    report = metrics.blocked_intent_report(records)
    assert report.tracked == 1
    assert report.buckets["NO_ACCESS"] == 1


def test_an_unrecognised_reason_code_is_counted_not_dropped() -> None:
    records = [
        metrics.TurnRecord(
            "c", 0, "done", _state(0, _intent("i1", "blocked", reason_code="MADE_UP"))
        )
    ]
    report = metrics.blocked_intent_report(records)
    assert report.buckets["UNKNOWN_REASON_CODE"] == 1
    assert report.blocked == 1


def test_zero_row_ratio_is_a_ratio_not_a_count() -> None:
    """04 §B.4: `REQUIRED_DATA_UNAVAILABLE` fires on any CORRECT query whose answer
    is legitimately empty, so the block COUNT says nothing on its own."""
    events = [
        ("loop_zero_row_block", {"intent_id": "i1"}),
        ("loop_zero_row_completion", {"intent_id": "i2"}),
        ("loop_zero_row_completion", {"intent_id": "i3"}),
    ]
    ratio = metrics.zero_row_ratio(events)
    assert (ratio.blocks, ratio.completions) == (1, 2)
    assert ratio.block_share == 1 / 3


def test_zero_row_ratio_is_empty_when_nothing_fired() -> None:
    ratio = metrics.zero_row_ratio([])
    assert ratio.total == 0
    assert ratio.block_share is None


def test_zero_row_events_are_deduped_per_intent() -> None:
    events = [
        ("loop_zero_row_block", {"intent_id": "i1"}),
        ("loop_zero_row_block", {"intent_id": "i1"}),
    ]
    assert metrics.zero_row_ratio(events).blocks == 1


# ---------------------------------------------------------------------------
# E.3 — multi-intent detection rate
# ---------------------------------------------------------------------------


def test_detection_rate_needs_ground_truth_and_measures_the_miss() -> None:
    """The failure being measured IS the model's own misjudgement, so the
    denominator can never be self-reported. Here the second observation is the
    exact failure the metric exists for: a genuinely multi-intent request where
    the model never created a state at all."""
    observations = [
        metrics.DetectionObservation("a", multi_intent=True, analysis_state_initialized=True),
        metrics.DetectionObservation("b", multi_intent=True, analysis_state_initialized=False),
        metrics.DetectionObservation("c", multi_intent=False, analysis_state_initialized=False),
    ]
    rate = metrics.multi_intent_detection(observations)
    assert rate.denominator == 2
    assert rate.numerator == 1
    assert rate.rate == 0.5
    assert rate.false_positive_rate == 0.0


def test_detection_rate_counts_false_positives_separately() -> None:
    """A state initialized for a SINGLE-intent request burns 03 §E's late-init
    boundary and adds CAS writes for nothing. It is not a detection success and
    must never inflate the numerator."""
    observations = [
        metrics.DetectionObservation("a", multi_intent=False, analysis_state_initialized=True),
    ]
    rate = metrics.multi_intent_detection(observations)
    assert rate.numerator == 0
    assert rate.denominator == 0
    assert rate.rate is None
    assert rate.false_positives == 1
    assert rate.false_positive_rate == 1.0


def test_a1_shaped_input_is_definitionally_one() -> None:
    """Every A1 multi-intent fixture scripts `updateAnalysisState`, so the rate is
    1.0 by construction and carries no information. Asserted HERE, once, so that
    the number is understood rather than reported beside the A1 results as though
    it had been measured."""
    observations = [
        metrics.DetectionObservation(f"case-{n}", multi_intent=True, analysis_state_initialized=True)
        for n in range(9)
    ]
    assert metrics.multi_intent_detection(observations).rate == 1.0


# ---------------------------------------------------------------------------
# §C.2 — the re-derivation predicate
# ---------------------------------------------------------------------------


def _blueprint_then_query(intent_of_query: str) -> list[TrailEntry]:
    return [
        _entry("b1", "runBlueprint", authoritative=True),
        _entry("q1", "runQuery"),
        _state_call(
            "s1",
            [
                {"intent_id": "i1", "status": "completed", "evidence_tool_call_id": "b1"},
                {
                    "intent_id": intent_of_query,
                    "status": "completed",
                    "evidence_tool_call_id": "q1",
                },
            ],
        ),
    ]


def test_re_derivation_fires_only_when_the_query_serves_the_same_intent() -> None:
    same = _blueprint_then_query("i1")
    assert metrics.re_derivation(same, {"b1": "i1", "q1": "i1"}, turn_index=0) is True

    distinct = _blueprint_then_query("i2")
    assert metrics.re_derivation(distinct, {"b1": "i1", "q1": "i2"}, turn_index=0) is False


def test_the_turn_scoped_predicate_flags_the_legal_shape() -> None:
    """Why §C.2 was rewritten. `prompts.py` says the model MAY run further queries
    for a DISTINCT part of the question the blueprint did not answer, so the
    turn-scoped form condemns correct behaviour."""
    trail = _blueprint_then_query("i2")
    assert metrics.re_derivation_turn_scoped(trail, turn_index=0) is True
    assert metrics.re_derivation(trail, {"b1": "i1", "q1": "i2"}, turn_index=0) is False


def test_a_query_before_the_blueprint_is_not_re_derivation() -> None:
    trail = [
        _entry("q1", "runQuery"),
        _entry("b1", "runBlueprint", authoritative=True),
    ]
    assert metrics.re_derivation(trail, {"b1": "i1", "q1": "i1"}, turn_index=0) is False


def test_a_non_authoritative_blueprint_cannot_be_re_derived() -> None:
    """An unverified blueprint result is not the trusted answer for that intent —
    re-running the work is the CORRECT response, not a regression."""
    trail = [
        _entry("b1", "runBlueprint", authoritative=False),
        _entry("q1", "runQuery"),
    ]
    assert metrics.re_derivation(trail, {"b1": "i1", "q1": "i1"}, turn_index=0) is False


def test_re_derivation_is_scoped_to_one_turn() -> None:
    trail = [
        _entry("b1", "runBlueprint", turn_index=0, authoritative=True),
        _entry("q1", "runQuery", turn_index=1),
    ]
    assert metrics.re_derivation(trail, {"b1": "i1", "q1": "i1"}, turn_index=0) is False


def test_serves_intent_recovers_replaced_bindings_from_the_trail() -> None:
    """Why the trail is the source and the final `AnalysisState` is not.

    The state is latest-wins, so once i1's evidence is rewritten from the
    blueprint to the query, the blueprint binding is GONE from it — and that
    rewrite is precisely the re-derivation shape. The trail keeps both.
    """
    trail = [
        _entry("b1", "runBlueprint", authoritative=True),
        _state_call(
            "s1", [{"intent_id": "i1", "status": "completed", "evidence_tool_call_id": "b1"}]
        ),
        _entry("q1", "runQuery"),
        _state_call(
            "s2", [{"intent_id": "i1", "status": "completed", "evidence_tool_call_id": "q1"}]
        ),
    ]
    mapping = metrics.serves_intent_from_trail(trail, 0)
    assert mapping == {"b1": {"i1"}, "q1": {"i1"}}
    assert metrics.re_derivation(trail, mapping, turn_index=0) is True


def test_serves_intent_ignores_rejected_state_calls() -> None:
    """A rejected `updateAnalysisState` persisted a DENIAL, not a binding — using
    its args would credit the model with an evidence link the runtime refused."""
    trail = [
        _state_call(
            "s1",
            [{"intent_id": "i1", "status": "completed", "evidence_tool_call_id": "b1"}],
            status="error",
        )
    ]
    assert metrics.serves_intent_from_trail(trail, 0) == {}


def test_serves_intent_tolerates_a_malformed_args_payload() -> None:
    """The args are MODEL-authored and reach here verbatim off the trail, so the
    deriver must never raise on a shape it did not expect."""
    trail = [
        _entry("s1", "updateAnalysisState", args={"intents": "not-a-list"}),
        _entry("s2", "updateAnalysisState", args={"intents": [None, {"intent_id": 3}]}),
        _entry("s3", "updateAnalysisState", args={}),
    ]
    assert metrics.serves_intent_from_trail(trail, 0) == {}


def test_one_evidence_id_can_serve_several_intents() -> None:
    """Completion reuse is deliberately ALLOWED (04 §A) — one query genuinely
    answers "headcount and average salary by department"."""
    trail = [
        _state_call(
            "s1",
            [
                {"intent_id": "i1", "status": "completed", "evidence_tool_call_id": "q1"},
                {"intent_id": "i2", "status": "completed", "evidence_tool_call_id": "q1"},
            ],
        )
    ]
    assert metrics.serves_intent_from_trail(trail, 0) == {"q1": {"i1", "i2"}}


# ---------------------------------------------------------------------------
# The inferred corpus-gap signal
# ---------------------------------------------------------------------------


def test_corpus_gap_needs_both_a_search_and_an_ad_hoc_completion() -> None:
    searched = ("tool_dispatch_ok", {"tool_name": "searchBlueprints"})
    ad_hoc = ("loop_intent_completed", {"intent_id": "i1", "evidence_tool_name": "runQuery"})
    blueprint = (
        "loop_intent_completed",
        {"intent_id": "i2", "evidence_tool_name": "runBlueprint"},
    )

    assert metrics.corpus_gap_signal([searched, ad_hoc]).fell_through is True
    assert metrics.corpus_gap_signal([searched, blueprint]).fell_through is False
    assert metrics.corpus_gap_signal([ad_hoc]).fell_through is False


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_pass_rate_is_a_rate_over_n_runs_not_a_boolean() -> None:
    """A2 is non-deterministic; one red run is noise, not a regression."""
    rate = metrics.PassRate("L1")
    for ok in (True, True, False, True):
        rate.record(ok, "routed to ad-hoc SQL")
    assert rate.runs == 4
    assert rate.passes == 3
    assert rate.rate == 0.75
    assert "3/4" in rate.line()
    assert rate.failures == ["routed to ad-hoc SQL"]


def test_pass_rate_of_an_unrun_case_is_zero_not_a_crash() -> None:
    assert metrics.PassRate("L1").rate == 0.0


def test_blocked_report_renders_only_non_empty_buckets() -> None:
    records = [
        metrics.TurnRecord(
            "c", 0, "done", _state(0, _intent("i1", "blocked", reason_code="NO_ACCESS"))
        )
    ]
    line = metrics.format_blocked_report(metrics.blocked_intent_report(records))
    assert "NO_ACCESS=1" in line
    assert "USER_STOPPED" not in line
