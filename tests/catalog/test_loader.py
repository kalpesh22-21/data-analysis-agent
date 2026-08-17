"""
Unit tests for the Semantic Catalog projections (D42, D53, D83/D84).

Layer: 1 — Unit (pure logic; driven from the committed export fixture
`tests/fixtures/catalog_export.json` — the MCP `/catalog/export` payload — via the
in-memory cores `load_semantic_catalog_from_catalog` /
`build_sqlglot_schema_from_catalog`; no ClickHouse connection required).

`load_semantic_catalog_from_catalog()` is a superset of
`build_sqlglot_schema_from_catalog()`: same keying, but the full per-table semantic
entry instead of the `{col: type}` projection. `databaseSchemaDocs/` (and the
dir-reading loaders that parsed it) is gone — the catalog is sourced from the MCP
export only.
"""

from __future__ import annotations

import pytest

from data_agent.catalog import (
    build_sqlglot_schema_from_catalog,
    load_semantic_catalog_from_catalog,
)
from tests._catalog_fixture import fixture_catalog

# The frozen MCP export catalog (`{db.table: <entry>}`) — the single source of
# truth now that `databaseSchemaDocs/` is gone.
_CATALOG = fixture_catalog()

_E = "dbpcm_warehouse.employee"
_P = "dbpcm_warehouse.payroll"
_CE = "dbpcm_warehouse.candidate_education"

# Expected table keys — one per table entry in the frozen MCP export fixture.
EXPECTED_TABLE_KEYS = frozenset(
    [
        "dbpcm_warehouse.accrual_events",
        "dbpcm_warehouse.applicant_tracking_application",
        "dbpcm_warehouse.applicant_tracking_requisition",
        "dbpcm_warehouse.candidate_education",
        "dbpcm_warehouse.candidate_employment_history",
        "dbpcm_warehouse.department",
        "dbpcm_warehouse.employee",
        "dbpcm_warehouse.labor_allocation",
        "dbpcm_warehouse.payroll",
        "dbpcm_warehouse.performance_discussions",
        "dbpcm_warehouse.personnel_action_form_changes",
    ]
)


# ---------------------------------------------------------------------------
# 1. One entry per valid table file, keyed "database.table"
# ---------------------------------------------------------------------------


def test_load_semantic_catalog_returns_one_entry_per_table() -> None:
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    assert set(catalog.keys()) == EXPECTED_TABLE_KEYS


# ---------------------------------------------------------------------------
# 2. Rich fields present + correct for a representative table (employee)
# ---------------------------------------------------------------------------


def test_employee_grain() -> None:
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    assert catalog[_E]["grain"] == ["employee_code"]


def test_employee_rules_contains_active_employee_rule() -> None:
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    rules = catalog[_E]["rules"]
    assert isinstance(rules, list)
    assert len(rules) >= 1
    active_rule = next((r for r in rules if r.get("id") == "active_employee"), None)
    assert active_rule is not None, "expected an 'active_employee' rule in employee.yaml rules[]"
    assert active_rule["predicate"] == "employee_status = 'A'"


def test_employee_ambiguities_contains_department_term() -> None:
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    ambiguities = catalog[_E]["ambiguities"]
    assert isinstance(ambiguities, list)
    assert len(ambiguities) >= 1
    department = next((a for a in ambiguities if a.get("term") == "department"), None)
    assert department is not None, (
        "expected a 'department' ambiguity in employee.yaml ambiguities[]"
    )
    assert department["default"] == "department_name for display and department_code for filtering"


def test_employee_status_enum_values_map() -> None:
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    status_col = catalog[_E]["columns"]["employee_status"]
    # The Wave-1 catalog authors `values` as the observed enum-code list.
    assert status_col["values"] == ["A", "D", "I", "R", "T", "V"]


def test_employee_column_with_unit() -> None:
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    annual_salary = catalog[_E]["columns"]["annual_salary"]
    assert annual_salary["unit"] == "USD"
    assert annual_salary["type"] == "Nullable(Decimal(18, 6))"

    rate1 = catalog[_E]["columns"]["rate_1"]
    assert rate1["unit"] == "USD"


def test_flow_style_decimal_types_are_not_truncated_by_yaml_comma() -> None:
    """Regression guard for a fixed data-authoring bug (2026-07-01).

    Several *.yaml files declared Decimal columns in FLOW-style mapping syntax
    (`{ type: Nullable(Decimal(18, 6)), ... }`); the unquoted comma inside the
    flow mapping truncated `type` to 'Nullable(Decimal(18' and injected a bogus
    '6))' key. Fixed at source by quoting the type value. This test asserts the
    corrected parse across all previously-affected real columns so the flow-style
    trap can't silently reappear.
    """
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    affected = [
        (_E, "annual_salary"),
        (_E, "rate_1"),
        ("dbpcm_warehouse.candidate_education", "student_gpa"),
        ("dbpcm_warehouse.applicant_tracking_application", "application_job_wage_start"),
        ("dbpcm_warehouse.applicant_tracking_application", "application_job_wage_end"),
    ]
    for table_key, col in affected:
        meta = catalog[table_key]["columns"][col]
        assert meta["type"] == "Nullable(Decimal(18, 6))", (table_key, col)
        assert "6))" not in meta, (table_key, col)


