"""Immutable, read-only handles over the Semantic Catalog, built once per catalog load.

`CatalogHandle` carries `{column: type}` for provenance extraction;
`SemanticCatalogHandle` carries the grain/temporal/measures view the D56 verify gate
and the D65 temporal gate read. Both are shared across every session and request —
never mutated, never a mutable global.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from data_agent.catalog.loader import (
    build_sqlglot_schema_from_catalog,
    load_description_cols_from_catalog,
    load_semantic_catalog_from_catalog,
)


class CatalogHandle:
    """Immutable, read-only view over the sqlglot catalog schema dict."""

    def __init__(
        self,
        schema: dict[str, dict[str, str]],
        description_cols: dict[str, dict[str, str]] | None = None,
    ) -> None:
        # Deep-freeze one level down (per-table column dicts) so callers cannot
        # mutate the shared catalog through the handle.
        self._schema: Mapping[str, Mapping[str, str]] = MappingProxyType(
            {table: MappingProxyType(dict(columns)) for table, columns in schema.items()}
        )
        # `description_cols` maps a code column to its declared sibling label
        # column, per `database.table` (resolveValues embeds both jointly). Frozen
        # the same way as `schema`; None == no declared links.
        self._description_cols: Mapping[str, Mapping[str, str]] = MappingProxyType(
            {
                table: MappingProxyType(dict(links))
                for table, links in (description_cols or {}).items()
            }
        )

    @property
    def schema(self) -> Mapping[str, Mapping[str, str]]:
        """The full `database.table` -> `{column: type}` mapping (read-only)."""
        return self._schema

    def description_col_for(self, db_table: str, column: str) -> str | None:
        """Return *column*'s declared sibling description column on `db_table`, or None.

                AUTHORED linkage only — not validated against the schema or the caller's scope
                here; `resolve_target` re-checks existence and scope before using it.
        """
        table_links = self._description_cols.get(db_table)
        if table_links is None:
            return None
        return table_links.get(column)

    def columns_for(self, database: str, table: str) -> frozenset[str] | None:
        """Return the column-name set for `database.table`, or None if uncatalogued."""
        columns = self._schema.get(f"{database}.{table}")
        if columns is None:
            return None
        return frozenset(columns)

    def is_catalogued(self, database: str, table: str) -> bool:
        return f"{database}.{table}" in self._schema


def load_catalog_handle_from_catalog(catalog: dict[str, Any]) -> CatalogHandle:
    """Build a `CatalogHandle` from a parsed catalog dict (the export's `catalog` value)."""
    return CatalogHandle(
        build_sqlglot_schema_from_catalog(catalog),
        load_description_cols_from_catalog(catalog),
    )


@dataclass(frozen=True)
class Measure:
    """One catalog measure (`{column, agg, defined_over}`).

        `defined_over` is best-effort prose, not a structural contract.
    """

    column: str | None
    agg: str | None
    defined_over: str | None


@dataclass(frozen=True)
class TableGrain:
    """The per-table grain/temporal/measures view the runtime gates consume (F3)."""

    grain: tuple[str, ...]
    grain_verifiable: bool
    temporal: Mapping[str, Any]
    measures: Mapping[str, Measure]


class SemanticCatalogHandle:
    """Immutable per-table `grain`/`grain_verifiable`/`temporal`/`measures` view over the
        full catalog overlay. Built once per load; read-only.
    """

    def __init__(self, semantic_catalog: Mapping[str, Mapping[str, Any]]) -> None:
        tables: dict[str, TableGrain] = {}
        for db_table, entry in semantic_catalog.items():
            tables[db_table] = _table_grain(entry)
        self._tables: Mapping[str, TableGrain] = MappingProxyType(tables)

    def grain_for(self, db_table: str) -> TableGrain | None:
        """The `TableGrain` for `database.table`, or `None` if uncatalogued."""
        return self._tables.get(db_table)

    def is_grain_verifiable(self, db_table: str) -> bool:
        """True iff the table declares a verifiable grain (a COUNT(*) == COUNT(DISTINCT)
                conformance probe applies).
        """
        grain = self._tables.get(db_table)
        return bool(grain and grain.grain_verifiable)

    @property
    def tables(self) -> Mapping[str, TableGrain]:
        return self._tables


def _table_grain(entry: Mapping[str, Any]) -> TableGrain:
    grain_raw = entry.get("grain") or []
    grain = tuple(str(c) for c in grain_raw) if isinstance(grain_raw, list) else ()
    # `grain_verifiable` defaults TRUE when the YAML omits it (employee declares a
    # real grain and no flag); payroll sets it explicitly false.
    grain_verifiable = entry.get("grain_verifiable", True)
    if not isinstance(grain_verifiable, bool):
        grain_verifiable = True
    temporal_raw = entry.get("temporal") or {}
    temporal = (
        MappingProxyType(dict(temporal_raw))
        if isinstance(temporal_raw, dict)
        else MappingProxyType({})
    )
    measures_raw = entry.get("measures") or {}
    measures: dict[str, Measure] = {}
    if isinstance(measures_raw, dict):
        for name, spec in measures_raw.items():
            if isinstance(spec, dict):
                measures[name] = Measure(
                    column=spec.get("column"),
                    agg=spec.get("agg"),
                    defined_over=spec.get("defined_over"),
                )
    return TableGrain(
        grain=grain,
        grain_verifiable=grain_verifiable,
        temporal=temporal,
        measures=MappingProxyType(measures),
    )


def load_semantic_catalog_handle_from_catalog(catalog: dict[str, Any]) -> SemanticCatalogHandle:
    """Build a `SemanticCatalogHandle` from a parsed catalog dict (the export's `catalog`
        value, which is itself the full overlay).
    """
    return SemanticCatalogHandle(load_semantic_catalog_from_catalog(catalog))


def load_catalog_handles_from_export(
    export: dict[str, Any],
) -> tuple[CatalogHandle, SemanticCatalogHandle]:
    """Build both catalog handles from one parsed `/catalog/export` payload."""
    catalog = export["catalog"]
    return (
        load_catalog_handle_from_catalog(catalog),
        load_semantic_catalog_handle_from_catalog(catalog),
    )


__all__ = [
    "CatalogHandle",
    "Measure",
    "SemanticCatalogHandle",
    "TableGrain",
    "load_catalog_handle_from_catalog",
    "load_catalog_handles_from_export",
    "load_semantic_catalog_handle_from_catalog",
]
