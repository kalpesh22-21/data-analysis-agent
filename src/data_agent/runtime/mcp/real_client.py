"""RealMCPClient — the real `MCPClient` over the `mcp` Python SDK (design §3.1).

Wraps `streamablehttp_client` + `ClientSession` exactly as `clickhouse-api`'s
own `examples/mcp_client.py` demonstrates. A `ClientSession` is opened **per
dispatched tool call** (matches the MCP's `stateless_http=True` posture — any
replica can serve any request, so there is no server-side session affinity to
preserve). Connection pooling is a pure performance optimization deferred per
design §11 OQ-J; correctness does not depend on it.

Credentials are attached only at this transport boundary (D5): the
`Authorization: Bearer <jwt>` and `X-Session-Id: <session_id>` headers are set
here and nowhere else in the runtime.

This module is exercised at Layer 2 only (the real `clickhouse-api` MCP
container must be running); its tests
(`tests/runtime/mcp/test_real_client.py`) are skipped automatically unless
`MCP_TEST_URL` is set in the environment, so `uv run pytest` stays green with
zero infrastructure (design §8).
"""

from __future__ import annotations

import json
import re
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .client import MCPToolError, MCPToolSpec

# Matches the `[{CODE}] message` marker that `clickhouse-api`'s
# `_domain_to_tool_error` (app/mcp_server.py) puts on every ToolError message.
#
# NOT anchored to the string start: FastMCP itself prepends
# `"Error executing tool <name>: "` ahead of the raw ToolError text before it
# reaches this client (observed live: "Error executing tool runQuery:
# [COLUMN_SCOPE_VIOLATION] This query references columns outside your
# permitted scope..."), so the `[{CODE}]` marker can appear anywhere in the
# string — `_parse_tool_error_text` uses `.search`, not `.match`, to find it.
# Anchor the code to either the start of the string (bare `[CODE] msg`) or
# immediately after FastMCP's `"Error executing tool X: "` wrapper (`: [CODE] msg`),
# so a stray `[UPPER_CASE]` token elsewhere in a codeless error (e.g. a ClickHouse
# message naming `[ALL_PARTITIONS]`) is NOT misread as a domain error code.
_TOOL_ERROR_CODE_RE = re.compile(r"(?:^|:\s)\[([A-Z_]+)\]\s*(.*)", re.DOTALL)


def _parse_tool_error_text(text: str) -> tuple[str | None, str]:
    """Split a raw MCP tool-error string into `(code, message)`.

    Finds the `[{CODE}] message` marker anywhere in *text* (FastMCP prepends
    its own `"Error executing tool <name>: "` wrapper ahead of it — see the
    `_TOOL_ERROR_CODE_RE` comment). Returns `(None, text)` if no recognizable
    `[{CODE}]` marker is present at all (an unexpected/internal MCP error).
    """
    match = _TOOL_ERROR_CODE_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    return None, text


class RealMCPClient:
    """`MCPClient` over the live `clickhouse-api` MCP (streamable-HTTP)."""

    def __init__(self, mcp_url: str) -> None:
        self._mcp_url = mcp_url

    def _headers(self, jwt: str, session_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {jwt}", "X-Session-Id": session_id}

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        jwt: str,
        session_id: str,
    ) -> dict[str, Any] | list[Any]:
        headers = self._headers(jwt, session_id)
        async with streamablehttp_client(self._mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, args)

        if result.isError:
            text = result.content[0].text if result.content else "unknown MCP tool error"
            code, message = _parse_tool_error_text(text)
            raise MCPToolError(code, message)

        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured
        for block in result.content:
            if getattr(block, "type", None) == "text":
                return json.loads(block.text)
        raise MCPToolError(None, "MCP tool returned no parseable content.")

    async def list_tools(self, *, jwt: str, session_id: str) -> list[MCPToolSpec]:
        headers = self._headers(jwt, session_id)
        async with streamablehttp_client(self._mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()

        return [
            MCPToolSpec(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema or {},
            )
            for tool in result.tools
        ]
