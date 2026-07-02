"""
Layer-1 unit tests for the D62 provenance false-reject oracle
(`data_agent.sqlparse.oracle`).

Pure logic — a hand-authored fixture catalog + hand-authored SQL strings; no
ClickHouse, no live infra.  Proves the classifier routes each query to the right
`Outcome`, aggregates counts/rate correctly, and never leaks raw SQL values in a
rejected sample (D25 redaction).

The live Layer-2 replay against `system.query_log` lives in
`tests/integration/test_provenance_oracle_live.py`.
"""

from __future__ import annotations

from data_agent.sqlparse.oracle import (
    Classification,
    Outcome,
    classify_query,
    run_oracle,
)

# ---------------------------------------------------------------------------
# Fixture catalog (subset of databaseSchemaDocs — case-sensitive, D70)
# ---------------------------------------------------------------------------

CATALOG: dict[str, dict[str, str]] = {
    "dbpcm_warehouse.employee": {
        "EmployeeCode": "String",
        "Department": "Nullable(String)",
        "AnnualSalary": "Nullable(Decimal(18, 6))",
    },
    "dbpcm_warehouse.payroll": {
        "EmployeeCode": "String",
        "Amount": "Nullable(Decimal(18, 6))",
        "RegisterType": "String",
    },
}

# A clean, in-scope SELECT — every column/table is catalogued.
CLEAN_SQL = (
    "SELECT Department, AnnualSalary FROM dbpcm_warehouse.employee WHERE Department = 'Sales'"
)
# An out-of-scope column: `amount` (lowercase) is not the catalogued `Amount`;
# ClickHouse identifiers are case-sensitive so this is unresolvable (fail-closed).
OUT_OF_SCOPE_COLUMN_SQL = "SELECT amount FROM dbpcm_warehouse.payroll"
# An unresolvable reference: `foo` is neither a table alias nor a CTE.
UNRESOLVABLE_REF_SQL = "SELECT foo.Amount FROM dbpcm_warehouse.payroll"
# Genuinely un-parseable syntax (sqlglot ParseError under the hood).
PARSE_ERROR_SQL = "SELECT FROM WHERE ((( "
# A non-SELECT statement — blocked as a fail-closed reject, not a parse error.
NON_SELECT_SQL = "INSERT INTO dbpcm_warehouse.employee VALUES (1)"


# ---------------------------------------------------------------------------
# classify_query — one query → one Outcome
# ---------------------------------------------------------------------------


def test_clean_in_scope_select_extracts_ok() -> None:
    c = classify_query(CLEAN_SQL, CATALOG)
    assert c.outcome is Outcome.EXTRACTED_OK
    assert c.is_reject is False
    # Department appears twice (SELECT + WHERE) but the USES set dedups it.
    assert c.uses_count == 2
    assert c.reason == ""


def test_out_of_scope_column_is_fail_closed_reject() -> None:
    c = classify_query(OUT_OF_SCOPE_COLUMN_SQL, CATALOG)
    assert c.outcome is Outcome.FAIL_CLOSED_REJECT
    assert c.is_reject is True
    assert c.uses_count == 0
    assert c.reason  # a non-empty, masked reason is recorded


def test_unresolvable_reference_is_fail_closed_reject() -> None:
    c = classify_query(UNRESOLVABLE_REF_SQL, CATALOG)
    assert c.outcome is Outcome.FAIL_CLOSED_REJECT
    assert c.is_reject is True


def test_unparseable_sql_is_parse_error() -> None:
    c = classify_query(PARSE_ERROR_SQL, CATALOG)
    assert c.outcome is Outcome.PARSE_ERROR
    assert c.is_reject is True


def test_non_select_is_fail_closed_not_parse_error() -> None:
    # A syntactically valid statement that is simply the wrong kind must NOT be
    # mis-attributed to the parser (it is a semantic reject, not a syntax gap).
    c = classify_query(NON_SELECT_SQL, CATALOG)
    assert c.outcome is Outcome.FAIL_CLOSED_REJECT


