"""
Unit tests for the column-provenance extractor.

Implements all 36 named cases from test-plans/column-provenance-extraction.md.
Decision coverage: D44, D52, D57, D62, D63, D64, D69.

Layer: 1 — Unit (pure logic; inputs are SQL strings + in-memory schema dicts;
no ClickHouse connection required).

Normalization contract for D-03:
    The extractor uses the catalog schema dict as-is for column qualification.
    Column name matching is case-sensitive (ClickHouse is case-sensitive).
    SQL keyword case (SELECT vs select, WHERE vs where) does not affect extraction.
    D-03 tests keyword case insensitivity specifically; the column names in the
    test SQL must match the catalog casing for qualification to succeed.
"""

from __future__ import annotations

import pytest

from data_agent.sqlparse import (
    ProvenanceExtractionError,
    ScratchSessionError,
    extract_column_provenance,
)

# ---------------------------------------------------------------------------
# Fixture catalog definitions (matching test-plans/column-provenance-extraction.md)
# ---------------------------------------------------------------------------

# Columns from databaseSchemaDocs/payroll.yaml
PAYROLL_COLUMNS: dict[str, str] = {
    "ClientCode": "Nullable(String)",
    "EmployeeCode": "String",
    "RegisterType": "String",
    "Amount": "Nullable(Decimal(18, 6))",
    "TypeHours": "Nullable(Decimal(18, 6))",
    "TypeCode": "Nullable(String)",
    "TypeCodeDescription": "Nullable(String)",
    "ProfileCode": "Nullable(String)",
    "DistributedDepartmentCode": "Nullable(String)",
    "distributedDepartmentDescription": "Nullable(String)",
    "TypeRate": "Nullable(Decimal(18, 6))",
    "PayDate": "DateTime64(6)",
    "PayPeriodStartDate": "DateTime64(6)",
    "PayPeriodEndDate": "DateTime64(6)",
    "TransactionNumber": "Nullable(String)",
}

# Columns from databaseSchemaDocs/employee.yaml
EMPLOYEE_COLUMNS: dict[str, str] = {
    "ClientCode": "Nullable(String)",
    "EmployeeCode": "String",
    "Department": "Nullable(String)",
    "Position": "Nullable(String)",
    "EmployeeName": "Nullable(String)",
    "EmployeeStatus": "Nullable(String)",
    "DepartmentCode": "Nullable(String)",
    "LivesInState": "Nullable(String)",
    "WorksInState": "Nullable(String)",
    "SUIState": "Nullable(String)",
    "TerminationDate": "Nullable(DateTime64(6))",
    "OldTerminationDate": "Nullable(DateTime64(6))",
    "FirstName": "Nullable(String)",
    "MiddleName": "Nullable(String)",
    "LastName": "Nullable(String)",
    "Nickname": "Nullable(String)",
    "HireDate": "Nullable(DateTime64(6))",
    "MostRecentHireDate": "Nullable(DateTime64(6))",
    "LeaveAbsenceStart": "Nullable(DateTime64(6))",
    "LeaveAbsenceEnd": "Nullable(DateTime64(6))",
    "PrimarySupervisorEmployeeName": "Nullable(String)",
    "SecondarySupervisorEmployeeName": "Nullable(String)",
    "TertiarySupervisorEmployeeName": "Nullable(String)",
    "QuaternarySupervisorEmployeeName": "Nullable(String)",
    "CountryCode": "Nullable(String)",
    "TermReason": "Nullable(String)",
    "FullTimeToPartTimeDate": "Nullable(DateTime64(6))",
    "LastCheckDate": "Nullable(DateTime64(6))",
    "PositionFamilyName": "Nullable(String)",
    "AccrualProfileDesc": "Nullable(String)",
    "BusinessTitlePositionSeat": "Nullable(String)",
    "PositionLevel": "Nullable(Int32)",
    "PrimaryAddressLine1": "Nullable(String)",
    "PrimaryAddressLine2": "Nullable(String)",
    "PrimaryCityMunicipality": "Nullable(String)",
    "PrimaryState": "Nullable(String)",
    "PrimaryZipCode": "Nullable(String)",
    "RehireDate": "Nullable(DateTime64(6))",
    "LastPositionChangeDate": "Nullable(DateTime64(6))",
    "LastWorkedDate": "Nullable(DateTime64(6))",
    "PositionTitlePositionInfo": "Nullable(String)",
    "PayTypeDescription": "Nullable(String)",
    "WorkLocationDescription": "Nullable(String)",
    "WorkLocationAddress": "Nullable(String)",
    "WorkLocationCity": "Nullable(String)",
    "WorkLocationState": "Nullable(String)",
    "WorkLocationZip": "Nullable(String)",
    "WorkLocationCountry": "Nullable(String)",
    "sales_sl_gradution": "Nullable(String)",
    "sales_sl_start_date": "Nullable(String)",
    "AnnualSalary": "Nullable(Decimal(18, 6))",
    "EmploymentType": "Nullable(String)",
    "Rate1": "Nullable(Decimal(18, 6))",
}

