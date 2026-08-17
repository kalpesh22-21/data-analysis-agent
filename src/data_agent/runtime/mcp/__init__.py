# mcp — transport + schema bridging to the adopted clickhouse-api MCP (D75).
#
# No re-exports: every caller imports from the submodule that owns the symbol —
# `client.py` (MCPClient, MCPToolError, MCPToolSpec), `fake_client.py`,
# `tool_schema.py`, `_transport.py` (the shared side-channel HTTP helpers).
