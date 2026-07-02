"""
Adversarial QA suite for the column-provenance extractor (D70 fail-open closure).

Context
-------
D70 was a fail-OPEN: an unqualified column (``col.table == ''``) that was absent
from the catalog was SILENTLY DROPPED.  That understated the USES set, which then
made the D44 replay filter treat the query as ``⊆`` any scope (a scope fail-open).

The fix (src/data_agent/sqlparse/provenance.py, byte-identical to
/Users/kalpeshmulye/Development/clickhouse-api/app/sqlparse/provenance.py) makes such
a column FAIL CLOSED (ProvenanceExtractionError) unless it is provably one of:
  (a) a declared SELECT-list output alias referenced OUTSIDE the projection list
      (GROUP BY / ORDER BY / HAVING / WHERE) — see ``_is_output_alias_reference``; or
  (b) a bare column whose enclosing SELECT draws ONLY from ``scratch`` DB sources
      (uncatalogued by design, D69/OQ-4) — see ``_references_only_scratch_sources``.
Lambda-body columns that are neither bound params nor in the extracted USES set also
fail closed (``_check_lambda_body_coverage``).

The developer added D-07..D-12.  This file (``*_qa``) EXTENDS the adversarial surface.

Contract for every test below
-----------------------------
Each query asserts EITHER:
  * ``pytest.raises(ProvenanceExtractionError)`` (fail-closed), OR
  * an EXACT expected USES frozenset (legit extract).
Never a silent empty/understated set for a query that genuinely reads a warehouse column.

Layer: 1 — Unit (pure logic; SQL strings + in-memory schema dicts; no ClickHouse).

QA finding: after exercising every attack below, NO residual fail-open was found — the
D70 fix holds across cross-scope alias shadowing, scratch/warehouse smuggling, and lambda
param/body smuggling.  If a future regression reopens one, convert the relevant test to an
``@pytest.mark.xfail(strict=True, reason="HIGH: D70 fail-open repro")`` with the crafted
query, per the task's xfail-repro protocol.
"""

from __future__ import annotations

import pytest

from data_agent.sqlparse import (
    ProvenanceExtractionError,
    extract_column_provenance,
)

# ---------------------------------------------------------------------------
# Fixture catalog — a minimal slice mirroring the production catalog shape
# (database.table -> {column: type}).  Intentionally small so the adversarial
# intent of each query is legible.  Column casing is load-bearing (ClickHouse
# identifiers are case-sensitive).
# ---------------------------------------------------------------------------

EMPLOYEE_COLUMNS: dict[str, str] = {
    "EmployeeCode": "String",
    "Department": "Nullable(String)",
    "EmployeeStatus": "Nullable(String)",
    "Position": "Nullable(String)",
}

PAYROLL_COLUMNS: dict[str, str] = {
    "EmployeeCode": "String",
    "Amount": "Nullable(Decimal(18, 6))",
    "RegisterType": "String",
    "PayDate": "DateTime64(6)",
}

CATALOG_SCHEMA: dict[str, dict[str, str]] = {
    "dbpcm_warehouse.employee": EMPLOYEE_COLUMNS,
    "dbpcm_warehouse.payroll": PAYROLL_COLUMNS,
}

_E = "dbpcm_warehouse.employee"
_P = "dbpcm_warehouse.payroll"
_SID = "sess_abc123"


# ===========================================================================
# GROUP 1 — ALIAS-SCOPE FAIL-OPEN ATTEMPTS  (must FAIL CLOSED)
#
# Each of these tries to slip a genuinely-uncatalogued column past the extractor
# by making its NAME resemble, or sit near, a legitimate SELECT-list alias.
# The column is NOT a provable output-alias reference, so it must raise.
# ===========================================================================


