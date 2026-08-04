"""QA2 fixture byte-exactness contract (neo4j-corpus-design §8, highest risk).

The single most important schema constraint: every `uses` string in
`tests/fixtures/corpus/blueprints.yaml` must byte-match a real
`database.table.column` scope key of the Layer-2 HR warehouse. The seed is now
split across TWO coherent snake_case migrations: the 4 core tables (employee,
payroll, department, labor_allocation) live in `hr-4tables-snake-migration.sql`
and the other 7 in `hr-warehouse.sql`. If any fixture key drifts by a byte, the
scope pre-filter (`candidate.uses <= column_scope`) silently drops that blueprint
at recall — no error, just empty retrieval.

This test derives the warehouse's true column set from BOTH ClickHouse DDL files
and asserts the fixtures are a subset of it, end-to-end at the fixture level (no
live infra) so the contract holds before it ever reaches neo4j.
"""

from __future__ import annotations

import re
from pathlib import Path

from data_agent.runtime.retrieval.corpus_loader import load_seed_fixtures

_REPO = Path(__file__).resolve().parents[3]
_FIXTURE_DIR = _REPO / "tests" / "fixtures" / "corpus"
_SQL = _REPO / "docker" / "clickhouse-init" / "hr-warehouse.sql"
# The 4 core tables (employee/payroll/department/labor_allocation) moved to this
# sibling migration; the warehouse scope is the UNION of both DDL files.
_SQL_4T = _REPO / "docker" / "clickhouse-init" / "hr-4tables-snake-migration.sql"

# `IF NOT EXISTS` is optional: the warehouse DDL uses `DROP TABLE IF EXISTS x;`
# followed by a bare `CREATE TABLE x (...)`. The non-greedy `(.*?)\)\s*ENGINE`
# still captures each table's full column body despite inner parens in types like
# `Nullable(String)`, `Decimal(18, 6)` or `DateTime64(6)`, because only the table's
# own closing paren is immediately followed by `ENGINE`.
_CREATE_TABLE = re.compile(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)\.(\w+)\s*\((.*?)\)\s*ENGINE",
    re.DOTALL | re.IGNORECASE,
)

# The eleven tables the Layer-2 warehouse seed (both DDL migrations) creates.
_EXPECTED_TABLES = {
    "dbpcm_warehouse.employee",
    "dbpcm_warehouse.payroll",
    "dbpcm_warehouse.department",
    "dbpcm_warehouse.labor_allocation",
    "dbpcm_warehouse.accrual_events",
    "dbpcm_warehouse.personnel_action_form_changes",
    "dbpcm_warehouse.applicant_tracking_application",
    "dbpcm_warehouse.applicant_tracking_requisition",
    "dbpcm_warehouse.candidate_education",
    "dbpcm_warehouse.candidate_employment_history",
    "dbpcm_warehouse.performance_discussions",
}


def _warehouse_scope_keys() -> set[str]:
    """Parse the ClickHouse DDL into the set of `database.table.column` keys —
    the exact strings the runtime column-scope is built from. The warehouse scope
    is the UNION of both snake_case seed migrations (7 tables + the 4 core)."""
    sql = _SQL.read_text(encoding="utf-8") + "\n" + _SQL_4T.read_text(encoding="utf-8")
    keys: set[str] = set()
    for db, table, body in _CREATE_TABLE.findall(sql):
        if db != "dbpcm_warehouse":
            continue  # the 4-table migration also creates a security-DB table
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("--"):
                continue
            column = line.split()[0].strip(",")
            if column:
                keys.add(f"{db}.{table}.{column}")
    return keys


def test_ddl_parser_recovers_the_expected_columns() -> None:
    # Guard the guard: if the DDL format changes and the parser silently returns
    # nothing, the subset assertion below would vacuously pass. Anchor on known
    # columns so a broken parser fails loudly instead.
    keys = _warehouse_scope_keys()
    # Known-column anchors (snake_case, Wave-1 catalog) spanning several seeded
    # tables. If the parser silently returned nothing/garbage, these fail loudly.
    assert "dbpcm_warehouse.employee.department_name" in keys
    assert "dbpcm_warehouse.payroll.amount" in keys
    assert "dbpcm_warehouse.payroll.pay_period_end_date" in keys
    assert "dbpcm_warehouse.accrual_events.earn_code" in keys
    assert "dbpcm_warehouse.performance_discussions.discussion_id" in keys
    assert "dbpcm_warehouse.applicant_tracking_application.application_id" in keys

    # The parser must recover exactly the eleven seeded tables — no more, no fewer.
    recovered_tables = {k.rsplit(".", 1)[0] for k in keys}
    assert recovered_tables == _EXPECTED_TABLES

    # Lower bounds rather than exact per-table counts: this test was previously
    # pinned to `== 6`/`== 5` and silently broke when the seed grew to full
    # fidelity (employee now 53 cols, payroll 15). Bounds keep the guard honest
    # against a broken parser while surviving future column additions.
    employee_cols = [k for k in keys if k.startswith("dbpcm_warehouse.employee.")]
    payroll_cols = [k for k in keys if k.startswith("dbpcm_warehouse.payroll.")]
    assert len(employee_cols) >= 50
    assert len(payroll_cols) >= 15


def test_every_fixture_uses_key_byte_matches_a_real_warehouse_column() -> None:
    scope_keys = _warehouse_scope_keys()
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    offenders: list[tuple[str, str]] = []
    for bp in blueprints:
        for key in bp.uses:
            if key not in scope_keys:
                offenders.append((bp.id, key))
    assert offenders == [], (
        "fixture uses keys that do NOT byte-match a warehouse column "
        f"(silent scope-drop at recall): {offenders}"
    )


def test_fixture_keys_are_case_sensitive_exact() -> None:
    # The catalog column names are case-sensitive snake_case (Wave-1). Prove the
    # fixtures use the exact snake_case name — the old PascalCase would drift.
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    all_keys = {k for bp in blueprints for k in bp.uses}
    assert "dbpcm_warehouse.employee.department_name" in all_keys
    assert "dbpcm_warehouse.employee.Department" not in all_keys


def test_no_fixture_key_references_a_disallowed_payroll_date_column() -> None:
    # payroll carries three date columns: pay_date, pay_period_start_date and
    # pay_period_end_date. All three ARE seeded in the current warehouse, but the
    # fixtures deliberately anchor every pay-period predicate on pay_period_end_date
    # (the register's period-close date) for a single, consistent period grain.
    # A fixture drifting to pay_date or pay_period_start_date would silently change
    # that grain, so guard against it — this is a convention guard, not a
    # "column is unseeded" guard.
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    all_keys = {k for bp in blueprints for k in bp.uses}
    assert not any(k.endswith(".pay_date") or "pay_period_start_date" in k for k in all_keys)
