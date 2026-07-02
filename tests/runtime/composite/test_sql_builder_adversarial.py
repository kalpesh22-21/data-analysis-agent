"""Adversarial Layer-1 tests for composite/sql_builder.py (D77, design §2).

Covers validation-bypass attempts (case mismatch per D70, whitespace/quoted
identifiers, wrong-db qualified forms), period edge cases, and description-column
convention corners the happy-path suite misses.
"""

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
CATALOG = CatalogHandle(
    {
        _T: {
            "EarnCode": "Nullable(String)",
            "EarnDescription": "Nullable(String)",
            "RequestDate": "Nullable(DateTime64(6))",
            "Hours": "Nullable(Decimal(18, 6))",
            "Status": "String",
            "StatusName": "String",
        }
    }
)


# --- validation-bypass: case, whitespace, quoting, wrong db -----------------


@pytest.mark.parametrize(
    ("table", "column"),
    [
        (_T, "earncode"),
        (_T, "EARNCODE"),
        (_T, "EarnCODE"),
        ("dbpcm_warehouse.ACCRUAL_EVENTS", "EarnCode"),
        ("DBPCM_WAREHOUSE.accrual_events", "EarnCode"),
    ],
)
def test_case_mismatch_fails_closed_d70(table: str, column: str) -> None:
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table=table, column=column, period=None)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        (" accrual_events", "EarnCode"),
        ("accrual_events\t", "EarnCode"),
        (_T + " ", "EarnCode"),
        (_T, " EarnCode"),
        (_T, "EarnCode "),
        (_T, "\nEarnCode"),
    ],
)
def test_whitespace_padded_identifiers_fail_closed(table: str, column: str) -> None:
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table=table, column=column, period=None)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        (_T, '"EarnCode"'),
        (_T, "'EarnCode'"),
        (_T, "`EarnCode`"),
        ('"dbpcm_warehouse"."accrual_events"', "EarnCode"),
        ("`dbpcm_warehouse`.`accrual_events`", "EarnCode"),
    ],
)
def test_quoted_identifiers_fail_closed(table: str, column: str) -> None:
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table=table, column=column, period=None)


def test_wrong_database_qualified_fails_closed() -> None:
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table="otherdb.accrual_events", column="EarnCode", period=None)


def test_empty_string_table_and_column_fail_closed() -> None:
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table="", column="EarnCode", period=None)
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table=_T, column="", period=None)


def test_dot_only_and_trailing_dot_table_fail_closed() -> None:
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table=".", column="EarnCode", period=None)
    with pytest.raises(TargetValidationError):
        resolve_target(CATALOG, table="dbpcm_warehouse.", column="EarnCode", period=None)


# --- description-column convention corners ----------------------------------


def test_description_convention_non_code_column_uses_name_suffix() -> None:
    # "Status" doesn't end in "Code" -> candidate "StatusDescription" (absent),
    # then "StatusName" (present).
    target = resolve_target(CATALOG, table=_T, column="Status", period=None)
    assert target.description_col == "StatusName"


def test_description_column_not_self_selected() -> None:
    # Resolving the description column itself yields no sibling description.
    target = resolve_target(CATALOG, table=_T, column="EarnDescription", period=None)
    assert target.description_col is None


# --- period corners ---------------------------------------------------------


def test_period_column_equal_to_value_column_builds() -> None:
    period = Period(column="EarnCode", start="A", end="Z")
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=period)
    assert target.period_col == "EarnCode"
    sql = build_sql(target, period=period, limit=200)
    assert "WHERE EarnCode >= 'A' AND EarnCode <= 'Z'" in sql


def test_period_start_after_end_no_validation() -> None:
    period = Period(column="RequestDate", start="2026-12-31", end="2026-01-01")
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=period)
    sql = build_sql(target, period=period, limit=200)
    # No start<=end validation — literals bound verbatim (design §2.4).
    assert "RequestDate >= '2026-12-31' AND RequestDate <= '2026-01-01'" in sql


def test_period_end_only_builds_lte_only() -> None:
    period = Period(column="RequestDate", start=None, end="2026-03-31")
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=period)
    sql = build_sql(target, period=period, limit=200)
    assert "WHERE RequestDate <= '2026-03-31'" in sql
    assert ">=" not in sql


def test_period_backslash_bound_is_escaped_literal() -> None:
    period = Period(column="RequestDate", start="a\\'; DROP", end=None)
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=period)
    sql = build_sql(target, period=period, limit=200)
    # sqlglot renders it as a single-quoted literal; the DROP keyword lives
    # inside the literal, never as bare SQL.
    where = sql.split("WHERE", 1)[1]
    assert where.strip().startswith("RequestDate >= '")
    assert "DROP" not in sql.split("'", 2)[0]  # nothing hostile before the literal


def test_period_unicode_bound_is_literal() -> None:
    period = Period(column="RequestDate", start="2026-Q1-☃", end=None)
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=period)
    sql = build_sql(target, period=period, limit=200)
    assert "'2026-Q1-☃'" in sql


# --- LIMIT / structural ------------------------------------------------------


def test_limit_zero_and_large_render_verbatim() -> None:
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=None)
    assert "LIMIT 0" in build_sql(target, period=None, limit=0)
    assert "LIMIT 1000000" in build_sql(target, period=None, limit=1_000_000)


def test_freq_alias_present_and_group_by_matches_select() -> None:
    target = resolve_target(CATALOG, table=_T, column="EarnCode", period=None)
    sql = build_sql(target, period=None, limit=200)
    assert "COUNT(*) AS freq" in sql
    assert "GROUP BY EarnCode, EarnDescription" in sql
    assert "ORDER BY freq DESC" in sql
