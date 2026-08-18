"""Per-tool-result provenance capture: the USES set attached to each successful call.

`runQuery` derives columns from its SQL; `sampleRows` is all columns of its table;
every other read tool has none — `getTableSchema` included, and it must stay that way
or a fetched schema fails the D44 subset check under a narrow scope and vanishes.
D44 contract: `None` == undetermined, dropped from replay; `frozenset()` == kept.
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
    """Compute the USES-set provenance for one successful tool call."""
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
