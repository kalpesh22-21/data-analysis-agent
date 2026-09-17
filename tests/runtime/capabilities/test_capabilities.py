from __future__ import annotations

import json

import httpx
import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.capabilities.client import (
    CapabilityDefinition,
    CapabilityError,
    CapabilityServiceError,
    HttpCapabilityClient,
    ToolParam,
)
from data_agent.runtime.capabilities.prefetch import (
    prefetch_capabilities,
    render_capability_prefetch,
)
from data_agent.runtime.capabilities.resolution import CapabilityResolutionRegistry
from data_agent.runtime.capabilities.router import PrefetchRouter
from data_agent.runtime.capabilities.tools import (
    GetCapabilityTool,
    PresentCapabilityCardTool,
    SearchCapabilityToolsTool,
)
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.loop.agent_loop import TurnContext
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.tool_schema import (
    GET_CAPABILITY_TOOL_SCHEMA,
    SEARCH_CAPABILITY_TOOLS_SCHEMA,
    ToolSchemaCache,
)


class FakeMCP:
    async def list_tools(self, *, jwt: str, session_id: str) -> list[MCPToolSpec]:
        return []


def _transport() -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "cards": [
                        {
                            "id": "capability:show_employee_profile",
                            "tool_name": "show_employee_profile",
                            "kind": "data_widget",
                            "summary": "Display an employee profile.",
                            "matched_questions": ["Show an employee profile"],
                            "matched_actions": [],
                            "matched_data_points": ["Employee position"],
                        }
                    ]
                },
            )
        if request.url.path.endswith("/hydrate"):
            assert request.headers["X-End-User-Authorization"] == "Bearer jwt"
            body = json.loads(request.content)
            assert body == {
                "query": "Show Jane's profile",
                "raw_arguments": {"employees": ["Jane Doe"]},
            }
            return httpx.Response(
                200,
                json={
                    "name": "show_employee_profile",
                    "arguments": {
                        "employees": [
                            {
                                "eecode": "JDOE",
                                "description": "Jane Doe",
                                "entity_type": "employee",
                            }
                        ],
                        "has_unresolved_entities": False,
                    },
                    "metadata": {
                        "preamble_url": "ember:EmployeeCard",
                        "ui_parameters": [],
                    },
                    "parameters": [],
                    "next_best_tools": [],
                    "are_best_tools_suggestion": False,
                    "resolved_entities": {"employee": ["Jane Doe"]},
                    "additional_arguments": {},
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "name": "show_employee_profile",
                    "version": "1",
                    "kind": "data_widget",
                    "description": "Display an employee profile.",
                    "parameters": [
                        {
                            "name": "employees",
                            "description": "The employee or employees.",
                            "type": "employee",
                            "collection": True,
                            "enum": None,
                            "enumDescriptions": None,
                            "enumDisplayNames": None,
                            "default": "all",
                            "resolution": {
                                "strategy": "torch",
                                "entity_type": "employee",
                            },
                        }
                    ],
                    "metadata": {"preamble_url": "ember:EmployeeCard"},
                },
            )
        raise AssertionError("The runtime must not invoke UI capabilities")

    return httpx.MockTransport(handler)


def _client() -> HttpCapabilityClient:
    return HttpCapabilityClient(base_url="https://cap.test/v1/capabilities", transport=_transport())


@pytest.mark.asyncio
async def test_prefetch_routes_searches_and_renders_cards() -> None:
    prefetch = await prefetch_capabilities(_client(), "Show Jane's profile")

    assert prefetch.route == "data"
    assert prefetch.cards[0].kind == "data_widget"
    rendered = render_capability_prefetch(prefetch)
    assert rendered is not None
    assert "show_employee_profile" in rendered["content"]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Take me to Position Management", "action_navigation"),
        ("How many active employees are there?", "data"),
        ("Show the totals and open Position Management", "both"),
        ("I need help with payroll", "ambiguous"),
    ],
)
def test_prefetch_router_is_local(question: str, expected: str) -> None:
    assert PrefetchRouter().route(question) == expected


