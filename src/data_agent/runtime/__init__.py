# runtime — Phase-0 agent runtime (docs/decisions/phase0-runtime-design.md)
#
# Pass A (this slice) builds everything below the agent loop: config, auth
# credentials, the MCP client + tool-schema bridge, provenance capture, the
# tool dispatcher + denial mapping, the session store, and context assembly.
#
# Pass B (not built here) adds: model/ (OpenAI client), loop/ (agent loop +
# budget guard), observability/ (OTel/Phoenix + redaction + progress), and
# app.py (the composition root / HTTP entrypoint). Every Pass-A module that
# Pass B plugs into documents its seam explicitly in its own docstring:
#   - dispatch/tool_dispatcher.py: `observer` callback param (no-op default)
#   - context/budget.py: `summarizer` callable dependency (identity default)
#   - mcp/client.py, session/store.py: Protocols Pass B's AgentLoop consumes
