"""Specific rejection copy and population-boundary instructions."""

import pytest

from data_agent.runtime.loop.answer_judge import (
    _ANSWER_JUDGE_PROMPT,
    ANSWER_VIOLATIONS,
    JudgeVerdict,
)
from data_agent.runtime.loop.judge_ship_guard import GROUNDING_DECLINE_TEXT, ship_decline_text
from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT
from tests.runtime.loop.test_judge_prose_correction import final
from tests.runtime.test_harness_improvements import Judge, build, discovery, query, run


@pytest.mark.parametrize("violation", ANSWER_VIOLATIONS)
def test_every_answer_violation_has_specific_public_copy(violation):
    assert ship_decline_text(violation) != GROUNDING_DECLINE_TEXT


def test_population_default_does_not_authorize_fact_record_filters():
    assert "If a blueprint conflicts with scope, use a declared slot override first" in AGENT_SYSTEM_PROMPT
    assert "default active/on-leave" not in AGENT_SYSTEM_PROMPT
    assert "explicit user scope overrides defaults" in AGENT_SYSTEM_PROMPT
    assert "Do not invent a status default" in _ANSWER_JUDGE_PROMPT
    assert "A table caption or assumption does not authorize that restriction" in _ANSWER_JUDGE_PROMPT
    assert "unrequested_employee_status_filter and repair_type=analysis" in _ANSWER_JUDGE_PROMPT



async def test_exhausted_population_rejection_has_envelope_and_specific_decline():
    verdict = JudgeVerdict(
        False,
        "unrequested_employee_status_filter",
        "Remove the unrequested Active/On Leave filter from the accrual aggregate.",
        reviewed=True,
        repair_type="analysis",
        measured_population="Active + On Leave",
        required_population="all accessible records",
    )
    judge = Judge([verdict, verdict])
    loop, store, model, mcp, events = build(
        [discovery(), query(), final("The totals are shown."), final("The totals are shown.")],
        judge,
    )
    result = await run(loop, "Summarize accrual balances across all records.")
    assert result.review["status"] == "rejected"
    assert result.review["site"] in {"exit_table", "exit_prose"}
    assert result.review["attempts"] == 2  # Report consumed proposal reviews; delivery adds no review.
    assert result.review["violation"] == "unrequested_employee_status_filter"
    assert result.assistant_text == ship_decline_text("unrequested_employee_status_filter", "Active + On Leave", "all accessible records")
    assert result.review["reason"] == result.assistant_text
    assert "used Active + On Leave" in result.assistant_text
    assert len(judge.briefs) == 3
    assert model.calls_made == 4  # The first refusal still grants its existing repair.


def test_ranking_review_checks_ui_selected_execution():
    assert "resolve the UI-selected table result_id" in _ANSWER_JUDGE_PROMPT
    assert "ORDER BY, limit, and tie handling" in _ANSWER_JUDGE_PROMPT
    assert "A different unselected query cannot justify" in _ANSWER_JUDGE_PROMPT
    assert "LIMIT applied before ranking" in _ANSWER_JUDGE_PROMPT
    assert "existing correct result can be selected unambiguously, approve corrected_document" in _ANSWER_JUDGE_PROMPT
    assert "measurement_mismatch and repair_type=analysis" in _ANSWER_JUDGE_PROMPT


def test_population_copy_uses_actual_direction_without_echoing_freeform_feedback():
    from data_agent.runtime.loop.judge_ship_guard import JudgeShipGuard
    from data_agent.runtime.loop.proposal import ReviewState
    verdict = JudgeVerdict(False, 'unrequested_employee_status_filter',
                           'Internal feedback with SQL must not be copied to users.',
                           measured_population='Active', required_population='Active + On Leave')
    state = ReviewState(scope_hash='scope')
    state.reject(verdict, 'version')
    restored = ReviewState.restore(state.to_doc(), 'scope')
    text = ship_decline_text(restored.violation, restored.measured_population, restored.required_population)
    assert 'used Active; the required population was Active + On Leave.' in text
    assert 'all records' not in text and 'SQL' not in text
    guard = JudgeShipGuard()
    guard.note_refusal('exit_table', verdict.violation, measured_population=restored.measured_population,
                       required_population=restored.required_population)
    assert guard.measured_population == 'Active'
    guard.note_approval()
    assert guard.measured_population == guard.required_population == ''
    assert ReviewState.restore(state.to_doc(), 'new-scope').measured_population == ''


def test_unknown_population_uses_neutral_copy_not_invented_all_records():
    text = ship_decline_text('unrequested_employee_status_filter', 'SELECT secret', 'invented')
    assert 'did not match the required scope' in text
    assert 'SELECT' not in text and 'all records' not in text


def test_population_labels_parse_and_do_not_break_prose_approval():
    from data_agent.runtime.loop.answer_judge import parse_verdict
    from tests.runtime.loop.test_answer_judge_unit import _raw_turn
    verdict = parse_verdict(_raw_turn({
        'approved': False, 'violation': 'unrequested_employee_status_filter',
        'feedback': 'Include On Leave employees.', 'repair_type': 'analysis',
        'measured_population': 'Active', 'required_population': 'Active + On Leave',
    }), 'exit_table')
    assert verdict.measured_population == 'Active'
    assert verdict.required_population == 'Active + On Leave'
    verdict = parse_verdict(_raw_turn({
        'approved': True, 'violation': '', 'feedback': '', 'repair_type': 'prose',
        'corrected_answer': 'The results are shown.',
        'measured_population': '', 'required_population': '',
    }), 'exit_table')
    assert verdict.approved and verdict.corrected_answer == 'The results are shown.'
