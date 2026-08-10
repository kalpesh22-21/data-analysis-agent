"""Unit tests for the snake_case naming-convention path in
composite/sql_builder.py::resolve_target / _candidate_description_columns (design §2.3).

These pin the CONVENTION FALLBACK only: every catalog here has NO authored
`description_col` linkage (empty or absent `description_cols` map), so the sibling
description column can be discovered by naming convention alone. This locks in the
snake_case-aware behaviour the recent fix added while keeping PascalCase support:

  * snake:  strip a single trailing `_code`/`_id` -> base; append
            `_description` / `_label` / `_name` to the base, plus (when stripped)
            to the full column -> `type_code` -> `type_code_description`.
  * `_description` is generated before `_name`, so it wins when both siblings exist.
  * the input column itself is never a candidate (self-select guard), so the value
    column can never become its own description and be emitted twice.
  * a convention candidate that is OUT OF SCOPE is skipped (availability pre-check,
    M1) and discovery falls through — mirroring the PascalCase scope tests.

Style mirrors test_sql_builder_description_col.py: hand-built
`CatalogHandle(schema, description_cols)` and assertions on
`resolve_target(...).description_col` and `build_sql(...)`.
"""

from __future__ import annotations

from data_agent.runtime.composite.sql_builder import (
    _candidate_description_columns,
    build_sql,
    resolve_target,
)
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_ACC = "dbpcm_warehouse.accrual_events"
_PAF = "dbpcm_warehouse.personnel_action_form_changes"
_PAY = "dbpcm_warehouse.payroll"
_REQ = "dbpcm_warehouse.requisitions"
_DEPT = "dbpcm_warehouse.departments"


def _sql(catalog: CatalogHandle, table: str, column: str, scope=frozenset()) -> str:
    target = resolve_target(
        catalog, table=table, column=column, period=None, column_scope=scope
    )
    return build_sql(target, period=None, limit=200)


# ---------------------------------------------------------------------------
# 1. earn_code -> earn_description by CONVENTION ALONE (the fix's regression).
#    No authored description_col map — only the snake convention can find it.
# ---------------------------------------------------------------------------


def test_snake_earn_code_discovers_earn_description_by_convention_alone() -> None:
    catalog = CatalogHandle(
        {_ACC: {"earn_code": "Nullable(String)", "earn_description": "Nullable(String)"}},
        # NO description_cols map at all — convention is the only discovery path.
    )
    target = resolve_target(catalog, table=_ACC, column="earn_code", period=None)
    assert target.description_col == "earn_description"
    sql = build_sql(target, period=None, limit=200)
    assert sql == (
        "SELECT earn_code, earn_description, COUNT(*) AS freq "
        "FROM dbpcm_warehouse.accrual_events "
        "GROUP BY earn_code, earn_description ORDER BY freq DESC LIMIT 200"
    )


def test_snake_earn_code_discovers_earn_description_with_empty_map() -> None:
    # Same, but with an explicitly-empty per-table links map (no declaration for
    # this table) — must still resolve via convention, not fall to value-only.
    catalog = CatalogHandle(
        {_ACC: {"earn_code": "Nullable(String)", "earn_description": "Nullable(String)"}},
        {_ACC: {}},
    )
    target = resolve_target(catalog, table=_ACC, column="earn_code", period=None)
    assert target.description_col == "earn_description"


# ---------------------------------------------------------------------------
# 2. field_id -> field_label by convention alone ("_id" stripped, "_label" suffix).
#    Before the fix this snake case was invisible to the (PascalCase-only) convention.
# ---------------------------------------------------------------------------


def test_snake_field_id_discovers_field_label_by_convention_alone() -> None:
    catalog = CatalogHandle(
        {_PAF: {"field_id": "Nullable(String)", "field_label": "Nullable(String)"}},
    )
    target = resolve_target(catalog, table=_PAF, column="field_id", period=None)
    assert target.description_col == "field_label"
    sql = build_sql(target, period=None, limit=200)
    assert "field_label" in sql
    assert sql == (
        "SELECT field_id, field_label, COUNT(*) AS freq "
        "FROM dbpcm_warehouse.personnel_action_form_changes "
        "GROUP BY field_id, field_label ORDER BY freq DESC LIMIT 200"
    )


# ---------------------------------------------------------------------------
# 3. type_code -> type_code_description by convention alone: the FULL-column +
#    `_description` variant (the stripped base `type_description` is absent).
# ---------------------------------------------------------------------------


def test_snake_type_code_discovers_full_column_description_variant() -> None:
    catalog = CatalogHandle(
        {_PAY: {"type_code": "Nullable(String)", "type_code_description": "Nullable(String)"}},
    )
    target = resolve_target(catalog, table=_PAY, column="type_code", period=None)
    assert target.description_col == "type_code_description"
    sql = build_sql(target, period=None, limit=200)
    assert "type_code_description" in sql


def test_snake_full_column_variant_present_in_candidate_list() -> None:
    # Pin the ordering property the above test depends on: the full-column sibling
    # `{column}_description` OUTRANKS the ambiguous stripped-base `{snake_base}_description`.
    # On a table with both a `type` column (sibling `type_description`) and a
    # `type_code` column (sibling `type_code_description`), resolving `type_code`
    # must prefer its OWN sibling `type_code_description` — the full-column variant
    # can only ever be the input column's own sibling, whereas `type_description`
    # is a DIFFERENT column's sibling. Both `_description` variants still precede
    # any `_label`/`_name` variant.
    candidates = _candidate_description_columns("type_code")
    assert "type_description" in candidates
    assert "type_code_description" in candidates
    assert candidates.index("type_code_description") < candidates.index("type_description")
    assert candidates.index("type_code_description") < candidates.index("type_name")


