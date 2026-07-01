"""Model-visible tool schemas — 6 MCP tools (live-fetched) + `askUser` (design §3.2).

Rather than hand-author 6 JSON schemas that can drift from the MCP's own (a
real risk — the MCP is adopted/external, D75), this module fetches
`MCPClient.list_tools()` and translates each `MCPToolSpec.input_schema`
(JSON Schema, produced by FastMCP) into an OpenAI `type: "function"` tool
declaration, passed through **verbatim** — the MCP is the single source of
truth for parameter shape.

`askUser` is the one locally-authored tool (no MCP equivalent — a runtime
control primitive, D6/D45). Neither schema declares `session_id`, `jwt`, or
`scope` as a parameter (D5) — those never appear in any model-visible JSON;
`test_no_credential_params_leak` in
`tests/runtime/mcp/test_tool_schema.py` guards this for every schema this
module can produce, including future MCP tool additions.

Pass-B seam: this module has no dependency on `model/openai_client.py` — it
only produces plain `dict`s in the OpenAI function-tool JSON shape. Pass B's
`ModelClient` implementations consume the output of `ToolSchemaCache.get_schemas()`
directly; no interface here needs to change when that lands.
"""

from __future__ import annotations

from typing import Any

from .client import MCPClient, MCPToolSpec

ASK_USER_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "askUser",
    "description": (
        "Pause and ask the user a clarifying question, then resume with their answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "nullable": True},
        },
        "required": ["question"],
    },
}


def translate_tool_spec(tool: MCPToolSpec) -> dict[str, Any]:
    """Translate one `MCPToolSpec` into an OpenAI `type: "function"` declaration."""
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
    }


async def fetch_function_schemas(mcp_client: MCPClient) -> list[dict[str, Any]]:
    """Fetch `list_tools()` from *mcp_client*, translate, and append `askUser`.

    No caching here — see `ToolSchemaCache` for the cached/TTL'd variant used
    at runtime startup.
    """
    tools = await mcp_client.list_tools()
    schemas = [translate_tool_spec(tool) for tool in tools]
    schemas.append(ASK_USER_TOOL_SCHEMA)
    return schemas


class ToolSchemaCache:
    """Caches the translated tool-schema list, refreshed on explicit reload.

    Startup dependency (design §11 sub-decision C): the first `get_schemas()`
    call requires the MCP to be reachable. Pass B's composition root decides
    the local-dev fallback behavior (e.g. cache-on-disk) — out of scope here.
    """

    def __init__(self, mcp_client: MCPClient) -> None:
        self._mcp_client = mcp_client
        self._cache: list[dict[str, Any]] | None = None

    async def get_schemas(self, *, force_reload: bool = False) -> list[dict[str, Any]]:
        if self._cache is None or force_reload:
            self._cache = await fetch_function_schemas(self._mcp_client)
        return self._cache
