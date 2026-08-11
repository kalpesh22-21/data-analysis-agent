# data-agent Helm chart

Production-quality Helm chart for **data-agent**, the HR data-analysis agent
(Python/FastAPI). Chart lives at [`data-agent/`](./data-agent).

All of the first-party workloads run from **one** application image built from
this repo. The chart wires each workload's launch command + environment; it does
**not** build the image and does **not** deploy any backing infrastructure.

## Workloads deployed by this chart

| Component | Kind | Command | Service | Scales? |
|---|---|---|---|---|
| `runtime` | Deployment | `uvicorn data_agent.runtime.app:create_app --factory --host 0.0.0.0 --port 8000` | ClusterIP :8000 | yes (HPA) |
| `ui` | Deployment | `uvicorn ui.server:app --host 0.0.0.0 --port 3000` | ClusterIP :3000 (+ optional Ingress) | yes |
| `learning-sweeper` | Deployment | `python scripts/run_learning_sweeper.py` | none | **no — keep 1** |
| `learning-consumer` | Deployment | `python scripts/run_learning_consumer.py` | none | yes (HPA) |
| `inbox` *(optional)* | Deployment | `python scripts/run_inbox_service.py` | ClusterIP :8100 | yes |

The `inbox` component is disabled by default (`components.inbox.enabled=false`).
When enabled, the chart injects `REVIEW_INBOX_ENABLED=1`, `INBOX_SERVICE_HOST=0.0.0.0`
and `INBOX_SERVICE_PORT` automatically.

### Probes
- `runtime` has **no unauthenticated health endpoint**, so it uses a **TCP** readiness/liveness probe on :8000.
- `ui` probes **HTTP GET /**.
- `inbox` probes **HTTP GET /inbox/health**.
- `learning-sweeper` / `learning-consumer` are background daemons with **no probes** (the Deployment restarts the process if it exits).

## External dependencies — NOT deployed here

These are external infra owned by other systems. Provision them separately and
supply their endpoints via `config` and credentials via `secrets` /
`existingSecret`:

- **ClickHouse** and the **MCP** (`clickhouse-api`) — `MCP_URL`
- **Token IdP** — `JWKS_URL`, `JWT_ISSUER`, `JWT_AUDIENCE`, `TOKEN_SERVICE_URL`, `TOKEN_ISSUER_API_KEY`
- **Warehouse tenant** — `TENANT_CLIENT_CODE`, `TENANT_PROC_CENTER`, `TENANT_JTI` (stamped into every minted token; the MCP rejects a token without them `403 MISSING_TENANT_CLAIM`)
- **OpenAI** (or compatible) — `OPENAI_MODEL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`
- **Couchbase** (sessions + learning audit/candidates/corpus buckets)
- **Neo4j** — `NEO4J_URL`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`
- **Redis** — `LEARNING_REDIS_URL` (learning job stream)
- **Embedding / reranker APIs** — `EMBEDDING_API_URL`, `RERANKER_API_URL`

No subcharts are bundled for any of these on purpose.

## Prerequisites

- Kubernetes >= 1.24, Helm 3+.
- The external services above, reachable from the cluster.
- A built + pushed application image (see below).
- (Production) a pre-created Secret with the secret keys, referenced via `secrets.existingSecret`.
- (Optional) an Ingress controller + cert-manager if you enable the UI Ingress.

## Build and push the image

There is **no Dockerfile / published image in the repo yet** — you must build
and push your own. The image must contain the installed `data_agent` package
(`src/`), the `ui/` package, and `scripts/`, with `uvicorn` and `python` on PATH.
A minimal example (adjust to your registry):

```dockerfile
# Dockerfile (example — not shipped with the chart)
FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY ui ./ui
COPY scripts ./scripts
RUN uv pip install --system .
RUN useradd -u 10001 -m app && chown -R app /app
USER 10001
```

```bash
docker build -t ghcr.io/acme/data-agent:1.4.2 .
docker push ghcr.io/acme/data-agent:1.4.2
```

Then set `image.repository` and `image.tag` (or use `values-example.yaml`).

## Install / upgrade

```bash
# Dry-run render to inspect manifests
helm template data-agent deploy/helm/data-agent \
  -f deploy/helm/data-agent/values-example.yaml

# Install / upgrade
helm upgrade --install data-agent deploy/helm/data-agent \
  --namespace data-agent --create-namespace \
  -f deploy/helm/data-agent/values-example.yaml
```

## Supplying secrets

Two options:

**1. existingSecret (recommended for production).** Create the Secret
out-of-band (sealed-secrets / external-secrets / vault) with these keys, then
set `secrets.existingSecret: <name>`. When set, the chart-managed Secret is not
rendered.

```bash
kubectl create secret generic data-agent-secrets \
  --namespace data-agent \
  --from-literal=OPENAI_API_KEY=... \
  --from-literal=COUCHBASE_PASSWORD=... \
  --from-literal=NEO4J_PASSWORD=... \
  --from-literal=TOKEN_ISSUER_API_KEY=... \
  --from-literal=EMBEDDING_API_KEY=... \
  --from-literal=RERANKER_API_KEY=... \
  --from-literal=LEARNING_EXTRACTOR_API_KEY=... \
  --from-literal=LEARNING_AUDIT_PASSWORD=... \
  --from-literal=LEARNING_CANDIDATES_PASSWORD=... \
  --from-literal=LEARNING_CORPUS_PASSWORD=... \
  --from-literal=REVIEWER_TOKEN=...
```

> **Rotation caveat (existingSecret).** Secrets are injected as environment
> variables via `envFrom`, which Kubernetes does not live-update. The chart's
> `checksum/secret` annotation only hashes the chart-managed Secret, so it
> cannot detect changes to an external `existingSecret`. After rotating an
> `existingSecret` you must restart the workloads manually, e.g.
> `kubectl rollout restart deploy -l app.kubernetes.io/instance=<release>`.

**2. Chart-managed Secret (dev/testing only).** Inline values under
`secrets.data`. Never commit real secrets. Empty-string values are skipped so
unset keys fall back to app defaults. The pod template carries a
`checksum/secret` annotation so changing a value triggers a rolling restart.

## Configuration model

- Non-secret env → shared **ConfigMap** (`config` block in values).
- Secret env → shared **Secret** (`secrets` block, or `existingSecret`).
- Every workload does `envFrom: [configMapRef, secretRef]` + any per-workload extras.
- `RUNTIME_URL` / `INBOX_SERVICE_URL` for the UI auto-derive from the release
  name when left empty, and are overridable via `config`.

Each component (`runtime`, `ui`, `learningSweeper`, `learningConsumer`, `inbox`)
can override `enabled`, `replicaCount`, `image`, `resources`, `nodeSelector`,
`tolerations`, `affinity`, `podAnnotations`, `podSecurityContext`,
`securityContext`, and `env`, falling back to the global defaults.

## Testing the release

```bash
helm test data-agent --namespace data-agent
```

Runs a connectivity probe against the UI Service.
