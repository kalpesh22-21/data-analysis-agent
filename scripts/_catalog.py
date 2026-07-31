"""Offline catalog loading for the standalone `scripts/` (D75 Wave 1b).

`databaseSchemaDocs/` has been deleted: the semantic catalog now comes from the
MCP `GET /catalog/export` route, or from the committed frozen snapshot
`tests/fixtures/catalog_export.json` (the SAME payload that route serves). The
runtime and the test suite are already migrated; these helpers give the
standalone scripts the same catalog WITHOUT importing anything under `tests/`.

The scripts here hold no per-request MCP JWT, so they read the FROZEN export
snapshot resolved by `RuntimeSettings.catalog_fixture_file()` — the committed
fixture by default, or whatever `CATALOG_FIXTURE_PATH` points at. To refresh
against the LIVE catalog without a code change, an operator dumps the MCP's
`GET /catalog/export` body to a file and sets `CATALOG_FIXTURE_PATH` to it.

Two projections, mirroring `load_catalog_handles_from_export` / the from-catalog
loaders the runtime uses:
  * `catalog_dict()`   — the raw `{db.table: <entry>}` mapping (feeds both the
    sqlglot schema and the `rules[*].id` grounding the extractor needs).
  * `catalog_handle()` — the immutable `CatalogHandle` (provenance/scope).
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.provenance.catalog_handle import (
    CatalogHandle,
    load_catalog_handle_from_catalog,
)


def catalog_fixture_path(settings: RuntimeSettings | None = None) -> str:
    """The frozen export path these scripts read (for logging/documentation)."""
    return str((settings or RuntimeSettings()).catalog_fixture_file())


def catalog_export(settings: RuntimeSettings | None = None) -> dict[str, Any]:
    """Parse the frozen `{"catalog_sha": ..., "catalog": {...}}` export snapshot."""
    path = (settings or RuntimeSettings()).catalog_fixture_file()
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def catalog_dict(settings: RuntimeSettings | None = None) -> dict[str, Any]:
    """Return just the `catalog` value (`{db.table: <entry>}`) from the export."""
    return catalog_export(settings)["catalog"]


def catalog_handle(settings: RuntimeSettings | None = None) -> CatalogHandle:
    """Build the immutable `CatalogHandle` from the frozen export snapshot."""
    return load_catalog_handle_from_catalog(catalog_dict(settings))