# Columns from databaseSchemaDocs/personnel_action_form_changes.yaml
# This dict mirrors the production YAML exactly — 18 columns, no additions.
# Columns NOT in the YAML (FieldName, NewValue) are intentionally absent;
# using them in SQL now correctly raises ProvenanceExtractionError (fail-closed).
PAF_COLUMNS: dict[str, str] = {
    "ClientCode": "Nullable(String)",
    "PafTransactionId": "Int32",
    "EmployeeCode": "Nullable(String)",
    "EmployeeName": "Nullable(String)",
    "PafEffectiveDate": "Nullable(DateTime64(6))",
    "PafEffectiveDateTime": "Nullable(DateTime64(6))",
    "PafStatus": "Nullable(String)",
    "PafComments": "Nullable(String)",
    "ForwardTo": "Nullable(String)",
    "CreatedBy": "Nullable(String)",
    "ModifiedBy": "Nullable(String)",
    "CreatedDate": "Nullable(DateTime64(6))",
    "ModifiedDate": "Nullable(DateTime64(6))",
    "ManagerSignDate": "Nullable(DateTime64(6))",
    "FieldId": "Nullable(String)",
    "FieldLabel": "Nullable(String)",
    "OldValue": "Nullable(String)",
    "ProposedValue": "Nullable(String)",
}

# Base catalog used by most tests: payroll + employee (D69/OQ-3 qualified keys)
CATALOG_SCHEMA: dict[str, dict[str, str]] = {
    "dbpcm_warehouse.payroll": PAYROLL_COLUMNS,
    "dbpcm_warehouse.employee": EMPLOYEE_COLUMNS,
}

# Full catalog including additional tables for multi-join tests
FULL_CATALOG_SCHEMA: dict[str, dict[str, str]] = {
    **CATALOG_SCHEMA,
    "dbpcm_warehouse.personnel_action_form_changes": PAF_COLUMNS,
}

# Convenience shortcuts
_P = "dbpcm_warehouse.payroll"
_E = "dbpcm_warehouse.employee"
_PAF = "dbpcm_warehouse.personnel_action_form_changes"

# ---------------------------------------------------------------------------
# PART 1 — POSITIVE CASES (correct extraction)
# ---------------------------------------------------------------------------


def test_prov_qualified_columns() -> None:
    """P-01 · prov-qualified-columns — baseline extraction with fully-qualified column refs.

    Decision refs: D62, D57, D44
    Oracle note: include in system.query_log.columns oracle harness (D62).
    """
    sql = """
        SELECT payroll.EmployeeCode, payroll.Amount
        FROM payroll
        WHERE payroll.RegisterType = 'EARN'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_P, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "RegisterType"),
    ])
    assert result == expected


def test_prov_unqualified_resolve() -> None:
    """P-02 · prov-unqualified-resolve — qualify_columns resolves bare column refs.

    Decision refs: D62, D57, D44
    Rationale: qualify_columns must attribute unqualified columns to their owning table.
    Oracle note: include in oracle harness.
    """
    sql = """
        SELECT EmployeeCode, Amount
        FROM payroll
        WHERE RegisterType = 'EARN'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_P, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "RegisterType"),
    ])
    assert result == expected


def test_prov_table_alias() -> None:
    """P-03 · prov-table-alias — alias `p` must dereference back to `payroll`.

    Decision refs: D62, D57
    Rationale: naive extractors that don't dereference aliases produce (p, col)
    which defeats scope checking against the real table name.
    """
    sql = """
        SELECT p.EmployeeCode, p.Amount
        FROM payroll AS p
        WHERE p.RegisterType = 'EARN'
          AND p.PayPeriodStartDate >= '2025-01-01'
          AND p.PayPeriodEndDate < '2025-04-01'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_P, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
        (_P, "PayPeriodEndDate"),
    ])
    assert result == expected


def test_prov_join_two_tables() -> None:
    """P-04 · prov-join-two-tables — columns from both tables attributed correctly.

    Decision refs: D62, D57, D44
    Rationale: JOIN predicate columns must be captured from each table independently.
    Oracle note: include in oracle harness.
    """
    sql = """
        SELECT e.EmployeeCode, e.Department, SUM(p.Amount) AS gross_earnings
        FROM employee AS e
        JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
        WHERE p.RegisterType = 'EARN'
          AND e.EmployeeStatus = 'A'
          AND p.PayPeriodStartDate >= '2025-01-01'
        GROUP BY e.EmployeeCode, e.Department
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "EmployeeCode"),
        (_E, "Department"),
        (_E, "EmployeeStatus"),
        (_P, "Amount"),
        (_P, "EmployeeCode"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
    ])
    assert result == expected


def test_prov_avg_gross_pay_gated() -> None:
    """P-05 · prov-avg-gross-pay-gated — core D57/D44 security invariant.

    Decision refs: D57, D44, D62
    Rationale: AVG(p.Amount) returns a derived aggregate — no raw payroll.Amount
    value appears in the output — but payroll.Amount IS referenced in the query
    computation. A user without payroll.Amount in scope MUST be blocked.
    Oracle note: system.query_log.columns must include payroll.Amount (D62).
    """
    sql = """
        SELECT e.Department, AVG(p.Amount) AS avg_gross_pay
        FROM employee AS e
        JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
        WHERE p.RegisterType = 'EARN'
          AND p.PayPeriodStartDate >= '2025-01-01'
        GROUP BY e.Department
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "Department"),
        (_E, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "EmployeeCode"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
    ])
    assert result == expected


def test_prov_where_subquery_forbidden_table() -> None:
    """P-06 · prov-where-subquery-forbidden-table — subquery hidden access gated.

    Decision refs: D57, D44, D62
    Rationale: payroll.Amount in the subquery WHERE must block users without payroll scope
    even though the outer query projects only employee columns.
    """
    sql = """
        SELECT e.EmployeeCode, e.Department
        FROM employee AS e
        WHERE e.EmployeeCode IN (
            SELECT p.EmployeeCode
            FROM payroll AS p
            WHERE p.RegisterType = 'EARN'
              AND p.Amount > 100000
              AND p.PayPeriodStartDate >= '2025-01-01'
        )
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "EmployeeCode"),
        (_E, "Department"),
        (_P, "EmployeeCode"),
        (_P, "RegisterType"),
        (_P, "Amount"),
        (_P, "PayPeriodStartDate"),
    ])
    assert result == expected


