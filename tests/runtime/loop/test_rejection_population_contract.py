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
    assert "Active + On Leave" not in AGENT_SYSTEM_PROMPT
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
    )
    judge = Judge([verdict, verdict])
    loop, store, model, mcp, events = build(
        [discovery(), query(), final("The totals are shown."), final("The totals are shown.")],
        judge,
    )
    result = await run(loop, "Summarize accrual balances across all records.")
    assert result.review["status"] == "rejected"
    assert result.review["site"] in {"exit_table", "exit_prose"}
    assert result.review["attempts"] == 0  # Existing explicit rejection; no terminal re-review.
    assert result.review["violation"] == "unrequested_employee_status_filter"
    assert result.assistant_text == ship_decline_text("unrequested_employee_status_filter")
    assert "only active and on-leave employees' records" in result.assistant_text
    assert len(judge.briefs) == 2
    assert model.calls_made == 4  # The first refusal still grants its existing repair.


def test_ranking_review_checks_ui_selected_execution():
    assert "resolve the UI-selected table result_id" in _ANSWER_JUDGE_PROMPT
    assert "ORDER BY, limit, and tie handling" in _ANSWER_JUDGE_PROMPT
    assert "A different unselected query cannot justify" in _ANSWER_JUDGE_PROMPT
    assert "LIMIT applied before ranking" in _ANSWER_JUDGE_PROMPT
    assert "existing correct result can be selected, request presentation repair" in _ANSWER_JUDGE_PROMPT
    assert "measurement_mismatch and repair_type=analysis" in _ANSWER_JUDGE_PROMPT
