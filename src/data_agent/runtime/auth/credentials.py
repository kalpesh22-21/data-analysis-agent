"""RuntimeCredentials — the model-invisible per-turn credential bundle (D5, §2).

`RuntimeCredentials` is constructed exactly once per inbound HTTP turn request
(Pass B's `app.py`, from the `Authorization` header + `X-Session-Id` header —
the UI/UI-backend already minted and holds the JWT per D82) and is then
threaded as an explicit function/constructor argument down the call chain:

    app.py (turn handler)
      -> ContextAssembler.assemble(session_id, column_scope)   # scope only
      -> AgentLoop.run(credentials, context, ...)                (Pass B)
           -> ToolDispatcher.dispatch(tool_name, model_args, credentials)
                -> MCPClient.call_tool(tool_name, model_args,
                                        jwt=credentials.jwt,
                                        session_id=credentials.session_id)

Design rule (load-bearing, D5): `RuntimeCredentials` — and in particular the
`jwt` field — must NEVER be placed into any dict/structure that is serialized
to the model (messages, tool schemas, ToolResult, TrailEntry, ...). Every
Pass-A module that touches credentials documents this boundary explicitly;
`tests/runtime/dispatch/test_tool_dispatcher.py` asserts it end-to-end against
the `FakeMCPClient` transport boundary.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeCredentials:
    """Per-turn credentials, explicit-argument-passed (not a ContextVar — §2).

    Attributes:
        session_id: From the UI, forwarded as the unsigned `X-Session-Id`
            header on every MCP call (D81).
        jwt: Opaque bearer string, forwarded as-is to the MCP (D79b/D82). The
            runtime never inspects its signature for authorization purposes —
            the MCP is the enforcement boundary (D57/D80); Pass B's
            jwt_verify.py independently verifies it only to compute
            `column_scope` below (defense-in-depth for the D44 replay-filter,
            §11 sub-decision B).
        column_scope: Decoded+verified locally from the jwt. Empty frozenset
            == allow-all, matching clickhouse-api's `Principal.column_scope`
            and D80(b) exactly.
    """

    session_id: str
    jwt: str
    column_scope: frozenset[str]