def test_column_with_sensitive_true() -> None:
    """candidate_education.education_phone_number is marked sensitive: true in the YAML."""
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    phone_col = catalog[_CE]["columns"]["education_phone_number"]
    assert phone_col["sensitive"] is True
    assert phone_col["type"] == "Nullable(String)"

    # A representative non-sensitive column in the same table should not carry the flag.
    institute_name_col = catalog[_CE]["columns"]["institute_name"]
    assert "sensitive" not in institute_name_col


def test_employee_client_defined_flag() -> None:
    """department_code is client_defined: true in employee.yaml; employee_code is not."""
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    columns = catalog[_E]["columns"]
    assert columns["department_code"]["client_defined"] is True
    assert "client_defined" not in columns["employee_code"]


# ---------------------------------------------------------------------------
# 3. `measures` present where declared (payroll), absent where not (employee)
# ---------------------------------------------------------------------------


def test_payroll_measures_present() -> None:
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    measures = catalog[_P]["measures"]
    assert "gross_earnings" in measures
    assert measures["gross_earnings"] == {
        "column": "amount",
        "agg": "sum",
        "defined_over": (
            "register_type = 'EARN'; summed over payroll line items within the "
            "selected employee and payroll-period or pay-date grain."
        ),
    }
    assert "paid_hours" in measures


def test_employee_measures_absent() -> None:
    """employee.yaml declares no `measures` block — must not be fabricated."""
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    assert "measures" not in catalog[_E]


# ---------------------------------------------------------------------------
# 4. Case preservation (D70) — mixed-case column authored in the real YAML
# ---------------------------------------------------------------------------


def test_case_preservation_mixed_case_column() -> None:
    """payroll.yaml authors `distributed_job_cost_code` in exact snake_case.

    The loader must not re-case or mangle the authored name — the exact casing
    from the YAML must be preserved (D70).
    """
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    columns = catalog[_P]["columns"]
    assert "distributed_job_cost_code" in columns
    # No re-cased PascalCase / lowercased-run variant should exist as a different key.
    assert "DistributedJobCostCode" not in columns
    assert "distributedjobcostcode" not in columns


def test_case_preservation_table_and_database_keys() -> None:
    """Table/database key components are not lowercased or uppercased."""
    catalog = load_semantic_catalog_from_catalog(_CATALOG)
    assert catalog[_E]["table"] == "employee"
    assert catalog[_E]["database"] == "dbpcm_warehouse"


# ---------------------------------------------------------------------------
# 7. Alignment regression — the semantic overlay and the sqlglot schema
#    must never silently drift (same tables, same columns, same types).
# ---------------------------------------------------------------------------


def test_semantic_catalog_and_sqlglot_schema_table_keys_match() -> None:
    semantic = load_semantic_catalog_from_catalog(_CATALOG)
    sqlglot_schema = build_sqlglot_schema_from_catalog(_CATALOG)
    assert set(semantic.keys()) == set(sqlglot_schema.keys())


def test_semantic_catalog_and_sqlglot_schema_column_pairs_match() -> None:
    """The set of (db.table, column) pairs derivable from each view must match exactly."""
    semantic = load_semantic_catalog_from_catalog(_CATALOG)
    sqlglot_schema = build_sqlglot_schema_from_catalog(_CATALOG)

    semantic_pairs = {
        (table_key, col_name)
        for table_key, entry in semantic.items()
        for col_name in entry["columns"]
    }
    sqlglot_pairs = {
        (table_key, col_name) for table_key, cols in sqlglot_schema.items() for col_name in cols
    }
    assert semantic_pairs == sqlglot_pairs


def test_semantic_catalog_column_types_match_sqlglot_schema() -> None:
    """Per-column `type` in the overlay must equal the sqlglot schema's type string."""
    semantic = load_semantic_catalog_from_catalog(_CATALOG)
    sqlglot_schema = build_sqlglot_schema_from_catalog(_CATALOG)

    for table_key, entry in semantic.items():
        for col_name, col_def in entry["columns"].items():
            expected_type = sqlglot_schema[table_key][col_name]
            if isinstance(col_def, dict):
                actual_type = str(col_def.get("type", "TEXT"))
            else:
                actual_type = "TEXT"
            assert actual_type == expected_type, (
                f"{table_key}.{col_name}: semantic type {actual_type!r} != "
                f"sqlglot schema type {expected_type!r}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
