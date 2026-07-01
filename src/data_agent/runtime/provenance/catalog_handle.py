"""CatalogHandle — a read-only, shared, immutable handle around the Semantic Catalog schema.

`data_agent.catalog.build_sqlglot_schema()` is pure/deploy-coupled (D53) and is
called **once** at process startup; the resulting dict is handed to
`ContextAssembler`/`ToolDispatcher` as a read-only handle — never a mutable
global. The same handle is reused for every session and request (design §2).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from data_agent.catalog import build_sqlglot_schema


class CatalogHandle:
    """Immutable, read-only view over the sqlglot catalog schema dict."""

    def __init__(self, schema: dict[str, dict[str, str]]) -> None:
        # Deep-freeze one level down (per-table column dicts) so callers cannot
        # mutate the shared catalog through the handle.
        self._schema: Mapping[str, Mapping[str, str]] = MappingProxyType(
            {table: MappingProxyType(dict(columns)) for table, columns in schema.items()}
        )

    @property
    def schema(self) -> Mapping[str, Mapping[str, str]]:
        """The full `database.table` -> `{column: type}` mapping (read-only)."""
        return self._schema

    def columns_for(self, database: str, table: str) -> frozenset[str] | None:
        """Return the column-name set for `database.table`, or None if uncatalogued."""
        columns = self._schema.get(f"{database}.{table}")
        if columns is None:
            return None
        return frozenset(columns)

    def is_catalogued(self, database: str, table: str) -> bool:
        return f"{database}.{table}" in self._schema


def load_catalog_handle(schema_dir: Path | str | None = None) -> CatalogHandle:
    """Build the process-wide `CatalogHandle` from `databaseSchemaDocs/` (called once at startup)."""
    return CatalogHandle(build_sqlglot_schema(schema_dir))
