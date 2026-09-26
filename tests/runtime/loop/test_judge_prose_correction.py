"""Exact prose corrections ship without another agent round or evidence mutation."""

import pytest

from data_agent.runtime.loop.answer_judge import APPROVED, AnswerJudge, JudgeBrief, JudgeVerdict
from data_agent.runtime.loop.proposal import ReviewState
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from tests.runtime.loop.test_answer_judge_unit import _raw_turn
from tests.runtime.test_harness_improvements import (
    CREDS,
    Judge,
    batch,
    build,
    call,
    discovery,
    query,
    run,
)

ORIGINAL = "The ranking is shown. Employee names are missing because the employee table is empty."
CORRECTED = "The ranking is shown. Employee names are unavailable in the returned results."


def final(answer=ORIGINAL):
    return batch(
        call(
            "finalizeAnswer",
            "answer",
            answer=answer,
            tables=[{"result_id": "q", "caption": "Ranking"}],
            evidence=["q"],
        )
    )


def correction(text=CORRECTED):
    return JudgeVerdict(True, reviewed=True, repair_type="prose", corrected_answer=text)


def response(**overrides):
    return _raw_turn(
        {
            "approved": True,
            "violation": "",
            "feedback": "",
            "repair_type": "prose",
            "corrected_answer": CORRECTED,
            **overrides,
        }
    )


async def test_corrected_ranking_ships_exact_text_with_same_table_and_one_judge_call():
    judge = Judge([correction()])
    loop, store, model, mcp, events = build(
        [discovery(), query(), final()],
        judge,
        rows=[
            {
                "columns": ["Department", "n"],
                "rows": [["Sales", 24], ["Operations", 8]],
                "row_count": 2,
                "truncated": False,
            }
        ],
    )
    out = await run(loop)
    assert out.assistant_text == CORRECTED
    assert out.review["status"] == "approved"
    assert len(out.answer_tables) == 1
    assert out.answer_tables[0]["caption"] == "Ranking"
    assert len(judge.briefs) == 1
    assert judge.briefs[0].allow_prose_correction
    assert judge.briefs[0].draft == ORIGINAL
    assert judge.briefs[0].referenced_result_ids == ("q",)
    assert judge.briefs[0].selected_components[0]["result_id"] == "q"
    assert model.calls_made == 3
    assert len(mcp.calls) == 1
    doc = await store.get_or_create_session(CREDS.session_id)
    state = doc.review_states["0"]
    assert state["prose_correction"]["original_answer"] == ORIGINAL
    assert state["prose_correction"]["corrected_answer"] == CORRECTED
    assert state["approved_version"] == state["prose_correction"]["corrected_version"]
    assert state["approved_version"] != state["prose_correction"]["original_version"]
    assert doc.messages[-1].content == CORRECTED
    assert any(e == "loop_answer_judge_prose_corrected" for e, _ in events)


@pytest.mark.parametrize(
    "text", ["", "   ", "There are 999 employees.", "SELECT * FROM hr.employee", "Answer\x00"]
)
async def test_invalid_correction_does_not_clear_prior_rejection(text):
    rejected = JudgeVerdict(False, "unsupported_by_evidence", "Correct the explanation.", True)
    judge = Judge([rejected, correction(text)])
    loop, store, _, _, _ = build([discovery(), query(), final(), final()], judge)
    out = await run(loop)
    assert out.review["status"] == "rejected"
    assert not out.answer_tables
    state = (await store.get_or_create_session(CREDS.session_id)).review_states["0"]
    assert not state["approved_version"]
    assert not state["prose_correction"]


async def test_substantive_rejection_still_returns_to_agent_for_repair():
    judge = Judge(
        [
            JudgeVerdict(
                False,
                "measurement_mismatch",
                "Correct the measurement.",
                True,
                repair_type="analysis",
            ),
            JudgeVerdict(True, reviewed=True),
        ]
    )
    loop, _, model, _, _ = build([discovery(), query(), final(), final(CORRECTED)], judge)
    out = await run(loop)
    assert model.calls_made == 4
    assert len(judge.briefs) == 2
    assert out.review["status"] == "approved"


@pytest.mark.parametrize(
    "overrides",
    [
        {"corrected_answer": ""},
        {"corrected_answer": 123},
        {"corrected_answer": "x" * 20001},
        {"approved": False},
        {"repair_type": "analysis"},
        {"violation": "unsupported_by_evidence"},
        {"feedback": "Fix it"},
    ],
)
async def test_malformed_correction_is_never_reviewed_approval(overrides):
    judge = AnswerJudge(ScriptedModelClient([response(**overrides)]), token_budget=10000)
    verdict = await judge.review(
        JudgeBrief("exit_table", "Ranking?", draft=ORIGINAL, allow_prose_correction=True)
    )
    assert verdict is APPROVED
    assert not verdict.reviewed


@pytest.mark.parametrize("site,allowed", [("ask_user", True), ("exit_table", False)])
async def test_correction_cannot_approve_clarification_or_fallback(site, allowed):
    judge = AnswerJudge(ScriptedModelClient([response()]), token_budget=10000)
    verdict = await judge.review(
        JudgeBrief(site, "Ranking?", draft=ORIGINAL, allow_prose_correction=allowed)
    )
    assert not verdict.reviewed
    assert verdict.corrected_answer is None


async def test_real_judge_parses_exact_approved_correction():
    judge = AnswerJudge(ScriptedModelClient([response()]), token_budget=10000)
    verdict = await judge.review(
        JudgeBrief("exit_table", "Ranking?", draft=ORIGINAL, allow_prose_correction=True)
    )
    assert verdict == correction()


def test_scope_change_removes_correction_text():
    state = ReviewState(
        scope_hash="old",
        prose_correction={"original_answer": ORIGINAL, "corrected_answer": CORRECTED},
    )
    assert not ReviewState.restore(state.to_doc(), "new").prose_correction


@pytest.mark.parametrize(
    "original,corrected,valid",
    [
        ("There are 120 employees.", "There are 120 employees in accessible records.", True),
        ("There are 120 employees.", "There are 125 employees in accessible records.", False),
        ("The totals are 24 and 8.", "The available totals are 24 and 8.", True),
        ("The totals are 24 and 8.", "The available total is 24.", False),
        ("The change is -10%.", "The change is 10%.", False),
    ],
)
async def test_numeric_corrections_preserve_values(original, corrected, valid):
    judge = Judge([correction(corrected), APPROVED])
    loop, _, _, _, _ = build([discovery(), query(), final(original), final(original)], judge)
    out = await run(loop)
    if valid:
        assert out.assistant_text == corrected
        assert out.review["status"] == "approved"
        assert len(judge.briefs) == 1
    else:
        assert out.review["status"] == "rejected"
        assert not out.answer_tables


async def test_correction_cannot_request_new_components():
    judge = AnswerJudge(
        ScriptedModelClient([response(tables=[{"result_id": "other"}])]), token_budget=10000
    )
    verdict = await judge.review(
        JudgeBrief("exit_table", "Ranking?", draft=ORIGINAL, allow_prose_correction=True)
    )
    assert not verdict.reviewed
