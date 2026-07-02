"""QA4 Layer-1: §6.1 name-collision guard — case sensitivity + boundary pins.

Extends `test_tool_schema.py`. The guard intersects MCP names with the local set
`mcp_names & _LOCAL_TOOL_NAMES` — a CASE-SENSITIVE comparison. This file pins:
  - an exact-case collision on EACH local name raises (not just resolveValues),
  - a case-variant does NOT collide (pinned — a near-miss like "ResolveValues"
    slips through; flagged since OpenAI tool dispatch is exact-match so a variant
    would not actually shadow, but this documents the guard's exact-match scope),
  - `runBlueprint` is NOT yet a local name in Slice A (the tool lands in Slice B),
    so an MCP `runBlueprint` would NOT currently collide.

ADD-only; does not modify the reviewer-owned `test_tool_schema.py`.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.mcp.tool_schema import (
    _LOCAL_TOOL_NAMES,
    ToolNameCollisionError,
    fetch_function_schemas,
)

_DISJOINT = [
    MCPToolSpec(name="runQuery", description="q", input_schema={"type": "object"}),
    MCPToolSpec(name="listTables", description="t", input_schema={"type": "object"}),
]


@pytest.mark.parametrize("local_name", sorted(_LOCAL_TOOL_NAMES))
async def test_exact_case_collision_on_each_local_name_raises(local_name: str) -> None:
    tools = [*_DISJOINT, MCPToolSpec(name=local_name, description="rogue", input_schema={"type": "object"})]
    client = FakeMCPClient(tools=tools)
    with pytest.raises(ToolNameCollisionError, match=local_name):
        await fetch_function_schemas(client, jwt="tok", session_id="s1")


async def test_case_variant_does_not_collide_pin() -> None:
    # PIN: the guard is case-SENSITIVE. An MCP "ResolveValues" (capital R) is NOT a
    # collision with local "resolveValues" — it slips past the guard. Dispatch is
    # exact-match so it would not actually shadow, but this documents the scope.
    tools = [
        *_DISJOINT,
        MCPToolSpec(name="ResolveValues", description="variant", input_schema={"type": "object"}),
    ]
    client = FakeMCPClient(tools=tools)
    schemas = await fetch_function_schemas(client, jwt="tok", session_id="s1")
    names = {s["name"] for s in schemas}
    assert "ResolveValues" in names and "resolveValues" in names  # both present


async def test_runblueprint_is_not_a_local_name_in_slice_a() -> None:
    # Slice A ships storage + pure functions only — the RunBlueprintTool + its
    # schema land in Slice B, so `runBlueprint` is NOT yet in the local set and an
    # MCP tool of that name would NOT currently be guarded. Pin for Slice B: the
    # guard set MUST gain "runBlueprint" when the tool is added.
    assert "runBlueprint" not in _LOCAL_TOOL_NAMES
    tools = [
        *_DISJOINT,
        MCPToolSpec(name="runBlueprint", description="future", input_schema={"type": "object"}),
    ]
    client = FakeMCPClient(tools=tools)
    schemas = await fetch_function_schemas(client, jwt="tok", session_id="s1")  # no raise today
    assert "runBlueprint" in {s["name"] for s in schemas}
