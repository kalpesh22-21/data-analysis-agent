"""Timeout and cancellation exercise the real loop, store, and observer boundaries."""

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import ValidationError

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from data_agent.runtime.loop.reliability import MODEL_CALL_TIMEOUT_TEXT
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing
from tests.runtime.test_harness_improvements import (
    CREDS,
    SQL,
    Judge,
    Tool,
    batch,
    build,
    call,
    discovery,
    query,
    run,
)


class HangAfterScript(ScriptedModelClient):
    def __init__(self, steps):
        super().__init__(steps)
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def send_turn(self, messages, tools):
        if self.calls_made < len(self._script):
            return await super().send_turn(messages, tools)
        self.entered.set()
        try:
            await asyncio.Future()
        finally:
            self.cancelled.set()


def hanging_loop(steps=(), extra=None):
    loop, store, _, mcp, events = build([], extra=extra)
    model = HangAfterScript(list(steps))
    loop._model_client = model
    loop._model_call_timeout_seconds = 0.02
    return loop, store, model, mcp, events


@pytest.mark.parametrize("partial", [False, True])
async def test_timeout_cancels_call_finishes_and_preserves_completed_sql(partial):
    loop, store, model, _, events = hanging_loop([discovery(), query()] if partial else [])
    out = await asyncio.wait_for(run(loop), 2)
    assert out.status == "done"
    assert out.assistant_text == MODEL_CALL_TIMEOUT_TEXT
    assert out.sql_executed == ([SQL] if partial else None)
    assert model.cancelled.is_set()
    assert out.capability_cards is None
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].content == out.assistant_text
    assert doc.pause_checkpoint is None
    timeout = [p for e, p in events if e == "loop_model_call_timeout"]
    assert len(timeout) == 1
    assert timeout[0]["iteration"] == (3 if partial else 1)
    assert timeout[0]["limit"] == 0.02
    assert not any(e == "loop_turn_aborted" for e, _ in events)


async def test_timeout_closes_pending_intent_without_claiming_data_absence():
    loop, store, *_ = hanging_loop(
        [
            batch(call("updateAnalysisState", "declare", intents=[{"description": "Count staff"}])),
        ]
    )
    await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.analysis_state.intents[0].status == "blocked"
    assert doc.analysis_state.intents[0].reason_code == "ENFORCEMENT_EXHAUSTED"


async def test_timeout_is_capped_by_remaining_window():
    loop, _, _, _, events = hanging_loop()
    loop._model_call_timeout_seconds = 100
    loop._max_wall_clock_seconds = 0.03
    await asyncio.wait_for(run(loop), 2)
    payload = next(p for e, p in events if e == "loop_model_call_timeout")
    assert 0 < payload["limit"] <= 0.03


async def test_timeout_withholds_selected_table_after_unresolved_refusal():
    loop, store, *_ = hanging_loop(
        [
            discovery(),
            query(),
            batch(
                call(
                    "finalizeAnswer",
                    "answer",
                    answer="There are 999 employees.",
                    tables=[{"result_id": "q", "caption": "Staff"}],
                    capability_refs=[],
                    evidence=["q"],
                )
            ),
        ]
    )
    loop._answer_judge = Judge([JudgeVerdict(False, "contradicts_result", "Use 120.", True)])
    out = await run(loop)
    assert MODEL_CALL_TIMEOUT_TEXT in out.assistant_text
    assert out.answer_tables is None
    assert "999" not in out.assistant_text
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].content == out.assistant_text
    assert doc.review_states["0"]["violation"] == "contradicts_result"


@pytest.mark.parametrize("refused", [False, True])
async def test_timeout_never_promotes_prepared_or_refused_capability(refused):
    card = {
        "name": "show_card",
        "prepared": True,
        "capability_ref": "show_card",
        "_agent_evidence": {"kind": "data_widget", "description": "Staff profile"},
    }
    steps = [batch(call("show_card", "card"))]
    if refused:
        steps.append(
            batch(
                call(
                    "finalizeAnswer",
                    "answer",
                    answer="Here is the paystub.",
                    tables=[],
                    capability_refs=["show_card"],
                    evidence=["card"],
                )
            )
        )
    loop, _, _, _, events = hanging_loop(steps, extra={"show_card": Tool("show_card", card)})
    loop._answer_judge = Judge(
        [JudgeVerdict(False, "capability_intent_mismatch", "Wrong option.", True)]
    )
    out = await run(loop)
    assert MODEL_CALL_TIMEOUT_TEXT in out.assistant_text
    assert out.capability_cards is None
    assert sum(e == "loop_model_call_timeout" for e, _ in events) == 1


