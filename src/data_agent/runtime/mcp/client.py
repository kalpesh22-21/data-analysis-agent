"""MCPClient Protocol — transport + schema bridging to the adopted MCP (D75).

`call_tool` takes `jwt`/`session_id` as explicit keyword-only arguments so the D5
credential-injection boundary is visible at every call site: this is the ONLY place in
the runtime where the JWT and session_id are attached to an outbound request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class MCPToolSpec:
    """One MCP tool description, as returned by `list_tools()`."""

    name: str
    description: str
    input_schema: dict[str, Any]


class MCPToolError(Exception):
    """Raised on a tool-level denial/failure — mirrors the MCP's `ToolError("[{CODE}] msg")`.

        `code` is one of `denial_mapping.KNOWN_DENIAL_CODES`, or None when the `[{CODE}]`
        prefix could not be parsed out of the raw tool-error text (an unexpected or
        internal MCP error).
    """

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MCPClient(Protocol):
    """The transport seam `dispatch/tool_dispatcher.py` depends on."""

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        jwt: str,
        session_id: str,
    ) -> dict[str, Any] | list[Any]:
        """Invoke *tool_name* with *args*, injecting credentials at the transport boundary.

                Returns the tool's parsed JSON payload; raises `MCPToolError` on any tool-level
                denial or execution failure.
        """
        ...

    async def list_tools(self, *, jwt: str, session_id: str) -> list[MCPToolSpec]:
        """Return the live MCP's tool catalogue (for `mcp/tool_schema.py` to translate).

                The MCP requires a valid Bearer JWT on EVERY request, including `tools/list`,
                so credentials are threaded through here exactly like `call_tool` — but the
                catalogue itself is scope-INDEPENDENT, so callers should cache it rather than
                re-fetch per turn (see `mcp/tool_schema.py::ToolSchemaCache`).
        """
        ...
