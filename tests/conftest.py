"""Root test fixtures (D75 Wave 1b).

Shared catalog fixtures built from the committed export `tests/fixtures/catalog_export.json`
(the MCP `/catalog/export` payload), so tests no longer read the deleted
`databaseSchemaDocs/` directory. The plain-function equivalents live in
`tests/_catalog_fixture.py` for use at module scope or inside other fixtures.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.provenance.catalog_handle import (
    CatalogHandle,
    SemanticCatalogHandle,
)

from ._catalog_fixture import (
    fixture_catalog,
    fixture_catalog_handle,
    fixture_semantic_catalog_handle,
    load_catalog_export,
)


@pytest.fixture
def catalog_export() -> dict[str, Any]:
    """The parsed `{"catalog_sha": ..., "catalog": {...}}` export fixture."""
    return load_catalog_export()


@pytest.fixture
def catalog_dict() -> dict[str, Any]:
    """The `catalog` value from the export fixture (`{db.table: <entry>}`)."""
    return fixture_catalog()


@pytest.fixture
def catalog_handle() -> CatalogHandle:
    """The immutable `CatalogHandle` built from the export fixture."""
    return fixture_catalog_handle()


@pytest.fixture
def semantic_catalog_handle() -> SemanticCatalogHandle:
    """The immutable `SemanticCatalogHandle` built from the export fixture."""
    return fixture_semantic_catalog_handle()
