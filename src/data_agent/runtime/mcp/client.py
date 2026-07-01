"""MCPClient Protocol — transport + schema bridging to the adopted MCP (D75, §3.1/§8).

Two implementations:
  - `fake_client.FakeMCPClient` (Layer 1, scripted responses).
  - `real_client.RealMCPClient` (Layer 2/3, `mcp` SDK `streamablehttp_client`
    + `ClientSession`, one session per dispatched call per design §3.1).

`call_tool` takes `jwt`/`session_id` as explicit keyword-only arguments so the
credential-injection boundary (D5) is visible at every call site: this is the
*only* place in the runtime where the JWT and session_id are attached to an
outbound request. `ToolDispatcher.dispatch` (dispatch/tool_dispatcher.py) is
the sole caller in Pass A.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class MCPToolSpec:
    """One MCP tool description, as returned by `list_tools()` (design §3.2)."""

    name: str
    description: str
    input_schema: dict[str, Any]


class MCPToolError(Exception):
    """Raised on a tool-level denial/failure — mirrors the MCP's own
    `ToolError("[{CODE}] message")` convention (design §3.4).

    `code` is one of the seven codes classified by `dispatch/denial_mapping.py`
    (COLUMN_SCOPE_VIOLATION, SCRATCH_SESSION_VIOLATION, PARSE_FAILED_CLOSED,
    DATABASE_NOT_ALLOWED, TABLE_NOT_FOUND, CLICKHOUSE_QUERY_ERROR,
    CLICKHOUSE_UNAVAILABLE) or None if the `[{CODE}]` prefix could not be
    parsed out of the raw tool-error text (unexpected/internal MCP error).
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

        Returns the tool's parsed JSON payload on success.

        Raises:
            MCPToolError: on any tool-level denial or execution failure.
        """
        ...

    async def list_tools(self, *, jwt: str, session_id: str) -> list[MCPToolSpec]:
        """Return the live MCP's tool catalogue (for `mcp/tool_schema.py` to translate).

        The live MCP requires a valid Bearer JWT on EVERY request, including
        `tools/list` (no anonymous introspection) — so *jwt*/*session_id* are
        threaded through here exactly like `call_tool`, even though the
        resulting tool catalogue itself is scope-INDEPENDENT (every caller
        sees the same 6 tools regardless of column scope; only per-call
        results are scope-filtered). Callers that want to avoid repeating
        this fetch on every turn should cache the result — see
        `mcp/tool_schema.py::ToolSchemaCache`.
        """
        ...
