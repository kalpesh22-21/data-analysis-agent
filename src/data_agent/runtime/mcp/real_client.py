"""RealMCPClient — the real `MCPClient` over the `mcp` Python SDK.

A `ClientSession` is opened PER dispatched tool call, matching the MCP's
`stateless_http=True` posture; connection pooling is a deferred performance question,
not a correctness one. Credentials are attached only here (D5) — the `Authorization:
Bearer` and `X-Session-Id` headers are set at this boundary and nowhere else in the
runtime. Layer-2 only: its tests skip unless `MCP_TEST_URL` is set.
"""

from __future__ import annotations

import json
import re
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ._transport import side_channel_headers
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

        Finds the `[{CODE}] message` marker anywhere in *text* (FastMCP prepends its own
        wrapper ahead of it). Returns `(None, text)` when no recognizable marker is present
        at all — an unexpected or internal MCP error.
    """
    match = _TOOL_ERROR_CODE_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    return None, text


def _unwrap_fastmcp_result(structured: Any) -> Any:
    """Strip FastMCP's `{"result": <value>}` envelope from a `structuredContent`.

        The MCP spec requires `structuredContent` to be a JSON OBJECT, so FastMCP wraps any
        tool whose return type is not one under a single `"result"` key. Unwrapping HERE, at
        the transport boundary, keeps the rest of the runtime written against each tool's
        real return shape rather than a serialization detail of the server's framework.

        Narrow by construction: only a dict whose ONLY key is `"result"` is unwrapped, and
        no tool returns such an object itself, so a real payload can never be mistaken for
        the envelope.
    """
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured


class RealMCPClient:
    """`MCPClient` over the live `clickhouse-api` MCP (streamable-HTTP)."""

    def __init__(self, mcp_url: str) -> None:
        self._mcp_url = mcp_url

    def _headers(self, jwt: str, session_id: str) -> dict[str, str]:
        # The D5 binding, shared with every HTTP side channel (`_transport.py`). The
        # tool plane and the side channels sit behind the SAME `JWTAuthMiddleware`, so
        # one spelling of the header pair is the point.
        return side_channel_headers(jwt, session_id)

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
            return _unwrap_fastmcp_result(structured)
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
