"""Unit tests for observability/redaction.py (Layer 1, pure — D25 table-driven)."""

from __future__ import annotations

import json

import pytest

from data_agent.runtime.context.scope_filter import compute_scope_hash
from data_agent.runtime.observability.redaction import (
    Redactor,
    hash_scope,
    mask_sql,
    redact_tool_args,
)

SECRET_JWT = "eyJhbGciOi.super-secret-jwt-body.sig"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM employee WHERE Name = 'Jane Doe'", "SELECT * FROM employee WHERE Name = ''"),
        ("SELECT * FROM t LIMIT 20", "SELECT * FROM t LIMIT 0"),
        (
            "SELECT * FROM payroll WHERE Amount > 1000.50 AND Dept = 'Sales'",
            "SELECT * FROM payroll WHERE Amount > 0 AND Dept = ''",
        ),
        ("SELECT s3_column FROM t", "SELECT s3_column FROM t"),  # identifier untouched
        ("SELECT * FROM t WHERE note = 'v1.0; beta'", "SELECT * FROM t WHERE note = ''"),
        ("SELECT 1", "SELECT 0"),
    ],
)
def test_mask_sql_masks_string_and_numeric_literals(sql: str, expected: str) -> None:
    assert mask_sql(sql) == expected


def test_mask_sql_never_leaves_raw_literal_substrings() -> None:
    sql = "SELECT * FROM employee WHERE EmployeeName = 'Jane Doe' AND Salary > 128000"
    masked = mask_sql(sql)
    assert "Jane Doe" not in masked
    assert "128000" not in masked


def test_hash_scope_matches_scope_filter_convention() -> None:
    scope = frozenset({"dbpcm_warehouse.employee.Department"})
    assert hash_scope(scope) == compute_scope_hash(scope)


def test_hash_scope_never_contains_raw_scope_members() -> None:
    scope = frozenset({"dbpcm_warehouse.payroll.Amount"})
    digest = hash_scope(scope)
    assert "dbpcm_warehouse" not in digest
    assert "Amount" not in digest


def test_redact_tool_args_masks_sql_key_only() -> None:
    args = {"sql": "SELECT * FROM t WHERE x = 'secret'", "limit": 20}
    redacted = redact_tool_args("runQuery", args)
    assert redacted["sql"] == "SELECT * FROM t WHERE x = ''"
    assert redacted["limit"] == 20  # non-SQL args pass through untouched


def test_redact_tool_args_leaves_non_sql_tools_untouched() -> None:
    args = {"database": "dbpcm_warehouse", "table": "employee"}
    redacted = redact_tool_args("getTableSchema", args)
    assert redacted == args


def test_redact_tool_args_resolve_values_masks_concept_keeps_identifiers() -> None:
    args = {
        "table": "accrual_events",
        "column": "EarnCode",
        "concept": "employees on maternity leave for Jane Doe",
    }
    redacted = redact_tool_args("resolveValues", args)
    assert redacted["concept"] == "<redacted>"
    assert "Jane Doe" not in json.dumps(redacted)
    # Structural catalog identifiers are kept for debuggability.
    assert redacted["table"] == "accrual_events"
    assert redacted["column"] == "EarnCode"


def test_redact_tool_args_resolve_values_masks_period_literals_keeps_column() -> None:
    args = {
        "table": "accrual_events",
        "column": "EarnCode",
        "concept": "pto",
        "period": {"column": "RequestDate", "start": "2026-01-01", "end": "2026-03-31"},
    }
    redacted = redact_tool_args("resolveValues", args)
    assert redacted["period"]["column"] == "RequestDate"  # kept
    # L1: bounds are fully redacted (they are unvalidated free text, not
    # guaranteed SQL-shaped), not passed through mask_sql.
    assert redacted["period"]["start"] == "<redacted>"
    assert redacted["period"]["end"] == "<redacted>"
    assert "2026" not in json.dumps(redacted["period"])
    # Original args not mutated.
    assert args["period"]["start"] == "2026-01-01"


def test_redactor_class_delegates_to_module_functions() -> None:
    redactor = Redactor()
    scope = frozenset({"a.b.c"})
    assert redactor.hash_scope(scope) == hash_scope(scope)
    assert redactor.mask_sql("SELECT 1") == mask_sql("SELECT 1")
    assert redactor.redact_tool_args("runQuery", {"sql": "SELECT 1"}) == redact_tool_args(
        "runQuery", {"sql": "SELECT 1"}
    )


def test_redaction_never_carries_jwt_substring() -> None:
    # A defense-in-depth guard: even if a caller accidentally stuffed the JWT
    # into a SQL-like string, masking still strips any quoted literal content.
    sql = f"SELECT * FROM t WHERE token = '{SECRET_JWT}'"
    masked = mask_sql(sql)
    assert SECRET_JWT not in masked
