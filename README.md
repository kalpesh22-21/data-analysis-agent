# data-analysis-agent

A conversational HR analytics agent for a ClickHouse warehouse. It combines
scope-aware SQL execution, reusable analysis blueprints, capability tools, and a
reviewed learning pipeline with a streaming web interface.

## What it does

- Answers warehouse questions through a bounded agent loop with SQL validation,
  column provenance, result verification, and clarification or approval pauses.
- Retrieves and executes reusable blueprints, including multi-step analysis DAGs.
- Produces structured answer documents with tables, charts, data-as-of context,
  and snapshot-backed pagination and exports when configured.
- Persists sessions in Couchbase and filters replay and retrieval by access scope.
- Extracts learning candidates for reviewer approval and corpus promotion.
- Emits OpenTelemetry traces for model, tool, retrieval, and learning activity.

The online runtime and asynchronous learning services are separate deployment
planes. See the [architecture](docs/01-architecture.md) and
[documentation index](docs/README.md) for their contracts and design status.

## Getting started

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies (including dev/test tools)
uv sync --extra dev

# Run tests
uv run pytest

# Lint
uv run ruff check src/ tests/
```

CI runs lint and the unit suite on Python 3.12 and 3.14. Some tests require
additional services or opt-in flags; see [testing](docs/11-testing.md),
[browser tests](tests/e2e/README.md), and [evaluations](tests/eval/README.md).
CI also checks out the companion ClickHouse API repository for corpus canonical
parity checks; local checks can locate it through `CLICKHOUSE_API_REPO`.

## Run locally

The development UI uses a runtime on port **8000** and a BFF on port **3000**.
Both scripted and live modes verify JWTs against the development token service
on port **19000**.

The [integration Compose file](docker-compose.integration.yml) provides the local
services. It currently contains absolute paths to the companion `clickhouse-api`
checkout; update those paths for your machine before starting it.

```bash
docker compose -f docker-compose.integration.yml up -d --build
bash scripts/couchbase-init.sh

# Scripted model and warehouse responses; still requires the token service
bash scripts/run_ui.sh

# Alternatively, use the live model, MCP backend, and Couchbase sessions
REAL=1 bash scripts/run_ui.sh
```

Open <http://localhost:3000>. Live mode requires `OPENAI_API_KEY` in the environment
or root `.env`, plus the MCP, token, Couchbase, and Phoenix services. Set
`OPENAI_MODEL` to choose a model; compatible providers also use `OPENAI_BASE_URL`
and provider-appropriate model settings. See the
[live launcher](scripts/run_ui_runtime_real.py) for the full configuration.

The live development launcher defaults to retrieval off and `hints` mode.
`REAL_RETRIEVAL=1` enables retrieval and selects `required_search`; it requires
Neo4j and an appropriately seeded corpus. An explicit `RETRIEVAL_MODE` overrides
that choice. The production runtime defaults to `required_search` as described below.

For a configured standalone runtime API:

```bash
uv run python scripts/run_runtime_api.py --host 127.0.0.1 --port 8000
```

Production environment settings are defined in
[`RuntimeSettings`](src/data_agent/runtime/config.py). Kubernetes installation,
external secrets, and online/learning deployment configuration are documented in
[the Helm guide](deploy/helm/README.md).

## Retrieval mode

`RETRIEVAL_MODE` selects how the agent discovers supporting context:

| Value | Retrieval hints | Mandatory searches before other work |
| --- | --- | --- |
| `hints` | Ordinary reference context when retrieval is available | None |
| `required_search` (default) | Not injected | `searchBlueprints`, `searchKnowledge`, plus `searchCapabilityTools` when capabilities are enabled |

Hint mode works without Neo4j/retrieval and retains normal recovery. Required-search
mode is the default: startup fails clearly unless retrieval is
configured and enabled. It adds search calls to every turn, including chat,
clarifications, Help Center questions and saved-result summaries. A runtime retrieval
outage or timeout before discovery completes prevents answering/recovery in that
mode. Use hints mode when graceful degradation is required.

The deprecated `RETRIEVAL_PREFETCH_TOOL_ENABLED` is ignored, with a startup warning: both
of its values only chose how hints were shown. It does not override `RETRIEVAL_MODE`
or its `required_search` default. Set `RETRIEVAL_MODE=hints` explicitly to enable hints. No simulated `prefetchContext` tool calls are
created in either mode.

The main prompt describes the selected mode. Required searches use the current
request and conversation; empty successful searches count, failures do not. The gate
enforces completion, not semantic relevance. Successful receipts survive same-scope
resume, but not a new turn or scope. Mixed batches retain pending searches and drop
premature work. Gate violations have their own bounded correction path, separate
from answer repair. Normal wall-clock, token, iteration and read limits still apply.
`loop_required_search_gate` records waiting, completed, ready, blocked and incomplete
states without recording query text.

## Project structure

| Path | Purpose |
| --- | --- |
| `src/data_agent/runtime/` | Agent loop, HTTP API, tools, sessions, retrieval, and answer contracts |
| `src/data_agent/learning/` | Candidate extraction, validation, review, and promotion |
| `src/data_agent/catalog/` | Semantic catalog loading |
| `src/data_agent/sqlparse/` | SQL parsing and column provenance |
| `src/data_agent/corpus/` | Shared corpus models and utilities |
| `databaseSchemaDocs/` | Curated warehouse table metadata |
| `ui/` | Web BFF and static chat, review, and mint interfaces |
| `scripts/` | Service entrypoints, local launchers, probes, and maintenance tools |
| `tests/` | Unit, integration, deployment, evaluation, and browser tests |
| `deploy/helm/` | Runtime, learning, and platform Helm charts |
| `docs/` | Specifications, integration contracts, and decision records |

## Security and provenance

JWT verification and backend scope enforcement constrain data access. Column
provenance supports scope-aware history and blueprint filtering; live query
provenance parsing fails closed on parse errors. See
[security and governance](docs/06-security-and-governance.md),
[query provenance](docs/api-query-provenance.md), and the
[decision record](docs/decisions/DECISIONS.md).

## Integrations

- [Tools and API](docs/02-tools-and-api.md)
- [Capability API](docs/capability-api-spec.md)
- [Answer document and Ember/BFF handoff](docs/ui-answer-document-spec.md)
- [Observability](docs/10-observability.md)

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