def test_qa_uncat_name_coincides_with_alias_of_different_expr_failclosed() -> None:
    """HAVING references a bare uncatalogued column whose sibling clause has a same-shaped alias.

    ``count() c`` declares alias ``c``; ``Department AS foo`` declares alias ``foo``.
    ``bar`` in HAVING is NEITHER — it is a genuinely-uncatalogued non-alias column that
    happens to live in a query full of aliases.  A naive "is there any alias?" check would
    mis-skip it.  ``_is_output_alias_reference`` checks the exact declared alias names, so
    ``bar`` must fail closed.
    """
    sql = (
        "SELECT Department AS foo, count() c "
        "FROM employee GROUP BY foo HAVING bar > 1"
    )
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_qa_alias_shadow_across_nested_selects_outer_uncat_failclosed() -> None:
    """Inner subquery declares alias ``zzz``; outer GROUP BY references an uncatalogued ``Ghost``.

    The inner ``Department AS zzz`` alias is in a DIFFERENT (inner) SELECT scope.  The outer
    query's ``Ghost`` in GROUP BY is not declared as an alias of the OUTER projection and is
    absent from the catalog — it must fail closed, not borrow the inner scope's alias.
    """
    sql = (
        "SELECT count() c FROM (SELECT Department AS zzz FROM employee) t "
        "GROUP BY Ghost"
    )
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_qa_bare_uncatalogued_column_in_projection_list_failclosed() -> None:
    """Identity-alias wrapping: a bare uncatalogued column IS the projection output.

    ``qualify_columns`` auto-wraps every projection as ``X AS X``.  A column sitting in the
    projection list (arg_key ``expressions``) can never be a *reference* to some other
    output alias — it is the (broken) output itself.  ``_is_output_alias_reference`` returns
    False for projection-list position, so this must raise even though ``Ghost`` textually
    equals its own auto-generated alias.
    """
    sql = "SELECT Ghost FROM employee"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_qa_uncat_projection_aliased_to_real_catalog_name_failclosed() -> None:
    """A bare uncatalogued column aliased to a REAL catalog name must still raise.

    ``Ghost AS Department`` — the alias name ``Department`` is a real catalog column, but the
    SOURCE ``Ghost`` is uncatalogued.  The alias name must not launder the unresolved source
    column into the USES set (or drop it silently).  Fail closed.
    """
    sql = "SELECT Ghost AS Department FROM employee"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_qa_groupby_genuinely_uncatalogued_nonalias_failclosed() -> None:
    """GROUP BY on a genuinely-uncatalogued NON-alias column must raise."""
    sql = "SELECT count() FROM employee GROUP BY NoSuchCol"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_qa_orderby_genuinely_uncatalogued_nonalias_failclosed() -> None:
    """ORDER BY on a genuinely-uncatalogued NON-alias column must raise."""
    sql = "SELECT count() c FROM employee ORDER BY NoSuchCol"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_qa_having_genuinely_uncatalogued_nonalias_with_real_groupby_failclosed() -> None:
    """HAVING on an uncatalogued NON-alias column raises even with a valid GROUP BY present.

    The valid ``GROUP BY Department`` must not mask an unresolved ``Ghost`` in HAVING.
    """
    sql = (
        "SELECT Department, count() c FROM employee "
        "GROUP BY Department HAVING Ghost > 1"
    )
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_qa_correlated_subquery_uncatalogued_predicate_failclosed() -> None:
    """A correlated subquery predicate on an uncatalogued column must fail closed.

    ``p.Ghost`` qualifies to payroll but ``Ghost`` is not a payroll column — an unverifiable
    reference hidden inside a correlated WHERE-IN subquery.
    """
    sql = (
        "SELECT Department FROM employee e "
        "WHERE EmployeeStatus IN ("
        "  SELECT RegisterType FROM payroll p WHERE p.Ghost = e.Department"
        ")"
    )
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


# ===========================================================================
# GROUP 2 — ALIAS LEGIT CASES  (must EXTRACT the exact real columns; no false-reject)
# ===========================================================================


def test_qa_alias_in_groupby_orderby_having_extracts() -> None:
    """Canonical legit case: alias reused across GROUP BY, ORDER BY, HAVING.

    ``dept`` (alias of Department) and ``c`` (alias of count()) are query-internal derived
    names.  Only the real ``Department`` access is captured; ``c`` has no column source.
    """
    sql = (
        "SELECT Department AS dept, count() c "
        "FROM employee GROUP BY dept ORDER BY c HAVING c > 1"
    )
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    assert result == frozenset([(_E, "Department")])


def test_qa_alias_reused_in_multiple_nonprojection_clauses_extracts() -> None:
    """Same alias ``dept`` referenced in GROUP BY, HAVING, and ORDER BY simultaneously."""
    sql = (
        "SELECT Department AS dept, count() c "
        "FROM employee GROUP BY dept HAVING c > 0 ORDER BY dept, c"
    )
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    assert result == frozenset([(_E, "Department")])


def test_qa_qualified_columns_multitable_join_extracts() -> None:
    """Qualified real columns across a two-table join extract exactly, no false-reject."""
    sql = (
        "SELECT e.Department, p.Amount "
        "FROM employee e JOIN payroll p ON e.EmployeeCode = p.EmployeeCode "
        "WHERE p.RegisterType = 'EARN'"
    )
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    assert result == frozenset([
        (_E, "Department"),
        (_E, "EmployeeCode"),
        (_P, "Amount"),
        (_P, "EmployeeCode"),
        (_P, "RegisterType"),
    ])


