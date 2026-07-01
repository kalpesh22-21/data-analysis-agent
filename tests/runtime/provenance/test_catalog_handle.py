"""Unit tests for provenance/catalog_handle.py (Layer 1 — no infra)."""

from __future__ import annotations

import pytest

from data_agent.runtime.provenance.catalog_handle import CatalogHandle


@pytest.fixture
def handle() -> CatalogHandle:
    return CatalogHandle(
        {
            "dbpcm_warehouse.employee": {"EmployeeCode": "String", "Department": "Nullable(String)"},
            "dbpcm_warehouse.payroll": {"Amount": "Nullable(Decimal(18, 6))"},
        }
    )


def test_columns_for_catalogued_table(handle: CatalogHandle) -> None:
    columns = handle.columns_for("dbpcm_warehouse", "employee")
    assert columns == frozenset({"EmployeeCode", "Department"})


def test_columns_for_uncatalogued_table_is_none(handle: CatalogHandle) -> None:
    assert handle.columns_for("dbpcm_warehouse", "nonexistent") is None


def test_is_catalogued(handle: CatalogHandle) -> None:
    assert handle.is_catalogued("dbpcm_warehouse", "employee") is True
    assert handle.is_catalogued("dbpcm_warehouse", "nonexistent") is False


def test_schema_is_read_only(handle: CatalogHandle) -> None:
    with pytest.raises(TypeError):
        handle.schema["dbpcm_warehouse.employee"]["NewColumn"] = "String"  # type: ignore[index]


def test_schema_top_level_is_read_only(handle: CatalogHandle) -> None:
    with pytest.raises(TypeError):
        handle.schema["new.table"] = {}  # type: ignore[index]
