import asyncio
import json

import httpx
import pytest

from data_agent.runtime.app import _stream_turn
from data_agent.runtime.loop.answer_judge import APPROVED, JudgeVerdict
from data_agent.runtime.model.client import ModelTurnResult
from data_agent.runtime.observability.progress import ProgressEmitter
from data_agent.runtime.session_history import project_history
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


def hello():
    return batch(
        call(
            "finalizeAnswer",
            "hello",
            answer="Hello! How can I help?",
            tables=[],
            capability_refs=[],
            evidence=[],
        )
    )


async def test_conversation_prose_is_coached_then_finalized_and_reviewed():
    judge = Judge()
    loop, _, model, mcp, _ = build([ModelTurnResult(assistant_text="Hello!"), hello()], judge)
    out = await run(loop, "Hello")
    assert out.review["status"] == "approved"
    assert len(judge.briefs) == 1
    assert not mcp.calls
    assert "conversational reply" in str(model.calls[-1].messages)
    assert not out.answer_tables and not out.capability_cards


@pytest.mark.parametrize("dependency", ["model", "tool_schema"])
@pytest.mark.parametrize("kind,retryable", [("connection", True), ("404", False)])
async def test_dependency_failure_streams_result_and_persists_history(dependency, kind, retryable):
    loop, store, model, _, events = build([], Judge())

    async def fail(*args):
        if kind == "connection":
            raise httpx.ConnectError("PRIVATE transport detail")
        response = httpx.Response(404, request=httpx.Request("POST", "https://gateway.invalid"))
        raise httpx.HTTPStatusError(
            "PRIVATE nginx body", request=response.request, response=response
        )

    if dependency == "model":
        model.send_turn = fail
    else:
        loop._tools_provider = fail
    stream = "".join([event async for event in _stream_turn(lambda: run(loop), ProgressEmitter())])
    assert "event: result" in stream and "event: error" not in stream
    assert "PRIVATE" not in stream
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(
        doc.messages, doc.tool_trail, CREDS.column_scope, doc.pause_checkpoint
    )
    answer = history["turns"][0]
    assert answer["failure"]["dependency"] == dependency
    assert answer["failure"]["retryable"] is retryable
    assert answer["review"]["status"] == "approved"
    assert len([m for m in doc.messages if m.role == "assistant"]) == 1
    assert any(e == "loop_dependency_failed" for e, _ in events)


async def test_spent_finalization_still_reviews_fallback():
    judge = Judge()
    loop, _, _, _, _ = build(
        [discovery(), query(), batch(finish()), batch(finish()), batch(finish())],
        judge,
        rows=[{"columns": ["Department", "n"], "rows": [["A", 1], ["B", 2]], "row_count": 2}],
    )
    out = await run(loop)
    assert judge.briefs
    assert judge.briefs[-1].draft == out.assistant_text
    assert out.review["status"] == "approved"


async def test_no_verdict_exhaustion_is_not_approval():
    judge = Judge([APPROVED] * 3)
    loop, _, _, _, _ = build([hello()], judge)
    out = await run(loop, "Hello")
    assert out.review["status"] == "exhausted"
    assert out.assistant_text == "Hello! How can I help?"
    assert len(judge.briefs) == 3  # one proposal attempt plus two delivery attempts


async def test_rejected_question_cannot_escape_through_no_verdict_repair():
    judge = Judge(
        [
            JudgeVerdict(False, "non_contextual_question", "Ask about the period.", reviewed=True),
            APPROVED,
            APPROVED,
        ]
    )
    loop, store, _, _, _ = build(
        [
            batch(call("askUser", "a1", question="Which unrelated detail?")),
            batch(call("askUser", "a2", question="Which other unrelated detail?")),
        ],
        judge,
    )
    out = await run(loop)
    assert out.review["status"] == "rejected"
    assert out.pending_question is None
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.pause_checkpoint.consumed
    assert doc.review_states["0"]["question_refusals"]


