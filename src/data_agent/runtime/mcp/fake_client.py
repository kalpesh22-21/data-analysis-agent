"""FakeMCPClient — scripted in-memory MCPClient double (Layer 1, design §8).

Scripted as `{tool_name: [responses...]}` where each response is either a
plain dict/list (returned as-is) or an `Exception` instance (raised as-is —
typically an `MCPToolError`). Responses are consumed in order per tool name;
calling a tool more times than it has scripted responses raises
`AssertionError` (a test-authoring bug, not a runtime condition).

Every call is recorded in `self.calls` (tool_name, args, jwt, session_id) so
tests can assert the credential-injection boundary (D5): the JWT/session_id
DID reach this transport boundary, even though they never appear in any
`ToolResult`/`TrailEntry`/model-facing structure produced downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import MCPToolSpec


@dataclass(frozen=True)
class RecordedCall:
    tool_name: str
    args: dict[str, Any]
    jwt: str
    session_id: str


class FakeMCPClient:
    """Layer-1 `MCPClient` double with scripted, ordered responses per tool."""

    def __init__(
        self,
        scripted: dict[str, list[Any]] | None = None,
        tools: list[MCPToolSpec] | None = None,
    ) -> None:
        self._scripted: dict[str, list[Any]] = {k: list(v) for k, v in (scripted or {}).items()}
        self._tools = list(tools) if tools is not None else []
        self.calls: list[RecordedCall] = []

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        jwt: str,
        session_id: str,
    ) -> dict[str, Any] | list[Any]:
        self.calls.append(
            RecordedCall(tool_name=tool_name, args=dict(args), jwt=jwt, session_id=session_id)
        )
        queue = self._scripted.get(tool_name)
        if not queue:
            raise AssertionError(
                f"FakeMCPClient has no more scripted responses for tool {tool_name!r}."
            )
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def list_tools(self) -> list[MCPToolSpec]:
        return list(self._tools)