def test_prov_cte_single() -> None:
    """P-07 · prov-cte-single — CTE alias is unpacked back to base table columns.

    Decision refs: D62, D57, D44
    Oracle note: include in oracle harness.
    """
    sql = """
        WITH earn_rows AS (
            SELECT EmployeeCode, Amount, PayPeriodStartDate
            FROM payroll
            WHERE RegisterType = 'EARN'
              AND PayPeriodStartDate >= '2025-01-01'
        )
        SELECT e.EmployeeCode, e.Department, er.Amount
        FROM employee AS e
        JOIN earn_rows AS er ON e.EmployeeCode = er.EmployeeCode
        WHERE e.EmployeeStatus = 'A'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_P, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
        (_E, "EmployeeCode"),
        (_E, "Department"),
        (_E, "EmployeeStatus"),
    ])
    assert result == expected


def test_prov_cte_chained() -> None:
    """P-08 · prov-cte-chained — two-level CTE chain traced to base tables.

    Decision refs: D62, D57, D44
    Rationale: the final SELECT references only CTE names; the extractor must
    trace all levels back to base table columns.
    """
    sql = """
        WITH
        earn_rows AS (
            SELECT EmployeeCode, SUM(Amount) AS total_earn
            FROM payroll
            WHERE RegisterType = 'EARN'
              AND PayPeriodStartDate >= '2025-01-01'
            GROUP BY EmployeeCode
        ),
        dept_totals AS (
            SELECT e.Department, SUM(er.total_earn) AS dept_earn
            FROM employee AS e
            JOIN earn_rows AS er ON e.EmployeeCode = er.EmployeeCode
            WHERE e.EmployeeStatus = 'A'
            GROUP BY e.Department
        )
        SELECT Department, dept_earn
        FROM dept_totals
        ORDER BY dept_earn DESC
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_P, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
        (_E, "EmployeeCode"),
        (_E, "Department"),
        (_E, "EmployeeStatus"),
    ])
    assert result == expected


def test_prov_window_function() -> None:
    """P-09 · prov-window-function — PARTITION BY / ORDER BY columns captured.

    Decision refs: D62, D57
    Rationale: window function PARTITION BY and ORDER BY columns do not appear
    in the output projection but ARE referenced. A scope checker that only looks
    at the SELECT list would miss them.
    Oracle note: include in oracle harness.
    """
    sql = """
        SELECT
            e.EmployeeCode,
            e.Department,
            p.Amount,
            p.PayDate,
            ROW_NUMBER() OVER (PARTITION BY e.Department ORDER BY p.Amount DESC) AS rank_in_dept
        FROM employee AS e
        JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
        WHERE p.RegisterType = 'EARN'
          AND p.PayPeriodStartDate >= '2025-01-01'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "EmployeeCode"),
        (_E, "Department"),
        (_P, "Amount"),
        (_P, "PayDate"),
        (_P, "EmployeeCode"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
    ])
    assert result == expected


def test_prov_argmax_combinator() -> None:
    """P-10 · prov-argmax-combinator — ClickHouse argMax ordering column captured.

    Decision refs: D62, D57
    Rationale: argMax(value_col, ordering_col) — the second arg (PayDate) is an
    ordering reference that must appear in the USES set.
    Oracle note: include in oracle harness; oracle may report ClickHouse-specific
    columns for combinators differently.
    """
    sql = """
        SELECT
            e.Department,
            argMax(p.Amount, p.PayDate) AS latest_amount,
            argMax(p.PayPeriodEndDate, p.PayDate) AS latest_period_end
        FROM employee AS e
        JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
        WHERE p.RegisterType = 'EARN'
        GROUP BY e.Department
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "Department"),
        (_E, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "PayDate"),
        (_P, "PayPeriodEndDate"),
        (_P, "EmployeeCode"),
        (_P, "RegisterType"),
    ])
    assert result == expected


def test_prov_count_with_where_forbidden() -> None:
    """P-11 · prov-count-with-where-forbidden — WHERE-only column still gates query.

    Decision refs: D57, D44, D62
    Rationale: count() returns a scalar integer; no raw payroll.Amount appears in the
    output. Yet payroll.Amount is referenced in WHERE and MUST gate users without scope.
    Oracle note: include in oracle harness.
    """
    sql = """
        SELECT count() AS payroll_rows
        FROM payroll
        WHERE RegisterType = 'EARN'
          AND Amount > 0
          AND PayPeriodStartDate >= '2025-01-01'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_P, "RegisterType"),
        (_P, "Amount"),
        (_P, "PayPeriodStartDate"),
    ])
    assert result == expected


def test_prov_scratch_own_session() -> None:
    """P-12 · prov-scratch-own-session — scratch columns accepted without catalog (D69/OQ-4).

    Decision refs: D64, D62, D57
    Rationale: scratch tables are session-gated only (D64 s_<sessionId>_* name-match);
    column scope enforcement applies only to warehouse tables.
    """
    sql = """
        SELECT s.employee_id, s.hire_date_override, e.Department
        FROM scratch.s_sessabc123_onboarding_data AS s
        JOIN employee AS e ON s.employee_id = e.EmployeeCode
        WHERE e.EmployeeStatus = 'A'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA, session_id="sessabc123")
    expected = frozenset([
        ("scratch.s_sessabc123_onboarding_data", "employee_id"),
        ("scratch.s_sessabc123_onboarding_data", "hire_date_override"),
        (_E, "Department"),
        (_E, "EmployeeCode"),
        (_E, "EmployeeStatus"),
    ])
    assert result == expected


