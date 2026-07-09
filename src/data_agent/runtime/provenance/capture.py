"""Per-tool-result provenance capture (design §3.3 table).

| Tool | Provenance source |
|---|---|
| `runQuery` | `extract_column_provenance(sql, catalog_schema, session_id=...)`, run via `asyncio.to_thread` |
| `sampleRows` | Declarative: all columns of the referenced table (`SELECT *` semantics) |
| `listDatabases`, `listTables`, `explainQuery`, `getTableSchema` | No provenance — always `frozenset()` |

`getTableSchema` vs `sampleRows` (the distinction that keeps a fetched schema
replayable, 2026-07-09):
    - `sampleRows` is `SELECT * LIMIT n` — it returns real CELL VALUES from
      EVERY column of the table, so its provenance is genuinely "all columns"
      and a narrow `column_scope` correctly drops it from replay.
    - `getTableSchema` returns column METADATA ONLY (names/types/descriptions,
      NO cell values), and the MCP itself scope-filters that metadata to the
      in-scope columns before returning it (verified in
      `clickhouse-api/app/service.py::get_table_schema` ->
      `app/semantic_catalog/overlay.py::build_table_schema_response`: when
      `column_scope` is non-empty the merged `columns`, plus grain/primary_key/
      join_keys/measures/rules/ambiguities/temporal, are all filtered to the
      in-scope set). A getTableSchema RESULT therefore can never carry
      out-of-scope column info, so it must NOT carry `SELECT *`/all-columns
      provenance. It is a `_NO_PROVENANCE_TOOLS` member: safe-empty
      (`frozenset()`) provenance, ALWAYS replayable (empty set ⊆ any scope),
      D57-clean. Previously it lived in `_DECLARATIVE_ALL_COLUMNS_TOOLS`, which
      made a successfully-fetched schema fail the D44 subset check under a
      restricted scope and vanish from context (the model then saw the
      repeated-read guard's "you already have this" nudge with no schema
      anywhere — the bug this fixes).

Return-value contract (D44 fail-closed — see `session/models.py` module
docstring for the full rationale):
    - `None` == undetermined (parse failure, or an uncatalogued table for
      `sampleRows`) — ALWAYS dropped from replay by
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
# `getTableSchema` returns MCP-scope-filtered column METADATA (names/types/
# descriptions, no cell values — see the module docstring's getTableSchema vs
# sampleRows note), so its result can never carry out-of-scope column info:
# safe-empty provenance, always replayable, and a fetched schema survives a
# restricted `column_scope` (the 2026-07-09 fix).
# `searchBlueprints` (thin cards, no `uses` in the result) and `searchKnowledge`
# (entity-agnostic prose) are read-path siblings — genuinely zero-column, so
# `frozenset()` is correct; they are intercepted in the loop and set it
# themselves, so this is pure defense-in-depth if one were ever routed through
# `dispatch`. `getBlueprint` is deliberately NOT here: its provenance is the
# blueprint's `uses` FOOTPRINT (S1), so an empty default would be fail-OPEN —
# an ever-dispatched getBlueprint falls through to the unknown-tool `None`
# (undetermined → dropped fail-closed), which is the safe posture.
_NO_PROVENANCE_TOOLS = frozenset(
    {
        "listDatabases",
        "listTables",
        "explainQuery",
        "getTableSchema",
        "searchBlueprints",
        "searchKnowledge",
    }
)

# Tools whose provenance is "every column of the referenced table" (SELECT *
# semantics). Only `sampleRows` qualifies: it returns real CELL VALUES from all
# columns (`SELECT * LIMIT n`). `getTableSchema` is deliberately NOT here — it
# returns MCP-scope-filtered metadata only, so it belongs in
# `_NO_PROVENANCE_TOOLS` above (design §3.3, revised 2026-07-09).
_DECLARATIVE_ALL_COLUMNS_TOOLS = frozenset({"sampleRows"})


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
