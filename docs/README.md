# HR Data Analysis Agent — Specification

A conversational data-analysis agent over an HR data warehouse (ClickHouse), built on the
fundamentals of OpenAI's in-house data agent but adapted for a **curated** warehouse and a
**blueprint-driven** learning loop.

## Reading order

| # | Doc | What it covers |
|---|-----|----------------|
| 00 | [Overview](00-overview.md) | Problem, OpenAI lineage, where we differ, goals |
| 01 | [Architecture](01-architecture.md) | Two-plane model, components, request lifecycle |
| 02 | [Tools & API](02-tools-and-api.md) | The 11-tool inventory, contracts, injected vs model-visible args |
| 03 | [Context & Retrieval](03-context-and-retrieval.md) | Retrieval pipeline, reranker, progressive disclosure |
| 04 | [Blueprints](04-blueprints.md) | DAG model, execution, lifecycle, scope edges |
| 05 | [Memory & Learning](05-memory-and-learning.md) | Memory-layer mapping + the end-of-session write router |
| 06 | [Security & Governance](06-security-and-governance.md) | JWT/scope injection, entity-agnostic rules, PII |
| 07 | [External Data](07-external-data.md) | CSV/xlsx uploads, scratch schema, joins |
| 08 | [UI](08-ui.md) | UX principles and key flows |
| 09 | [Infrastructure](09-infrastructure.md) | Stores, session management |
| 10 | [Observability](10-observability.md) | Arize Phoenix + OTel tracing, evals, PII redaction (cross-cutting) |
| 11 | [Testing](11-testing.md) | Four-layer pyramid; Docker + Playwright(MCP) spec-conformance suite |

## Decision record

- [decisions/DECISIONS.md](decisions/DECISIONS.md) — locked decisions, dated, with rationale.
- [decisions/OPEN-QUESTIONS.md](decisions/OPEN-QUESTIONS.md) — forks still open.
- [decisions/TRACEABILITY.md](decisions/TRACEABILITY.md) — decision → invariant → test matrix; the enforcement registry. A decision is not done until its tagged test is green in CI.

## Conventions

- Each chapter ends with a **Status** line (`Locked` / `Partial` / `Stub`) and an **Open questions** list.
- "Locked" = agreed in design discussion; "Partial" = direction agreed, details pending; "Stub" = placeholder.
- Diagrams are inline ASCII or mermaid so they live in git with the prose.
