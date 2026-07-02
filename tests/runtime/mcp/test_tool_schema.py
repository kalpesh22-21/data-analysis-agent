"""Unit tests for mcp/tool_schema.py translation (Layer 1 — fake list_tools() payload)."""

from __future__ import annotations

import json

from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.mcp.tool_schema import (
    ASK_USER_TOOL_SCHEMA,
    RESOLVE_VALUES_TOOL_SCHEMA,
    ToolSchemaCache,
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


async def test_fetch_function_schemas_includes_all_6_plus_ask_user_plus_resolve_values() -> None:
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
    }
    ask_user = next(s for s in schemas if s["name"] == "askUser")
    assert ask_user == ASK_USER_TOOL_SCHEMA
    resolve_values = next(s for s in schemas if s["name"] == "resolveValues")
    assert resolve_values == RESOLVE_VALUES_TOOL_SCHEMA
    assert set(resolve_values["parameters"]["required"]) == {"table", "column", "concept"}


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
    assert len(first) == 8

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
    assert reloaded == [ASK_USER_TOOL_SCHEMA, RESOLVE_VALUES_TOOL_SCHEMA]
