"""
Semantic Catalog projections (D42, D53, D75 Wave 1b).

Projects a parsed catalog dict — `{"database.table": <verbatim catalog entry>}`,
the shape the MCP's `GET /catalog/export` serves under its `catalog` key — into
the three views the runtime consumes: the sqlglot schema that qualify_columns
(D62) expects, the description-col linkage, and the full semantic overlay. The
sqlglot schema dict shape is:

    { "database.table": { "column": "column_type_string", ... }, ... }

Per D69/OQ-3, keys are at `database.table` granularity so that extracted
three-part references resolve correctly and scope comparison works at the same
granularity as the USES set.

The catalog is NOT read from a local YAML tree: `databaseSchemaDocs/` was removed
in D75 Wave 1b and the dir-reading loaders with it. Entry points: the runtime
`CatalogCache` / `provenance/catalog_handle.py::load_catalog_handles_from_export`
(production) and `tests/_catalog_fixture.py` (tests).
"""

from __future__ import annotations

import re
from typing import Any

# The single warehouse database name — the NAMED ANCHOR for that convention, cited by
# `runtime/config.py` (the default-database setting) and `context/discovery_emulation.py`.
# No code here reads it: the dir-based loader that applied it as a per-file fallback is
# gone (D75 Wave 1b), and every catalog entry from the MCP export carries `database`.
DEFAULT_DATABASE = "dbpcm_warehouse"

# The scratch database prefix used by session-scoped scratch tables (D64/OQ-4).
# Scratch tables are NOT included in the catalog schema fed to qualify_columns —
# they are session-gated only and have no Semantic Catalog entry.
SCRATCH_DATABASE = "scratch"

# Pattern for scratch table names: scratch.s_<sessionId>_<file> (D64)
_SCRATCH_TABLE_RE = re.compile(r"^s_(?P<session_id>.+?)_.+$")


def _extract_columns(raw_columns: dict[str, Any]) -> dict[str, str]:
    """Return {column_name: type_string} from a catalog entry's `columns` block.

    Each column def is a mapping (the authored YAML, verbose or inline, arrives
    parsed the same way through the export), so we just extract the `type` field
    where present, defaulting to "TEXT" when the field is absent (e.g. for
    enum-typed columns whose type is declared differently).
    """
    result: dict[str, str] = {}
    for col_name, col_def in raw_columns.items():
        if not isinstance(col_def, dict):
            # Bare value — treat column as TEXT
            result[col_name] = "TEXT"
            continue
        col_type = col_def.get("type", "TEXT")
        result[col_name] = str(col_type)
    return result


def _extract_description_cols(raw_columns: dict[str, Any]) -> dict[str, str]:
    """Return {code_column: description_column} from a catalog `columns` block.

    A code column declares its sibling human-readable label column via a truthy
    string `description_col` field (e.g. `FieldId` -> `FieldLabel`). Columns whose
    def is not a dict, or that omit / blank / non-string `description_col`, are
    ignored. Casing is preserved exactly as authored (D70).
    """
    result: dict[str, str] = {}
    for col_name, col_def in raw_columns.items():
        if not isinstance(col_def, dict):
            continue
        desc_col = col_def.get("description_col")
        if isinstance(desc_col, str) and desc_col.strip():
            result[col_name] = desc_col.strip()
    return result


# ---------------------------------------------------------------------------
# In-memory catalog cores (D75 Wave 1b).
#
# The three projections below are the SINGLE source of truth for how a parsed
# catalog dict — `{"database.table": <verbatim catalog entry>, ...}` — is turned
# into the sqlglot schema, the description-col linkage, and the semantic overlay.
# Every consumer (runtime CatalogCache, learning plane, tests) goes through them,
# so no two views of one export can drift.
#
# The input `catalog` has the EXACT shape the MCP's `GET /catalog/export` serves
# under its `catalog` key: one entry per `database.table`, each entry carrying at
# least a `columns` block.
# ---------------------------------------------------------------------------


def build_sqlglot_schema_from_catalog(
    catalog: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Project a parsed catalog dict to the sqlglot schema `{db.table: {col: type}}`.

    Applies `_extract_columns` per entry. An entry with no (or a non-dict)
    `columns` block contributes an empty column map.
    """
    result: dict[str, dict[str, str]] = {}
    for qualified_key, entry in catalog.items():
        columns = entry.get("columns")
        result[qualified_key] = _extract_columns(columns) if isinstance(columns, dict) else {}
    return result


def load_description_cols_from_catalog(
    catalog: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Project a parsed catalog dict to `{db.table: {code_col: description_col}}`.

    Applies `_extract_description_cols` per entry (the authored code→label linkage).
    Every table is present as a key; its value is `{}` when it declares no links.
    """
    result: dict[str, dict[str, str]] = {}
    for qualified_key, entry in catalog.items():
        columns = entry.get("columns")
        result[qualified_key] = (
            _extract_description_cols(columns) if isinstance(columns, dict) else {}
        )
    return result


def load_semantic_catalog_from_catalog(
    catalog: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return the full semantic overlay from a parsed catalog dict — IDENTITY.

    The MCP export's `catalog` value IS already the `{db.table: <full entry>}` shape
    the semantic overlay is defined as, so this is a shallow copy for isolation, not
    a transform.
    """
    return dict(catalog)


def is_scratch_table(database: str, table: str, session_id: str) -> bool:
    """Return True iff the table reference is a valid own-session scratch table (D64/OQ-4).

    A scratch table must:
      1. Be in the `scratch` database.
      2. Match the naming pattern `s_<session_id>_<anything>`.

    This is the session-boundary gate described in D64 and D69/OQ-4.  It is
    intentionally separate from column-scope enforcement, which applies only to
    warehouse tables.
    """
    if database != SCRATCH_DATABASE:
        return False
    m = _SCRATCH_TABLE_RE.match(table)
    return m is not None and m.group("session_id") == session_id
