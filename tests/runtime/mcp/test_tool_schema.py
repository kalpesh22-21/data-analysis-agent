"""Unit tests for mcp/tool_schema.py translation (Layer 1 — fake list_tools() payload)."""

from __future__ import annotations

import json

import pytest

from data_agent.runtime.composite.analysis_state import (
    INTENT_TAGGABLE_TOOLS,
    SUBSTANTIVE_TOOLS,
)
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.mcp.tool_schema import (
    ANSWER_WITH_TABLE_TOOL_SCHEMA,
    ASK_USER_TOOL_SCHEMA,
    GET_BLUEPRINT_TOOL_SCHEMA,
    RECORD_ASSUMPTIONS_TOOL_SCHEMA,
    RESOLVE_VALUES_TOOL_SCHEMA,
    RUN_BLUEPRINT_TOOL_SCHEMA,
    SEARCH_BLUEPRINTS_TOOL_SCHEMA,
    SEARCH_KNOWLEDGE_TOOL_SCHEMA,
    SERVES_INTENT_PARAM,
    UPDATE_ANALYSIS_STATE_TOOL_SCHEMA,
    ToolNameCollisionError,
    ToolSchemaCache,
    augment_with_serves_intent,
    fetch_function_schemas,
    translate_tool_spec,
)

# A representative fake list_tools() payload mirroring the 6 real MCP tools
# (design §0/§3.2), including a realistic FastMCP-shaped inputSchema.
_FAKE_TOOLS = [
    MCPToolSpec(
        name="listDatabases",
        description="Return the list of ClickHouse databases.",
        input_schema={"type": "object", "properties": {}},
    ),
    MCPToolSpec(
        name="listTables",
        description="Return all tables in the specified database.",
        input_schema={
            "type": "object",
            "properties": {"database": {"type": "string", "description": "The database"}},
            "required": ["database"],
        },
    ),
    MCPToolSpec(
        name="getTableSchema",
        description="Return the full column schema for the specified table.",
        input_schema={
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "table": {"type": "string"},
            },
            "required": ["database", "table"],
        },
    ),
    MCPToolSpec(
        name="sampleRows",
        description="Return a small sample of raw rows.",
        input_schema={
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "table": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["database", "table"],
        },
    ),
    MCPToolSpec(
        name="runQuery",
        description="Execute a read-only SQL query.",
        input_schema={
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "limit": {"type": "integer", "nullable": True},
            },
            "required": ["sql"],
        },
    ),
    MCPToolSpec(
        name="explainQuery",
        description="Run EXPLAIN on a SQL statement.",
        input_schema={
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    ),
]


def test_translate_tool_spec_shape() -> None:
    schema = translate_tool_spec(_FAKE_TOOLS[0])
    assert schema == {
        "type": "function",
        "name": "listDatabases",
        "description": "Return the list of ClickHouse databases.",
        "parameters": {"type": "object", "properties": {}},
    }


def test_translate_passes_input_schema_verbatim() -> None:
    """The MCP is the single source of truth for parameter shape — no re-derivation."""
    for tool in _FAKE_TOOLS:
        schema = translate_tool_spec(tool)
        assert schema["parameters"] is tool.input_schema or schema["parameters"] == tool.input_schema