@pytest.mark.asyncio
async def test_action_prefetch_searches_navigation_and_action_bearing_widgets() -> None:
    class RecordingClient:
        kinds = None

        async def search(self, query, kinds, limit=5, *, end_user_jwt=None):
            self.kinds = kinds
            return []

    client = RecordingClient()
    await prefetch_capabilities(client, "Change an employee position")
    assert client.kinds == ("navigation", "data_widget")


def test_definition_translates_locked_parameter_types_for_the_model() -> None:
    definition = CapabilityDefinition(
        name="example",
        version="1",
        kind="data_widget",
        description="Example",
        parameters=(
            ToolParam("employees", "Employees", "employee", True, None, None, None, "all"),
            ToolParam("period", "Period", "string", False, ("ytd",), None, None, "ytd"),
            ToolParam("active", "Active", "boolean", False, None, None, None, "true"),
            ToolParam("range", "Range", "dateRange", False, None, None, None, None),
            ToolParam("_team", "Team", "code_induced", False, None, None, None, None),
        ),
        metadata={"preamble_url": "ember:Example"},
    )

    properties = definition.argument_schema()["properties"]

    assert properties["employees"]["type"] == "array"
    assert "resolveValues" not in properties["employees"]["description"]
    assert properties["period"]["enum"] == ["ytd"]
    assert "resolveValues" not in properties["period"]["description"]
    assert properties["active"]["default"] is True
    assert "resolveValues" not in properties["active"]["description"]
    assert properties["range"]["required"] == ["start", "end"]
    assert "resolveValues" not in properties["range"]["description"]
    assert "_team" not in properties


def test_resolution_registry_enriches_dynamic_schema_from_local_yaml(tmp_path) -> None:
    path = tmp_path / "resolution.yaml"
    path.write_text(
        "version: 1\ntypes:\n  department:\n"
        "    table: dbpcm_warehouse.department\n"
        "    column: department_name\n"
    )
    registry = CapabilityResolutionRegistry.load(path)
    parameter = ToolParam(
        "department",
        "Destination department.",
        "department",
        False,
        None,
        None,
        None,
        None,
        resolution_strategy="resolve_values",
        semantic_type="department",
    )

    schema = parameter.model_schema(registry)

    assert schema is not None
    assert "dbpcm_warehouse.department" in schema["description"]
    assert "department_name" in schema["description"]


def test_employee_code_is_translated_to_name_before_hydration(tmp_path) -> None:
    path = tmp_path / "resolution.yaml"
    path.write_text(
        "version: 1\ntypes:\n  employee_identifier:\n"
        "    table: dbpcm_warehouse.employee\n"
        "    column: employee_code\n"
        "    output_column: employee_name\n"
    )
    registry = CapabilityResolutionRegistry.load(path)
    parameter = ToolParam(
        "employees",
        "The employee or employees.",
        "employee",
        True,
        None,
        None,
        None,
        "all",
        resolution_strategy="torch",
        entity_type="employee",
    )

    schema = parameter.model_schema(registry)

    assert schema is not None
    assert "must be employee names" in schema["description"]
    assert "dbpcm_warehouse.employee" in schema["description"]
    assert "employee_code" in schema["description"]
    assert "employee_name" in schema["description"]
    assert "Never pass the employee code as a name" in schema["description"]


@pytest.mark.asyncio
async def test_navigation_hydration_does_not_forward_end_user_jwt() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "X-End-User-Authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "name": "navigate",
                "arguments": {},
                "metadata": {"preamble_url": "ember:GenericButton"},
                "parameters": [],
                "next_best_tools": [],
                "are_best_tools_suggestion": False,
                "resolved_entities": {},
                "additional_arguments": {},
            },
        )

    client = HttpCapabilityClient(
        base_url="https://cap.test/v1/capabilities", transport=httpx.MockTransport(handler)
    )
    card = await client.hydrate(
        "navigate", query="Open payroll", raw_arguments={}, end_user_jwt=None
    )

    assert card is not None
    assert card["name"] == "navigate"


