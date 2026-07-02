"""CatalogHandle — a read-only, shared, immutable handle around the Semantic Catalog schema.

`data_agent.catalog.build_sqlglot_schema()` is pure/deploy-coupled (D53) and is
called **once** at process startup; the resulting dict is handed to
`ContextAssembler`/`ToolDispatcher` as a read-only handle — never a mutable
global. The same handle is reused for every session and request (design §2).

`SemanticCatalogHandle` (runblueprint-design F3/§4.2) is its grain/measures/temporal
sibling: the runtime `CatalogHandle` carries only `{column: type}`, but the D56
verify gate needs GRAIN and the D65 temporal gate needs TEMPORAL DIMENSIONS — both
live only in `load_semantic_catalog()` (the full YAML overlay). This handle surfaces
per-table `grain`, `grain_verifiable`, `temporal`, and per-measure `{column, agg,
defined_over}` to the runtime as a read-only, deploy-coupled, immutable view built
once at startup — mirroring `CatalogHandle`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from data_agent.catalog import build_sqlglot_schema
from data_agent.catalog.loader import load_semantic_catalog


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

    Built ONCE at startup from `load_semantic_catalog()`; deploy-coupled + immutable,
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
    temporal = MappingProxyType(dict(temporal_raw)) if isinstance(temporal_raw, dict) else MappingProxyType({})
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


def load_semantic_catalog_handle(schema_dir: Path | str | None = None) -> SemanticCatalogHandle:
    """Build the process-wide `SemanticCatalogHandle` from `databaseSchemaDocs/`
    (called once at startup, mirroring `load_catalog_handle`, F3)."""
    return SemanticCatalogHandle(load_semantic_catalog(schema_dir))


__all__ = [
    "CatalogHandle",
    "Measure",
    "SemanticCatalogHandle",
    "TableGrain",
    "load_catalog_handle",
    "load_semantic_catalog_handle",
]
