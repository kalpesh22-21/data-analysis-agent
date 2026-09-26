"""Prior dialogue reaches the reviewer; bare completion reuses existing work."""

import json

from data_agent.runtime.loop.judge_evidence import recent_conversation
from data_agent.runtime.model.client import ModelTurnResult
from data_agent.runtime.session.models import TurnMessage
from tests.runtime.test_harness_improvements import (
    Judge,
    batch,
    build,
    call,
    discovery,
    finish,
    query,
    run,
)


async def test_followup_judge_receives_prior_user_and_assistant_context():
    judge = Judge()
    loop, _, model, mcp, _ = build(
        [
            discovery(),
            query(),
            batch(finish()),
            batch(
                call("askUser", "clarify", question="Which categories?", options=["Leave", "Other"])
            ),
        ],
        judge,
    )
    await run(loop, "Rank leave by approved hours in May.")
    out = await run(loop, "What categories do we have?")
    assert out.status == "paused_ask_user"
    context = judge.briefs[-1].recent_conversation
    assert any(m["content"] == "Rank leave by approved hours in May." for m in context)
    assert any(m["role"] == "assistant" for m in context)
    assert all(m["turn_index"] == 0 for m in context)


async def test_bare_answer_draft_is_carried_once_without_repeating_query():
    draft = "There are 120 employees in the accessible records."
    loop, _, model, mcp, _ = build(
        [
            discovery(),
            query(),
            ModelTurnResult(assistant_text=draft),
            batch(finish(draft)),
        ],
        Judge(),
    )
    out = await run(loop)
    assert out.assistant_text == draft
    assert len(mcp.calls) == 1
    request = json.dumps(model.calls[-1].messages)
    assert draft in request
    assert "do not repeat completed queries" in request


def test_recent_history_filters_denied_answers_and_bounds_content():
    messages = [
        TurnMessage(ts="now", role="user", content="Leave categories?", turn_index=0),
        TurnMessage(
            ts="now",
            role="assistant",
            content="SECRET",
            turn_index=0,
            provenance=frozenset({("hr.employee", "salary")}),
        ),
        TurnMessage(
            ts="now",
            role="assistant",
            content="x" * 3000,
            turn_index=0,
            provenance=frozenset({("hr.employee", "id")}),
        ),
        TurnMessage(ts="now", role="user", content="Current question", turn_index=1),
    ]
    context = recent_conversation(messages, frozenset({"hr.employee.id"}), 1)
    assert "SECRET" not in json.dumps(context)
    assert "Current question" not in json.dumps(context)
    assert len(context[-1]["content"]) == 2000
    assert context[-1]["truncated"]