@pytest.mark.asyncio
async def test_hydrate_then_prepare_nonterminal_widget_card_without_invocation() -> None:
    definitions = []
    hydrate = GetCapabilityTool(client=_client(), hydrate=definitions.append, visible_names=set())
    credentials = RuntimeCredentials(jwt="jwt", session_id="session", column_scope=frozenset())

    hydrated = await hydrate.run({"tool_name": "show_employee_profile"}, credentials)
    schema = definitions[0].tool_schema()
    execution = PresentCapabilityCardTool(client=_client(), definition=definitions[0])
    result = await execution.run(
        {"employees": ["Jane Doe"], "answer": "Jane is active."},
        credentials,
        turn=TurnContext(turn_index=0, question="Show Jane's profile"),
    )

    assert hydrated.result_full == {
        "data": [],
        "presentation": "data widget — card (EmployeeCard)",
        "found": True,
        "tool_name": "show_employee_profile",
        "kind": "data_widget",
        "ready": True,
        "presented": False,
        "next_step": (
            "Call show_employee_profile to prepare this option only if it has not already been prepared "
            "with the required arguments. Loading this definition does not display it. "
            "Reuse a previously prepared result when its arguments still fit; include its "
            "capability_ref in finalizeAnswer to display it. Omit optional arguments the "
            "user did not supply; do not substitute today's date for an unspecified date."
        ),
    }
    assert result.terminal is False
    assert schema["strict"] is False  # Optional UI filters must remain omittable at the API.
    assert "serves_intents" in schema["parameters"]["properties"]
    assert "answer" not in schema["parameters"]["properties"]
    assert schema["parameters"]["properties"]["employees"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": "The employee or employees.",
        "default": ["all"],
    }
    assert result.result_full == {
        "name": "show_employee_profile",
        "arguments": {
            "employees": [
                {
                    "eecode": "JDOE",
                    "description": "Jane Doe",
                    "entity_type": "employee",
                }
            ],
            "has_unresolved_entities": False,
        },
        "metadata": {"preamble_url": "ember:EmployeeCard", "ui_parameters": []},
        "parameters": [],
        "next_best_tools": [],
        "are_best_tools_suggestion": False,
        "resolved_entities": {"employee": ["Jane Doe"]},
        "additional_arguments": {},
        "answer": "Jane is active.",
        "prepared": True,
        "capability_ref": "show_employee_profile",
        "_agent_evidence": {
            "kind": "data_widget",
            "activation": "user_interaction_required",
            "description": "Display an employee profile.",
            "parameters": [
                {
                    "name": "employees",
                    "description": "The employee or employees.",
                    "type": "employee",
                    "collection": True,
                }
            ],
            "metadata": {"preamble_url": "ember:EmployeeCard"},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "get_definition", "hydrate"])
@pytest.mark.parametrize(
    "failure", ["unavailable", "service", "unexpected", "access", "invalid", "missing"]
)
async def test_capability_failure_routes_data_to_warehouse_without_bypassing_denials(
    monkeypatch, operation, failure
) -> None:
    client = _client()
    definitions = []
    credentials = RuntimeCredentials(jwt="jwt", session_id="session", column_scope=frozenset())
    loader = GetCapabilityTool(client=client, hydrate=definitions.append, visible_names=set())
    await loader.run({"tool_name": "show_employee_profile"}, credentials)

    async def fail(*args, **kwargs):
        if failure == "missing":
            return None
        if failure == "service":
            raise CapabilityServiceError(status_code=503, code="SERVICE_UNAVAILABLE")
        if failure == "access":
            raise CapabilityServiceError(status_code=403, code="FORBIDDEN")
        if failure == "invalid":
            raise CapabilityServiceError(status_code=422, code="INVALID_ARGUMENTS")
        if failure == "unexpected":
            raise RuntimeError("provider internals must not leak")
        raise CapabilityError("provider internals must not leak")

    monkeypatch.setattr(client, operation, fail)
    tool, args = {
        "search": (SearchCapabilityToolsTool(client=client), {"query": "paystub"}),
        "get_definition": (loader, {"tool_name": "show_employee_profile"}),
        "hydrate": (
            PresentCapabilityCardTool(client=client, definition=definitions[0]),
            {"employees": ["Venkat"]},
        ),
    }[operation]
    result = await tool.run(args, credentials)
    message = result.user_message or (result.result_full or {}).get("note", "")
    assert "provider internals" not in message
    if failure == "access":
        assert result.error_code == "CAPABILITY_FORBIDDEN"
        assert "bypass" in message
        assert "searchBlueprints" not in message
    elif failure == "invalid":
        assert "corrected" in message
        assert "searchBlueprints" not in message
    else:
        assert "searchBlueprints" in message
        assert "runQuery" in message
        assert "navigation/actions" in message
        assert "actual paystub document" in message


