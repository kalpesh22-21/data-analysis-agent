"""A visual shown as a table tells the user plainly and gives review the backend reason."""
from unittest.mock import patch

from data_agent.runtime import composer
from data_agent.runtime.composer import PresentationError
from data_agent.runtime.loop.answer_judge import JudgeBrief, JudgeVerdict
from tests.runtime.test_harness_improvements import Judge, batch, build, call, discovery, run

REASON = "Select distinct category columns and a quantitative measure, or use a table."


def bars(ref):
    return {"takeaway": "Sales has 3 Active employees.", "evidence": [ref], "sections": [{
        "id": "headcount", "title": "Active employees by department", "evidence_refs": [ref],
        "caveats": [], "actions": [],
        "presentation": {"kind": "bars", "result_id": ref, "category_column": "Department",
                         "measure_column": "n", "label": "Active employees"},
    }]}


async def test_failed_chart_ships_plain_notice_and_reviews_backend_reason():
    def broken(*args, **kwargs):
        raise PresentationError(REASON)

    judge = Judge([JudgeVerdict(True, reviewed=True)])
    loop, _, _, _, _ = build([
        discovery(),
        batch(call("runQuery", "q", sql="SELECT Department, count() AS n FROM hr.employee GROUP BY Department")),
        batch(call("finalizeAnswer", "a", **bars("q"))),
    ], judge, rows=[{"columns": ["Department", "n"], "rows": [["Sales", 3]], "row_count": 1, "truncated": False}])
    with patch.object(composer, "_visual", broken):
        out = await run(loop, "Show Active employees by department as a bar chart.")
    section = out.answer_document["sections"][0]
    assert section["presentation"]["kind"] == "table"
    assert section["explanation"] == "The requested bar chart couldn't be drawn from this result, so it is shown as a table."
    assert "quantitative measure" not in str(out.answer_document)
    assert judge.briefs[0].presentation_fallbacks == (
        {"section_id": "headcount", "requested_kind": "bars", "reason": REASON},
    )


def test_fallback_reasons_reach_the_judge_payload_only_when_present():
    fallback = {"section_id": "s", "requested_kind": "bars", "reason": REASON}
    assert JudgeBrief("exit_table", "q", presentation_fallbacks=(fallback,)).payload()["presentation_fallbacks"] == [fallback]
    assert "presentation_fallbacks" not in JudgeBrief("exit_table", "q").payload()
