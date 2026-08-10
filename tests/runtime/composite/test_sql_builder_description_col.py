"""Unit tests for the authored `description_col` discovery path in
composite/sql_builder.py::resolve_target (design §2.3).

Discovery order under test: AUTHORED `description_col` linkage first, then the
`_candidate_description_columns` naming convention, then value-only. The declared
column is NOT trusted blindly — it must still exist in the catalog AND be in the
caller's scope (M1), else it is skipped and discovery falls through.

These exercise the cases the naming convention silently missed:
  * `FieldId` -> `FieldLabel`  (Id not stripped, Label not a convention suffix)
  * mixed-case / non-convention targets
and the fallback chain when the declaration is out-of-scope or a typo.
"""

from __future__ import annotations

from data_agent.runtime.composite.sql_builder import build_sql, resolve_target
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from tests._catalog_fixture import fixture_catalog_handle

_PAF = "dbpcm_warehouse.personnel_action_form_changes"
_DEPT = "dbpcm_warehouse.departments"
_ACC = "dbpcm_warehouse.accrual_events"


def _sql(catalog: CatalogHandle, table: str, column: str, scope=frozenset()) -> str:
    target = resolve_target(catalog, table=table, column=column, period=None, column_scope=scope)
    return build_sql(target, period=None, limit=200)


# ---------------------------------------------------------------------------
# 1. Declared column selected where the convention would NOT find it
#    (FieldId -> FieldLabel: "Id" not stripped, "Label" not a suffix).
# ---------------------------------------------------------------------------


def test_declared_selected_when_convention_would_miss() -> None:
    catalog = CatalogHandle(
        {_PAF: {"FieldId": "Nullable(String)", "FieldLabel": "Nullable(String)"}},
        {_PAF: {"FieldId": "FieldLabel"}},
    )
    target = resolve_target(catalog, table=_PAF, column="FieldId", period=None)
    assert target.description_col == "FieldLabel"
    sql = build_sql(target, period=None, limit=200)
    assert sql == (
        "SELECT FieldId, FieldLabel, COUNT(*) AS freq "
        "FROM dbpcm_warehouse.personnel_action_form_changes "
        "GROUP BY FieldId, FieldLabel ORDER BY freq DESC LIMIT 200"
    )


def test_declared_selected_confirms_convention_alone_would_miss() -> None:
    """Same catalog, but with NO declaration: the convention finds nothing and the
    query is value-only. This pins that the previous test's success is due to the
    declaration, not the convention."""
    catalog = CatalogHandle(
        {_PAF: {"FieldId": "Nullable(String)", "FieldLabel": "Nullable(String)"}},
        # no description_cols
    )
    target = resolve_target(catalog, table=_PAF, column="FieldId", period=None)
    assert target.description_col is None
    assert "FieldLabel" not in build_sql(target, period=None, limit=200)


# ---------------------------------------------------------------------------
# 2. Declared takes PRIORITY over a convention match when both exist.
# ---------------------------------------------------------------------------


def test_declared_wins_over_convention_candidate() -> None:
    # DepartmentCode -> convention would pick "DepartmentName"; the declaration
    # points at a different, authoritative label column.
    catalog = CatalogHandle(
        {
            _DEPT: {
                "DepartmentCode": "String",
                "DepartmentName": "String",  # convention candidate, present
                "DepartmentLabel": "String",  # declared, NOT a convention candidate
            }
        },
        {_DEPT: {"DepartmentCode": "DepartmentLabel"}},
    )
    target = resolve_target(catalog, table=_DEPT, column="DepartmentCode", period=None)
    assert target.description_col == "DepartmentLabel"
    sql = build_sql(target, period=None, limit=200)
    assert "DepartmentLabel" in sql
    assert "DepartmentName" not in sql  # convention candidate lost to the declaration


# ---------------------------------------------------------------------------
# 3. Declared-but-out-of-scope -> skipped -> convention / value-only.
#    Never selects the out-of-scope column (would trip COLUMN_SCOPE_VIOLATION).
# ---------------------------------------------------------------------------


def test_declared_out_of_scope_falls_back_to_value_only() -> None:
    catalog = CatalogHandle(
        {_PAF: {"FieldId": "Nullable(String)", "FieldLabel": "Nullable(String)"}},
        {_PAF: {"FieldId": "FieldLabel"}},
    )
    # Scope grants the code column only — NOT the declared sibling label.
    # column_scope is "database.table.column" strings (scope_filter D44/D80b).
    scope = frozenset({f"{_PAF}.FieldId"})
    target = resolve_target(catalog, table=_PAF, column="FieldId", period=None, column_scope=scope)
    assert target.description_col is None  # dropped, not forced
    sql = build_sql(target, period=None, limit=200)
    assert "FieldLabel" not in sql  # the out-of-scope column never reaches the SQL