# ---------------------------------------------------------------------------
# 4. `_description` beats `_name` when a table has BOTH siblings (ordering).
# ---------------------------------------------------------------------------


def test_snake_description_suffix_wins_over_name_suffix() -> None:
    catalog = CatalogHandle(
        {
            _ACC: {
                "earn_code": "Nullable(String)",
                "earn_name": "Nullable(String)",  # convention candidate, later in order
                "earn_description": "Nullable(String)",  # convention candidate, first
            }
        },
    )
    target = resolve_target(catalog, table=_ACC, column="earn_code", period=None)
    assert target.description_col == "earn_description"
    sql = build_sql(target, period=None, limit=200)
    assert "earn_description" in sql
    assert "earn_name" not in sql  # the lower-priority suffix loses


def test_snake_name_suffix_used_when_description_absent() -> None:
    # `department` has no `_code`/`_id` to strip; `department_description` is absent
    # so discovery falls through to `department_name`.
    catalog = CatalogHandle(
        {_DEPT: {"department": "String", "department_name": "String"}},
    )
    target = resolve_target(catalog, table=_DEPT, column="department", period=None)
    assert target.description_col == "department_name"


# ---------------------------------------------------------------------------
# 5. Self-select guard: the input column is never a candidate, so a snake value
#    column can never become its own description and be emitted twice.
# ---------------------------------------------------------------------------


def test_snake_column_never_self_selects_as_its_own_description() -> None:
    # Resolving the description-shaped column itself: no OTHER sibling exists, so
    # the guard must yield value-only, not select `earn_description` twice.
    catalog = CatalogHandle(
        {_ACC: {"earn_code": "Nullable(String)", "earn_description": "Nullable(String)"}},
    )
    target = resolve_target(catalog, table=_ACC, column="earn_description", period=None)
    assert target.description_col is None
    sql = build_sql(target, period=None, limit=200)
    assert sql == (
        "SELECT earn_description, COUNT(*) AS freq "
        "FROM dbpcm_warehouse.accrual_events "
        "GROUP BY earn_description ORDER BY freq DESC LIMIT 200"
    )
    # The value column appears exactly twice (once SELECT, once GROUP BY) — never
    # a third time as a self-selected description column.
    assert sql.count("earn_description") == 2


def test_snake_candidate_never_equals_input_column() -> None:
    # Structural guard, independent of any catalog: the input is never emitted.
    for column in ("earn_code", "field_id", "type_code", "department", "earn_description"):
        assert column not in _candidate_description_columns(column)


# ---------------------------------------------------------------------------
# 6. Out-of-scope snake convention candidate -> skipped (M1 availability pre-check).
#    Mirrors test_sql_builder_description_col::test_declared_out_of_scope_*.
# ---------------------------------------------------------------------------


def test_snake_convention_candidate_out_of_scope_falls_back_to_value_only() -> None:
    catalog = CatalogHandle(
        {_ACC: {"earn_code": "Nullable(String)", "earn_description": "Nullable(String)"}},
    )
    # Scope grants the code column only — NOT the convention sibling.
    scope = frozenset({f"{_ACC}.earn_code"})
    target = resolve_target(
        catalog, table=_ACC, column="earn_code", period=None, column_scope=scope
    )
    assert target.description_col is None  # dropped, not forced -> no scope violation
    sql = build_sql(target, period=None, limit=200)
    assert "earn_description" not in sql


def test_snake_out_of_scope_candidate_falls_through_to_next_in_scope_candidate() -> None:
    # `earn_description` (first candidate) is OUT of scope; `earn_name` (later
    # candidate) is IN scope -> discovery must fall through to `earn_name`.
    catalog = CatalogHandle(
        {
            _ACC: {
                "earn_code": "Nullable(String)",
                "earn_description": "Nullable(String)",
                "earn_name": "Nullable(String)",
            }
        },
    )
    scope = frozenset({f"{_ACC}.earn_code", f"{_ACC}.earn_name"})
    target = resolve_target(
        catalog, table=_ACC, column="earn_code", period=None, column_scope=scope
    )
    assert target.description_col == "earn_name"
    sql = build_sql(target, period=None, limit=200)
    assert "earn_name" in sql
    assert "earn_description" not in sql  # out-of-scope candidate never reaches SQL


# ---------------------------------------------------------------------------
# 7. requisition_department_code -> requisition_department_description (multi-segment
#    base; only the single trailing `_code` is stripped) by convention alone.
# ---------------------------------------------------------------------------


def test_snake_multi_segment_code_strips_single_trailing_code_segment() -> None:
    catalog = CatalogHandle(
        {
            _REQ: {
                "requisition_department_code": "String",
                "requisition_department_description": "String",
            }
        },
    )
    target = resolve_target(
        catalog, table=_REQ, column="requisition_department_code", period=None
    )
    assert target.description_col == "requisition_department_description"


# ---------------------------------------------------------------------------
# 8. PascalCase family still works with the snake-aware rewrite (kept behaviour).
# ---------------------------------------------------------------------------


def test_pascal_family_still_resolves_after_snake_rewrite() -> None:
    catalog = CatalogHandle(
        {
            _ACC: {"EarnCode": "Nullable(String)", "EarnDescription": "Nullable(String)"},
            _DEPT: {"DepartmentCode": "String", "DepartmentName": "String"},
        },
    )
    earn = resolve_target(catalog, table=_ACC, column="EarnCode", period=None)
    assert earn.description_col == "EarnDescription"
    dept = resolve_target(catalog, table=_DEPT, column="DepartmentCode", period=None)
    assert dept.description_col == "DepartmentName"
