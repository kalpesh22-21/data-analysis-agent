"""Presentation cleanup retains prose and never imposes a prose-size gate."""
import pytest

from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief
from data_agent.runtime.loop.answer_rules import contains_sql, has_markdown_table
from data_agent.runtime.loop.approved_answer import normalize_approved_text
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from tests.runtime.loop.test_judge_prose_correction import correction, final, response
from tests.runtime.test_harness_improvements import Judge, build, discovery, query, run


@pytest.mark.parametrize("artifact", [
    "| Department | Count |\n|---|---|\n| Sales | 120 |",
])
def test_removes_artifacts_preserves_surrounding_narrative(artifact):
    result = normalize_approved_text(
        "There are 120 employees.\n\n" + artifact + "\n\nThis covers accessible records.",
        provenance=frozenset(),
    )
    assert "There are 120 employees." in result
    assert "This covers accessible records." in result
    assert not contains_sql(result) and not has_markdown_table(result)


@pytest.mark.parametrize("corrected", [False, True])
async def test_long_approved_answer_reaches_ui_without_truncation(corrected):
    text = "There are 120 employees. " + "This describes the accessible records. " * 600
    assert len(text) > 20000
    judge = Judge([correction(text)]) if corrected else Judge()
    loop, _, _, _, _ = build([discovery(), query(), final("There are 120 employees." if corrected else text)], judge)
    result = await run(loop)
    assert result.review["status"] == "approved"
    assert result.assistant_text == text.strip()
    assert result.answer_tables
    assert len(judge.briefs) == 1


async def test_judge_parser_accepts_long_correction():
    text = "Supported narrative. " * 1100
    judge = AnswerJudge(ScriptedModelClient([response(corrected_answer=text)]), token_budget=10000)
    verdict = await judge.review(JudgeBrief("exit_table", "Headcount?", allow_prose_correction=True))
    assert verdict.reviewed and verdict.approved and verdict.corrected_answer == text


@pytest.mark.parametrize("route", ["proposal", "correction", "salvage"])
@pytest.mark.parametrize("sql", ["SELECT * FROM hr.employee;", "```sql\nSELECT count(*) FROM hr.employee\n```"])
async def test_sql_is_blocked_before_presentation_cleanup(route, sql):
    from data_agent.runtime.loop.answer_judge import JudgeVerdict
    from tests.runtime.loop.test_partial_answer import PART

    text = "There are 120 employees.\n\n" + sql
    class SQLJudge(Judge):
        async def salvage(self, brief):
            return {**PART, 'takeaway': text, 'outcome': 'full', 'unfinished': []}

    judge = SQLJudge([correction(text)]) if route == "correction" else SQLJudge([JudgeVerdict(False, "unsupported_by_evidence", "Repair prose.", reviewed=True, repair_type="prose")]) if route == "salvage" else SQLJudge()
    loop, _, _, _, _ = build([discovery(), query(), final(text if route == "proposal" else "There are 120 employees.")], judge)
    result = await run(loop)
    assert result.review["status"] != "approved"
    assert not contains_sql(result.assistant_text)
    assert "SELECT" not in result.assistant_text