def test_prov_multi_join_three_tables() -> None:
    """P-13 · prov-multi-join-three-tables — three-table join attribution.

    Decision refs: D62, D57, D44
    Rationale: three-table fan-out joins are the realistic maximum complexity in
    typical HR queries. Tests linear scaling.
    SQL uses real PAF columns from the YAML: FieldLabel (not FieldName),
    ProposedValue (not NewValue). Fixtures mirror production schema exactly.
    """
    sql = """
        SELECT
            e.EmployeeCode,
            e.Department,
            e.EmployeeStatus,
            SUM(p.Amount) AS total_earn,
            MAX(paf.ProposedValue) AS latest_position_change
        FROM employee AS e
        JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
        JOIN personnel_action_form_changes AS paf ON e.EmployeeCode = paf.EmployeeCode
        WHERE p.RegisterType = 'EARN'
          AND p.PayPeriodStartDate >= '2025-01-01'
          AND paf.FieldLabel = 'Position'
        GROUP BY e.EmployeeCode, e.Department, e.EmployeeStatus
    """
    result = extract_column_provenance(sql, FULL_CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "EmployeeCode"),
        (_E, "Department"),
        (_E, "EmployeeStatus"),
        (_P, "Amount"),
        (_P, "EmployeeCode"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
        (_PAF, "EmployeeCode"),
        (_PAF, "ProposedValue"),
        (_PAF, "FieldLabel"),
    ])
    assert result == expected


# ---------------------------------------------------------------------------
# PART 2 — ADVERSARIAL / NEGATIVE CASES
# ---------------------------------------------------------------------------


def test_prov_unparseable_failclosed() -> None:
    """A-01 · prov-unparseable-failclosed — exotic syntax triggers fail-closed.

    Decision refs: D63, D52, D57, D44
    Rationale: generateRandom is a ClickHouse table-valued function that causes
    an empty-table parse. The fail-closed invariant: must NOT return empty frozenset
    as "no columns used = in scope". Must raise ProvenanceExtractionError.
    Oracle note: not runnable; track as parser-gap false-reject in D63 metric.
    """
    sql = "SELECT * FROM generateRandom('a UInt8, b Float32', 1, 10, 2) LIMIT 5"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_prov_unparseable_clickhouse_lambdas() -> None:
    """A-02 · prov-unparseable-clickhouse-lambdas — lambda body coverage (D69/OQ-1).

    Decision refs: D63, D52, D62
    Rationale: D69/OQ-1 requires that lambda-body columns ARE in the USES set.
    Exactly two outcomes are acceptable:
      Outcome A: parse succeeds + lambda body is walked -> Amount in USES set.
      Outcome B: parse fails or lambda body not proved walked -> ProvenanceExtractionError.
    Silent omission of lambda-body columns is NEVER acceptable.

    EMPIRICAL FINDING (D69/OQ-1 requirement):
      sqlglot 30.x DOES walk lambda body expressions when traversing with find_all().
      The lambda `x -> x * 2` in `arrayMap(x -> x * 2, groupArray(p.Amount))` has
      body `x * 2` where `x` is the lambda PARAMETER — not a real column reference.
      `p.Amount` is an argument to `groupArray()`, not inside the lambda body.
      Therefore qualify_columns finds Amount as a regular column argument (Outcome A).
      When the lambda body itself directly references a catalog column (e.g.
      `arrayMap(x -> x * Amount, ...)`), sqlglot recurses into the body and finds it.
      The lambda_body_coverage check verifies this invariant; a future regression
      would trigger ProvenanceExtractionError (fail-closed, no silent skip).
    """
    sql = """
        SELECT arrayMap(x -> x * 2, groupArray(p.Amount)) AS doubled_amounts,
               EmployeeCode
        FROM payroll AS p
        WHERE RegisterType = 'EARN'
          AND PayPeriodStartDate >= '2025-01-01'
        GROUP BY EmployeeCode
    """
    # Outcome A is expected: parse succeeds and Amount is found (it's a groupArray arg)
    try:
        result = extract_column_provenance(sql, CATALOG_SCHEMA)
        # Outcome A: Amount must be in the USES set
        assert (_P, "Amount") in result, (
            "Lambda case A-02: payroll.Amount must be in USES set "
            "(it is an argument to groupArray() and must be extracted) — "
            "silent omission is a security gap (D69/OQ-1)."
        )
        assert (_P, "EmployeeCode") in result
        assert (_P, "RegisterType") in result
        assert (_P, "PayPeriodStartDate") in result
    except ProvenanceExtractionError:
        # Outcome B is also acceptable: fail-closed (D69/OQ-1)
        pass


