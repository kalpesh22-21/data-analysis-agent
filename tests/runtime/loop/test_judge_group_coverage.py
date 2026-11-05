"""Keep bound group-coverage evidence available to ordinary review and recovery.

Live DeepSeek replay separately checks the verdict; scripted clients here check
the actual messages, not a fabricated model's ability to interpret SQL.
"""

import json

import pytest

from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief
from tests.runtime.loop.test_salvage_contract import RecoveryClient
from tests.runtime.test_harness_improvements import batch, call


@pytest.mark.parametrize("mode", ["Include", "Exclude"])
async def test_bound_group_filter_survives_review_and_salvage(mode):
    sql = (
        "SELECT d.name, coalesce(e.n, 0) AS n FROM department d "
        "LEFT JOIN (SELECT department, count(*) n FROM employee "
        "WHERE status='Active' GROUP BY department) e ON e.department=d.id "
        f"WHERE ('{mode}' = 'Include' OR coalesce(e.n, 0) > 0)"
    )
    result = {
        "tool_call_id": "reused-result",
        "tool_name": "runBlueprint",
        "execution": {"sql": [sql], "terminal_sql": sql,
                      "bound_slots": {"zero_headcount": mode}},
        "result_preview": {"columns": ["name", "n"], "row_count": 2,
                           "preview_rows": [["Sales", 3], ["Engineering", 2]],
                           "truncated": False},
    }
    brief = JudgeBrief(
        "exit_table", "Change the saved counts to a donut including every department.",
        results=(result,), previous_rejection="Only nonzero departments were included.",
    )
    response = {"takeaway": "No supported answer.", "sections": [],
                "evidence": [], "unfinished": [], "outcome": "none"}
    client = RecoveryClient([batch(call("salvageAnswer", "s", **response))])
    judge = AnswerJudge(client, token_budget=10000)
    normal = judge.messages_for(brief)
    await judge.salvage(brief)
    recovery = client.calls[0].messages
    for messages in (normal, recovery):
        payload = json.loads(messages[1]["content"])
        assert payload["results"][0]["execution"] == result["execution"]
        assert payload["previous_rejection"] == brief.previous_rejection
        # The recovery overlay must not lose the evidence-first review policy.
        assert "feedback is a review claim, not an execution receipt" in messages[0]["content"]
        assert "complete executed predicate with its bound slot values" in messages[0]["content"]
    assert result["execution"]["bound_slots"] == {"zero_headcount": mode}