async def test_fetch_function_schemas_includes_all_6_plus_runtime_tools() -> None:
    client = FakeMCPClient(tools=_FAKE_TOOLS)
    schemas = await fetch_function_schemas(client, jwt="tok", session_id="s1")
    names = {s["name"] for s in schemas}
    assert names == {
        "listDatabases",
        "listTables",
        "getTableSchema",
        "sampleRows",
        "runQuery",
        "explainQuery",
        "askUser",
        "resolveValues",
        "searchBlueprints",
        "getBlueprint",
        "searchKnowledge",
        "runBlueprint",
        "recordAssumptions",
        "answerWithTable",
        "updateAnalysisState",
    }
    assert len(schemas) == 15
    ask_user = next(s for s in schemas if s["name"] == "askUser")
    assert ask_user == ASK_USER_TOOL_SCHEMA
    resolve_values = next(s for s in schemas if s["name"] == "resolveValues")
    assert resolve_values == RESOLVE_VALUES_TOOL_SCHEMA
    assert set(resolve_values["parameters"]["required"]) == {"table", "column", "concept"}
    # The three model-facing read tools (read-tools §1), appended verbatim.
    assert next(s for s in schemas if s["name"] == "searchBlueprints") == (
        SEARCH_BLUEPRINTS_TOOL_SCHEMA
    )
    assert next(s for s in schemas if s["name"] == "getBlueprint") == GET_BLUEPRINT_TOOL_SCHEMA
    assert next(s for s in schemas if s["name"] == "searchKnowledge") == (
        SEARCH_KNOWLEDGE_TOOL_SCHEMA
    )
    assert set(SEARCH_BLUEPRINTS_TOOL_SCHEMA["parameters"]["required"]) == {"query"}
    assert set(GET_BLUEPRINT_TOOL_SCHEMA["parameters"]["required"]) == {"id"}
    assert set(SEARCH_KNOWLEDGE_TOOL_SCHEMA["parameters"]["required"]) == {"query"}
    # runBlueprint (Slice B), appended verbatim after the read tools.
    assert next(s for s in schemas if s["name"] == "runBlueprint") == RUN_BLUEPRINT_TOOL_SCHEMA
    assert set(RUN_BLUEPRINT_TOOL_SCHEMA["parameters"]["required"]) == {"id", "slot_bindings"}
    # recordAssumptions, appended verbatim after runBlueprint.
    assert next(s for s in schemas if s["name"] == "recordAssumptions") == (
        RECORD_ASSUMPTIONS_TOOL_SCHEMA
    )
    assert set(RECORD_ASSUMPTIONS_TOOL_SCHEMA["parameters"]["required"]) == {"assumptions"}
    # updateAnalysisState (Release 1), appended verbatim last — 14 -> 15.
    assert next(s for s in schemas if s["name"] == "updateAnalysisState") == (
        UPDATE_ANALYSIS_STATE_TOOL_SCHEMA
    )
    assert set(UPDATE_ANALYSIS_STATE_TOOL_SCHEMA["parameters"]["required"]) == {"intents"}


async def test_serves_intent_is_advertised_on_exactly_the_taggable_tools() -> None:
    """Call-time intent tagging adds ONE optional parameter, to exactly the three
    tools whose results 04 §A accepts as completion evidence. Advertising it
    anywhere else would be a path to nowhere: a tag on a fourth tool could never
    resolve to anything, and the model would spend calls learning that.

    Derived from `INTENT_TAGGABLE_TOOLS`, not a hand-written list, so the schema
    surface and the resolver cannot drift apart.
    """
    client = FakeMCPClient(tools=_FAKE_TOOLS)
    schemas = await fetch_function_schemas(client, jwt="tok", session_id="s1")
    assert len(schemas) == 15, "the augmentation must not add or drop a tool"

    advertised = {
        schema["name"]
        for schema in schemas
        if "serves_intent" in (schema.get("parameters") or {}).get("properties", {})
    }
    assert advertised == set(INTENT_TAGGABLE_TOOLS)
    for schema in schemas:
        properties = (schema.get("parameters") or {}).get("properties", {})
        if "serves_intent" in properties:
            assert properties["serves_intent"] == SERVES_INTENT_PARAM
            # OPTIONAL — a tag is a convenience, never a precondition for running
            # the tool.
            assert "serves_intent" not in (schema["parameters"].get("required") or [])


def test_the_augmentation_does_not_mutate_the_mcps_own_schema() -> None:
    """`MCPToolSpec.input_schema` belongs to the client and the translated list is
    CACHED by `ToolSchemaCache`, so the augmentation rebuilds rather than writing
    into either. A mutating version would leak `serves_intent` into whatever else
    shares that object — including a `force_reload` comparison."""
    spec = next(t for t in _FAKE_TOOLS if t.name == "runQuery")
    before = json.loads(json.dumps(spec.input_schema))
    augmented = augment_with_serves_intent(translate_tool_spec(spec))
    assert "serves_intent" in augmented["parameters"]["properties"]
    assert spec.input_schema == before, "the MCP's own schema object was mutated"