def test_declared_out_of_scope_falls_back_to_convention_in_scope() -> None:
    # DepartmentLabel is declared but out of scope; DepartmentName (convention) is
    # in scope -> discovery must fall through to the convention candidate.
    catalog = CatalogHandle(
        {
            _DEPT: {
                "DepartmentCode": "String",
                "DepartmentName": "String",
                "DepartmentLabel": "String",
            }
        },
        {_DEPT: {"DepartmentCode": "DepartmentLabel"}},
    )
    scope = frozenset({f"{_DEPT}.DepartmentCode", f"{_DEPT}.DepartmentName"})
    target = resolve_target(
        catalog, table=_DEPT, column="DepartmentCode", period=None, column_scope=scope
    )
    assert target.description_col == "DepartmentName"
    sql = build_sql(target, period=None, limit=200)
    assert "DepartmentName" in sql
    assert "DepartmentLabel" not in sql


# ---------------------------------------------------------------------------
# 4. Declared-but-nonexistent (author typo) -> skipped -> convention/value-only.
# ---------------------------------------------------------------------------


def test_declared_typo_not_in_columns_falls_back_no_crash() -> None:
    catalog = CatalogHandle(
        {_PAF: {"FieldId": "Nullable(String)", "FieldLabel": "Nullable(String)"}},
        {_PAF: {"FieldId": "FieldLbl"}},  # typo: not a real column
    )
    target = resolve_target(catalog, table=_PAF, column="FieldId", period=None)
    # No convention match either -> value-only; the typo is silently ignored.
    assert target.description_col is None
    sql = build_sql(target, period=None, limit=200)
    assert "FieldLbl" not in sql
    assert "FieldLabel" not in sql


def test_declared_typo_falls_back_to_convention_match() -> None:
    catalog = CatalogHandle(
        {
            _DEPT: {
                "DepartmentCode": "String",
                "DepartmentName": "String",  # convention candidate
            }
        },
        {_DEPT: {"DepartmentCode": "NoSuchLabel"}},  # typo
    )
    target = resolve_target(catalog, table=_DEPT, column="DepartmentCode", period=None)
    assert target.description_col == "DepartmentName"
    assert "NoSuchLabel" not in build_sql(target, period=None, limit=200)


# ---------------------------------------------------------------------------
# 4b. Declared column points at ITSELF (author error) -> not self-selected.
#     The value column must never become its own description column, else the
#     built SQL would list it twice. Mirrors the convention self-select guard
#     (test_sql_builder_adversarial::test_description_column_not_self_selected).
# ---------------------------------------------------------------------------


def test_declared_self_reference_not_selected_falls_back_value_only() -> None:
    catalog = CatalogHandle(
        {_PAF: {"FieldId": "Nullable(String)", "FieldLabel": "Nullable(String)"}},
        {_PAF: {"FieldId": "FieldId"}},  # author points the code column at itself
    )
    target = resolve_target(catalog, table=_PAF, column="FieldId", period=None)
    # Self-reference excluded; no convention match -> value-only.
    assert target.description_col is None
    sql = build_sql(target, period=None, limit=200)
    assert sql == (
        "SELECT FieldId, COUNT(*) AS freq "
        "FROM dbpcm_warehouse.personnel_action_form_changes "
        "GROUP BY FieldId ORDER BY freq DESC LIMIT 200"
    )
    # The value column appears exactly once in the SELECT / GROUP BY, not twice.
    assert sql.count("FieldId") == 2  # one in SELECT, one in GROUP BY


def test_declared_self_reference_still_allows_convention_fallback() -> None:
    # Self-declaration is dropped, but a valid convention candidate still applies.
    catalog = CatalogHandle(
        {_ACC: {"EarnCode": "Nullable(String)", "EarnDescription": "Nullable(String)"}},
        {_ACC: {"EarnCode": "EarnCode"}},  # self-reference: ignored
    )
    target = resolve_target(catalog, table=_ACC, column="EarnCode", period=None)
    assert target.description_col == "EarnDescription"  # convention wins the fallthrough
    sql = build_sql(target, period=None, limit=200)
    assert "EarnDescription" in sql


# ---------------------------------------------------------------------------
# 5. Regression guard — convention unchanged when no `description_col` declared.
# ---------------------------------------------------------------------------


