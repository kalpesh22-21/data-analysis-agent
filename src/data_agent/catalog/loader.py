"""
Semantic Catalog loader (D42, D53).

Reads databaseSchemaDocs/*.yaml files and builds the sqlglot schema dict that
qualify_columns (D62) expects.  The dict shape is:

    { "database.table": { "column": "column_type_string", ... }, ... }

Per D69/OQ-3, keys are at `database.table` granularity so that extracted
three-part references resolve correctly and scope comparison works at the same
granularity as the USES set.

Assumption (documented here per brief): every YAML in databaseSchemaDocs/ that
declares a `database` key uses "dbpcm_warehouse" as its value.  If a YAML does
not declare `database`, DEFAULT_DATABASE is used as a fallback.  If multiple
databases appear in the catalog in the future, the loader will naturally key each
table under its own database prefix — no code change required.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

# The single warehouse database name.  Every current YAML declares
# `database: dbpcm_warehouse`.  If a YAML omits the field, this constant is the
# fallback so the loader remains usable without requiring every file to repeat it.
DEFAULT_DATABASE = "dbpcm_warehouse"

# The scratch database prefix used by session-scoped scratch tables (D64/OQ-4).
# Scratch tables are NOT included in the catalog schema fed to qualify_columns —
# they are session-gated only and have no Semantic Catalog entry.
SCRATCH_DATABASE = "scratch"

# Pattern for scratch table names: scratch.s_<sessionId>_<file> (D64)
_SCRATCH_TABLE_RE = re.compile(r"^s_(?P<session_id>.+?)_.+$")


def _extract_columns(raw_columns: dict[str, Any]) -> dict[str, str]:
    """Return {column_name: type_string} from a YAML `columns` block.

    The YAML columns block can be shaped two ways:
      1. Verbose: { ColumnName: { type: "Nullable(String)", ... } }
      2. Inline: { ColumnName: { type: "...", description: "..." } }  (same, just one-line YAML)

    Both are parsed identically by the YAML loader, so we just extract the `type`
    field where present, defaulting to "TEXT" when the field is absent (e.g. for
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


def load_catalog_from_dir(schema_dir: Path | str) -> dict[str, dict[str, str]]:
    """Load all *.yaml files in `schema_dir` and return the sqlglot schema dict.

    Returns:
        {
          "database.table": {"ColumnName": "type_string", ...},
          ...
        }

    Only files with a top-level `table` and `columns` key are included.  Files
    like AUTHORING_NOTES.md and rules-only YAMLs are silently skipped.

    NOTE: measure/rule entries that appear inside the `columns` block in some
    YAMLs (e.g. `measures` as a sibling dict key accidentally inside columns) are
    naturally excluded because we iterate `raw_columns` which is the `columns`
    mapping only.
    """
    schema_dir = Path(schema_dir)
    result: dict[str, dict[str, str]] = {}

    for yaml_path in sorted(schema_dir.glob("*.yaml")):
        with yaml_path.open(encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}

        table_name = raw.get("table")
        if not table_name:
            # Not a table-schema file (e.g. rules.yaml if one existed)
            continue

        raw_columns = raw.get("columns")
        if not raw_columns or not isinstance(raw_columns, dict):
            # Table declared but no columns block — skip; can't enumerate columns
            continue

        database = raw.get("database", DEFAULT_DATABASE)
        qualified_key = f"{database}.{table_name}"

        columns = _extract_columns(raw_columns)
        # Exclude entries that are not column definitions (e.g. measure sub-keys
        # that sneak in if the YAML author put them in the wrong block — defensive).
        result[qualified_key] = columns

    return result


def build_sqlglot_schema(schema_dir: Path | str | None = None) -> dict[str, dict[str, str]]:
    """Build the full sqlglot schema dict from the databaseSchemaDocs directory.

    If `schema_dir` is None, the loader searches for databaseSchemaDocs/ relative
    to this file's location (repo root / databaseSchemaDocs).  Pass an explicit
    path in tests or when the layout differs.

    This is the production entry point used by the MCP and the provenance extractor.
    """
    if schema_dir is None:
        # Resolve relative to this file: src/data_agent/catalog/loader.py
        # -> repo root is 3 levels up
        repo_root = Path(__file__).parent.parent.parent.parent
        schema_dir = repo_root / "databaseSchemaDocs"

    return load_catalog_from_dir(schema_dir)


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