def test_the_augmentation_degrades_on_a_schema_shape_it_does_not_recognise() -> None:
    """The tag is an optimisation for the model, never a precondition. A schema
    whose `parameters`/`properties` are not dicts (nothing the live MCP produces,
    but the shape is not ours to assume) comes back untouched rather than raising
    at startup and taking the whole tool catalogue with it."""
    odd = MCPToolSpec(name="runQuery", description="", input_schema={"type": "string"})
    schema = translate_tool_spec(odd)
    assert augment_with_serves_intent(schema) == schema


def test_update_analysis_state_description_teaches_the_one_binding_path() -> None:
    """The tool description is re-sent every round-trip and is the model's only
    other source of the contract. There is now exactly ONE binding path — the tag
    (citation scored 0/9 live and was retired, 01a §14) — and the description has
    to say what replaced the citation escape hatch, because 04 §A's reuse
    allowance is unreachable to a model that does not know it may close a second
    intent on the same call."""
    description = UPDATE_ANALYSIS_STATE_TOOL_SCHEMA["description"]
    assert "serves_intent" in description
    assert "You never name the call" in description
    assert "IF ONE CALL ANSWERS TWO DELIVERABLES" in description
    assert "simply mark the other completed too" in description
    # The ordering caveat still applies to the tag.
    assert "has not run yet" in description


def test_update_analysis_state_schema_declares_exactly_three_item_properties() -> None:
    """01a §14: `{description}` on the first call, `{intent_id, status}` on later
    ones, and NOTHING ELSE.

    Asserted on the payload, not just the prose, because the payload is what the
    provider serialises: a model that cannot omit keys emits every property this
    object declares, so each retired one is a placeholder the runtime has to
    normalise away on every single call. The two names are also asserted absent
    from the DESCRIPTION — the model reads that as instructions, and an
    instruction to send a field the schema does not declare is how the 0/9
    citation path stayed alive for as long as it did.
    """
    items = UPDATE_ANALYSIS_STATE_TOOL_SCHEMA["parameters"]["properties"]["intents"]["items"]
    assert set(items["properties"]) == {"description", "intent_id", "status"}
    description = UPDATE_ANALYSIS_STATE_TOOL_SCHEMA["description"]
    for retired in ("evidence_tool_call_id", "reason_code", "NO_ACCESS",
                    "REQUIRED_DATA_UNAVAILABLE"):
        assert retired not in description, f"retired field still taught: {retired}"


def test_answer_with_table_declares_tables_as_the_only_designation_carrier() -> None:
    """08 §O: `answer` + `tables`, both REQUIRED, and no top-level `sql` or
    `blueprint_id`.

    The same argument as the `updateAnalysisState` slim-down above, from the same
    measurement (03 §C.3.1): the model CANNOT omit a declared key. R7 q1's live call
    was `{"answer": …, "sql": "", "blueprint_id": "bp-…", "tables": []}` — four
    declared properties carrying ONE field's worth of information, two placeholders
    and an empty array, because every declared key has to be filled with something.
    Two carriers for one fact is a second thing to fill in wrong, and the runtime
    then has to guess which one the model meant.

    Asserted on the PAYLOAD, because the payload is what the provider serialises:
    prose alone cannot stop a property from being emitted. The read path keeps
    understanding the old shape forever (`resolve_designations`), which is a
    separate guarantee with its own tests — this one is only about what the model is
    shown.
    """
    params = ANSWER_WITH_TABLE_TOOL_SCHEMA["parameters"]
    assert set(params["properties"]) == {"answer", "tables"}
    assert set(params["required"]) == {"answer", "tables"}
    # The two designation fields live on the ITEM, where they name ONE table two
    # ways and `sql` wins locally — not at the call level, where they were a second
    # parallel carrier for the whole answer.
    item = params["properties"]["tables"]["items"]
    assert set(item["properties"]) == {"sql", "blueprint_id", "caption"}
    # A single-table answer is a one-entry list, so an empty list is never the
    # intended shape. Advisory only — these schemas are non-strict — which is why
    # `resolve_designations` still has to handle `tables: []` rather than trust it.
    assert params["properties"]["tables"]["minItems"] == 1
    # And the description teaches the same thing the payload enforces.
    description = ANSWER_WITH_TABLE_TOOL_SCHEMA["description"]
    assert "EVERY TABLE GOES IN 'tables'" in description
    assert "A single-table answer is ONE entry" in description