def test_prov_select_star_single_table() -> None:
    """A-03 · prov-select-star-single-table — SELECT * expands to ALL catalog columns (D69/OQ-2).

    Decision refs: D62, D57, D44
    Rationale: scope is column-level (D57); SELECT * must expand to all 15 payroll columns.
    Treating SELECT * as 'no columns' would allow scope bypass.
    """
    sql = """
        SELECT *
        FROM payroll
        WHERE RegisterType = 'EARN'
          AND PayPeriodStartDate >= '2025-01-01'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    # All 15 payroll columns must be in the USES set
    assert len([r for r in result if r[0] == _P]) == len(PAYROLL_COLUMNS)
    for col in PAYROLL_COLUMNS:
        assert (_P, col) in result, f"Expected ({_P}, {col}) in USES set for SELECT *"
    # The WHERE columns are a subset of the star expansion
    assert (_P, "RegisterType") in result
    assert (_P, "PayPeriodStartDate") in result


def test_prov_select_star_join() -> None:
    """A-04 · prov-select-star-join — SELECT * expands ALL columns of ALL joined tables.

    Decision refs: D62, D57
    Rationale: user with employee scope but not payroll scope must be blocked because
    payroll columns are in the expanded USES set.
    """
    sql = """
        SELECT *
        FROM employee AS e
        JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
        WHERE p.RegisterType = 'EARN'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    # All employee and payroll columns must be in USES
    for col in EMPLOYEE_COLUMNS:
        assert (_E, col) in result, f"Expected ({_E}, {col}) in USES set for SELECT *"
    for col in PAYROLL_COLUMNS:
        assert (_P, col) in result, f"Expected ({_P}, {col}) in USES set for SELECT *"


def test_prov_union_forbidden_table() -> None:
    """A-05 · prov-union-forbidden-table — both UNION branches extracted.

    Decision refs: D57, D44, D62
    Rationale: second UNION branch accesses payroll.Amount which must appear in USES set
    even though the first branch projects NULL in its place. An extractor that only
    walks the first branch would miss the payroll access entirely.
    """
    sql = """
        SELECT EmployeeCode, Department, NULL AS Amount
        FROM employee
        WHERE EmployeeStatus = 'A'

        UNION ALL

        SELECT EmployeeCode, NULL AS Department, Amount
        FROM payroll
        WHERE RegisterType = 'EARN'
          AND PayPeriodStartDate >= '2025-01-01'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "EmployeeCode"),
        (_E, "Department"),
        (_E, "EmployeeStatus"),
        (_P, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
    ])
    assert result == expected


def test_prov_comment_trick_union() -> None:
    """A-06 · prov-comment-trick-union — commented-out UNION branch NOT extracted.

    Decision refs: D57, D63, D62
    Rationale: inverse of A-05; the commented-out payroll branch must NOT produce
    payroll.Amount in the USES set. Over-extraction would wrongly block a legitimate query.
    """
    sql = """
        SELECT EmployeeCode, Department
        FROM employee
        WHERE EmployeeStatus = 'A'
        -- UNION ALL SELECT EmployeeCode, Amount FROM payroll WHERE RegisterType = 'EARN'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "EmployeeCode"),
        (_E, "Department"),
        (_E, "EmployeeStatus"),
    ])
    assert result == expected
    # The commented payroll branch must NOT appear
    assert (_P, "Amount") not in result


def test_prov_stacked_statements() -> None:
    """A-07 · prov-stacked-statements — semicolon-separated multi-statement rejected.

    Decision refs: D57, D63, D62
    Rationale: multi-statement injection. parse_one (D62) raises on multiple statements.
    Must fail-closed; must NOT extract only the first statement and silently discard second.
    """
    sql = (
        "SELECT EmployeeCode, Department FROM employee WHERE EmployeeStatus = 'A';"
        "SELECT EmployeeCode, Amount FROM payroll WHERE RegisterType = 'EARN'"
    )
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_prov_subquery_hidden_forbidden_column() -> None:
    """A-08 · prov-subquery-hidden-forbidden-column — derived subquery wrapper.

    Decision refs: D57, D44, D62
    Rationale: outer query projects only Department + row_count (a count), but
    the inner subquery uses payroll.Amount in a WHERE predicate. payroll.Amount
    must appear in the USES set. This is the "hidden access via derived subquery" pattern.
    Oracle note: include in oracle harness.
    """
    sql = """
        SELECT dept_summary.Department, dept_summary.row_count
        FROM (
            SELECT e.Department, COUNT(*) AS row_count
            FROM employee AS e
            JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
            WHERE p.RegisterType = 'EARN'
              AND p.Amount > 50000
              AND p.PayPeriodStartDate >= '2025-01-01'
            GROUP BY e.Department
        ) AS dept_summary
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "Department"),
        (_E, "EmployeeCode"),
        (_P, "EmployeeCode"),
        (_P, "RegisterType"),
        (_P, "Amount"),
        (_P, "PayPeriodStartDate"),
    ])
    assert result == expected


def test_prov_scratch_crosssession_rejected() -> None:
    """A-09 · prov-scratch-crosssession-rejected — cross-session scratch access blocked (D64).

    Decision refs: D64, D57, D63
    Rationale: scratch table name `s_sessxyz789_*` does not match injected
    session_id `sessabc123`. The session-boundary gate must reject.
    """
    sql = """
        SELECT s.EmployeeCode, s.salary_adjustment, e.Department
        FROM scratch.s_sessxyz789_compensation_data AS s
        JOIN employee AS e ON s.EmployeeCode = e.EmployeeCode
        WHERE e.EmployeeStatus = 'A'
    """
    with pytest.raises(ScratchSessionError):
        extract_column_provenance(sql, CATALOG_SCHEMA, session_id="sessabc123")


def test_prov_scratch_malformed_name_rejected() -> None:
    """A-10 · prov-scratch-malformed-name-rejected — non-prefixed scratch table blocked.

    Decision refs: D64, D57
    Rationale: `scratch.compensation_export` does not follow the `s_<sessionId>_<file>`
    naming convention. A naming convention is NOT an access boundary without enforcement.
    """
    sql = """
        SELECT * FROM scratch.compensation_export
        JOIN employee AS e ON compensation_export.EmployeeCode = e.EmployeeCode
    """
    with pytest.raises((ScratchSessionError, ProvenanceExtractionError)):
        extract_column_provenance(sql, CATALOG_SCHEMA, session_id="sessabc123")


def test_prov_ddl_blocked() -> None:
    """A-11 · prov-ddl-blocked — DDL is blocked before provenance extraction.

    Decision refs: D57, D63
    Rationale: DDL injection. `runQuery` blocks DDL per D02/D21. The extractor must
    raise ProvenanceExtractionError rather than silently succeed or return columns.
    """
    sql = "CREATE TABLE payroll_export AS SELECT * FROM payroll WHERE RegisterType = 'EARN'"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_prov_insert_blocked() -> None:
    """A-12 · prov-insert-blocked — INSERT is blocked at the read-only gate.

    Decision refs: D57, D63, D21
    Rationale: INSERT is not a SELECT; the read-only guard fires before provenance
    extraction. Must raise ProvenanceExtractionError.
    """
    sql = """
        INSERT INTO payroll (EmployeeCode, Amount, RegisterType)
        SELECT EmployeeCode, Amount * 1.1, RegisterType FROM payroll WHERE RegisterType = 'EARN'
    """
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_prov_with_clause_forbidden_col_hidden() -> None:
    """A-13 · prov-with-clause-forbidden-col-hidden — CTE hides payroll.Amount.

    Decision refs: D57, D44, D62
    Rationale: outer SELECT projects only Department + headcount (a count), masking
    payroll.Amount from the CTE definition. payroll.Amount must still appear in USES.
    Oracle note: include in oracle harness.
    """
    sql = """
        WITH anon_counts AS (
            SELECT e.Department, COUNT(*) AS headcount, SUM(p.Amount) AS dept_total
            FROM employee AS e
            JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
            WHERE p.RegisterType = 'EARN'
              AND p.PayPeriodStartDate >= '2025-01-01'
            GROUP BY e.Department
        )
        SELECT Department, headcount
        FROM anon_counts
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "Department"),
        (_E, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "EmployeeCode"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
    ])
    assert result == expected


