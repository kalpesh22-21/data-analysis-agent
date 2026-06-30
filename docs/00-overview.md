# 00 — Overview

## The problem

Internal users need answers from the HR data warehouse without writing SQL or knowing which of
many flattened tables to use. The warehouse covers **employee information, payroll, and time &
attendance**, exposed as flattened views that mostly join on `employee_code`. Users also bring
**external datasets** (xlsx/csv) that join to HR data via keys like `EmployeeCode`,
`DepartmentCode`/`DepartmentName`.

The agent must behave like a trusted teammate: conversational, transparent about the SQL it runs,
able to ask for clarification, and improving over time.

## Lineage: OpenAI's in-house data agent

We adopt the core pattern from OpenAI's in-house data agent:

- **One capable LLM** per request that writes SQL, inspects results, self-corrects, and verifies.
- **A runtime orchestrator** that loops: model output → tool calls → results fed back → repeat.
- **A rich context-assembly layer** — the real engineering — that grounds the model.
- **A small, curated, non-overlapping tool set.**
- **Memory** so past corrections are not repeated.
- **Evaluation** via curated Q&A with golden SQL, graded by result set, run as canaries.

## Where we differ from OpenAI

Our warehouse is **curated** (low table duplicity), so several of OpenAI's six context layers are
unnecessary. Our mapping:

| OpenAI layer | Our decision |
|---|---|
| 1. Table usage metadata | **Skip** — curated warehouse, low duplicity. |
| 2. Human annotations | **In `getTableSchema`** — rich semantic YAML (entities, grain, columns, synonyms, rules, ambiguities). |
| 3. Codex enrichment | **Skip** — not needed. |
| 4. Institutional knowledge | **RAG tool** over knowledge docs. |
| 5. Memory | **Global knowledge** (lessons from past mistakes) + **Blueprints** (reusable query DAGs). |
| 6. Runtime context | **Skip** — tables rarely mutate; changes flow into `getTableSchema`. |

Our defining addition is the **blueprint**: a parameterized, validated query DAG stored in neo4j
that acts as a learned, verified fast-path. And our **learning loop** runs after every chat session
to mine new blueprints, global knowledge, user knowledge, and schema-edit proposals from the
transcript — under a strict **entity-agnostic** constraint for everything global.

## Goals

- Trustworthy answers: always show SQL, verify results, ask when genuinely ambiguous.
- Get faster and more accurate over time via blueprints + memory.
- Enforce column-level access at every layer.
- Keep the agent's tool surface small and its context clean.

## Non-goals (for now)

- Write access to the warehouse from the agent path (read-only by design).
- Per-query generated apps (OpenAI's future direction) — revisit later.

---

**Status:** Locked
**Open questions:** none at the overview level.