def test_update_analysis_state_description_names_every_locking_tool() -> None:
    """Derived from `SUBSTANTIVE_TOOLS`, not from a hand-written list, so adding a
    tool to the locking set cannot silently drift out of the model-facing text.

    The description enumerated only three of the four for a while, omitting
    `runBlueprint` — the MOST likely first substantive call in a blueprint-first
    release. A model following it would run a blueprint, then declare, and take
    the NON-RETRYABLE `ANALYSIS_STATE_LATE_INIT`, after which the turn runs
    untracked and nothing errors: exactly the asymmetric silent failure 03 §E
    warns about, live on every turn because the schema is re-sent every
    round-trip.
    """
    description = UPDATE_ANALYSIS_STATE_TOOL_SCHEMA["description"]
    missing = sorted(tool for tool in SUBSTANTIVE_TOOLS if tool not in description)
    assert not missing, f"the late-init boundary text does not name {missing}"


def test_update_analysis_state_description_agrees_with_the_base_prompt() -> None:
    """Both are re-sent every round-trip, so a disagreement between them is live
    on every turn. They must name the same four tools for the same boundary."""
    from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT

    for tool in SUBSTANTIVE_TOOLS:
        assert tool in AGENT_SYSTEM_PROMPT, tool


async def test_name_collision_guard_raises_when_mcp_shadows_a_local_tool() -> None:
    """§6.1: an MCP tool whose name collides with a locally-authored runtime tool
    fails LOUD at schema-fetch — never a silent shadow in the loop's registry."""
    colliding = [
        MCPToolSpec(name="resolveValues", description="rogue", input_schema={"type": "object"}),
    ]
    client = FakeMCPClient(tools=colliding)
    with pytest.raises(ToolNameCollisionError, match="resolveValues"):
        await fetch_function_schemas(client, jwt="tok", session_id="s1")


async def test_name_collision_guard_allows_disjoint_names() -> None:
    # The real 6 MCP tools are disjoint from the 9 local names — no collision.
    client = FakeMCPClient(tools=_FAKE_TOOLS)
    schemas = await fetch_function_schemas(client, jwt="tok", session_id="s1")
    assert len(schemas) == 15


async def test_no_credential_params_leak_in_any_schema() -> None:
    """D5: no schema (MCP-derived or askUser) may declare session_id/jwt/scope."""
    client = FakeMCPClient(tools=_FAKE_TOOLS)
    schemas = await fetch_function_schemas(client, jwt="tok", session_id="s1")
    blob = json.dumps(schemas).lower()
    for forbidden in ("session_id", "jwt", "column_scope", "\"scope\""):
        assert forbidden not in blob, f"credential-shaped parameter leaked: {forbidden}"


async def test_tool_schema_cache_caches_until_reload() -> None:
    client = FakeMCPClient(tools=_FAKE_TOOLS)
    cache = ToolSchemaCache(client)

    first = await cache.get_schemas(jwt="tok", session_id="s1")
    assert len(first) == 15

    # Mutate the underlying client's tool list; without force_reload the cache
    # must not reflect the change.
    client._tools = []  # deliberate white-box test of cache staleness
    second = await cache.get_schemas(jwt="tok", session_id="s1")
    assert second == first

    # Different credentials do not bust the cache either (D5: catalogue is
    # scope-independent; the fetch is only re-triggered by force_reload).
    third = await cache.get_schemas(jwt="other-tok", session_id="s2")
    assert third == first

    reloaded = await cache.get_schemas(jwt="tok", session_id="s1", force_reload=True)
    assert reloaded == [
        ASK_USER_TOOL_SCHEMA,
        RESOLVE_VALUES_TOOL_SCHEMA,
        SEARCH_BLUEPRINTS_TOOL_SCHEMA,
        GET_BLUEPRINT_TOOL_SCHEMA,
        SEARCH_KNOWLEDGE_TOOL_SCHEMA,
        RUN_BLUEPRINT_TOOL_SCHEMA,
        RECORD_ASSUMPTIONS_TOOL_SCHEMA,
        ANSWER_WITH_TABLE_TOOL_SCHEMA,
        UPDATE_ANALYSIS_STATE_TOOL_SCHEMA,
    ]
