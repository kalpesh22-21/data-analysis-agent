"""CatalogHandle — a read-only, shared, immutable handle around the Semantic Catalog schema.

`catalog/loader.py`'s projections are pure/deploy-coupled (D53) and are applied
**once** per catalog load (`load_catalog_handles_from_export`, from the MCP
`/catalog/export` payload); the resulting dict is handed to
`ContextAssembler`/`ToolDispatcher` as a read-only handle — never a mutable
global. The same handle is reused for every session and request (design §2).

`SemanticCatalogHandle` (runblueprint-design F3/§4.2) is its grain/measures/temporal
sibling: the runtime `CatalogHandle` carries only `{column: type}`, but the D56
verify gate needs GRAIN and the D65 temporal gate needs TEMPORAL DIMENSIONS — both
live only in the FULL catalog overlay (`load_semantic_catalog_from_catalog`). This
handle surfaces per-table `grain`, `grain_verifiable`, `temporal`, and per-measure
`{column, agg, defined_over}` to the runtime as a read-only, deploy-coupled,
immutable view built once per load — mirroring `CatalogHandle`.
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
        """Return *column*'s declared sibling description column on the qualified
        `db_table` (e.g. `"dbpcm_warehouse.payroll"`), or None if none is declared.

        This is the AUTHORED linkage only — it is NOT validated against the schema
        or the caller's scope here (resolve_target still checks existence + scope
        before using it, so an author typo or out-of-scope target falls back to the
        naming convention rather than crashing)."""
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
    """Build a `CatalogHandle` from a parsed catalog dict (the MCP `/catalog/export`
    `catalog` value, or any `{db.table: <entry>}` mapping): the schema `{col: type}`
    plus the authored description-col linkage."""
    return CatalogHandle(
        build_sqlglot_schema_from_catalog(catalog),
        load_description_cols_from_catalog(catalog),
    )


@dataclass(frozen=True)
class Measure:
    """One catalog measure (`{column, agg, defined_over}`, F3/§4.2).

    `defined_over` is BEST-EFFORT PROSE (OPEN-QUESTIONS §measures) — so the
    structural measure-agg check is deferred to the Phase-2 authoring gate (D37b,
    §4.3); Phase-1 surfaces it for observability/completeness only.
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
    """Immutable, read-only per-table `grain`/`grain_verifiable`/`temporal`/`measures`
    view over the full Semantic Catalog overlay (F3/§4.2).

    Built ONCE per catalog load from the full overlay; deploy-coupled + immutable,
    exactly like `CatalogHandle`. Read-only: it is consumed by the D56 verify
    assertion (the result-grain probe) and the D65 temporal gate — never mutated.
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
        """True iff the table declares a verifiable grain (a COUNT(*)==COUNT(DISTINCT)
        conformance probe applies). `payroll` declares `grain_verifiable: false`."""
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
    """Build a `SemanticCatalogHandle` from a parsed catalog dict (the MCP export's
    `catalog` value). The export IS the full overlay, so this is the identity view
    the grain/temporal/measures projection reads from."""
    return SemanticCatalogHandle(load_semantic_catalog_from_catalog(catalog))


def load_catalog_handles_from_export(
    export: dict[str, Any],
) -> tuple[CatalogHandle, SemanticCatalogHandle]:
    """Build BOTH the `CatalogHandle` and `SemanticCatalogHandle` from one parsed
    `/catalog/export` payload (`{"catalog_sha": ..., "catalog": {...}}`).

    This is the single entry point the runtime `CatalogCache` (and the shared test
    fixture) use to build the two immutable handles from the MCP export. The export's
    `catalog` value carries the verbatim per-table entries, so the description-col
    linkage the `CatalogHandle` exposes is derived from the SAME entries."""
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