def test_qa_cte_aliased_projection_referenced_downstream_extracts() -> None:
    """A CTE with an aliased projection referenced by the outer query traces to base columns.

    ``d`` (alias of Department) and ``n`` (alias of count()) are consumed downstream; only the
    real ``Department`` access is captured.
    """
    sql = (
        "WITH c AS ("
        "  SELECT Department AS d, count() n FROM employee GROUP BY Department"
        ") SELECT d, n FROM c ORDER BY n"
    )
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    assert result == frozenset([(_E, "Department")])


# ===========================================================================
# GROUP 3 — SCRATCH CASES
#   scratch-only bare col  -> extract (no raise; scratch not scope-checked)
#   scratch + warehouse    -> FAIL CLOSED (cannot smuggle a warehouse col as scratch)
# ===========================================================================


def test_qa_scratch_only_bare_column_extracts() -> None:
    """A bare column against a scratch-ONLY SELECT is a legit scratch reference (no raise).

    Scratch tables are uncatalogued by design (D69/OQ-4); their columns are not scope-checked,
    so the USES set stays empty rather than failing closed.
    """
    sql = "SELECT hire_override, salary_adj FROM scratch.s_sess_abc123_comp"
    result = extract_column_provenance(sql, CATALOG_SCHEMA, session_id=_SID)
    assert result == frozenset()


def test_qa_two_scratch_join_bare_columns_extract() -> None:
    """A JOIN of TWO scratch tables with bare columns extracts the scratch USES (no raise)."""
    sql = (
        "SELECT k FROM scratch.s_sess_abc123_a AS a "
        "JOIN scratch.s_sess_abc123_b AS b ON a.j = b.j"
    )
    result = extract_column_provenance(sql, CATALOG_SCHEMA, session_id=_SID)
    assert result == frozenset([
        ("scratch.s_sess_abc123_a", "j"),
        ("scratch.s_sess_abc123_b", "j"),
    ])


def test_qa_scratch_join_warehouse_bare_unresolved_failclosed() -> None:
    """SMUGGLE ATTEMPT: scratch table JOINed to a catalogued warehouse table + bare column.

    Because a catalogued warehouse table (employee) is a DIRECT source of the SELECT,
    ``_references_only_scratch_sources`` returns False.  A bare ``smuggled`` column that stayed
    unresolved is then genuinely unverifiable — it must FAIL CLOSED, not be waved through as if
    it belonged to the scratch table.  This is the core "can't smuggle a warehouse column as
    scratch" guard.
    """
    sql = (
        "SELECT smuggled FROM scratch.s_sess_abc123_t AS s "
        "JOIN employee AS e ON s.k = e.EmployeeCode"
    )
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA, session_id=_SID)


def test_qa_scratch_join_warehouse_bare_name_is_catalog_column_failclosed() -> None:
    """SMUGGLE ATTEMPT (sharper): the bare column NAME equals a real catalog column.

    ``Amount`` is a real payroll column, but payroll is NOT in this query; employee has no
    ``Amount``; the scratch table is uncatalogued.  Naming the column after a catalog column
    must not smuggle it in — a warehouse table is in direct scope, so fail closed.
    """
    sql = (
        "SELECT Amount FROM scratch.s_sess_abc123_t AS s "
        "JOIN employee e ON s.k = e.EmployeeCode"
    )
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA, session_id=_SID)


def test_qa_scratch_subquery_feeding_warehouse_outer_extracts() -> None:
    """A scratch subquery feeding a warehouse outer query extracts only the warehouse columns.

    The scratch table is confined to the subquery (not a DIRECT source of the outer SELECT),
    so the outer query's real warehouse accesses are captured exactly and the scratch column is
    not scope-checked.
    """
    sql = (
        "SELECT e.Department FROM employee e "
        "WHERE e.EmployeeCode IN (SELECT k FROM scratch.s_sess_abc123_t)"
    )
    result = extract_column_provenance(sql, CATALOG_SCHEMA, session_id=_SID)
    assert result == frozenset([
        (_E, "Department"),
        (_E, "EmployeeCode"),
    ])


def test_qa_warehouse_table_aliased_to_look_like_scratch_failclosed() -> None:
    """A catalogued table given the alias ``scratch`` does NOT become a scratch source.

    Scratch status is determined by the DB of the table node (``scratch``), not by an alias
    that merely spells "scratch".  ``employee AS scratch`` is still a warehouse table, so a
    bare uncatalogued ``Ghost`` against it must fail closed.
    """
    sql = "SELECT Ghost FROM employee AS scratch"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


# ===========================================================================
# GROUP 4 — LAMBDA / HIGHER-ORDER
# ===========================================================================


