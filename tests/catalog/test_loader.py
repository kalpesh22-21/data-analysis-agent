"""
Unit tests for the Semantic Catalog loader's `load_semantic_catalog()` (D42, D53, D83/D84).

Layer: 1 — Unit (pure logic; reads real databaseSchemaDocs/*.yaml fixtures plus
temp-dir YAML fixtures for edge cases; no ClickHouse connection required).

`load_semantic_catalog()` is a superset of `build_sqlglot_schema()`: same parse,
keying, and file-skip rules (`_load_raw_table_entries()`), but returns the full
per-table semantic entry instead of the `{col: type}` projection. These tests
assert against the real production YAML files (values transcribed by hand from
the source files, not invented) plus small temp-dir fixtures for the
DEFAULT_DATABASE-fallback and file-skip edge cases that the real catalog
doesn't currently exercise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_agent.catalog import DEFAULT_DATABASE, build_sqlglot_schema, load_semantic_catalog

REPO_ROOT = Path(__file__).parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "databaseSchemaDocs"

_E = "dbpcm_warehouse.employee"
_P = "dbpcm_warehouse.payroll"
_CE = "dbpcm_warehouse.candidate_education"

# Expected table keys, one per *.yaml file in databaseSchemaDocs/ that declares
# a top-level `table` + `columns` block (AUTHORING_NOTES.md is not a YAML file
# and is correctly excluded by the *.yaml glob).
EXPECTED_TABLE_KEYS = frozenset([
    "dbpcm_warehouse.accrual_events",
    "dbpcm_warehouse.applicant_tracking_application",
    "dbpcm_warehouse.applicant_tracking_requisition",
    "dbpcm_warehouse.candidate_education",
    "dbpcm_warehouse.candidate_employment_history",
    "dbpcm_warehouse.employee",
    "dbpcm_warehouse.payroll",
    "dbpcm_warehouse.performance_discussions",
    "dbpcm_warehouse.personnel_action_form_changes",
])


# ---------------------------------------------------------------------------
# 1. One entry per valid table file, keyed "database.table"
# ---------------------------------------------------------------------------


def test_load_semantic_catalog_returns_one_entry_per_table() -> None:
    catalog = load_semantic_catalog(SCHEMA_DIR)
    assert set(catalog.keys()) == EXPECTED_TABLE_KEYS


def test_load_semantic_catalog_default_schema_dir_matches_explicit() -> None:
    """schema_dir=None resolves to the same real databaseSchemaDocs/ directory."""
    catalog_default = load_semantic_catalog()
    catalog_explicit = load_semantic_catalog(SCHEMA_DIR)
    assert set(catalog_default.keys()) == set(catalog_explicit.keys())


# ---------------------------------------------------------------------------
# 2. Rich fields present + correct for a representative table (employee)
# ---------------------------------------------------------------------------


def test_employee_grain() -> None:
    catalog = load_semantic_catalog(SCHEMA_DIR)
    assert catalog[_E]["grain"] == ["EmployeeCode"]


def test_employee_rules_contains_active_employee_rule() -> None:
    catalog = load_semantic_catalog(SCHEMA_DIR)
    rules = catalog[_E]["rules"]
    assert isinstance(rules, list)
    assert len(rules) >= 1
    active_rule = next((r for r in rules if r.get("id") == "active_employee"), None)
    assert active_rule is not None, "expected an 'active_employee' rule in employee.yaml rules[]"
    assert active_rule["predicate"] == "EmployeeStatus = 'A'"


def test_employee_ambiguities_contains_headcount_term() -> None:
    catalog = load_semantic_catalog(SCHEMA_DIR)
    ambiguities = catalog[_E]["ambiguities"]
    assert isinstance(ambiguities, list)
    assert len(ambiguities) >= 1
    headcount = next((a for a in ambiguities if a.get("term") == "headcount"), None)
    assert headcount is not None, "expected a 'headcount' ambiguity in employee.yaml ambiguities[]"
    assert headcount["default"] == "active_only"


def test_employee_status_enum_values_map() -> None:
    catalog = load_semantic_catalog(SCHEMA_DIR)
    status_col = catalog[_E]["columns"]["EmployeeStatus"]
    assert status_col["values"] == {
        "A": "active",
        "D": "deceased",
        "I": "inactive",
        "N": "not_hired",
        "P": "pre_hire",
        "R": "retired",
        "T": "terminated",
        "V": "on_leave",
    }
    assert status_col["observed_values"] == ["A", "D", "I", "N", "T", "V"]


def test_employee_column_with_unit() -> None:
    catalog = load_semantic_catalog(SCHEMA_DIR)
    annual_salary = catalog[_E]["columns"]["AnnualSalary"]
    assert annual_salary["unit"] == "USD"
    assert annual_salary["type"] == "Nullable(Decimal(18, 6))"

    rate1 = catalog[_E]["columns"]["Rate1"]
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
    catalog = load_semantic_catalog(SCHEMA_DIR)
    affected = [
        (_E, "AnnualSalary"),
        (_E, "Rate1"),
        ("dbpcm_warehouse.candidate_education", "StudentGPA"),
        ("dbpcm_warehouse.applicant_tracking_application", "ApplicationJobWageStart"),
        ("dbpcm_warehouse.applicant_tracking_application", "ApplicationJobWageEnd"),
    ]
    for table_key, col in affected:
        meta = catalog[table_key]["columns"][col]
        assert meta["type"] == "Nullable(Decimal(18, 6))", (table_key, col)
        assert "6))" not in meta, (table_key, col)


def test_column_with_sensitive_true() -> None:
    """candidate_education.EducationPhoneNumber is marked sensitive: true in the YAML."""
    catalog = load_semantic_catalog(SCHEMA_DIR)
    phone_col = catalog[_CE]["columns"]["EducationPhoneNumber"]
    assert phone_col["sensitive"] is True
    assert phone_col["type"] == "Nullable(String)"

    # A representative non-sensitive column in the same table should not carry the flag.
    institute_name_col = catalog[_CE]["columns"]["InstituteName"]
    assert "sensitive" not in institute_name_col


def test_employee_client_defined_flag() -> None:
    """Department is client_defined: true in employee.yaml; EmployeeCode is not."""
    catalog = load_semantic_catalog(SCHEMA_DIR)
    columns = catalog[_E]["columns"]
    assert columns["Department"]["client_defined"] is True
    assert "client_defined" not in columns["EmployeeCode"]


# ---------------------------------------------------------------------------
# 3. `measures` present where declared (payroll), absent where not (employee)
# ---------------------------------------------------------------------------


def test_payroll_measures_present() -> None:
    catalog = load_semantic_catalog(SCHEMA_DIR)
    measures = catalog[_P]["measures"]
    assert "gross_earnings" in measures
    assert measures["gross_earnings"] == {
        "column": "Amount",
        "agg": "sum",
        "defined_over": "RegisterType = 'EARN'; summed over line items within (EmployeeCode, pay period)",
    }
    assert "paid_hours" in measures


def test_employee_measures_absent() -> None:
    """employee.yaml declares no `measures` block — must not be fabricated."""
    catalog = load_semantic_catalog(SCHEMA_DIR)
    assert "measures" not in catalog[_E]


# ---------------------------------------------------------------------------
# 4. Case preservation (D70) — mixed-case column authored in the real YAML
# ---------------------------------------------------------------------------


def test_case_preservation_mixed_case_column() -> None:
    """payroll.yaml authors `distributedDepartmentDescription` (lowercase leading d).

    The loader must not lowercase or titlecase this — exact casing from the YAML
    must be preserved (D70).
    """
    catalog = load_semantic_catalog(SCHEMA_DIR)
    columns = catalog[_P]["columns"]
    assert "distributedDepartmentDescription" in columns
    # Neither the fully-lowercased nor the PascalCase variant should exist as a
    # *different* key — only the exact authored casing.
    assert "DistributedDepartmentDescription" not in columns
    assert "distributeddepartmentdescription" not in columns


def test_case_preservation_table_and_database_keys() -> None:
    """Table/database key components are not lowercased or uppercased."""
    catalog = load_semantic_catalog(SCHEMA_DIR)
    assert catalog[_E]["table"] == "employee"
    assert catalog[_E]["database"] == "dbpcm_warehouse"


# ---------------------------------------------------------------------------
# 5. DEFAULT_DATABASE fallback (temp-dir fixture — no real YAML omits `database`)
# ---------------------------------------------------------------------------


def test_default_database_fallback(tmp_path: Path) -> None:
    yaml_text = """
