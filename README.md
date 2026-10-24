# data-analysis-agent

HR data-analysis agent — Phase 0 walking skeleton (see `docs/decisions/DECISIONS.md` D68).

## Getting started

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies (including dev/test tools)
uv sync --extra dev

# Run tests
uv run pytest

# Lint
uv run ruff check
```

## Project structure

```
src/data_agent/
  catalog/       — Semantic Catalog loader (D42, D53): reads databaseSchemaDocs/*.yaml
  sqlparse/      — ClickHouse-dialect SQL parser / column-provenance extractor (D52, D62)

tests/sqlparse/
  test_column_provenance.py — 36 unit tests (D44, D52, D57, D62, D63, D64, D69)

databaseSchemaDocs/
  *.yaml         — Semantic Catalog YAML files (one per table)

docs/
  decisions/DECISIONS.md  — Locked architecture decisions
  decisions/OPEN-QUESTIONS.md
```

## Key decisions

- D52/D62: ClickHouse-dialect parser uses `sqlglot` (pure-Python, in-process).
- D63: parse failure on live query -> fail-closed + alert (never fail-open).
- D69: column-provenance contract — fully-qualified (database.table, column) USES triples.


### Answer-snapshot REST proxy

Set `CLICKHOUSE_API_REST_BASE_URL` to the clickhouse-api host root (no `/mcp`
suffix). Empty configuration returns `501 EXPORT_NOT_CONFIGURED`; it is not
inferred from `MCP_URL`. The existing live session catalog must be configured:
the proxy verifies the JWT and obtains the backend's `X-Catalog-Scope` before
forwarding requests. A caller-supplied scope header is not trusted.

The four `/query/snapshots*` routes support synchronous creation (150s read
timeout), page/download URL reads (60s), and unbuffered CSV streaming (120s).
Backend JSON/status/Retry-After are preserved; network failures return a canned
502. No query is retried automatically by the proxy.

Creation body is `{"sql":"..."}`. Identical requests in the same verified
session/scope reuse the frozen snapshot until expiry. Ember's Refresh handler
must instead send `{"sql":"...","refresh_of":"<current snapshot ID>"}`.
Retries keep the same reference; after success, the next refresh references the
replacement. Replace the UI's snapshot only after a successful 201. If the old
snapshot was removed (404), clear it and create normally. Backend ownership
checks apply to page, download, and refresh independently of Ember's checks.
Older caller-only snapshots must be recreated after deploying the new backend.

This remains synchronous: no 202/status-polling integration is required. Configure
BFF/gateway deadlines to allow creation plus session-catalog setup. No changes to
Ember's source tree are included in this repository.


Scope-change recovery: on backend `409 SNAPSHOT_SCOPE_CHANGED`, the proxy forces
one session-catalog refresh and calls the backend's owned-snapshot `/rebuild`
endpoint. The backend revalidates its privately stored SQL under current grants.
On success the proxy returns `409 SNAPSHOT_REPLACED` with `snapshot` (new creation
metadata) and `reset_table: true`. Ember must clear rows, save the replacement,
and reload from offset zero; downloads restart on the replacement. This avoids
mixing results from two permission scopes. Rebuild failures keep their backend
error/status with `reset_table: true`; pending responses retain Retry-After.
There is no retry loop or fallback to stale data. The Ember handler change is
still required; it cannot be implemented in this repo. The proxy does not detect
upstream permission changes until they reach the verified session scope.
