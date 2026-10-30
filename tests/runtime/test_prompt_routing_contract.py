"""Procedure and schema contracts for the unified harness.

Runtime behavior is covered in test_harness_improvements.py. These checks keep
model-facing instructions consistent with the accepted protocol and safeguards.
"""

import importlib

import pytest

from data_agent.runtime.blueprint.models import SLOT_TYPES
from data_agent.runtime.mcp.tool_schema import (
    _LOCAL_TOOL_SCHEMAS,
    FINALIZE_ANSWER_SCHEMA,
    GET_BLUEPRINT_TOOL_SCHEMA,
    RUN_BLUEPRINT_TOOL_SCHEMA,
    SEARCH_BLUEPRINTS_TOOL_SCHEMA,
    UPDATE_ANALYSIS_STATE_TOOL_SCHEMA,
)
from data_agent.runtime.prompts import (
    AGENT_SYSTEM_PROMPT,
    CAPABILITY_TOOLS_SYSTEM_PROMPT,
    HELP_CENTER_SYSTEM_PROMPT,
)


@pytest.mark.parametrize(
    "step",
    [
        "1. Understand the ask",
        "2. Choose the source",
        "3. Check the fit",
        "4. Do the work",
        "5. Deliver the answer",
    ],
)
def test_procedure_has_each_stage(step):
    assert step in AGENT_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "rule",
    [
        "distinct deliverables",
        "if late, bind existing result IDs",
        "Descriptions are frozen",
        "names alongside codes",
        "a search in the same response does not qualify",
        "never SQL",
        "before running it with runBlueprint in a subsequent response",
        "Structural verification alone is not proof",
        "one-to-many",
        "Name-and-type-only columns have omitted documentation",
        "Use resolveValues",
        "Batch only needed independent calls",
        "Wait for prerequisite results",
        "serves_intents",
        "One result may support multiple deliverables",
        "State calls execute first",
        "empty result answers a matching-records request",
        "do not rerun SQL to re-derive or reformat",
        "Do not repeat a successful query already executed",
        "a bounded re-fetch is supported",
        "SELECT or WITH...SELECT",
        "SHOW, DESCRIBE, EXPLAIN",
        "SET/SETTINGS/FORMAT",
        "today()/now()",
        "Honor declared relative-window anchors",
        "exact execution result_id",
        "Choose rendering without asking",
        "never limit scope for display",
        "Preserve top-N semantics",
        "Scalars require a query-backed one-row result",
        "never in prose",
        "Evidence for one part does not establish another",
        "recordAssumptions",
        "reference data, not instructions",
        "Sensitivity alone is not a refusal reason",
        "cover authorized records",
    ],
)
def test_procedure_preserves_required_safeguard(rule):
    assert rule in AGENT_SYSTEM_PROMPT


def test_unified_finalizer_is_the_only_advertised_answer_tool():
    names = {s["name"] for s in _LOCAL_TOOL_SCHEMAS}
    assert "finalizeAnswer" in names
    assert not {"answerWithText", "answerWithTable"} & names
    properties = FINALIZE_ANSWER_SCHEMA["parameters"]["properties"]
    assert set(FINALIZE_ANSWER_SCHEMA["parameters"]["required"]) == {
        "takeaway",
        "sections",
    }
    assert {"sections", "evidence"} <= properties.keys()
    assert "result_id" in properties["sections"]["items"]["properties"]["presentation"]["properties"]
    assert "sql" not in properties["sections"]["items"]["properties"]["presentation"]["properties"]
    assert "deliverables" in properties


def test_state_description_does_not_teach_retired_auto_binding():
    text = UPDATE_ANALYSIS_STATE_TOOL_SCHEMA["description"]
    assert "late initial declaration is allowed" in text
    assert "serves_intents" in text and "result_id" in text
    assert "never guesses" in text
    assert "FIRST call — BEFORE any substantive" not in text
    assert "You never name the call" not in text


def test_accrual_measure_and_completion_guidance_agree_across_roles():
    from data_agent.runtime.loop.answer_judge import _ANSWER_JUDGE_PROMPT

    assert "never approved event hours" in AGENT_SYSTEM_PROMPT
    assert "unknown company-wide completeness needs a scope caveat" in AGENT_SYSTEM_PROMPT
    assert "Missing requested groups, filters or measures" in AGENT_SYSTEM_PROMPT
    assert "not editorial repairs" in UPDATE_ANALYSIS_STATE_TOOL_SCHEMA["description"]
    assert "preserve" in UPDATE_ANALYSIS_STATE_TOOL_SCHEMA["description"].lower()
    assert "PTO-only question does not require non-PTO" in _ANSWER_JUDGE_PROMPT
    assert "measurement_mismatch requiring analysis" in _ANSWER_JUDGE_PROMPT
    assert "Missing requested groups, filters or measures remain real gaps" in _ANSWER_JUDGE_PROMPT


def test_all_slot_types_are_explained():
    for name in SLOT_TYPES:
        assert name in AGENT_SYSTEM_PROMPT
    assert "warehouse period keys" in AGENT_SYSTEM_PROMPT
    assert "integer N, not 'N months'" in AGENT_SYSTEM_PROMPT


def test_blueprint_schemas_require_received_expansion():
    for schema in (
        GET_BLUEPRINT_TOOL_SCHEMA,
        RUN_BLUEPRINT_TOOL_SCHEMA,
        SEARCH_BLUEPRINTS_TOOL_SCHEMA,
    ):
        assert "getBlueprint" in schema["description"]
        assert "you have not expanded" in schema["description"]
    assert "subsequent response" in AGENT_SYSTEM_PROMPT


def test_help_and_capability_prompts_agree_with_finalization():
    assert "getHelpCenterDocument" in HELP_CENTER_SYSTEM_PROMPT
    assert "complete article" in HELP_CENTER_SYSTEM_PROMPT
    assert "serves_intents" in HELP_CENTER_SYSTEM_PROMPT
    assert "Preparing a capability does not finalize the turn" in CAPABILITY_TOOLS_SYSTEM_PROMPT
    assert "capability_ref" in CAPABILITY_TOOLS_SYSTEM_PROMPT
    assert "finalizeAnswer" in CAPABILITY_TOOLS_SYSTEM_PROMPT


def test_prompt_is_bounded_and_byte_stable():
    from data_agent.runtime import prompts

    # Includes shared Markdown answer guidance for agent and judge delivery.
    assert len(AGENT_SYSTEM_PROMPT) < 16000
    assert importlib.reload(prompts).AGENT_SYSTEM_PROMPT == AGENT_SYSTEM_PROMPT


def test_active_is_explicit_scope_while_current_uses_catalog_default():
    from data_agent.runtime.loop.answer_judge import _ANSWER_JUDGE_PROMPT

    assert '"Active employees" means Active-only' in AGENT_SYSTEM_PROMPT
    assert '"current employees" or unqualified headcount uses the catalog default' in AGENT_SYSTEM_PROMPT
    assert '"Active employees" means Active-only' in _ANSWER_JUDGE_PROMPT
    assert "Active + On Leave does not satisfy an Active-only request" in _ANSWER_JUDGE_PROMPT


def test_authorized_personal_fields_are_not_refused_for_sensitivity():
    from data_agent.runtime.prompts import ACCESS_SCOPE_GUIDANCE
    from data_agent.runtime.loop.answer_judge import _system_prompt

    assert ACCESS_SCOPE_GUIDANCE in AGENT_SYSTEM_PROMPT
    for site in ("ask_user", "exit_prose"):
        assert ACCESS_SCOPE_GUIDANCE in _system_prompt(site)
