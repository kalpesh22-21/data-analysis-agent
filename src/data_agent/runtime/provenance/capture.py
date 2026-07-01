"""Per-tool-result provenance capture (design §3.3 table).

| Tool | Provenance source |
|---|---|
| `runQuery` | `extract_column_provenance(sql, catalog_schema, session_id=...)`, run via `asyncio.to_thread` |
| `sampleRows`, `getTableSchema` | Declarative: all columns of the referenced table (`SELECT *` semantics) |
| `listDatabases`, `listTables`, `explainQuery` | No provenance — always `frozenset()` |

Return-value contract (D44 fail-closed — see `session/models.py` module
docstring for the full rationale):
    - `None` == undetermined (parse failure, or an uncatalogued table for
      `sampleRows`/`getTableSchema`) — ALWAYS dropped from replay by
      `context/scope_filter.py`, regardless of scope.
    - `frozenset()` (empty, non-None) == determined, zero columns referenced —
      trivially in-scope, always kept.
    - A non-empty `frozenset[tuple[str, str]]` == the determined USES set.

Async/sync boundary (design §3.3, explicit callout): `extract_column_provenance`
is pure CPU-bound (a sqlglot parse + optimizer pass), not I/O, but runs inside
an event loop also juggling concurrent MCP calls. `asyncio.to_thread` avoids
one large/pathological query blocking the loop; correctness (fail-closed on
parse failure) is identical whether run inline or in a thread.
"""

from __future__ import annotations

import asyncio
from typing import Any

from data_agent.sqlparse import ProvenanceExtractionError, extract_column_provenance

from .catalog_handle import CatalogHandle

# Tools that expose no column-level data — always recorded with empty (not
# undetermined) provenance, and therefore never gated by the scope filter.
_NO_PROVENANCE_TOOLS = frozenset({"listDatabases", "listTables", "explainQuery"})

# Tools whose provenance is "every column of the referenced table" (SELECT *
# semantics) — sampleRows/getTableSchema per design §3.3.
_DECLARATIVE_ALL_COLUMNS_TOOLS = frozenset({"sampleRows", "getTableSchema"})


async def capture_provenance(
    tool_name: str,
    args: dict[str, Any],
    catalog: CatalogHandle,
    *,
    session_id: str | None,
) -> frozenset[tuple[str, str]] | None:
    """Compute the USES-set provenance for one successful tool call.

    Called only on the MCP-success path (a denied/errored call never reaches
    here — `dispatch/tool_dispatcher.py` short-circuits on `MCPToolError`).
    """
    if tool_name == "runQuery":
        sql = args.get("sql", "")
        try:
            return await asyncio.to_thread(
                extract_column_provenance,
                sql,
                {table: dict(columns) for table, columns in catalog.schema.items()},
                session_id=session_id,
            )
        except ProvenanceExtractionError:
            # Runtime's own independent re-parse failed — fail-closed (D63/D44).
            # The live call already succeeded (the MCP's own parse gated it),
            # but this trail entry cannot be safely replayed without a trusted
            # USES set, so it is marked undetermined (see OQ-B: version skew
            # between this repo's extractor and clickhouse-api's copy).
            return None

    if tool_name in _DECLARATIVE_ALL_COLUMNS_TOOLS:
        database = args.get("database", "")
        table = args.get("table", "")
        columns = catalog.columns_for(database, table)
        if columns is None:
            # Uncatalogued table — undetermined, fail-closed (drop from replay).
            return None
        return frozenset((f"{database}.{table}", column) for column in columns)

    if tool_name in _NO_PROVENANCE_TOOLS:
        return frozenset()

    # Unknown tool name reached the capture step — treat conservatively as
    # undetermined rather than silently assuming no data exposure.
    return None
