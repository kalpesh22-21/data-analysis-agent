"""RuntimeCredentials — the model-invisible per-turn credential bundle (D5).

Constructed once per inbound turn request and threaded down as an explicit argument
(app.py -> AgentLoop.run -> ToolDispatcher.dispatch -> MCPClient.call_tool), never a
ContextVar. Load-bearing: `RuntimeCredentials` — the `jwt` field above all — must NEVER
be placed into any structure that is serialized to the model (messages, tool schemas,
ToolResult, TrailEntry, ...).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeCredentials:
    """Per-turn credentials, passed as an explicit argument (not a ContextVar).

        `session_id` rides every MCP call as the unsigned `X-Session-Id` header (D81). `jwt`
        is opaque and forwarded as-is (D79b/D82) — the runtime never inspects it for
        authorization; the MCP is the enforcement boundary (D57/D80). `column_scope` is
        decoded locally from the jwt for the D44 replay filter only; empty == allow-all,
        matching `Principal.column_scope` and D80(b).
    """

    session_id: str
    jwt: str
    column_scope: frozenset[str]
