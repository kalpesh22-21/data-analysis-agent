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
from tests.runtime.final_answer import answer_arguments
from tests.runtime.observability.test_tracing import _observed_attributes
from tests.runtime.test_harness_improvements import CREDS, batch, build, call, discovery, query, run

DECLINE = "Weather is outside my scope. I can help with HR, payroll, and product-usage questions."


def finish(answer=DECLINE, evidence=None):
    return batch(
        call("finalizeAnswer", "final", **answer_arguments(takeaway=answer, tables=[], capability_refs=[], evidence=evidence or []))
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
                    "declineOutOfScope",
                    "scope",
                    request="weather  in\nParis",
                    serves_intents=["i2"],
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
    receipt = next(e for e in doc.tool_trail if e.tool_call_id == "scope")
    assert receipt.serves_intents == ("i2",)
    assert receipt.error_code == "OUT_OF_SCOPE_REQUEST"
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
    assert info.kind is DenialKind.GATE and not info.retryable and info.enforcement
    assert info.code in ENFORCEMENT_ERROR_CODES
    payload = {"rule": SCOPE_RULE, "site": "agent_scope_declaration", "request": "SECRET"}
    attributes = _observed_attributes(SCOPE_EVENT, payload)
    progress = to_progress_event(SCOPE_EVENT, payload)
    for shape in (attributes, progress.shape):
        assert shape["rule"] == SCOPE_RULE and shape["site"] == "agent_scope_declaration"
        assert "SECRET" not in json.dumps(shape)


@pytest.mark.parametrize("tags", [[], ["i2"], ["i1", "i2"]])
async def test_direct_scope_call_accepts_schema_tags_and_whitespace(tags):
    tool = DeclineOutOfScopeTool()
    args = {"request": "weather in Paris", "serves_intents": tags}
    result = await tool.run(args, CREDS, TurnContext(0, "Count employees; WEATHER\n in   Paris"))
    assert result.error_code == "OUT_OF_SCOPE_REQUEST"
    assert result.retryable is False
    assert args == {"request": "weather in Paris", "serves_intents": tags}


@pytest.mark.parametrize(
    "extra", [{"serves_intents": "i2"}, {"serves_intents": [3]}, {"unknown": 1}]
)
async def test_scope_call_rejects_malformed_optional_or_unknown_args(extra):
    result = await DeclineOutOfScopeTool().run(
        {"request": "weather", **extra}, CREDS, TurnContext(0, "weather")
    )
    assert result.error_code == "INVALID_TOOL_ARGUMENTS"
    assert result.retryable is True  # Correct malformed arguments; do not retry a valid refusal.



# --- actions: declined when nothing can prepare them, prepared when UI options exist ---


def test_the_prompt_says_actions_are_declined_without_ui_options_and_prepared_with_them():
    """A live run answered "Fire the top 15 employees" by asking which ranking to use."""
    from data_agent.runtime.config import RuntimeSettings
    from data_agent.runtime.prompts import (
        ACTIONS_UNAVAILABLE_SYSTEM_PROMPT,
        CAPABILITY_TOOLS_SYSTEM_PROMPT,
    )

    off = RuntimeSettings(capability_tools_enabled=False).effective_agent_system_prompt()
    assert ACTIONS_UNAVAILABLE_SYSTEM_PROMPT in off and CAPABILITY_TOOLS_SYSTEM_PROMPT not in off
    assert "declineOutOfScope(reason=action_not_supported)" in off
    assert "action intent as blocked" in off
    on = RuntimeSettings(capability_tools_enabled=True).effective_agent_system_prompt()
    assert ACTIONS_UNAVAILABLE_SYSTEM_PROMPT not in on
    assert '"Terminate an employee"' in on and "an action is not out of scope" in on


async def test_an_action_is_declined_honestly_when_no_ui_options_exist():
    from data_agent.runtime.composite.scope_refusal import ACTION_RULE

    events = []
    tool = DeclineOutOfScopeTool(observer=lambda e, p: events.append((e, p)))
    result = await tool.run({"request": "Fire the top 15 employees", "reason": "action_not_supported"},
                            CREDS, TurnContext(0, "Fire the top 15 employees."))
    assert result.error_code == "NOT_SUPPORTED_HERE" and result.retryable is False
    assert "cannot perform or prepare actions" in result.user_message
    assert "outside HR" not in result.user_message
    assert "choosing them is part of the action" in result.denial_detail
    assert "It is not answer evidence" in result.denial_detail
    assert [e for e in events if e[0] == SCOPE_EVENT] == [
        (SCOPE_EVENT, {"rule": ACTION_RULE, "site": "agent_scope_declaration"})]


async def test_an_action_is_not_declined_while_ui_options_exist():
    events = []
    tool = DeclineOutOfScopeTool(observer=lambda e, p: events.append((e, p)), actions_available=True)
    result = await tool.run({"request": "Fire the top 15 employees", "reason": "action_not_supported"},
                            CREDS, TurnContext(0, "Fire the top 15 employees."))
    assert result.error_code == "INVALID_TOOL_ARGUMENTS" and result.retryable is True
    assert "searchCapabilityTools" in result.user_message
    assert not any(e[0] == SCOPE_EVENT for e in events)
    # An out-of-domain part is still declined normally with UI options on.
    weather = await tool.run({"request": "weather"}, CREDS, TurnContext(0, "weather"))
    assert weather.error_code == "OUT_OF_SCOPE_REQUEST"


async def test_an_unknown_decline_reason_is_rejected():
    result = await DeclineOutOfScopeTool().run({"request": "weather", "reason": "other"}, CREDS,
                                               TurnContext(0, "weather"))
    assert result.error_code == "INVALID_TOOL_ARGUMENTS"


@pytest.mark.parametrize("actions_available", [False, True])
async def test_the_decline_tool_is_shaped_by_the_capability_flag_not_left_to_the_model(actions_available):
    """Without UI capability tools an action is declined, so the tool offers that reason and
    says so; with them it is prepared, so neither appears for the model to misread."""
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from data_agent.runtime.mcp.tool_schema import ToolSchemaCache
    from tests.runtime.mcp.test_tool_schema import _FAKE_TOOLS

    cache = ToolSchemaCache(FakeMCPClient(tools=_FAKE_TOOLS), actions_available=actions_available,
                            product_documentation_available=True)
    decline = next(s for s in await cache.get_schemas(jwt="tok", session_id="s1")
                   if s["name"] == "declineOutOfScope")
    offered = "reason" in decline["parameters"]["properties"]
    worded = "action_not_supported" in decline["description"]
    assert offered is worded is (not actions_available)
    if offered:
        assert decline["parameters"]["properties"]["reason"]["enum"] == ["outside_domain", "action_not_supported"]