@pytest.mark.parametrize("resume", [False, True])
async def test_external_cancel_propagates_once_without_persisting_final_answer(resume):
    steps = (
        [batch(call("askUser", "ask", question="Which period?", options=["Current", "Previous"]))]
        if resume
        else []
    )
    loop, store, model, _, events = hanging_loop(steps)
    loop._model_call_timeout_seconds = 5
    if resume:
        assert (await run(loop)).status == "paused_ask_user"
        operation = loop.resume(session_id=CREDS.session_id, credentials=CREDS, answer="Current")
    else:
        operation = run(loop)
    before = len((await store.get_or_create_session(CREDS.session_id)).messages)
    task = asyncio.create_task(operation)
    await asyncio.wait_for(model.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert model.cancelled.is_set()
    assert [p for e, p in events if e == "loop_turn_aborted"] == [
        {"phase": "model_call", "iteration": 1}
    ]
    assert not any(e == "loop_model_call_timeout" for e, _ in events)
    doc = await store.get_or_create_session(CREDS.session_id)
    assert all(m.role != "assistant" for m in doc.messages[before:])


async def test_cancel_at_dispatch_prevents_remaining_batch_calls():
    entered = asyncio.Event()

    class BlockingTool(Tool):
        async def run(self, *args, **kwargs):
            entered.set()
            await asyncio.Future()

    loop, store, _, mcp, events = hanging_loop(
        [
            batch(
                call("searchBlueprints", "blocked", query="staff"),
                call("runQuery", "never", sql=SQL),
            )
        ],
        extra={"searchBlueprints": BlockingTool("searchBlueprints")},
    )
    task = asyncio.create_task(run(loop))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not mcp.calls
    assert not await store.load_trail(CREDS.session_id)
    assert [p["phase"] for e, p in events if e == "loop_turn_aborted"] == ["dispatch"]


async def test_cancel_requested_by_immediate_tool_lands_before_next_dispatch():
    class CancelTool(Tool):
        async def run(self, *args, **kwargs):
            asyncio.current_task().cancel()
            return await super().run(*args, **kwargs)

    loop, _, _, mcp, events = hanging_loop(
        [
            batch(
                call("searchBlueprints", "cancel", query="staff"),
                call("runQuery", "never", sql=SQL),
            )
        ],
        extra={"searchBlueprints": CancelTool("searchBlueprints")},
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(run(loop))
    assert not mcp.calls
    assert sum(e == "loop_turn_aborted" for e, _ in events) == 1
    assert not any(e == "loop_turn_done" for e, _ in events)


async def test_resumed_turn_timeout_finishes_without_another_pause():
    loop, store, *_ = hanging_loop(
        [batch(call("askUser", "ask", question="Which period?", options=["Current", "Previous"]))]
    )
    assert (await run(loop)).status == "paused_ask_user"
    outcome = await loop.resume(session_id=CREDS.session_id, credentials=CREDS, answer="Current")
    assert outcome.status == "done"
    assert outcome.assistant_text == MODEL_CALL_TIMEOUT_TEXT
    assert outcome.pending_question is None
    assert (await store.get_or_create_session(CREDS.session_id)).messages[
        -1
    ].content == outcome.assistant_text


async def test_cancelled_span_closes_with_safe_error_reason():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with pytest.raises(asyncio.CancelledError):
        with tracing.span(
            provider.get_tracer("test"),
            "cancelled",
            tracing.OpenInferenceSpanKindValues.CHAIN,
            record_exception=False,
        ):
            raise asyncio.CancelledError("sensitive provider detail")
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["reason"] == "cancelled"
    assert not span.events
    assert "sensitive" not in span.status.description
    provider.shutdown()


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_timeout_config_rejects_invalid_limits(value):
    with pytest.raises(ValidationError):
        RuntimeSettings(_env_file=None, model_call_timeout_seconds=value)
