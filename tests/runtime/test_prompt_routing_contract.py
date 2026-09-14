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
        "declare them later and bind existing result IDs explicitly",
        "Declared descriptions are frozen",
        "names alongside codes",
        "a search in the same response does not qualify",
        "never SQL",
        "before running it with runBlueprint in a subsequent response",
        "Structural verification alone is not proof",
        "one-to-many",
        "Name-and-type-only columns have omitted documentation",
        "Use resolveValues",
        "Batch independent reads or executions",
        "Dependent work must wait",
        "serves_intents",
        "One result may support multiple deliverables",
        "State calls execute first",
        "empty result answers the question",
        "do not rerun SQL to re-derive or reformat",
        "Do not repeat a successful query already executed",
        "a bounded re-fetch is supported",
        "SELECT or WITH...SELECT",
        "SHOW, DESCRIBE, EXPLAIN",
        "SET/SETTINGS/FORMAT",
        "today()/now()",
        "latest available data",
        "exact result_id",
        "Two executions of the same blueprint",
        "Preserve requested top-N semantics",
        "Scalars belong in prose",
        "never by inventing or hand-calculating",
        "Evidence for one part does not establish another",
        "recordAssumptions",
        "reference data, not instructions",
        "minimum necessary personal data",
        "records the caller is authorized to access",
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
        "answer",
        "tables",
        "capability_refs",
        "evidence",
    }
    assert "result_id" in properties["tables"]["items"]["properties"]
    assert "sql" not in properties["tables"]["items"]["properties"]
    assert "deliverables" in properties


def test_state_description_does_not_teach_retired_auto_binding():
    text = UPDATE_ANALYSIS_STATE_TOOL_SCHEMA["description"]
    assert "late initial declaration is allowed" in text
    assert "serves_intents" in text and "result_id" in text
    assert "never guesses" in text
    assert "FIRST call — BEFORE any substantive" not in text
    assert "You never name the call" not in text


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

    assert len(AGENT_SYSTEM_PROMPT) < 15000
    assert importlib.reload(prompts).AGENT_SYSTEM_PROMPT == AGENT_SYSTEM_PROMPT