def test_prov_case_expression_forbidden_col() -> None:
    """A-14 · prov-case-expression-forbidden-col — CASE predicates reference forbidden col.

    Decision refs: D57, D44, D62
    Rationale: CASE WHEN p.Amount > 100000 THEN 'high' ... returns a string label;
    no raw Amount value appears in the output. Yet payroll.Amount is referenced in
    WHEN predicates and must gate users without payroll scope.
    """
    sql = """
        SELECT
            e.EmployeeCode,
            e.Department,
            CASE
                WHEN p.Amount > 100000 THEN 'high'
                WHEN p.Amount > 50000 THEN 'mid'
                ELSE 'standard'
            END AS pay_band
        FROM employee AS e
        JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
        WHERE p.RegisterType = 'EARN'
          AND p.PayPeriodStartDate >= '2025-01-01'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_E, "EmployeeCode"),
        (_E, "Department"),
        (_P, "Amount"),
        (_P, "EmployeeCode"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
    ])
    assert result == expected


def test_prov_cross_database_reference() -> None:
    """A-15 · prov-cross-database-reference — uncatalogued DB/table -> fail-closed.

    Decision refs: D62, D57, D63
    Rationale: external_hr_db.salary_bands is not in the catalog schema fed to
    qualify_columns. An unknown table must never be assumed in-scope. Fail-closed.
    """
    sql = """
        SELECT e.EmployeeCode, e.Department, ext.salary_band
        FROM dbpcm_warehouse.employee AS e
        JOIN external_hr_db.salary_bands AS ext ON e.EmployeeCode = ext.EmployeeCode
        WHERE e.EmployeeStatus = 'A'
    """
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_prov_fully_qualified_database_prefix() -> None:
    """A-16 · prov-fully-qualified-database-prefix — three-part name normalization (D69/OQ-3).

    Decision refs: D62, D57
    Rationale: the canonical USES pair is FULLY-QUALIFIED (database.table, column).
    Three-part SQL reference `dbpcm_warehouse.payroll.Amount` must resolve to
    the canonical key `(dbpcm_warehouse.payroll, Amount)`.
    """
    sql = """
        SELECT dbpcm_warehouse.payroll.EmployeeCode,
               dbpcm_warehouse.payroll.Amount
        FROM dbpcm_warehouse.payroll
        WHERE dbpcm_warehouse.payroll.RegisterType = 'EARN'
          AND dbpcm_warehouse.payroll.PayPeriodStartDate >= '2025-01-01'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_P, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "RegisterType"),
        (_P, "PayPeriodStartDate"),
    ])
    assert result == expected


def test_prov_empty_string_input() -> None:
    """A-17 · prov-empty-string-input — empty string is fail-closed.

    Decision refs: D62, D63
    Rationale: empty SQL is not a valid query; must not return empty set (which
    would pass scope check as "no columns").
    """
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance("", CATALOG_SCHEMA)


def test_prov_whitespace_only_input() -> None:
    """A-18 · prov-whitespace-only-input — whitespace-only is fail-closed.

    Decision refs: D62, D63
    Rationale: whitespace-only input is not a valid query. Must not return empty
    frozenset which would silently pass scope check.
    """
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance("   \n\t  ", CATALOG_SCHEMA)


