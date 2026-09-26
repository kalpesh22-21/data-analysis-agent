"""A UI clarification continues the request with fresh execution time and safe history."""

import json
from dataclasses import replace

import pytest

from data_agent.runtime.loop.answer_judge import JudgeVerdict
from data_agent.runtime.loop.delivery import CURRENT_DELIVERY
from tests.runtime.test_harness_improvements import (
    CREDS,
    Judge,
    batch,
    build,
    call,
    discovery,
    finish,
    query,
    run,
)


@pytest.mark.parametrize("narrow_scope", [False, True])
async def test_ui_answer_refreshes_execution_budgets_and_reaches_agent_and_judge(narrow_scope):
    contexts = []

    class BudgetJudge(Judge):
        async def review(self, brief):
            context = CURRENT_DELIVERY.get()
            contexts.append(context)
            assert context.review_seconds == 3 * self.timeout_seconds
            context.review_seconds = 0.0  # Spend the entire pre-pause review budget.
            return await super().review(brief)

    judge = BudgetJudge()
    loop, store, model, mcp, _ = build(
        [
            replace(discovery(), usage={"total_tokens": 90}),
            batch(
                call("askUser", "clarify", question="Which department?", options=["Sales", "All"])
            ),
            replace(query(), usage={"total_tokens": 20}),
            batch(finish()),
        ],
        judge,
    )
    now = [0.0]
    loop._clock = lambda: now[0]
    loop._max_loop_iterations = 2
    loop._max_token_spend = 100
    loop._max_wall_clock_seconds = 60
    paused = await run(loop)
    assert paused.status == "paused_ask_user"
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.pause_checkpoint.budget_window_count == 1
    now[0] += 86400  # The user answers tomorrow; waiting must not use execution time.
    refreshed = replace(
        CREDS,
        jwt="refreshed-token",
        column_scope=frozenset({"hr.employee.Department"}) if narrow_scope else CREDS.column_scope,
    )
    out = await loop.resume(session_id=CREDS.session_id, credentials=refreshed, answer="Sales")
    assert out.status == "done"
    assert out.review["status"] == "approved"
    assert out.assistant_text == "There are 120 employees."
    assert len(contexts) == 2 and contexts[0] is not contexts[1]
    assert contexts[1].credentials == refreshed
    assert mcp.calls[-1].jwt == "refreshed-token"
    assert judge.briefs[-1].question == "How many employees by department?"
    assert judge.briefs[-1].clarification_answers == ("Sales",)
    messages = model.calls[2].messages
    assert any(m.get("role") == "user" and m.get("content") == "Sales" for m in messages)
    receipts = [
        json.loads(m["content"])
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id") == "clarify"
    ]
    if narrow_scope:
        assert not receipts  # Prior-scope model arguments are not replayed.
    else:
        assert receipts[0]["status"] == "clarification_requested"
        assert receipts[0]["error_code"] is None
        assert "not a tool failure" in receipts[0]["evidence_usage"]
        assert "SQL failures" not in receipts[0]["evidence_usage"]
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.pause_checkpoint.consumed
    # Persistence retains the non-data control marker for evidence/scope filtering.
    assert (
        next(e for e in doc.tool_trail if e.tool_call_id == "clarify").error_code
        == "CLARIFICATION_REQUESTED"
    )


async def test_resume_does_not_erase_an_explicit_answer_rejection():
    rejection = JudgeVerdict(False, "contradicts_result", "Use the executed result.", True)
    judge = Judge(
        [rejection, JudgeVerdict(True, reviewed=True), JudgeVerdict(True, reviewed=True), rejection]
    )
    loop, store, _, _, _ = build(
        [
            discovery(),
            query(),
            batch(finish("There are 125 employees.")),
            batch(
                call(
                    "askUser", "clarify", question="Which period?", options=["Current", "Previous"]
                )
            ),
            batch(finish("There are 125 employees.")),
        ],
        judge,
    )
    assert (await run(loop)).status == "paused_ask_user"
    out = await loop.resume(session_id=CREDS.session_id, credentials=CREDS, answer="Current")
    assert out.review["status"] == "rejected"
    assert out.assistant_text != "There are 125 employees."
    assert not out.answer_tables
    assert (await store.get_or_create_session(CREDS.session_id)).review_states["0"]["calls"] == 2