def test_convention_unchanged_when_no_declaration() -> None:
    catalog = CatalogHandle(
        {_ACC: {"EarnCode": "Nullable(String)", "EarnDescription": "Nullable(String)"}},
        {_ACC: {}},  # explicitly no links declared for this table
    )
    target = resolve_target(catalog, table=_ACC, column="EarnCode", period=None)
    assert target.description_col == "EarnDescription"
    sql = build_sql(target, period=None, limit=200)
    assert "EarnDescription" in sql


def test_convention_unchanged_when_description_cols_map_absent_entirely() -> None:
    catalog = CatalogHandle(
        {_ACC: {"EarnCode": "Nullable(String)", "EarnDescription": "Nullable(String)"}}
    )
    target = resolve_target(catalog, table=_ACC, column="EarnCode", period=None)
    assert target.description_col == "EarnDescription"


# ---------------------------------------------------------------------------
# 6. Real-catalog integration (light) — the two cases the convention missed.
#    Built from the committed MCP export fixture via the shared test helper
#    (D75 Wave 1b — the runtime rebuilds the same handle from /catalog/export).
# ---------------------------------------------------------------------------


def test_real_catalog_field_id_discovers_field_label() -> None:
    catalog = fixture_catalog_handle()
    target = resolve_target(catalog, table=_PAF, column="field_id", period=None)
    # field_id -> field_label is the non-convention case (Label is not a convention
    # suffix, Id is not stripped): only the AUTHORED linkage discovers it.
    assert target.description_col == "field_label"
    assert "field_label" in build_sql(target, period=None, limit=200)


def test_real_catalog_payroll_type_code_discovers_declared_label() -> None:
    payroll = "dbpcm_warehouse.payroll"
    catalog = fixture_catalog_handle()
    target = resolve_target(catalog, table=payroll, column="type_code", period=None)
    # The AUTHORED sibling link resolves the code column to its label column.
    assert target.description_col == "type_code_description"
    sql = build_sql(target, period=None, limit=200)
    assert "type_code_description" in sql


def test_real_catalog_convention_still_works_for_earn_code() -> None:
    """Regression: earn_code -> earn_description is now BOTH declared and a convention
    match in the real catalog; either way it must resolve."""
    catalog = fixture_catalog_handle()
    target = resolve_target(catalog, table=_ACC, column="earn_code", period=None)
    assert target.description_col == "earn_description"


# ---------------------------------------------------------------------------
# 6b. Real-catalog employee code columns — the AUTHORED `description_col` links
#     now declared in the MCP semantic catalog (flowing into the export fixture).
#     These siblings are NOT convention matches (a `_code` suffix is not stripped
#     to `_name`/`_description`), so ONLY the authored linkage discovers them.
# ---------------------------------------------------------------------------

_EMP = "dbpcm_warehouse.employee"


def test_real_catalog_employee_department_code_discovers_declared_name() -> None:
    catalog = fixture_catalog_handle()
    # The link is AUTHORED, not convention-derived — confirm it at the source.
    assert catalog.description_col_for(_EMP, "department_code") == "department_name"
    target = resolve_target(catalog, table=_EMP, column="department_code", period=None)
    assert target.description_col == "department_name"
    assert "department_name" in build_sql(target, period=None, limit=200)


def test_real_catalog_employee_manager_level_code_discovers_declared_description() -> None:
    catalog = fixture_catalog_handle()
    assert (
        catalog.description_col_for(_EMP, "manager_level_code")
        == "manager_level_description"
    )
    target = resolve_target(catalog, table=_EMP, column="manager_level_code", period=None)
    assert target.description_col == "manager_level_description"
    assert "manager_level_description" in build_sql(target, period=None, limit=200)


def test_real_catalog_employee_paycode_profile_code_discovers_declared_description() -> None:
    catalog = fixture_catalog_handle()
    assert (
        catalog.description_col_for(_EMP, "paycode_profile_code")
        == "paycode_profile_description"
    )
    target = resolve_target(catalog, table=_EMP, column="paycode_profile_code", period=None)
    assert target.description_col == "paycode_profile_description"
    assert "paycode_profile_description" in build_sql(target, period=None, limit=200)


def test_real_catalog_employee_convention_only_columns_have_no_sibling() -> None:
    """Regression: code columns with NO authored sibling AND no convention match
    stay value-only. Confirms the authored links above did not broaden linking to
    every `_code` column on the table."""
    catalog = fixture_catalog_handle()
    for column in ("union_code", "position_code"):
        assert catalog.description_col_for(_EMP, column) is None
        target = resolve_target(catalog, table=_EMP, column=column, period=None)
        assert target.description_col is None