async def test_cancellation_during_delivery_review_propagates():
    entered = asyncio.Event()

    class WaitingJudge(Judge):
        async def review(self, brief):
            entered.set()
            await asyncio.Future()

    loop, store, *_ = build([hello()], WaitingJudge())
    task = asyncio.create_task(run(loop, "Hello"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    doc = await store.get_or_create_session(CREDS.session_id)
    assert not any(m.role == "assistant" for m in doc.messages)


def test_preview_excludes_ui_payload_and_is_bounded():
    from data_agent.runtime.capabilities.hydrate_trace import hydrate_response_attributes
    from data_agent.runtime.capabilities.preview import capability_preview

    card = {
        "prepared": True,
        "capability_ref": "profile",
        "_agent_evidence": {"kind": "data_widget"},
        "metadata": {
            "gql": "PRIVATE_QUERY" * 1000,
            "widgetName": "EmployeeCard",
            "ui_parameters": [{"name": "department", "values": ["Sales", "Engineering"]}],
        },
        "next_best_tools": [{"echo": "OPAQUE"}],
        "arguments": {"department": "Sales", "has_unresolved_entities": True},
        "additional_arguments": {"status": "active"},
        "resolved_entities": {"employee": [{"eecode": "JDOE", "description": "Jane Doe"}]},
    }
    preview = capability_preview(card)
    assert preview["resolved_entities"] == card["resolved_entities"]
    assert preview["arguments"] == card["arguments"]
    assert preview["additional_arguments"] == card["additional_arguments"]
    assert preview["filter_definitions"] == card["metadata"]["ui_parameters"]
    projected = json.dumps(preview)
    assert "PRIVATE_QUERY" not in projected and "OPAQUE" not in projected
    assert len(projected) < 1500
    assert card["metadata"]["gql"].startswith("PRIVATE_QUERY")
    attrs = hydrate_response_attributes(card)
    assert attrs["capability.hydrate.schema_version"] == 1
    assert attrs["capability.hydrate.has_widget_name"] is True


async def test_reused_preparation_and_legacy_replay_keep_graphql_out_of_model():
    from data_agent.runtime.capabilities.preparation import PreparationCache
    from data_agent.runtime.context.budget import _render_entry
    from data_agent.runtime.session.models import TrailEntry
    from tests.runtime.test_harness_improvements import Tool

    card = {
        "name": "profile",
        "prepared": True,
        "capability_ref": "profile",
        "metadata": {"gql": "PRIVATE_QUERY", "widgetName": "EmployeeCard"},
        "next_best_tools": [{"echo": {"opaque": "keep this"}}],
        "resolved_entities": {"employee": ["Jane Doe"]},
    }
    result = await Tool("profile", card).run()
    cache = PreparationCache(None, "session", 0, "scope", [])
    cache.record("profile", {}, result, "original")
    reused = await cache.lookup("profile", {})
    assert reused.result_full["metadata"] == card["metadata"]
    assert reused.result_full["next_best_tools"] == card["next_best_tools"]
    assert "PRIVATE_QUERY" not in json.dumps(reused.result_preview.to_doc())
    assert "Jane Doe" in json.dumps(reused.result_preview.to_doc())
    entry = TrailEntry(
        tool_call_id="original",
        tool_name="profile",
        args={},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=result.result_preview,
        result_full_ref=None,
        turn_index=0,
        ts="t",
        capability_terminal=True,
    )
    assert "PRIVATE_QUERY" not in json.dumps(_render_entry(entry, 20))
    assert "Jane Doe" in json.dumps(_render_entry(entry, 20))


async def test_failure_after_resume_does_not_reopen_consumed_question():
    loop, store, model, _, _ = build(
        [batch(call("askUser", "ask", question="Which department?"))], Judge()
    )
    assert (await run(loop)).status == "paused_ask_user"

    async def fail(*args):
        raise httpx.ConnectError("PRIVATE")

    model.send_turn = fail
    out = await loop.resume(session_id=CREDS.session_id, credentials=CREDS, answer="Sales")
    assert out.failure["dependency"] == "model"
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.pause_checkpoint.consumed
    assert doc.messages[-1].failure == out.failure


def test_partial_delivery_distinguishes_table_from_unselected_card():
    from types import SimpleNamespace

    from data_agent.runtime.composite.answer_with_table import AnswerTable
    from data_agent.runtime.loop.delivery import partial_delivery_text
    from data_agent.runtime.loop.turn_accumulators import TurnAccumulators
    from data_agent.runtime.session.models import AnalysisState, TrackedIntent

    accum = TurnAccumulators(answer_tables=[AnswerTable("SELECT 1", caption="Headcount")])
    accum._result_sql_by_call_id["q"] = "SELECT 1"
    doc = SimpleNamespace(
        analysis_state=AnalysisState(
            0,
            (
                TrackedIntent("i1", "Department headcount", "completed", "q"),
                TrackedIntent("i2", "Compensation history", "completed", "c"),
            ),
        ),
        tool_trail=[
            SimpleNamespace(tool_call_id="q", turn_index=0, capability_terminal=False),
            SimpleNamespace(
                tool_call_id="c", turn_index=0, capability_terminal=True, tool_name="history"
            ),
        ],
    )
    text = partial_delivery_text(doc, 0, accum)
    assert "Department headcount: The result is shown." in text
    assert "Compensation history: This part was not completed for display." in text
