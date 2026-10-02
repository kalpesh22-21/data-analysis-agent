"""Supported subsets survive failed full answers without shipping rejected content."""

import pytest

from data_agent.runtime.loop.answer_judge import APPROVED, AnswerJudge, JudgeBrief, JudgeVerdict
from data_agent.runtime.loop.proposal import ReviewState
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.session.models import TurnMessage
from data_agent.runtime.session_history import project_history
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

# These scenarios exercise exit recovery with a realistic reserved review budget.
_build = build


def build(*args, **kwargs):
    result = _build(*args, **kwargs)
    result[0]._answer_judge_review_budget_seconds = 90.0
    return result


PART = {
    "answer": "Sales has 120 employees in the available records.",
    "evidence": ["q"],
    "table_result_ids": [],
    "capability_refs": [],
    "unfinished": ["The leave ranking could not be verified."],
}
TEXT = PART["answer"] + "\n\nUnfinished: " + PART["unfinished"][0]


def reject(part=None):
    return JudgeVerdict(
        False,
        "unsupported_by_evidence",
        "The leave ranking is unsupported.",
        reviewed=True,
        partial_answer=part,
    )


def final():
    return batch(
        call(
            "finalizeAnswer",
            "final",
            answer="There are 120 employees. The leave ranking is shown.",
            tables=[{"result_id": "q", "caption": "Headcount"}],
            evidence=["q"],
        )
    )


async def test_terminal_review_delivers_partial_and_history_omits_unapproved_table():
    judge = Judge([reject(), reject(), reject(PART)])
    loop, store, model, mcp, _ = build([discovery(), query(), final(), final()], judge)
    out = await run(loop, "Give me headcount and leave ranking.")
    assert out.assistant_text == TEXT
    assert out.review == {"status": "approved", "completion": "partial"}
    assert not out.answer_tables and not out.assumptions
    assert len(judge.briefs) == 3
    assert judge.briefs[-1].allow_partial_answer
    assert judge.briefs[-1].previous_rejection
    assert len(mcp.calls) == 1 and model.calls_made == 4
    doc = await store.get_or_create_session(CREDS.session_id)
    message = TurnMessage.from_doc(doc.messages[-1].to_doc())
    assert message.delivered_components == {"tables": [], "cards": []}
    history = project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)
    assert history["turns"][0]["answer"] == TEXT
    assert not history["turns"][0]["answer_tables"]


async def test_cached_partial_survives_unavailable_later_review_and_keeps_approved_table():
    part = {**PART, "table_result_ids": ["q"]}
    judge = Judge([reject(part), APPROVED])
    loop, store, _, _, _ = build([discovery(), query(), final(), final()], judge)
    out = await run(loop)
    assert out.assistant_text == TEXT
    assert len(out.answer_tables) == 1
    assert out.answer_tables[0]["caption"] == "Headcount"
    assert len(judge.briefs) == 2  # reuse the independent subset approval
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)
    assert history["turns"][0]["answer_tables"] == out.answer_tables
    assert doc.review_states["0"]["approved_partial"]["original_violation"]


async def test_later_explicit_rejection_invalidates_older_partial():
    judge = Judge([reject(PART), reject(), APPROVED])
    loop, _, _, _, _ = build([discovery(), query(), final(), final()], judge)
    out = await run(loop)
    assert out.review["status"] == "rejected"
    assert out.assistant_text != TEXT
    assert not out.answer_tables


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence": ["missing"]},
        {"evidence": ["s"]},
        {"evidence": []},
        {"table_result_ids": ["missing"]},
        {"capability_refs": ["missing"]},
        {"unfinished": []},
        {"answer": ""},
        {"answer": "Bad\x00text"},
    ],
)
async def test_invalid_partial_never_clears_rejection(changes):
    invalid = {**PART, **changes}
    judge = Judge([reject(invalid)] * 3)
    loop, _, _, _, _ = build([discovery(), query(), final(), final()], judge)
    out = await run(loop)
    assert out.review["status"] == "rejected"
    assert not out.answer_tables


async def test_judge_contract_parses_independent_partial_approval():
    result = _raw_turn(
        {
            "approved": False,
            "violation": "unsupported_by_evidence",
            "feedback": "Ranking not supported.",
            "partial_answer": PART,
        }
    )
    for enabled in (True, False):
        judge = AnswerJudge(ScriptedModelClient([result]), token_budget=10000)
        verdict = await judge.review(
            JudgeBrief("exit_table", "Headcount?", allow_partial_answer=enabled)
        )
        assert not verdict.approved and verdict.reviewed
        assert verdict.partial_answer == (PART if enabled else None)


