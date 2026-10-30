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


@pytest.mark.parametrize('original,expected', [
    ('Based on payment date (pay_date). Total: $8,100.00.', 'Based on payment date. Total: $8,100.00.'),
    ('Payment dates (`pay_date`) in 2026, not work periods.', 'Payment dates in 2026, not work periods.'),
    ('Employee codes (employee_code), within authorized records.', 'Employee codes, within authorized records.'),
    ('Based on payment dates (payroll.pay_date) and submission dates (date_time_submitted).', None),
    ('Gross earnings (salary_amount).', None),
    ('Based on (pay_date).', None),
    ('Based on payment date (pay_date > 2026-01-01).', None),
    ('Payment date (pay_date, not period end).', None),
    ('Payment date (pay_date) excluding employee_status.', None),
])
def test_presentation_repairs_only_redundant_identifier_annotations(original, expected):
    from data_agent.runtime.composer import _text, PresentationError

    if expected is None:
        with pytest.raises(PresentationError):
            _text(original)
    else:
        assert _text(original) == expected
        assert _text(expected) == expected


def test_parenthetical_business_values_and_material_caveats_are_preserved():
    from data_agent.runtime.composer import _text

    for value in ('Employee code (EMP001).', 'Total pay (excluding bonuses).',
                  'Payment date (2026-01-01).', 'Payment date (pay_date).'):
        assert _text(value, business_values=frozenset({'pay_date'})) == value


async def test_guard_repaired_salvage_retains_data_and_approval_without_another_call():
    from data_agent.runtime.loop.answer_judge import JudgeVerdict
    from tests.runtime.loop.test_partial_answer import PART
    from tests.runtime.final_answer import recovery_sections
    from tests.runtime.test_harness_improvements import CREDS

    class SalvageJudge(Judge):
        salvage_calls = 0

        async def salvage(self, brief):
            self.salvage_calls += 1
            sections = recovery_sections(['q'])
            sections[0]['caveats'] = [
                'Counts use the payment dates (pay_date); only accessible records are included.'
            ]
            return {**PART, 'sections': sections, 'outcome': 'full', 'unfinished': []}

    judge = SalvageJudge([JudgeVerdict(False, 'unsupported_by_evidence',
                                      'Repair prose.', reviewed=True, repair_type='prose')])
    loop, store, _, mcp, events = build([discovery(), query(), final()], judge)
    out = await run(loop)
    assert out.review['status'] == 'approved'
    assert out.answer_document['sections'][0]['caveats'] == [
        'Counts use the payment dates; only accessible records are included.'
    ]
    assert out.answer_document['sections'][0]['presentation']['result_id'] == 'q'
    assert judge.salvage_calls == 1 and len(judge.briefs) == 1 and len(mcp.calls) == 1
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].answer_document == out.answer_document
    assert not any(name == 'loop_recovery_validation_failed' for name, _ in events)


@pytest.mark.parametrize('text', [
    'Data is live and up to date as of today.', 'The results are stale.',
    'Information is current.', '**Data** as of October 30.',
])
def test_explicit_freshness_claim_requires_presentation_repair(text):
    from data_agent.runtime.composer import _text, PresentationError
    with pytest.raises(PresentationError, match='recordAssumptions'):
        _text(text)


@pytest.mark.parametrize('text', [
    'Current employees means Active or On Leave.',
    'Gross earnings paid in 2026.', 'Headcount as of January 1, 2026.',
    'The observed payment dates range from January to June.',
])
def test_reporting_dates_and_populations_are_not_freshness_claims(text):
    from data_agent.runtime.composer import _text
    assert _text(text) == text
