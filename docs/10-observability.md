# 10 — Observability (Arize Phoenix + OpenTelemetry)

Cross-cutting concern: every layer emits **OpenTelemetry** spans following the **OpenInference**
semantic conventions, exported to a **self-hosted Arize Phoenix** collector. Phoenix gives us trace
trees, session grouping, token/cost/latency, and an evals/experiments harness — over both the
request path and the offline learning loop.

## Why Phoenix + OTel

- **OTel-native / vendor-neutral.** Instrument once via OpenInference; export OTLP to Phoenix (or
  anywhere) without rewrites.
- **LLM/agent-aware span kinds.** `AGENT`, `LLM`, `TOOL`, `RETRIEVER`, `EMBEDDING`, `RERANKER`,
  `CHAIN`, `GUARDRAIL` — a near-1:1 fit for our components.
- **Sessions.** Traces group by `session.id`, which we already have (UI-generated `session_id`).
- **Evals built in.** Online LLM-as-judge + offline experiments against golden Q&A canaries
  (ties to evaluation in [05-memory-and-learning.md](05-memory-and-learning.md)).

## Deployment

- **Self-hosted Phoenix** (container) inside our trust boundary. HR data is sensitive — traces must
  not leave to a SaaS endpoint. OTLP over gRPC/HTTP to the in-cluster collector.
- **Auto-instrument** the Anthropic SDK (the agent model is Claude) via the OpenInference Anthropic
  instrumentor; **manual spans** for our custom runtime (context assembly, tool dispatch, blueprint
  executor, learning loop).
- Separate Phoenix **projects** for `request-path` vs. `learning-loop`.

## Span boundaries double as UI progress events (D61)

The instrumented stage boundaries below are **one instrumentation with two consumers**: they export
spans to Phoenix **and** drive the UI's step-level **progress stream** ([08](08-ui.md)) that masks
turn latency. The same redaction applies to both — progress events carry **step/shape, not values**
(no cell values, no bound slot values), consistent with the redaction rules below.

## Span map — request path

Span kinds mapped to our architecture:

| Component | Span kind | Key attributes |
|---|---|---|
| One turn | `AGENT` | `session.id`, user id, scope id (**not** the JWT), turn index |
| Context assembly | `CHAIN` | candidate counts, pre-filter drops |
| Question embedding | `EMBEDDING` | model name (vector hidden — see redaction) |
| Vector recall | `RETRIEVER` | retrieved docs/blueprints + scores |
| Scope pre-filter | `CHAIN`/`GUARDRAIL` | in-scope vs. dropped counts |
| Cross-encoder rerank | `RERANKER` | input/output ordering + scores |
| Model call | `LLM` | `llm.model_name`, prompt/completion token counts, cost, latency |
| Each tool call | `TOOL` | `tool.name`, args (redacted), status |
| Blueprint execution | `CHAIN` | `blueprint.id`, `status`, `hit_count`, slot names (not values) |
| └ per DAG node | `TOOL`/`CHAIN` | node order, `feeds_from`, inline-vs-scratch, row count |
| Clarification | `TOOL`/event | `askUser` trigger reason (catalog rule / missing slot / low conf / cost) |

### Example trace tree (fast path)
```
AGENT  turn (session.id=…, scope=payroll:read)
├─ CHAIN  context-assembly
│  ├─ EMBEDDING  question
│  ├─ RETRIEVER  vector-recall (k=20)
│  ├─ GUARDRAIL  scope-pre-filter (20→14)
│  └─ RERANKER   top-3 thin cards
├─ LLM    model turn (picks blueprint)
├─ TOOL   getBlueprint(id=bp_dept_budget_variance)
├─ CHAIN  runBlueprint
│  ├─ TOOL  node#1 dept_actuals      (runQuery)
│  ├─ TOOL  node#2 dept_targets      (runQuery, parallel)
│  ├─ TOOL  node#3 company_avg       (runQuery, parallel)
│  └─ TOOL  node#4 flag_variance     (consumes 1,2,3)
└─ LLM    verify + narrate
```

## Span map — offline learning loop

| Stage | Span kind | Attributes |
|---|---|---|
| Write router run | `CHAIN` | `session.id`, candidate counts per target |
| Extractor | `LLM` | proposed candidates, token/cost |
| **Leakage gate** | `GUARDRAIL` | pass/quarantine/reject + reason (entity-agnostic enforcement) |
| Dedup / conflict | `CHAIN` | near-match hits, `hit_count` increments, conflict flags |
| Promotion / routing | `CHAIN` | target store, candidate vs. review-queue |

The leakage gate as a `GUARDRAIL` span makes entity-leakage prevention auditable per session.

## Redaction & PII (non-negotiable)

Traces must not become a PII or secret leak:

- **Never log** the JWT or raw scope token. Log a **scope identifier/hash** only.
- **Mask result rows and bound slot values** — they contain HR PII. Use OpenInference hide flags:
  `OPENINFERENCE_HIDE_INPUTS` / `HIDE_OUTPUTS` / `HIDE_INPUT_MESSAGES` / `HIDE_OUTPUT_MESSAGES` /
  `HIDE_INPUT_TEXT` / `HIDE_EMBEDDING_VECTORS`, plus a custom redactor on tool I/O.
- Log **shape, not content**: row counts, column names, types, latency, status — not cell values.
- Scratch-table contents and uploaded external data are **never** put in span payloads.
- Redaction is consistent with the entity-agnostic governance in
  [06-security-and-governance.md](06-security-and-governance.md): traces are operational telemetry,
  not a data store.

## What we get

- **Debugging:** full trace tree per turn — which blueprint, which SQL, where latency went.
- **Cost/latency:** token + cost per `LLM` span, p95 per tool, blueprint-vs-raw-loop comparison.
- **Quality signal:** online evals (hallucination, SQL correctness, retrieval relevance) annotate
  spans; failures feed the open-questions backlog and the learning loop's review inbox.
- **Eval harness:** golden Q&A canaries run as Phoenix experiments; also a blueprint validation
  method (golden-input replay) from [04-blueprints.md](04-blueprints.md).

---

**Status:** Locked (approach); instrumentation detail Partial
**Open questions:**
- Phoenix hosting (namespace, storage retention for traces).
- Exact redactor implementation + which attributes are allow-listed.
- Online eval set (which judges run live vs. batch).
- Trace sampling rate (100% vs. sampled) given volume + PII retention policy.
