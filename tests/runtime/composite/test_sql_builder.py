"""Unit tests for composite/sql_builder.py (Layer 1, pure — design §2)."""

from __future__ import annotations

import pytest

from data_agent.runtime.composite.sql_builder import (
    Period,
    TargetValidationError,
    build_sql,
    resolve_target,
)
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_T = "dbpcm_warehouse.accrual_events"
_T2 = "otherdb.accrual_events"  # collides on the bare name for ambiguity tests

CATALOG = CatalogHandle(
    {
        _T: {
            "EarnCode": "Nullable(String)",
            "EarnDescription": "Nullable(String)",
            "RequestDate": "Nullable(DateTime64(6))",
            "Hours": "Nullable(Decimal(18, 6))",
        },
        "dbpcm_warehouse.departments": {
            "DepartmentCode": "String",
            "DepartmentName": "String",
        },
    }
)

AMBIGUOUS_CATALOG = CatalogHandle(
    {
        _T: {"EarnCode": "String"},
        _T2: {"EarnCode": "String"},
    }
)


# --- target resolution ------------------------------------------------------


def test_resolve_target_fully_qualified_with_description_convention() -> None:
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=None)
    assert target.db_table == _T
    assert target.column == "EarnCode"
    assert target.description_col == "EarnDescription"
    assert target.period_col is None


def test_resolve_target_bare_name_unique_resolves() -> None:
    target = resolve_target(CATALOG, table="accrual_events", column="EarnCode", period=None)
    assert target.db_table == _T


def test_resolve_target_department_name_convention() -> None:
    target = resolve_target(
        CATALOG, table="dbpcm_warehouse.departments", column="DepartmentCode", period=None
    )
    # No DepartmentDescription; falls through to DepartmentName.
    assert target.description_col == "DepartmentName"


def test_resolve_target_value_only_when_no_description_column() -> None:
    target = resolve_target(CATALOG, table=_T, column="Hours", period=None)
    assert target.description_col is None


def test_resolve_target_unknown_table_fails_closed() -> None:
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table="no_such_table", column="EarnCode", period=None)


def test_resolve_target_unknown_column_fails_closed() -> None:
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table=_T, column="NopeColumn", period=None)


def test_resolve_target_ambiguous_bare_name_fails_closed() -> None:
    with pytest.raises(TargetValidationError, match="ambiguous"):
        resolve_target(AMBIGUOUS_CATALOG, table="accrual_events", column="EarnCode", period=None)


def test_resolve_target_unknown_period_column_fails_closed() -> None:
    with pytest.raises(TargetValidationError):
        resolve_target(
            CATALOG, table=_T, column="EarnCode", period=Period(column="NopeDate")
        )


def test_resolve_target_injection_column_never_validates() -> None:
    # A SQL-injection-shaped column is simply not in the catalog -> fail closed,
    # never reaching build_sql / the MCP.
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table=_T, column="c) FROM x --", period=None)


def test_resolve_target_injection_table_never_validates() -> None:
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table="Emp; DROP", column="EarnCode", period=None)


# --- SQL construction -------------------------------------------------------


def test_build_sql_value_and_description() -> None:
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=None)
    sql = build_sql(target, period=None, limit=200)
    assert sql == (
        "SELECT EarnCode, EarnDescription, COUNT(*) AS freq "
        "FROM dbpcm_warehouse.accrual_events "
        "GROUP BY EarnCode, EarnDescription ORDER BY freq DESC LIMIT 200"
    )


def test_build_sql_value_only_fallback() -> None:
    target = resolve_target(CATALOG, table=_T, column="Hours", period=None)
    sql = build_sql(target, period=None, limit=50)
    assert "EarnDescription" not in sql
    assert sql == (
        "SELECT Hours, COUNT(*) AS freq FROM dbpcm_warehouse.accrual_events "
        "GROUP BY Hours ORDER BY freq DESC LIMIT 50"
    )


def test_build_sql_limit_from_argument() -> None:
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=None)
    assert "LIMIT 17" in build_sql(target, period=None, limit=17)


def test_build_sql_period_where_from_literals() -> None:
    period = Period(column="RequestDate", start="2026-01-01", end="2026-03-31")
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=period)
    sql = build_sql(target, period=period, limit=200)
    assert "WHERE RequestDate >= '2026-01-01' AND RequestDate <= '2026-03-31'" in sql


def test_build_sql_period_start_only() -> None:
    period = Period(column="RequestDate", start="2026-01-01", end=None)
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=period)
    sql = build_sql(target, period=period, limit=200)
    assert "WHERE RequestDate >= '2026-01-01'" in sql
    assert "<=" not in sql


def test_build_sql_period_literal_is_escaped_not_concatenated() -> None:
    # A quote-bearing period bound is rendered as a properly-escaped SQL literal
    # (doubled quote), never raw concatenation.
    period = Period(column="RequestDate", start="2026'; DROP--", end=None)
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=period)
    sql = build_sql(target, period=period, limit=200)
    assert "'2026''; DROP--'" in sql
    # The raw unescaped injection string never appears verbatim.
    assert "2026'; DROP--'" not in sql.replace("''", "\x00")


def test_build_sql_period_with_no_bounds_emits_no_where() -> None:
    period = Period(column="RequestDate", start=None, end=None)
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=period)
    sql = build_sql(target, period=period, limit=200)
    assert "WHERE" not in sql