def test_scope_change_discards_cached_partial():
    state = ReviewState(scope_hash="old", approved_partial={"answer": TEXT})
    assert not ReviewState.restore(state.to_doc(), "new").approved_partial


async def test_new_successful_evidence_requires_fresh_partial_review():
    # The agent runs another query after the first rejected answer. Even an
    # unchanged receipt binding needs re-review in light of the new evidence.
    judge = Judge([reject(PART), APPROVED, APPROVED])
    extra = batch(call("runQuery", "q2", sql="SELECT COUNT(*) AS n FROM hr.employee"))
    loop, _, _, _, _ = build(
        [discovery(), query(), final(), extra, final()],
        judge,
        rows=[
            {"columns": ["Department", "n"], "rows": [["Sales", 120]], "row_count": 1},
            {"columns": ["n"], "rows": [[0]], "row_count": 1},
        ],
    )
    out = await run(loop)
    assert out.assistant_text != TEXT
    assert out.review["status"] == "rejected"
    assert len(judge.briefs) == 2  # Unavailable review is not retried on exit.


async def test_service_failure_after_successful_query_recovers_without_final_proposal():
    import httpx

    judge = Judge([reject(PART)])
    loop, store, model, mcp, _ = build([discovery(), query()], judge)
    send = model.send_turn

    async def fail_after_query(*args, **kwargs):
        if model.calls_made >= 2:
            raise httpx.ConnectError("private transport detail")
        return await send(*args, **kwargs)

    model.send_turn = fail_after_query
    out = await run(loop)
    assert out.assistant_text == TEXT
    assert out.review["completion"] == "partial"
    assert out.failure["dependency"] == "model"
    assert len(mcp.calls) == 1
    assert len(judge.briefs) == 1
    assert judge.briefs[0].terminal_partial_review
    doc = await store.get_or_create_session(CREDS.session_id)
    # Narrowing access cannot replay the approved finding or its components.
    history = project_history(doc.messages, doc.tool_trail, frozenset({"other.column"}), None)
    assert history["turns"][0]["answer"] is None
    assert history["turns"][0]["answer_tables"] is None


async def test_no_successful_evidence_cannot_produce_partial():
    import httpx

    judge = Judge([reject(PART)])
    loop, _, model, _, _ = build([], judge)

    async def fail(*args, **kwargs):
        raise httpx.ConnectError("private transport detail")

    model.send_turn = fail
    out = await run(loop)
    assert out.assistant_text != TEXT
    assert "partial" not in out.review.values()
    assert not out.answer_tables
    assert not any(b.allow_partial_answer for b in judge.briefs)


async def test_partial_selects_only_approved_capability_and_persists_selection():
    from tests.runtime.loop.test_exhausted_capability_finalization import setup

    part = {
        "answer": "The employee view is available.",
        "evidence": ["prep:0"],
        "table_result_ids": [],
        "capability_refs": ["employees"],
        "unfinished": ["Compensation history could not be verified."],
    }
    loop, store, _, _ = setup(Judge([reject(part)]))
    loop._answer_judge_review_budget_seconds = 90.0
    out = await run(loop)
    assert out.review["completion"] == "partial"
    assert [c["name"] for c in out.capability_cards] == ["employees"]
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)
    assert history["turns"][0]["capability_cards"] == out.capability_cards


async def test_partial_cannot_masquerade_as_full_approval():
    result = _raw_turn({"approved": True, "violation": "", "feedback": "", "partial_answer": PART})
    judge = AnswerJudge(ScriptedModelClient([result]), token_budget=10000)
    verdict = await judge.review(JudgeBrief("exit_table", "Headcount?", allow_partial_answer=True))
    assert not verdict.reviewed
    assert verdict.partial_answer is None


async def test_null_partial_field_does_not_break_prose_correction():
    result = _raw_turn(
        {
            "approved": True,
            "violation": "",
            "feedback": "",
            "repair_type": "prose",
            "corrected_answer": "The result is shown.",
            "partial_answer": None,
        }
    )
    judge = AnswerJudge(ScriptedModelClient([result]), token_budget=10000)
    verdict = await judge.review(
        JudgeBrief("exit_table", "Headcount?", allow_prose_correction=True)
    )
    assert verdict.reviewed and verdict.corrected_answer == "The result is shown."