def test_prov_explain_statement() -> None:
    """A-19 · prov-explain-statement — EXPLAIN is a caller precondition (D69/OQ-5).

    Decision refs: D62, D63
    Rationale: extract_column_provenance is NEVER called on EXPLAIN queries (D69/OQ-5).
    This is a caller precondition. If the extractor IS called with EXPLAIN (a violation),
    it must raise ProvenanceExtractionError — the most conservative behavior.
    In production, EXPLAIN statements never reach runQuery and therefore never reach
    the provenance extractor (separate explainQuery tool).
    """
    sql = "EXPLAIN SELECT EmployeeCode, Amount FROM payroll WHERE RegisterType = 'EARN'"
    # If called with EXPLAIN (precondition violation), raise ProvenanceExtractionError
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_prov_nonselect_show() -> None:
    """A-20 · prov-nonselect-show — SHOW TABLES produces no USES set -> fail-closed.

    Decision refs: D62, D63
    Rationale: SHOW produces no column-provenance; calling the extractor on it is
    a caller error that must fail-closed.
    """
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance("SHOW TABLES", CATALOG_SCHEMA)


# ---------------------------------------------------------------------------
# PART 3 — DETERMINISM AND ISOLATION CASES
# ---------------------------------------------------------------------------


def test_prov_deterministic_same_input() -> None:
    """D-01 · prov-deterministic-same-input — 100 identical calls return same frozenset.

    Decision refs: D62
    Rationale: no randomness, no caching side-effects between calls.
    Uses P-05 (prov-avg-gross-pay-gated) as the input.
    """
    sql = """
        SELECT e.Department, AVG(p.Amount) AS avg_gross_pay
        FROM employee AS e
        JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
        WHERE p.RegisterType = 'EARN'
          AND p.PayPeriodStartDate >= '2025-01-01'
        GROUP BY e.Department
    """
    first_result = extract_column_provenance(sql, CATALOG_SCHEMA)
    for _ in range(99):
        result = extract_column_provenance(sql, CATALOG_SCHEMA)
        assert result == first_result, "Non-deterministic result detected"


def test_prov_order_independent() -> None:
    """D-02 · prov-order-independent — JOIN table order does not affect attribution.

    Decision refs: D62
    Rationale: join order in FROM clause must not affect which columns are attributed
    to which table.
    """
    sql_a = """
        SELECT e.Department, AVG(p.Amount) AS avg_earn
        FROM payroll AS p JOIN employee AS e ON p.EmployeeCode = e.EmployeeCode
        WHERE p.RegisterType = 'EARN' GROUP BY e.Department
    """
    sql_b = """
        SELECT e.Department, AVG(p.Amount) AS avg_earn
        FROM employee AS e JOIN payroll AS p ON e.EmployeeCode = p.EmployeeCode
        WHERE p.RegisterType = 'EARN' GROUP BY e.Department
    """
    result_a = extract_column_provenance(sql_a, CATALOG_SCHEMA)
    result_b = extract_column_provenance(sql_b, CATALOG_SCHEMA)
    assert result_a == result_b, (
        f"JOIN order affected attribution: A={sorted(result_a)} B={sorted(result_b)}"
    )


def test_prov_case_insensitive_keywords() -> None:
    """D-03 · prov-case-insensitive-keywords — SQL keyword case must not affect extraction.

    Decision refs: D62
    Rationale: SELECT vs select, FROM vs from, WHERE vs where — keyword case must not matter.
    Column name case must match the catalog schema (ClickHouse is case-sensitive for identifiers).
    This test uses lowercase SQL keywords with PascalCase column names matching the catalog.

    Normalization contract: column name matching is case-sensitive; column names in SQL must
    match catalog casing for qualify_columns to resolve them correctly.
    """
    # Note: EmployeeCode, Amount, RegisterType are PascalCase to match PAYROLL_COLUMNS
    sql = "select EmployeeCode, Amount from payroll where RegisterType = 'EARN'"
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    expected = frozenset([
        (_P, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "RegisterType"),
    ])
    assert result == expected