@pytest.mark.asyncio
async def test_unresolved_employee_prepares_ui_selection_handoff(monkeypatch) -> None:
    client = _client()
    definitions = []
    credentials = RuntimeCredentials(jwt="jwt", session_id="session", column_scope=frozenset())
    loader = GetCapabilityTool(client=client, hydrate=definitions.append, visible_names=set())
    await loader.run({"tool_name": "show_employee_profile"}, credentials)
    arguments = {
        "filters": {"employees": []},
        "unresolved_entities": {"employees": ["Venkat"]},
        "has_unresolved_entities": True,
    }
    calls = []

    async def hydrate(*args, **kwargs):
        calls.append(kwargs["raw_arguments"])
        return {"name": "show_employee_profile", "arguments": arguments}

    monkeypatch.setattr(client, "hydrate", hydrate)
    tool = PresentCapabilityCardTool(client=client, definition=definitions[0])
    result = await tool.run({"employees": ["Venkat"]}, credentials)
    assert result.status == "ok"
    assert result.terminal is False
    assert result.result_full["prepared"] is True
    assert result.result_full["capability_ref"] == "show_employee_profile"
    assert result.result_full["arguments"] == arguments
    assert calls == [{"employees": ["Venkat"]}]
    assert "finalizeAnswer" in result.result_full["next_step"]
    assert "user must select the employee there" in result.result_full["next_step"]


@pytest.mark.asyncio
async def test_card_builder_rejects_arguments_outside_hydrated_schema() -> None:
    definitions = []
    credentials = RuntimeCredentials(jwt="jwt", session_id="session", column_scope=frozenset())
    hydrate = GetCapabilityTool(client=_client(), hydrate=definitions.append, visible_names=set())
    await hydrate.run({"tool_name": "show_employee_profile"}, credentials)

    tool = PresentCapabilityCardTool(client=_client(), definition=definitions[0])
    result = await tool.run({"unknown": "value"}, credentials)

    assert result.status == "error"
    assert result.error_code == "CAPABILITY_INVALID_ARGS"


@pytest.mark.asyncio
async def test_disabled_agent_has_no_capability_schema_or_prompt() -> None:
    settings = RuntimeSettings(capability_tools_enabled=False)
    schemas = await ToolSchemaCache(FakeMCP()).get_schemas(jwt="jwt", session_id="session")

    assert "## UI capabilities" not in settings.effective_agent_system_prompt()
    assert not {"searchCapabilityTools", "getCapabilityTool"} & {
        schema["name"] for schema in schemas
    }


def test_capability_prompt_keeps_navigation_and_action_out_of_the_sql_fallback() -> None:
    prompt = RuntimeSettings(capability_tools_enabled=True).effective_agent_system_prompt()

    assert "follow the blueprint-first path" in prompt
    assert "SQL only if no blueprint fits" in prompt
    assert "rather than substituting SQL" in prompt


@pytest.mark.asyncio
async def test_enabled_discovery_schemas_are_explicitly_added() -> None:
    schemas = await ToolSchemaCache(
        FakeMCP(),
        additional_local_schemas=(SEARCH_CAPABILITY_TOOLS_SCHEMA, GET_CAPABILITY_TOOL_SCHEMA),
    ).get_schemas(jwt="jwt", session_id="session")

    names = {schema["name"] for schema in schemas}
    assert {"searchCapabilityTools", "getCapabilityTool"} <= names