def test_qa_lambda_body_uncatalogued_column_failclosed() -> None:
    """``arrayMap(x -> x + UncataloguedCol, arr)`` must fail closed.

    ``UncataloguedCol`` in the lambda body is neither a bound parameter nor a captured
    catalog/scratch column — the lambda-body coverage check must raise (D69/OQ-1, D70).
    """
    sql = "SELECT arrayMap(x -> x + UncataloguedCol, [1, 2]) FROM employee"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_qa_lambda_param_only_extracts_empty() -> None:
    """``arrayMap(x -> x + 1, arr)`` references only its bound param — extracts cleanly (empty)."""
    sql = "SELECT arrayMap(x -> x + 1, [1, 2]) FROM employee"
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    assert result == frozenset()


def test_qa_nested_lambda_body_uncatalogued_column_failclosed() -> None:
    """A nested lambda whose INNER body references an uncatalogued column must fail closed.

    Outer param ``x``, inner param ``y``; the inner body ``y + BadCol`` references an
    uncatalogued ``BadCol`` that is bound by neither lambda — fail closed.
    """
    sql = "SELECT arrayMap(x -> arrayMap(y -> y + BadCol, [1]), [2]) FROM employee"
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(sql, CATALOG_SCHEMA)


def test_qa_nested_lambda_body_uses_outer_param_extracts_empty() -> None:
    """A nested lambda whose inner body references the OUTER lambda's param extracts cleanly.

    ``x -> arrayMap(y -> y + x, [1])`` — inside the inner body, ``x`` is an enclosing-lambda
    bound param (union of scopes via ``_bound_lambda_param_names``), NOT a column.  Empty USES.
    """
    sql = "SELECT arrayMap(x -> arrayMap(y -> y + x, [1]), [2]) FROM employee"
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    assert result == frozenset()


def test_qa_lambda_param_name_equals_real_catalog_column_extracts_empty() -> None:
    """SMUGGLE ATTEMPT: a lambda param named after a real catalog column reads NOTHING.

    ``arrayMap(Amount -> Amount + 1, arr)`` binds ``Amount`` as the lambda parameter; inside the
    body ``Amount`` is the bound variable, NOT payroll.Amount.  No data access occurs, so the
    correct (safe) result is the empty set — the out-of-scope column is NOT smuggled in, and it
    is also not spuriously reported.  Shadowing here removes access rather than granting it.
    """
    sql = "SELECT arrayMap(Amount -> Amount + 1, [1, 2]) FROM payroll"
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    assert result == frozenset()


def test_qa_lambda_param_shadows_but_real_column_used_outside_extracts_it() -> None:
    """A shadowing lambda param must not suppress a genuine same-named access elsewhere.

    ``arrayMap(Amount -> Amount + 1, [1]), payroll.Amount`` — the lambda ``Amount`` is a bound
    param (no access), but the qualified ``payroll.Amount`` in the projection IS a real access
    and must be captured.  Guards against the param shadow eating the real outside reference.
    """
    sql = "SELECT arrayMap(Amount -> Amount + 1, [1]), payroll.Amount FROM payroll"
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    assert result == frozenset([(_P, "Amount")])


def test_qa_lambda_body_real_catalog_column_extracts_it() -> None:
    """``arrayMap(x -> x + Amount, arr)`` over payroll captures the real ``Amount`` access."""
    sql = "SELECT arrayMap(x -> x + Amount, [1, 2]) FROM payroll WHERE RegisterType = 'EARN'"
    result = extract_column_provenance(sql, CATALOG_SCHEMA)
    assert result == frozenset([
        (_P, "Amount"),
        (_P, "RegisterType"),
    ])


# ===========================================================================
# GROUP 5 — DETERMINISM / PARITY
#
# The extractor is pure over (sql, catalog_schema, session_id); repeated calls must
# classify identically.  The two repos ship a byte-identical provenance.py
# (shasum 7442f64…), so a data-agent Layer-1 assertion is representative of the
# clickhouse-api copy's behavior.
# ===========================================================================


def test_qa_deterministic_repeated_classification_extract() -> None:
    """The same legit query classifies to the SAME exact USES set across many runs."""
    sql = (
        "SELECT Department AS dept, count() c "
        "FROM employee GROUP BY dept ORDER BY c HAVING c > 1"
    )
    results = {extract_column_provenance(sql, CATALOG_SCHEMA) for _ in range(25)}
    assert len(results) == 1
    assert next(iter(results)) == frozenset([(_E, "Department")])


def test_qa_deterministic_repeated_classification_failclosed() -> None:
    """The same fail-open-attempt query fails closed on EVERY run (never intermittently skips)."""
    sql = "SELECT EmployeeCode, Ghost FROM employee"
    for _ in range(25):
        with pytest.raises(ProvenanceExtractionError):
            extract_column_provenance(sql, CATALOG_SCHEMA)