def test_prov_column_case_mismatch_failclosed() -> None:
    """D-04 · prov-column-case-mismatch-failclosed — wrong-case column identifier is fail-closed.

    Decision refs: D63, D62
    Rationale: ClickHouse identifiers are case-sensitive; `amount` and `Amount` are
    genuinely different identifiers.  A query referencing `amount` (lowercase) when
    the catalog only has `Amount` cannot be reliably scoped — the column cannot be
    attributed to any table after qualify_columns.  Must raise ProvenanceExtractionError
    (fail-closed, never a silent skip or case-insensitive fallback).

    This test covers the security contract ruling: identifier matching is CASE-SENSITIVE
    and EXACT against the catalog, for BOTH tables and columns.  Any table or column
    reference that cannot be exactly resolved → fail-closed (D63).
    """
    # 'amount' and 'registerType' are wrong-case vs catalog ('Amount', 'RegisterType')
    sql = "SELECT amount FROM payroll WHERE registerType = 'EARN'"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_prov_table_qualified_star_expands() -> None:
    """D-05 · prov-table-qualified-star-expands — SELECT t.* expands via catalog (D69/OQ-2).

    Decision refs: D62, D57, D69/OQ-2
    Rationale: `SELECT t.*` (table-qualified star) must expand to all columns of the
    referenced table when that table is in the catalog.  This is the `t.*` form of
    SELECT * and must be treated identically to `SELECT *` for scope purposes.
    """
    sql = """
        SELECT t.*
        FROM payroll AS t
        WHERE t.RegisterType = 'EARN'
          AND t.PayPeriodStartDate >= '2025-01-01'
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    # All 15 payroll columns must appear (star expands via catalog)
    assert len([r for r in result if r[0] == _P]) == len(PAYROLL_COLUMNS)
    for col in PAYROLL_COLUMNS:
        assert (_P, col) in result, f"Expected ({_P}, {col}) in USES set for SELECT t.*"


def test_prov_table_qualified_star_uncatalogued_failclosed() -> None:
    """D-06 · prov-table-qualified-star-uncatalogued-failclosed — SELECT t.* on uncatalogued table.

    Decision refs: D63, D62, D69/OQ-2
    Rationale: `SELECT t.*` where `t` maps to a table not in the catalog cannot be
    expanded.  qualify_columns leaves the Star node unresolved.  Must raise
    ProvenanceExtractionError (fail-closed, D69/OQ-2, D63).
    """
    sql = "SELECT t.* FROM external_db.secret_table AS t"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


# ---------------------------------------------------------------------------
# PART 4 — D70 SCOPE FAIL-OPEN REGRESSION (unqualified unresolvable columns)
# ---------------------------------------------------------------------------
#
# These cases pin the D70 fix: an unqualified column that qualify_columns leaves
# with col.table == '' and that is NEITHER a catalog column NOR a provable
# SELECT-list output alias reference is a genuinely-unresolvable column.  The
# extractor previously SKIPPED it (understated USES = fail-open; the D44 replay
# filter then treated the trail entry as ⊆ any scope).  It must now fail closed.


def test_prov_unresolvable_bare_column_single_table_failclosed() -> None:
    """D-07 · prov-unresolvable-bare-column-single-table — bare uncatalogued column fails closed.

    Decision refs: D63, D70
    Bug repro: `SELECT NonExistentCol FROM employee` — the column is absent from the
    single source table's catalog.  qualify_columns leaves col.table == '' and the name
    is not a SELECT-list alias reference.  Previously returned uses=∅ (the column
    vanished).  Must now raise ProvenanceExtractionError (fail-closed).
    """
    sql = "SELECT NonExistentCol FROM dbpcm_warehouse.employee"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_prov_mixed_valid_and_unresolvable_column_failclosed() -> None:
    """D-08 · prov-mixed-valid-and-unresolvable-column — one bad column poisons the query.

    Decision refs: D63, D70
    Bug repro: `SELECT EmployeeCode, NonExistentCol FROM employee` previously returned
    uses={EmployeeCode} — NonExistentCol was silently dropped, an understated USES set.
    A single unresolvable column must fail the whole extraction closed.
    """
    sql = "SELECT EmployeeCode, NonExistentCol FROM employee"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_prov_uncatalogued_lambda_body_column_failclosed() -> None:
    """D-09 · prov-uncatalogued-lambda-body-column — uncatalogued lambda-body column fails closed.

    Decision refs: D63, D69/OQ-1, D70
    Bug repro: `arrayMap(x -> x + BadCol, [1,2]) FROM employee` where BadCol is
    uncatalogued.  The lambda-body coverage check previously only examined columns that
    were IN the catalog, so BadCol was never inspected → uses=∅ (fail-open).  A lambda
    body column that is neither a bound parameter nor a captured catalog/scratch column
    must fail closed.
    """
    sql = "SELECT arrayMap(x -> x + BadCol, [1, 2]) AS mapped FROM employee"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_prov_output_alias_reference_still_extracts() -> None:
    """D-10 · prov-output-alias-reference — legit SELECT-list alias in GROUP BY/ORDER BY/HAVING.

    Decision refs: D62, D70
    Regression guard for the D70 fix: a computed SELECT-list output alias referenced in
    GROUP BY / ORDER BY / HAVING is a query-internal derived name (no new data access) and
    must still be SKIPPED, not rejected.  Only the underlying real columns are captured.
    """
    sql = """
        SELECT Department AS dept, count() AS c
        FROM employee
        GROUP BY dept
        HAVING c > 5
        ORDER BY c DESC
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    # `dept` (alias of Department) resolves the real Department access; `c` (alias of
    # count()) is a derived name with no column source.
    assert result == frozenset([(_E, "Department")])


def test_prov_lambda_bound_param_not_rejected() -> None:
    """D-11 · prov-lambda-bound-param — a lambda parameter name is not a column reference.

    Decision refs: D69/OQ-1, D70
    Regression guard: the lambda-body coverage check must exclude the lambda's own bound
    parameters (and those of enclosing lambdas).  A lambda whose body references only its
    parameters must extract cleanly, not fail closed.
    """
    sql = """
        SELECT arrayMap(x -> x * 2, groupArray(Amount)) AS doubled, EmployeeCode
        FROM payroll
        WHERE RegisterType = 'EARN'
        GROUP BY EmployeeCode
    """
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    assert (_P, "Amount") in result
    assert (_P, "EmployeeCode") in result
    assert (_P, "RegisterType") in result


def test_prov_unqualified_scratch_only_column_not_rejected() -> None:
    """D-12 · prov-unqualified-scratch-only-column — bare scratch column not fail-closed.

    Decision refs: D64, D69/OQ-4, D70
    Regression guard: a scratch table is uncatalogued by design, so qualify_columns
    leaves its bare (unqualified) column with col.table == '' — identical in shape to a
    genuinely-unresolvable warehouse column.  When the enclosing SELECT draws ONLY from
    scratch sources, the bare column is a legitimate scratch reference and must NOT fail
    closed (the D70 case-(D) fail-closed applies only when a catalogued warehouse table is
    in scope).  Scratch columns are not scope-checked, so the USES set stays empty here.
    """
    sql = (
        "SELECT hire_date_override, salary_adj "
        "FROM scratch.s_sessabc123_compensation"
    )
    result = extract_column_provenance(sql, CATALOG_SCHEMA, session_id="sessabc123")
    assert result == frozenset()
