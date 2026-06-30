# 07 — External Data

Users bring **external datasets** (xlsx/csv) that join to HR data via keys like `EmployeeCode`,
`DepartmentCode`/`DepartmentName`.

## Why ClickHouse scratch, not a second engine

We considered a runtime DuckDB sandbox and rejected it. A second engine means:
- a **second SQL dialect**,
- **copying large HR result-sets out** of the warehouse,
- **governance/access-control split** across two places,
- **forked blueprints** ("pure-ClickHouse" vs. "sandbox" DAGs).

Instead: a **writable, session-scoped scratch schema inside ClickHouse**.

## Mechanism

```
upload (xlsx/csv)
   │  via PRIVILEGED INGESTION SIDE-CHANNEL (platform action, NOT an agent tool)
   ▼
scratch.s_<sessionId>_<file>   (TTL'd, access-scoped)
   │
   ▼
agent discovers it via listTables / getTableSchema  (unchanged read-only MCP)
   │
   ▼
runQuery JOINs scratch.s_… ON employee_code / department_code
```

- The agent's read-only MCP is **unchanged** in shape — `runQuery` can join the scratch table because
  the scratch db is in the allowlist — but every `runQuery` referencing a `scratch.*` table is checked
  for **session ownership**: the table must match `s_<injected session_id>_*`, else it is rejected
  (D64, [06-security-and-governance.md](06-security-and-governance.md) §Scratch isolation).
- **Zero new data-plane tools.** One SQL dialect. Joins run at warehouse scale with ClickHouse's
  access controls intact.
- Cost vs. DuckDB: needs a **separate privileged connection** for scratch writes (not the agent's).
  Cheap since we control the warehouse.

## Column mapping

On upload, the UI runs a **column-mapping step** — which uploaded column is `EmployeeCode` /
`DepartmentCode` / `DepartmentName` — before the scratch table is queryable.

## Reuse for blueprint intermediates

The same scratch schema materializes **large blueprint intermediates** (see
[04-blueprints.md](04-blueprints.md) §Pass intermediates). Small scalars/tables inline as CTE/`VALUES`;
large tables land in scratch. One mechanism for both.

## Governance guardrails

- Scratch tables: **session-scoped**, **access-scoped**, **TTL'd** (auto-drop). Session-scoping is
  **enforced**, not just named: the MCP rejects any `runQuery` whose `scratch.*` reference doesn't
  belong to the injected `session_id` (D64) — cross-session scratch access is impossible.
- Uploaded data is often PII → it **must not** feed the entity-agnostic global stores.
- Sessions involving uploads are **weak blueprint candidates**. If learned, the extractor generalizes
  the upload into a **"bring-your-own fact table with columns {employee_code, …}" slot** — never bakes
  the specific file or its values.

---

**Status:** Locked
**Open questions:**
- Ingestion side-channel implementation (loader service, supported formats/size caps).
- Scratch naming/TTL policy (shared with [06](06-security-and-governance.md)).
