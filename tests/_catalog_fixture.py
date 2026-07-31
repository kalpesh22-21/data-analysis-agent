"""Shared catalog-fixture helpers (D75 Wave 1b).

After `databaseSchemaDocs/` is deleted, every test that used to build the catalog
from the local YAML directory rebuilds it from the committed, frozen export fixture
`tests/fixtures/catalog_export.json` instead — the SAME payload the MCP's
`GET /catalog/export` serves and the runtime `CatalogCache` consumes.

These are plain module-level helpers (callable from any test body, fixture, or
module scope). `tests/conftest.py` also exposes pytest fixtures wrapping them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from data_agent.runtime.provenance.catalog_handle import (
    CatalogHandle,
    SemanticCatalogHandle,
    load_catalog_handles_from_export,
)

CATALOG_EXPORT_PATH = Path(__file__).parent / "fixtures" / "catalog_export.json"


def load_catalog_export() -> dict[str, Any]:
    """Return the parsed `{"catalog_sha": ..., "catalog": {...}}` export dict."""
    with CATALOG_EXPORT_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def fixture_catalog() -> dict[str, Any]:
    """Return just the `catalog` value (`{db.table: <entry>}`) from the export."""
    return load_catalog_export()["catalog"]


def fixture_catalog_handle() -> CatalogHandle:
    """Build the immutable `CatalogHandle` from the frozen export fixture."""
    return load_catalog_handles_from_export(load_catalog_export())[0]


def fixture_semantic_catalog_handle() -> SemanticCatalogHandle:
    """Build the immutable `SemanticCatalogHandle` from the frozen export fixture."""
    return load_catalog_handles_from_export(load_catalog_export())[1]
