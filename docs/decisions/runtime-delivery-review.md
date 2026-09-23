# Delivery review and dependency failures

The runtime reviews terminal answers and clarification questions. Progress events remain operational telemetry. The answer judge defaults to enabled in runtime settings and the real local launcher.

All finish paths, including budget-stop resume and blueprint questions, use the common delivery boundary. A matching approved proposal receipt avoids a second review. A changed fallback or question is reviewed against the available scoped evidence. Conversational answers use `finalizeAnswer` with text and empty selection/evidence lists; the judge determines whether evidence-free qualitative prose needs substantive support. Numeric, SQL/schema, and answer-shape checks still apply.

Review attempts share a 30-second request-local model-call budget. A delivery without a verdict gets at most two final review attempts, in addition to the existing bounded proposal/repair reviews. `review.status` is one of `approved`, `exhausted`, `rejected`, or `disabled`; exhaustion never counts as approval. Explicit answer rejections withhold artifacts until a real repair approval. Clarification rejections survive changes of wording and pause/resume until a real approval. A rejected question is not left as a resumable checkpoint. A fixed limitation notice explains withholding without publishing the rejected draft.

The additive `review` field is emitted on the SSE result and persisted with terminal assistant messages for history parity. Disabled or absent review is reported as `disabled`, not exhausted. Direct loop test fixtures may omit the judge; operational deployments should keep it enabled. Explicitly disabled judge objects withhold delivery.

Known model and tool-schema connection/status failures use the existing terminal `done` corridor with an additive `failure` object (`code`, `dependency`, `reason`, `retryable`). Here `done` means processing has ended, not that the requested work succeeded. The safe failure text and metadata are persisted. Clients should render the failure distinctly and offer retry only when `retryable` is true. HTTP 404/configuration and authorization failures are not transient retries. Cancellation, unexpected programming exceptions, and resume CAS conflicts retain their existing behavior.

Authorization remains with the provider and MCP data planes. Review or exhaustion never grants access or overrides a denial. Partial fallback prose identifies which declared parts were delivered and which were not completed for display.

Capability model previews contain bounded descriptive labels, preparation/reference state, provider-supplied `resolved_entities`, unresolved-selection state, and guidance. Resolved identities retain their structure within the standard preview budget so the model can explain the selection, including cached reuse and legacy replay. GraphQL, opaque UI payloads, and internal evidence metadata stay out of the preview. Full payloads remain available to the runtime and UI under the existing echo contract. Hydrate shape telemetry is version 1 and emits `has_widget_name`; this change introduces no full-payload logging.

Local qualification:

Follow-up: both the agent preview and judge context expose hydrated `arguments`, `additional_arguments`, `resolved_entities`, `unresolved_entities`, `parameters`, and `filter_definitions` (the complete `metadata.ui_parameters` structure within the agent's standard preview budget). The agent and judge are instructed to consider these together, distinguishing supported filter controls from supplied selection values and from data that has actually been retrieved.

```sh
uv run pytest tests/runtime
uv run python scripts/probe_delivery_review.py --output /tmp/delivery-probe.json
```

The probe requires the local token/MCP stack, live model, and the configured capability service. It checks a greeting, a department table, a navigation card, and a clarification, including approval metadata and terminal history parity. Its private output file may contain authorized fixture data.

Validation on 2026-09-23: the runtime suite passed with 3,921 tests passed and 5 skipped; changed Python files passed Ruff. After restarting the backend on port 8000 with gpt-5.5, judge enabled, and Couchbase persistence, all four live probes passed. Results are in `/tmp/runtime-delivery-probe-20260923.json`; backend logs are in `/tmp/runtime-delivery-review-server.log`.

The local `/ready` endpoint still returns 503: Neo4j contains 12 blueprint nodes but no `CorpusMeta` freshness marker. No hydrator daemon is running, and the local hydrator settings have no Neo4j/embedding endpoints or MCP service key. Delivery probes work, but this environment is not fully ready for retrieval qualification. Configure and run the normal hydrator to establish readiness; do not manufacture a freshness marker.
