"""Presentation economy replaces the former every-multi-row-result table gate."""
import pytest

from data_agent.runtime.composer import PresentationError, proposal_to_internal
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from tests.runtime.final_answer import answer_arguments, final_answer
from tests.runtime.test_harness_improvements import (
    Judge,
    batch,
    build,
    call,
    discovery,
    query,
    run,
)


@pytest.mark.parametrize("rows,truncated", [([], False), ([["Sales", 1]], False),
    ([["Sales", 1], ["Engineering", 2]], False), ([["Sales", 1]], True)])
async def test_execution_shape_does_not_force_visible_tables(rows, truncated):
    judge = Judge()
    loop, _, model, mcp, events = build([
        discovery(), query(), final_answer(assistant_text="The requested analysis is complete.", evidence=["q"]),
    ], judge, rows=[{"columns": ["Department", "n"], "rows": rows,
                     "row_count": 200 if truncated else len(rows), "truncated": truncated}])
    result = await run(loop)
    assert result.answer_document["sections"] == []
    assert result.answer_document["sources"] == {}
    assert not result.answer_tables
    assert len(judge.briefs) == 1 and len(mcp.calls) == 1 and model.calls_made == 3
    assert not any(e.startswith("loop_answer_shape_") for e, _ in events)


async def test_only_selected_result_is_displayed_and_other_executions_remain_evidence():
    loop, _, _, mcp, _ = build([
        discovery(), query(),
        batch(call("runQuery", "total", sql="SELECT count() AS n FROM hr.employee")),
        final_answer(assistant_text="The requested departments are shown.",
                     tables=[{"result_id": "q", "caption": "Departments"}], evidence=["q", "total"]),
    ], Judge(), rows=[
        {"columns": ["Department", "n"], "rows": [["Sales", 1], ["Engineering", 2]], "row_count": 2},
        {"columns": ["n"], "rows": [[3]], "row_count": 1},
    ])
    result = await run(loop)
    document = result.answer_document
    assert list(document["sources"]) == ["q"]
    assert [s["presentation"]["result_id"] for s in document["sections"]] == ["q"]
    assert len(mcp.calls) == 2


async def test_judge_can_request_visible_data_without_repeating_the_query():
    judge = Judge([
        JudgeVerdict(False, "unsupported_by_evidence", "Show the requested department breakdown.",
                     reviewed=True, repair_type="presentation"),
        JudgeVerdict(True, reviewed=True),
    ])
    loop, _, model, mcp, _ = build([
        discovery(), query(), final_answer(assistant_text="Analysis complete.", evidence=["q"]),
        final_answer(assistant_text="Department counts are shown.", tables=[{"result_id": "q"}]),
    ], judge)
    result = await run(loop)
    assert result.review["status"] == "approved"
    assert result.answer_document["sections"][0]["presentation"]["kind"] == "table"
    assert len(judge.briefs) == 2 and model.calls_made == 4 and len(mcp.calls) == 1


def test_extra_visible_tables_require_explicit_reasons():
    proposal = answer_arguments(takeaway="Two requested breakdowns.", tables=[{"result_id": "a"}, {"result_id": "b"}])
    assert len(proposal_to_internal(proposal)["tables"]) == 2
    proposal["sections"][1].pop("table_reason")
    with pytest.raises(PresentationError, match="zero or one"):
        proposal_to_internal(proposal)


async def test_followup_does_not_automatically_display_a_prior_turns_table():
    loop, _, _, mcp, _ = build([
        discovery(), query(), final_answer(assistant_text="Department counts.", tables=[{"result_id": "q"}]),
        final_answer(assistant_text="You're welcome."),
    ], Judge())
    first = await run(loop)
    second = await run(loop, "Thanks")
    assert first.answer_document["sections"]
    assert second.answer_document["sections"] == []
    assert second.answer_document["sources"] == {}
    assert len(mcp.calls) == 1
