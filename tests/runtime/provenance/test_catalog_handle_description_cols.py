"""Unit tests for CatalogHandle.description_col_for (Layer 1 — no infra).

The handle carries the AUTHORED code-col -> label-col linkage as a second frozen
map alongside the schema. `description_col_for` is a pure lookup — it does NOT
validate the target against the schema or scope (resolve_target does that), it
only surfaces what the YAML declared.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_T = "dbpcm_warehouse.personnel_action_form_changes"


@pytest.fixture
def handle() -> CatalogHandle:
    return CatalogHandle(
        {
            _T: {"FieldId": "Nullable(String)", "FieldLabel": "Nullable(String)"},
            "dbpcm_warehouse.payroll": {"Amount": "Nullable(Decimal(18, 6))"},
        },
        {_T: {"FieldId": "FieldLabel"}},
    )


def test_declared_column_returned(handle: CatalogHandle) -> None:
    assert handle.description_col_for(_T, "FieldId") == "FieldLabel"


def test_undeclared_column_is_none(handle: CatalogHandle) -> None:
    # FieldLabel exists in the schema but declares no link of its own.
    assert handle.description_col_for(_T, "FieldLabel") is None


def test_unknown_table_is_none(handle: CatalogHandle) -> None:
    assert handle.description_col_for("dbpcm_warehouse.nonexistent", "FieldId") is None


def test_table_present_but_column_undeclared_is_none(handle: CatalogHandle) -> None:
    assert handle.description_col_for("dbpcm_warehouse.payroll", "Amount") is None


def test_description_cols_omitted_defaults_to_no_links() -> None:
    """The parameter is optional; a handle built without it declares no links."""
    bare = CatalogHandle({_T: {"FieldId": "Nullable(String)"}})
    assert bare.description_col_for(_T, "FieldId") is None


def test_description_cols_map_is_read_only_inner(handle: CatalogHandle) -> None:
    with pytest.raises(TypeError):
        handle._description_cols[_T]["FieldId"] = "Tampered"  # type: ignore[index]


def test_description_cols_map_is_read_only_top_level(handle: CatalogHandle) -> None:
    with pytest.raises(TypeError):
        handle._description_cols["new.table"] = {}  # type: ignore[index]


def test_source_dict_mutation_does_not_leak_into_handle() -> None:
    """The handle deep-copies its input, so mutating the caller's dict afterwards
    cannot change what the handle reports."""
    src = {_T: {"FieldId": "FieldLabel"}}
    h = CatalogHandle({_T: {"FieldId": "Nullable(String)", "FieldLabel": "Nullable(String)"}}, src)
    src[_T]["FieldId"] = "Tampered"
    src["injected.table"] = {"X": "Y"}
    assert h.description_col_for(_T, "FieldId") == "FieldLabel"
    assert h.description_col_for("injected.table", "X") is None