table: widget
description: A widget table with no explicit database key.
grain: [ WidgetId ]
columns:
  WidgetId: { type: Int32, description: Widget identifier. }
  WidgetName: { type: Nullable(String), description: Widget name. }
"""
    (tmp_path / "widget.yaml").write_text(yaml_text, encoding="utf-8")

    catalog = load_semantic_catalog(tmp_path)
    expected_key = f"{DEFAULT_DATABASE}.widget"
    assert expected_key in catalog
    assert catalog[expected_key]["database"] == DEFAULT_DATABASE
    assert catalog[expected_key]["columns"]["WidgetId"]["type"] == "Int32"


# ---------------------------------------------------------------------------
# 6. File-skip: YAML lacking `table`/`columns` is skipped without crashing
# ---------------------------------------------------------------------------


def test_file_without_table_and_columns_is_skipped(tmp_path: Path) -> None:
    # Valid table file.
    (tmp_path / "good.yaml").write_text(
        """
database: dbpcm_warehouse
table: good_table
columns:
  Id: { type: Int32, description: Identifier. }
""",
        encoding="utf-8",
    )
    # Rules-only file with no `table` key at all.
    (tmp_path / "rules_only.yaml").write_text(
        """
rules:
  - { id: some_rule, predicate: "1 = 1", applies_when: always, description: A global rule. }
