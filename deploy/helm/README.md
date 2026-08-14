# data-agent Helm charts

Two production-quality Helm charts for **data-agent**, the HR data-analysis agent
(Python/FastAPI):

| Chart | Path | Plane | Deploys |
|---|---|---|---|
| `data-agent` | [`data-agent/`](./data-agent) | **online** | agent runtime API, agent UI, neo4j hydrator |
| `data-agent-learning` | [`data-agent-learning/`](./data-agent-learning) | **offline** | learning sweeper, learning consumer, learning scheduler, review-inbox service, reviewer UI |

They are split because the two planes have different lifecycles, different
blast radii and different reasons to be scaled or turned off: you can stop the
entire learning loop without touching the agent serving traffic, and you can
redeploy the agent without disturbing a reviewer's queue. They are **not**
independent — see [How the two charts couple](#how-the-two-charts-couple).

**Both charts ship the SAME image**, built from the repo-root `Dockerfile`. One
image, eight workloads (3 Deployments in Chart A + 5 in Chart B): each Deployment
overrides `command:`. Neither chart builds the image and neither deploys any
backing infrastructure.

> **Release naming.** Do not install the `data-agent` chart under a release name
> that *contains* `data-agent-learning` (e.g. `data-agent-learning-prod`). Helm's
> fullname rule collapses `<release>-<chart>` to just `<release>` when the release
> already contains the chart name, so such a release would render Chart A's
> ServiceAccount, ConfigMap, Secret and test Pod under exactly the names Chart B
> uses — a silent collision between the two releases.

## Workloads

### Chart A — `data-agent` (online)

| Component | Kind | Command | Service | Scales? |
|---|---|---|---|---|
| `runtime` | Deployment | `uvicorn data_agent.runtime.app:create_app --factory --host 0.0.0.0 --port 8000` | ClusterIP :8000 | yes (HPA) |
| `ui` | Deployment | `uvicorn ui.server:app --host 0.0.0.0 --port 3000` | ClusterIP :3000 (+ optional Ingress) | yes |
| `hydrator` | Deployment | `python scripts/run_hydrator.py` | none | **no — pinned to 1** |

### Chart B — `data-agent-learning` (offline)

| Component | Kind | Command | Service | Scales? |
|---|---|---|---|---|
| `learning-sweeper` | Deployment | `python scripts/run_learning_sweeper.py` | none | **no — pinned to 1** |
| `learning-consumer` | Deployment | `python scripts/run_learning_consumer.py` | none | yes (HPA) |
| `learning-scheduler` | Deployment | `python scripts/run_learning_scheduler.py` | none | **no — pinned to 1** |
| `inbox` | Deployment | `python scripts/run_inbox_service.py` | ClusterIP :8100 | yes |
| `inbox-ui` | Deployment | `uvicorn ui.server:app --host 0.0.0.0 --port 3000` | ClusterIP :3000 (+ optional Ingress) | yes |

`learning-sweeper`, `learning-scheduler` and `hydrator` are pinned to one replica
**in the template** (no `replicaCount` knob): a second sweeper double-enqueues, a
second scheduler double-promotes the same candidate, and a second hydrator races
the destructive graph rebuild. Only `runtime`, `ui`, `learning-consumer`, `inbox`
and `inbox-ui` are horizontally scalable.

### One app, two UIs

`ui/server.py` is a single FastAPI app serving **both** surfaces:

* the agent chat shell — `GET /`, `/api/session*`, `/api/turn*`, `/api/history`,
  `/api/query/page`, `/api/upload*`
* the reviewer inbox — `GET /inbox`, `/api/inbox*`, which 404 unless
  `REVIEW_INBOX_ENABLED=1`

The split deploys that one app **twice**, with no application change:

* Chart A's `ui` sets no `REVIEW_INBOX_ENABLED` and no `INBOX_SERVICE_URL`, so
  every inbox route 404s.
* Chart B's `inbox-ui` sets `REVIEW_INBOX_ENABLED=1` and gets `INBOX_SERVICE_URL`
  derived from its own release's inbox Service.

**The chat routes still exist on the `inbox-ui` pod** — they cannot be compiled
out. What keeps them unpublished is Chart B's Ingress path list, which defaults to
`/inbox` and `/api/inbox` only (the app mounts no `/static` directory; `inbox.html`
is self-contained, so no asset path is needed). Widening those paths to `/` opens a
second door into the agent. Treat pod-level access to `inbox-ui` as reviewer-level
access.

Reviewer authentication is **not** in the app: the inbox service authenticates the
UI→inbox hop on a shared `REVIEWER_TOKEN` (held server-side, never sent to the
browser), and nothing authenticates the human. Put your SSO / forward-auth / IP
allowlist on Chart B's Ingress.

### Probes

- `runtime` — readiness `httpGet /ready` (unauthenticated; 200 only once the
  hydrator has seeded neo4j, so a pod stays out of the Service until it can
  actually recall). Liveness stays a **TCP** probe, so hydrator lag de-routes a
  pod without restarting it.
- `ui` / `inbox-ui` — HTTP. The agent UI probes `GET /`; the reviewer UI probes
  `GET /inbox`, because `/` answers 200 on that pod whether or not the reviewer
  surface is enabled.
- `inbox` — **TCP**, deliberately. Every inbox route including `/inbox/health`
  requires an `X-Reviewer-Token`; the kubelet sends none, so an httpGet probe
  would read 401 (or 503 with the token unset), treat it as a failure, and the pod
  would never become Ready. Putting the token in `httpHeaders` is not the
  alternative — that writes a live credential into the pod spec.
- `learning-sweeper` / `learning-consumer` / `learning-scheduler` / `hydrator` —
  background daemons with **no probes** (the Deployment restarts the process if it
  exits).

## How the two charts couple

Chart B reads Chart A's data. This is the part a split makes easy to get wrong,
and every failure mode below is **silent** — no error, no crash, just a plane
working on the wrong rows.

1. **Couchbase session bucket.** The sweeper scans the *agent's* session bucket
   for idle sessions. Chart B's `COUCHBASE_CONNECTION_STRING` / `COUCHBASE_USERNAME`
   / `COUCHBASE_BUCKET` / `COUCHBASE_PASSWORD` must be Chart A's. Point them
   somewhere else and the learning loop simply idles forever with nothing to learn
   from. **Chart B needs Chart A's session bucket populated** — a learning release
   installed beside an agent nobody has used yet is correctly, permanently quiet.
2. **The tenant triple.** `TENANT_CLIENT_CODE` / `TENANT_PROC_CENTER` /
   `TENANT_JTI` must be identical in both charts. Chart B's promotion scheduler
   and inbox mint their own JWT and replay against the warehouse as that tenant.
   Warehouse row policies **filter rather than error**, so a divergent tenant reads
   a different (or empty) row set and the golden replay's grain check still passes
   — `0 == 0`. Both charts default all three to `""`, which trips the readiness
   gates and drops the learning plane into its logged dormant posture (nothing
   auto-promotes). That is the safe failure; a wrong value is the unsafe one.
3. **Neo4j graph + embedding model.** A validated blueprint *lands* in the same
   retrieval graph the agent recalls from. `NEO4J_*` must match, and so must
   `EMBEDDING_API_URL` / `EMBEDDING_MODEL` — vectors written by one model and
   searched by another compare as noise.
4. **Session TTL vs idle threshold.** Chart A's `SESSION_TTL_SECONDS` must exceed
   Chart B's `LEARNING_IDLE_THRESHOLD_SECONDS` plus P95(dwell + processing), or
   sessions expire before they are learned from.
5. **MCP + token service.** Both planes call the same MCP and mint from the same
   IdP: `MCP_URL`, `TOKEN_SERVICE_URL`, `TOKEN_ISSUER_API_KEY`.

### Recommended: one Secret for both releases

Create **one** Secret out-of-band containing the union of both charts' keys and
set `secrets.existingSecret` to it in **both** releases. `COUCHBASE_PASSWORD`,
`NEO4J_PASSWORD`, `TOKEN_ISSUER_API_KEY` and `EMBEDDING_API_KEY` authenticate the
same identity in both planes; two hand-maintained Secrets drift, and per point 2
above the drift has no symptom. Unused keys in a shared Secret are harmless —
each pod only reads what it needs.

```bash
kubectl create secret generic data-agent-secrets \
  --namespace data-agent \
  `# shared` \
  --from-literal=COUCHBASE_PASSWORD=... \
  --from-literal=NEO4J_PASSWORD=... \
  --from-literal=TOKEN_ISSUER_API_KEY=... \
  --from-literal=EMBEDDING_API_KEY=... \
  `# data-agent only` \
  --from-literal=OPENAI_API_KEY=... \
  --from-literal=MCP_SERVICE_KEY=... \
  --from-literal=RERANKER_API_KEY=... \
  `# data-agent-learning only` \
  --from-literal=LEARNING_EXTRACTOR_API_KEY=... \
  --from-literal=LEARNING_AUDIT_PASSWORD=... \
  --from-literal=LEARNING_CANDIDATES_PASSWORD=... \
  --from-literal=LEARNING_CORPUS_PASSWORD=... \
  --from-literal=REVIEWER_TOKEN=...
```

> **Rotation caveat (existingSecret).** Secrets are injected as environment
> variables via `envFrom`, which Kubernetes does not live-update. Each chart's
> `checksum/secret` annotation only hashes its own chart-managed Secret, so it
> cannot detect changes to an external `existingSecret`. After rotating one you
> must restart the workloads manually, e.g.
> `kubectl rollout restart deploy -l app.kubernetes.io/instance=<release>`.

## Config split

Each chart renders its **own** ConfigMap + Secret, `envFrom`-mounted into every
one of its workloads, and each supports `secrets.existingSecret`.

| Chart A only | Chart B only | Both (must agree) |
|---|---|---|
| `RERANKER_*`, `RETRIEVAL_*`, `SCRATCH_*`, `UPLOAD_MAX_BYTES`, `RUNTIME_URL`, `SESSION_TTL_SECONDS`, `MODEL_CONTEXT_WINDOW`, `OPENAI_*`, `JWKS_URL`, `JWT_ISSUER`, `JWT_AUDIENCE`, `HYDRATOR_*`, `MCP_SERVICE_KEY`, `OTLP_SERVICE_NAME`, `CATALOG_SOURCE`, `CATALOG_API_URL` | `LEARNING_*` (transport, sweeper cadence, extractor, judge, promotion policy, audit/candidates/corpus buckets, tracing), `REVIEWER_TOKEN`, `INBOX_SERVICE_URL` | `MCP_URL`, `CATALOG_FIXTURE_PATH`, `COUCHBASE_*`, `NEO4J_*`, `EMBEDDING_*`, `TOKEN_SERVICE_URL`, `TOKEN_ISSUER_API_KEY`, `TENANT_CLIENT_CODE`/`TENANT_PROC_CENTER`/`TENANT_JTI`, `OTLP_ENDPOINT` |

`CATALOG_SOURCE` / `CATALOG_API_URL` are Chart A's alone because **the learning
plane never reads the live catalog export**: its entrypoints hold no per-request
MCP JWT, so `scripts/_catalog.py` resolves the catalog through
`RuntimeSettings.catalog_fixture_file()`, which consults `CATALOG_FIXTURE_PATH`
and nothing else. Shipping the other two in Chart B would have been two knobs that
look configured and do nothing.

Two rendering rules worth knowing:

- A **blank** value is omitted from the ConfigMap, so the process falls through to
  its settings-model default. The three `TENANT_*` keys are the carve-out: they
  render even when blank, because their code defaults are the local dev seed and
  an omitted key would silently re-mint it.
- A typo'd `LEARNING_*` key is **not** an error — the settings model sets
  `extra="ignore"`, so the shipped default silently applies. Each learning
  entrypoint logs the ignored names at startup
  (`warn_unrecognized_learning_env_vars`); grep for that after a config change.

`RUNTIME_URL` (Chart A) and `INBOX_SERVICE_URL` (Chart B) auto-derive from their
own release name when left empty. Chart A has **no** inbox derivation: it cannot
know the learning release's name, so a derived URL there would point at nothing.

## External dependencies — NOT deployed by either chart

Provision separately and supply endpoints via `config`, credentials via
`secrets` / `existingSecret`:

- **ClickHouse** and the **MCP** (`clickhouse-api`) — `MCP_URL` *(both charts)*
- **Token IdP** — `JWKS_URL`, `JWT_ISSUER`, `JWT_AUDIENCE` *(A)*, `TOKEN_SERVICE_URL`, `TOKEN_ISSUER_API_KEY` *(both)*
- **Warehouse tenant** — `TENANT_CLIENT_CODE`, `TENANT_PROC_CENTER`, `TENANT_JTI` *(both — see coupling point 2; you must set all three, identically)*
- **OpenAI** (or compatible) — `OPENAI_*` *(A)*, `LEARNING_EXTRACTOR_*` *(B)*
- **Couchbase** — session bucket *(both)*, learning audit/candidates/corpus buckets *(B)*
- **Neo4j** — `NEO4J_URL`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` *(both)*
- **Redis** — `LEARNING_REDIS_URL` *(B only — the agent never touches it)*
- **Embedding API** — `EMBEDDING_API_URL` *(both)*; **reranker** — `RERANKER_API_URL` *(A)*

No subcharts are bundled for any of these on purpose.

## Prerequisites

- Kubernetes >= 1.24, Helm 3+.
- The external services above, reachable from the cluster.
- A built + pushed application image (see below).
- (Production) a pre-created Secret, referenced via `secrets.existingSecret` in
  both releases.
- (Optional) an Ingress controller + cert-manager if you enable either Ingress.

## Build and push the image

The repo root ships a production `Dockerfile` that builds the one image both
charts deploy (it contains the installed `data_agent` package, `ui/` and
`scripts/`, with `uvicorn` and `python` on PATH, running as uid 10001).

```bash
docker build -t ghcr.io/acme/data-agent:1.4.2 .
docker push ghcr.io/acme/data-agent:1.4.2
```

Then set `image.repository` and `image.tag` in **both** charts (or use their
`values-example.yaml` files). Keep the two releases on the same tag unless you are
deliberately staging a rollout.

## Install / upgrade

```bash
# Dry-run render to inspect manifests
helm template data-agent deploy/helm/data-agent \
  -f deploy/helm/data-agent/values-example.yaml
helm template data-agent-learning deploy/helm/data-agent-learning \
  -f deploy/helm/data-agent-learning/values-example.yaml

# Install / upgrade — the online plane first (it creates the sessions the
# learning plane consumes; the reverse order works too, just idles).
helm upgrade --install data-agent deploy/helm/data-agent \
  --namespace data-agent --create-namespace \
  -f deploy/helm/data-agent/values-example.yaml

helm upgrade --install data-agent-learning deploy/helm/data-agent-learning \
  --namespace data-agent \
  -f deploy/helm/data-agent-learning/values-example.yaml
```

Turning the learning plane off is `helm uninstall data-agent-learning` (or
`LEARNING_ENABLED=false`, which every learning daemon re-reads each cycle without
a restart). Neither affects the agent.

### Migrating from the combined chart (0.1.0)

If you are running the old single `data-agent` release that carried the learning
workloads, **order matters**:

```bash
# 1. FIRST upgrade the existing release to 0.2.0. This DELETES its learning
#    sweeper / consumer / scheduler / inbox Deployments.
helm upgrade data-agent deploy/helm/data-agent \
  --namespace data-agent -f <your-values.yaml>

# 2. THEN install the learning release.
helm upgrade --install data-agent-learning deploy/helm/data-agent-learning \
  --namespace data-agent -f <your-learning-values.yaml>
```

Doing it in the other order runs **two sweepers and two promotion schedulers
concurrently** — the exact double-enqueue / double-promote the single-replica
pinning exists to prevent, and neither copy knows about the other.

The gap between the two steps is safe: the Redis stream and its consumer-group
offsets are external state that survives both releases, so jobs enqueued before
the upgrade are still pending when the new consumers join. Candidates already in
the inbox live in Couchbase and are likewise untouched. The only cost of a long
gap is latency.

## Per-component overrides

Each component in either chart can override `enabled`, `replicaCount` (where the
workload is scalable), `image`, `resources`, `nodeSelector`, `tolerations`,
`affinity`, `podAnnotations`, `podSecurityContext`, `securityContext` and `env`,
falling back to that chart's global defaults.

## Testing a release

```bash
helm test data-agent --namespace data-agent
helm test data-agent-learning --namespace data-agent
```

Chart A's test hits the agent UI Service at `GET /`. Chart B's hits the reviewer
UI at `GET /inbox` — which is 200 only if `REVIEW_INBOX_ENABLED=1` actually
reached the pod. The inbox service itself is not probed by the test: all of its
routes require the reviewer token, so an unauthenticated request could only ever
assert a 401.

Render-level regression tests live in `tests/deploy/` (they skip when the `helm`
binary is absent).
