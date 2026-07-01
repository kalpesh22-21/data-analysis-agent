"""Unit tests for FakeMCPClient (Layer 1 — no infra)."""

from __future__ import annotations

import pytest

from data_agent.runtime.mcp.client import MCPToolError, MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient


async def test_scripted_responses_returned_in_order() -> None:
    client = FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["a"], "rows": [[1]], "row_count": 1, "truncated": False},
                {"columns": ["a"], "rows": [[2]], "row_count": 1, "truncated": False},
            ]
        }
    )
    first = await client.call_tool("runQuery", {"sql": "SELECT 1"}, jwt="tok", session_id="s1")
    second = await client.call_tool("runQuery", {"sql": "SELECT 2"}, jwt="tok", session_id="s1")
    assert first["rows"] == [[1]]
    assert second["rows"] == [[2]]


async def test_scripted_exception_is_raised() -> None:
    client = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "denied")]}
    )
    with pytest.raises(MCPToolError) as exc_info:
        await client.call_tool("runQuery", {"sql": "SELECT 1"}, jwt="tok", session_id="s1")
    assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"


async def test_exhausted_script_raises_assertion_error() -> None:
    client = FakeMCPClient(scripted={"runQuery": []})
    with pytest.raises(AssertionError):
        await client.call_tool("runQuery", {}, jwt="tok", session_id="s1")


async def test_calls_are_recorded_with_credentials() -> None:
    client = FakeMCPClient(scripted={"runQuery": [{"columns": [], "rows": [], "row_count": 0, "truncated": False}]})
    await client.call_tool("runQuery", {"sql": "SELECT 1"}, jwt="secret-jwt", session_id="sess-9")
    assert len(client.calls) == 1
    recorded = client.calls[0]
    assert recorded.tool_name == "runQuery"
    assert recorded.jwt == "secret-jwt"
    assert recorded.session_id == "sess-9"


async def test_list_tools_returns_configured_specs() -> None:
    specs = [MCPToolSpec(name="listDatabases", description="d", input_schema={"type": "object"})]
    client = FakeMCPClient(tools=specs)
    result = await client.list_tools(jwt="tok", session_id="s1")
    assert result == specs


async def test_list_tools_records_credentials() -> None:
    client = FakeMCPClient(tools=[])
    await client.list_tools(jwt="secret-jwt", session_id="sess-9")
    assert len(client.list_tools_calls) == 1
    assert client.list_tools_calls[0].jwt == "secret-jwt"
    assert client.list_tools_calls[0].session_id == "sess-9"
