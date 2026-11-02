# Session scope recovery (I14)

A catalog rollout invalidates previously minted MCP scope tokens. The API returns
HTTP 403 with `code: SESSION_SCOPE_STALE` before tool dispatch. Lane5 re-mints the
session catalog, creates a fresh MCP client session and retries once. It does not
retry arbitrary 403/404 responses, expired tokens, binding failures or permission
denials through this mechanism. A second stale response ends recovery.

Concurrent stale requests use the rejected token as their CAS baseline, sharing
an already completed refresh. Changed column grants stop the retry; terminal SSE
delivery also checks the current session scope against the turn's credentials.
The caller receives `SESSION_SCOPE_CHANGED` and must submit a new turn.

MCP output schemas are discovered before calling a tool. The SDK otherwise fetches
them after execution; a rollout during that lookup could replay an already
completed query. Notifications and session cleanup cannot trigger scope recovery.
Recovery emits a `mcp.scope_recovered` trace event with code and retry count only.
Tokens and response bodies are never included in that event.

## Local cross-repository verification

Run from the lane5 repository with an interpreter containing both repositories'
runtime dependencies:

```sh
CLICKHOUSE_API_REPO=/path/to/clickhouse-api PYTHONPATH=src:/path/to/clickhouse-api \
  .venv/bin/python -m pytest tests/integration/test_scope_recovery_cross_repo.py -q
```

The test starts the actual clickhouse-api auth middleware and MCP server on a local
TCP port. It exercises signed scope minting/verification, the real MCP SDK,
SessionCatalog, dispatcher, blueprint executor, agent run/resume and SSE delivery.
Identity verification, warehouse rows and model/judge responses are deterministic
fixtures. This is not a production warehouse or live-model battery.

Coverage includes catalog rollover, discovery, pause/resume, a rollover between
blueprint execution and its grain probe, persistent stale failures, other denials,
changed grants and blocked terminal delivery. No clickhouse-api code change is
required: its existing middleware already returns the exact stale-scope code.
