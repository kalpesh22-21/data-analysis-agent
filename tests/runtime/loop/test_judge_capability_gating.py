"""Capability-off requests retain data review without retired UI tool context."""

import json
from dataclasses import replace

import pytest

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.scope_filter import compute_scope_hash
from data_agent.runtime.loop.agent_loop import _assembled_to_canonical
from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief
from data_agent.runtime.model.conversation import restore_response_batches
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.session.models import TrailEntry, TurnMessage
from tests.runtime.loop.test_answer_judge_unit import _verdict_turn
from tests.runtime.test_harness_improvements import CREDS, build


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("site", ["exit_prose", "exit_table", "exit_capability", "ask_user"])
def test_judge_rules_and_verdict_schema_match_feature_flag(enabled, site):
    judge = AnswerJudge(ScriptedModelClient([]), token_budget=100_000, capabilities_enabled=enabled)
    system = judge.messages_for(JudgeBrief(site=site, question="Headcount?"))[0]["content"]
    schema = judge._tools_for(site)[0]["parameters"]["properties"]
    expected = enabled and site != "ask_user"
    assert ("capability_coverage_gap" in system) is expected
    assert ("capability_intent_mismatch" in schema["violation"]["enum"]) is expected
    if expected:
        assert "resolved_entities" in system
        assert "filter_definitions" in system
        assert "matching capability_ref in selected_components" in system
        assert "independently answers a requested part" in system
    elif site != "ask_user":
        assert "data_widget" not in system
        assert "capability_presented" not in system
        assert "aggregation safety" in system
        assert "getHelpCenterDocument" in system
    assert "checked before final prose review" not in system


@pytest.mark.parametrize("enabled", [False, True])
async def test_disabled_capability_slug_is_unreviewed_not_a_real_approval(enabled):
    judge = AnswerJudge(
        ScriptedModelClient(
            [_verdict_turn(False, "capability_intent_mismatch", "Remove the unrelated option.")]
        ),
        token_budget=100_000,
        capabilities_enabled=enabled,
    )
    verdict = await judge.review(JudgeBrief(site="exit_prose", question="Headcount?"))
    assert verdict.reviewed is enabled
    assert verdict.approved is not enabled


def test_disabled_agent_routes_data_without_widget_instructions():
    prompt = RuntimeSettings(capability_tools_enabled=False).effective_agent_system_prompt()
    for text in ("data widget", "UI option", "capability preparation", "getCapabilityTool"):
        assert text not in prompt
    # This is still part of the shared finalization contract.
    assert "capability_refs" in prompt
    assert "blueprint first" in prompt
    enabled = RuntimeSettings(capability_tools_enabled=True).effective_agent_system_prompt()
    assert "first look for a matching data widget" in enabled


@pytest.mark.parametrize("enabled", [False, True])
async def test_reused_session_filters_capability_evidence_and_restored_batches(enabled):
    loop, store, *_ = build([])
    assembler = ContextAssembler(store, capability_tools_enabled=enabled)
    loop._context_assembler = assembler
    scope = frozenset()
    envelope = {
        "id": "old-batch",
        "scope_hash": compute_scope_hash(scope),
        "content": "CAPABILITY_PRIVATE_CONTEXT",
        "reasoning_metadata": {"reasoning_content": "CAPABILITY_PRIVATE_CONTEXT"},
        "tool_calls": [
            {"id": ident, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for ident, name in [("load", "getCapabilityTool"), ("query", "runQuery")]
        ],
    }
    base = TrailEntry(
        turn_index=0,
        tool_call_id="load",
        tool_name="getCapabilityTool",
        args={"tool_name": "employee_card"},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref=None,
        ts="2026-09-24T00:00:00Z",
        model_response=envelope,
    )
    entries = [
        base,
        replace(
            base, tool_call_id="search", tool_name="searchCapabilityTools", model_response=None
        ),
        replace(base, tool_call_id="query", tool_name="runQuery", args={"sql": "SELECT 1"}),
        replace(
            base,
            tool_call_id="card",
            tool_name="employee_card",
            capability_terminal=True,
            model_response=None,
        ),
        # A failed preparation does not have capability_terminal set.
        replace(
            base,
            tool_call_id="failed",
            tool_name="employee_card",
            status="error",
            capability_terminal=False,
            model_response=None,
        ),
    ]
    for entry in entries:
        await store.append_trail_entry(CREDS.session_id, entry)
    await store.append_message(
        CREDS.session_id,
        TurnMessage(turn_index=0, role="user", content="Show employee details", ts=base.ts),
    )
    assembled = await assembler.assemble(CREDS.session_id, scope, current_turn_index=0)
    canonical = restore_response_batches(
        _assembled_to_canonical(assembled.messages),
        scope_hash=compute_scope_hash(scope),
        use_reasoning_metadata=True,
    )
    rendered = json.dumps(canonical)
    assert "Show employee details" in rendered
    assert "runQuery" in rendered
    assert ("employee_card" in rendered) is enabled
    assert ("CAPABILITY_PRIVATE_CONTEXT" in rendered) is enabled
    results, _, trail = await loop._judge_results(
        CREDS.session_id, 0, scope, include_all_successful=True
    )
    assert ("employee_card" in json.dumps(results)) is enabled
    assert {entry.tool_call_id for entry in trail} == (
        {entry.tool_call_id for entry in entries} if enabled else {"query"}
    )
    if not enabled:
        assert await loop._compute_turn_capability_cards(CREDS.session_id, 0) == []
    # Filtering is request-local; it must not erase the audit trail.
    assert len(await store.load_trail(CREDS.session_id)) == len(entries)