""",
        encoding="utf-8",
    )
    # `table` present but no `columns` block — also must be skipped.
    (tmp_path / "table_no_columns.yaml").write_text(
        """
database: dbpcm_warehouse
table: incomplete_table
description: Declares a table but no columns block.
""",
        encoding="utf-8",
    )

    catalog = load_semantic_catalog(tmp_path)
    assert set(catalog.keys()) == {"dbpcm_warehouse.good_table"}


# ---------------------------------------------------------------------------
# 7. Alignment regression — load_semantic_catalog and build_sqlglot_schema
#    must never silently drift (same tables, same columns, same types).
# ---------------------------------------------------------------------------


def test_semantic_catalog_and_sqlglot_schema_table_keys_match() -> None:
    semantic = load_semantic_catalog(SCHEMA_DIR)
    sqlglot_schema = build_sqlglot_schema(SCHEMA_DIR)
    assert set(semantic.keys()) == set(sqlglot_schema.keys())


def test_semantic_catalog_and_sqlglot_schema_column_pairs_match() -> None:
    """The set of (db.table, column) pairs derivable from each view must match exactly."""
    semantic = load_semantic_catalog(SCHEMA_DIR)
    sqlglot_schema = build_sqlglot_schema(SCHEMA_DIR)

    semantic_pairs = {
        (table_key, col_name)
        for table_key, entry in semantic.items()
        for col_name in entry["columns"]
    }
    sqlglot_pairs = {
        (table_key, col_name)
        for table_key, cols in sqlglot_schema.items()
        for col_name in cols
    }
    assert semantic_pairs == sqlglot_pairs


def test_semantic_catalog_column_types_match_sqlglot_schema() -> None:
    """Per-column `type` in load_semantic_catalog must equal build_sqlglot_schema's type string."""
    semantic = load_semantic_catalog(SCHEMA_DIR)
    sqlglot_schema = build_sqlglot_schema(SCHEMA_DIR)

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
