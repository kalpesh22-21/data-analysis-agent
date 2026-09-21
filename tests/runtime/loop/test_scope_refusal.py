"""B8: scope receipts, repair and mixed-request completion without a judge."""

import json

import pytest

from data_agent.learning.summary.loader import ENFORCEMENT_ERROR_CODES
from data_agent.runtime.composite.analysis_state import (
    classify_block_evidence,
    validate_completion_evidence,
)
from data_agent.runtime.composite.scope_refusal import (
    SCOPE_EVENT,
    SCOPE_RULE,
    DeclineOutOfScopeTool,
)
from data_agent.runtime.dispatch.denial_mapping import DenialKind, classify_denial
from data_agent.runtime.loop.agent_loop import TurnContext
from data_agent.runtime.observability.progress import to_progress_event
from tests.runtime.observability.test_tracing import _observed_attributes
from tests.runtime.test_harness_improvements import CREDS, batch, build, call, discovery, query, run

DECLINE = "Weather is outside my scope. I can help with HR, payroll, and product-usage questions."


def finish(answer=DECLINE, evidence=None):
    return batch(
        call(
            "finalizeAnswer",
            "final",
            answer=answer,
            tables=[],
            capability_refs=[],
            evidence=evidence or [],
        )
    )


def scope_loop(steps):
    tool = DeclineOutOfScopeTool()
    result = build(steps, extra={"declineOutOfScope": tool})
    tool._observer = result[0]._observer
    return result


async def test_weather_scope_receipt_and_same_session_in_scope_follow_up():
    loop, store, model, mcp, events = scope_loop(
        [
            batch(call("declineOutOfScope", "scope", request="What's the weather in Paris?")),
            finish(),
            discovery(),
            query(),
            finish("There are 120 active employees.", ["q"]),
        ]
    )
    first = await run(loop, "What's the weather in Paris?")
    assert first.status == "done" and first.assistant_text == DECLINE
    assert mcp.calls == []
    trail = await store.load_trail(CREDS.session_id)
    receipt = next(e for e in trail if e.tool_call_id == "scope")
    assert receipt.status == "error" and receipt.error_code == "OUT_OF_SCOPE_REQUEST"
    assert "outside HR, payroll, and product" in receipt.denial_detail
    assert receipt.result_preview is None
    assert classify_block_evidence("scope", trail, 0) == "OUT_OF_SCOPE_REQUEST"
    assert validate_completion_evidence("scope", trail, 0) is not None
    assert [(e, p) for e, p in events if e == SCOPE_EVENT] == [
        (SCOPE_EVENT, {"rule": SCOPE_RULE, "site": "agent_scope_declaration"})
    ]
    second = await run(loop, "How many active employees do we have?")
    assert second.status == "done" and "120" in second.assistant_text
    assert mcp.calls
    assert all(m.get("tool_call_id") != "scope" for m in model.calls[2].messages)
    assert len([e for e, p in events if e == SCOPE_EVENT]) == 1


async def test_mixed_request_finishes_supported_intent_and_blocks_only_scope_part():
    loop, store, _, _, events = scope_loop(
        [
            batch(
                call(
                    "updateAnalysisState",
                    "declare",
                    intents=[
                        {"description": "Active employee count"},
                        {"description": "Weather in Paris"},
                    ],
                )
            ),
            batch(
                call(
                    "declineOutOfScope", "scope", request="weather in Paris", serves_intents=["i2"]
                )
            ),
            discovery(),
            batch(
                call(
                    "runQuery",
                    "q",
                    sql="SELECT count() AS n FROM hr.employee",
                    serves_intents=["i1"],
                )
            ),
            batch(
                call(
                    "updateAnalysisState",
                    "close",
                    intents=[
                        {"intent_id": "i1", "status": "completed"},
                        {"intent_id": "i2", "status": "blocked"},
                    ],
                )
            ),
            finish("There are 120 active employees. " + DECLINE, ["q"]),
        ]
    )
    result = await run(loop, "How many active employees, and what is the weather in Paris?")
    assert (
        result.status == "done"
        and "120" in result.assistant_text
        and "scope" in result.assistant_text
    )
    doc = await store.get_or_create_session(CREDS.session_id)
    assert [(i.status, i.reason_code) for i in doc.analysis_state.intents] == [
        ("completed", None),
        ("blocked", "OUT_OF_SCOPE_REQUEST"),
    ]
    assert doc.analysis_state.intents[1].evidence_tool_call_id == "scope"
    assert len([e for e, p in events if e == SCOPE_EVENT]) == 1


async def test_existing_deterministic_scope_gate_uses_same_code_and_repairs():
    loop, store, _, _, events = scope_loop(
        [
            finish("Here is your poem about the moon."),
            finish("I can't create poems. I can help with HR, payroll, and product usage."),
        ]
    )
    result = await run(loop, "Write a poem about the moon.")
    assert result.status == "done" and "can't" in result.assistant_text
    trail = await store.load_trail(CREDS.session_id)
    refusal = next(e for e in trail if e.error_code == "OUT_OF_SCOPE_REQUEST")
    assert "scope" in refusal.denial_detail
    assert [(e, p) for e, p in events if e == SCOPE_EVENT] == [
        (SCOPE_EVENT, {"rule": SCOPE_RULE, "site": "answer_scope_rule"})
    ]


@pytest.mark.parametrize(
    "question",
    [
        "How do I configure payroll in Paycom?",
        "Show employee schedules.",
    ],
)
async def test_unavailable_product_or_data_is_not_a_scope_refusal(question):
    loop, store, _, _, events = scope_loop(
        [
            finish(
                "I couldn't verify this information because the required service is unavailable."
            ),
        ]
    )
    result = await run(loop, question)
    assert result.status == "done"
    assert not any(e == SCOPE_EVENT for e, p in events)
    assert not any(
        e.error_code == "OUT_OF_SCOPE_REQUEST" for e in await store.load_trail(CREDS.session_id)
    )


async def test_scope_channel_rejects_invented_request_without_scope_event():
    events = []
    tool = DeclineOutOfScopeTool(observer=lambda e, p: events.append((e, p)))
    result = await tool.run(
        {"request": "invented weather request"}, CREDS, TurnContext(0, "Count employees")
    )
    assert result.error_code == "INVALID_TOOL_ARGUMENTS"
    assert not any(e == SCOPE_EVENT for e, p in events)


def test_scope_classification_and_shape_only_telemetry():
    info = classify_denial("OUT_OF_SCOPE_REQUEST")
    assert info.kind is DenialKind.GATE and info.retryable and info.enforcement
    assert info.code in ENFORCEMENT_ERROR_CODES
    payload = {"rule": SCOPE_RULE, "site": "agent_scope_declaration", "request": "SECRET"}
    attributes = _observed_attributes(SCOPE_EVENT, payload)
    progress = to_progress_event(SCOPE_EVENT, payload)
    for shape in (attributes, progress.shape):
        assert shape["rule"] == SCOPE_RULE and shape["site"] == "agent_scope_declaration"
        assert "SECRET" not in json.dumps(shape)
