"""FakeMCPClient — scripted in-memory `MCPClient` double (Layer 1).

Scripted as `{tool_name: [responses...]}`; a response that is an `Exception` instance
is raised as-is, and consuming past the end raises `AssertionError` (a test-authoring
bug, not a runtime condition). Every call is recorded in `self.calls` so tests can
assert the D5 boundary: credentials DID reach the transport, and appear nowhere
downstream. The catalogue `list_tools` returns never varies by credentials.
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


@dataclass(frozen=True)
class RecordedListToolsCall:
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
        self.list_tools_calls: list[RecordedListToolsCall] = []

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

    async def list_tools(self, *, jwt: str, session_id: str) -> list[MCPToolSpec]:
        self.list_tools_calls.append(RecordedListToolsCall(jwt=jwt, session_id=session_id))
        return list(self._tools)