async def test_hard_budget_stop_delivers_existing_findings_without_another_agent_turn():
    judge = Judge([reject(PART)])
    loop, store, model, mcp, _ = build([discovery(), query()], judge)
    loop._max_loop_iterations = 2
    loop._max_budget_windows = 1
    out = await run(loop)
    assert out.status == "stopped_hard_ceiling"
    assert out.assistant_text == TEXT
    assert out.review["completion"] == "partial"
    assert model.calls_made == 2 and len(mcp.calls) == 1
    assert len(judge.briefs) == 1
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].content == TEXT


async def test_extra_known_discovery_citation_does_not_discard_supported_partial():
    part = {**PART, "evidence": ["s", "q"]}
    judge = Judge([reject(part), APPROVED])
    loop, store, _, _, _ = build([discovery(), query(), final(), final()], judge)
    out = await run(loop)
    assert out.assistant_text == TEXT
    assert len(judge.briefs) == 2
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.review_states["0"]["approved_partial"]["evidence"] == ["q"]


async def test_unavailable_judge_skips_exit_retry_and_keeps_existing_rationale():
    from data_agent.runtime.loop.judge_ship_guard import ship_decline_text

    judge = Judge([reject(), APPROVED])
    loop, store, _, _, events = build([discovery(), query(), final(), final()], judge)
    out = await run(loop)
    assert len(judge.briefs) == 2
    assert out.review["status"] == "rejected"
    assert out.assistant_text == ship_decline_text("unsupported_by_evidence")
    assert not out.answer_tables
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].content == out.assistant_text


async def test_slow_exit_review_is_cancelled_without_another_final_review(monkeypatch):
    import asyncio

    from data_agent.runtime.loop.judge_ship_guard import ship_decline_text

    monkeypatch.setattr(Judge, "timeout_seconds", 0.02)

    class SlowExitJudge(Judge):
        cancelled = False

        async def review(self, brief):
            if brief.terminal_partial_review:
                self.briefs.append(brief)
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled = True
            return await super().review(brief)

    judge = SlowExitJudge([reject(), reject()])
    loop, _, _, _, events = build([discovery(), query(), final(), final()], judge)
    out = await asyncio.wait_for(run(loop), timeout=1)
    assert judge.cancelled and len(judge.briefs) == 3
    assert out.review["status"] == "rejected"
    assert out.assistant_text == ship_decline_text("unsupported_by_evidence")
    assert not out.answer_tables
    assert ("loop_partial_answer_failed", {"reason": "exit_deadline"}) in events


async def test_exit_skips_new_review_when_less_than_thirty_seconds_remain():
    judge = Judge([reject(), reject(), reject(PART)])
    loop, _, _, _, _ = build([discovery(), query(), final(), final()], judge)
    loop._answer_judge_review_budget_seconds = 29.0
    out = await run(loop)
    assert len(judge.briefs) == 2
    assert out.review["status"] == "rejected"
    assert out.assistant_text != TEXT


async def test_cached_partial_is_reused_even_without_thirty_seconds_remaining():
    judge = Judge([reject(PART), APPROVED])
    loop, _, _, _, _ = build([discovery(), query(), final(), final()], judge)
    loop._answer_judge_review_budget_seconds = 5.0
    out = await run(loop)
    assert out.assistant_text == TEXT
    assert out.review["status"] == "approved"
    assert len(judge.briefs) == 2


@pytest.mark.parametrize("configured,remaining,expected", [(60.0, 90.0, 60.0), (60.0, 45.0, 45.0), (30.0, 90.0, 30.0)])
async def test_exit_deadline_uses_configured_timeout_and_remaining_budget(monkeypatch, configured, remaining, expected):
    import asyncio
    from types import SimpleNamespace

    from data_agent.runtime.loop import partial_answer
    from data_agent.runtime.loop.delivery import CURRENT_DELIVERY, DeliveryContext

    deadlines = []

    async def fake_wait(awaitable, timeout):
        deadlines.append(timeout)
        return await awaitable

    async def recovered(*args, **kwargs):
        return "cached"

    monkeypatch.setattr(asyncio, "wait_for", fake_wait)
    monkeypatch.setattr(partial_answer, "_recover_partial", recovered)
    token = CURRENT_DELIVERY.set(DeliveryContext(CREDS, review_seconds=remaining))
    try:
        loop = SimpleNamespace(_answer_judge=SimpleNamespace(timeout_seconds=configured))
        assert await partial_answer.recover_partial(loop, "s", 0, None, None) == "cached"
        assert deadlines == [expected]
    finally:
        CURRENT_DELIVERY.reset(token)