# ---------------------------------------------------------------------------
# Redaction (D25) — a rejected sample never carries raw SQL values
# ---------------------------------------------------------------------------


def test_classification_redacts_string_and_numeric_literals() -> None:
    sql = "SELECT amount FROM dbpcm_warehouse.payroll WHERE RegisterType = 'SECRET-VALUE-123'"
    c = classify_query(sql, CATALOG)
    # The literal value must not appear anywhere in the redacted classification.
    assert "SECRET-VALUE-123" not in c.masked_preview
    assert "SECRET-VALUE-123" not in c.reason
    # Structure is preserved (identifiers/keywords survive masking).
    assert "RegisterType" in c.masked_preview
    # The hash is a full sha256 hex digest of the raw SQL (stable identity).
    assert len(c.query_hash) == 64
    assert c.query_len == len(sql)


def test_numeric_literals_are_masked_in_preview() -> None:
    sql = "SELECT Amount FROM dbpcm_warehouse.payroll WHERE Amount > 999999"
    c = classify_query(sql, CATALOG)
    assert "999999" not in c.masked_preview


# ---------------------------------------------------------------------------
# run_oracle — aggregation, rate, samples
# ---------------------------------------------------------------------------


def _corpus() -> list[str]:
    return [
        CLEAN_SQL,
        CLEAN_SQL,  # a second OK to prove counting, not dedup
        OUT_OF_SCOPE_COLUMN_SQL,
        UNRESOLVABLE_REF_SQL,
        PARSE_ERROR_SQL,
    ]


def test_run_oracle_aggregates_counts_and_rate() -> None:
    report = run_oracle(_corpus(), CATALOG)

    assert report.total == 5
    assert report.extracted_ok == 2
    assert report.fail_closed_reject == 2  # out-of-scope column + unresolvable ref
    assert report.parse_error == 1
    assert report.reject_count == 3
    assert report.reject_rate == 3 / 5

    # Counts partition the corpus exactly.
    assert report.extracted_ok + report.reject_count == report.total


def test_run_oracle_preserves_order() -> None:
    report = run_oracle(_corpus(), CATALOG)
    outcomes = [c.outcome for c in report.classifications]
    assert outcomes == [
        Outcome.EXTRACTED_OK,
        Outcome.EXTRACTED_OK,
        Outcome.FAIL_CLOSED_REJECT,
        Outcome.FAIL_CLOSED_REJECT,
        Outcome.PARSE_ERROR,
    ]


def test_rejected_samples_are_only_rejects_and_respect_limit() -> None:
    report = run_oracle(_corpus(), CATALOG)
    samples = report.rejected_samples()
    assert len(samples) == 3
    assert all(isinstance(s, Classification) and s.is_reject for s in samples)

    limited = report.rejected_samples(limit=1)
    assert len(limited) == 1


def test_empty_corpus_has_zero_rate_not_division_error() -> None:
    report = run_oracle([], CATALOG)
    assert report.total == 0
    assert report.reject_count == 0
    assert report.reject_rate == 0.0
    assert report.rejected_samples() == []


# ---------------------------------------------------------------------------
# render — a printable, redacted report
# ---------------------------------------------------------------------------


def test_render_contains_headline_metrics_and_no_raw_values() -> None:
    sql = "SELECT amount FROM dbpcm_warehouse.payroll WHERE RegisterType = 'TOP-SECRET'"
    report = run_oracle([CLEAN_SQL, sql], CATALOG)
    text = report.render()

    assert "reject_rate" in text
    assert "0.5000" in text  # 1 reject / 2 total
    assert "rejected samples" in text
    # No raw literal value leaks into the rendered report.
    assert "TOP-SECRET" not in text


def test_render_reports_no_rejects_for_clean_corpus() -> None:
    report = run_oracle([CLEAN_SQL], CATALOG)
    text = report.render()
    assert "no rejects" in text
