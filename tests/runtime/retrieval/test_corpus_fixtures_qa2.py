"""QA2 fixture byte-exactness contract (neo4j-corpus-design §8, highest risk).

The single most important schema constraint: every `uses` string in
`tests/fixtures/corpus/blueprints.yaml` must byte-match a real
`database.table.column` scope key of the Layer-2 HR warehouse
(`docker/clickhouse-init/hr-warehouse.sql`). If any fixture key drifts by a byte,
the scope pre-filter (`candidate.uses <= column_scope`) silently drops that
blueprint at recall — no error, just empty retrieval.

This test derives the warehouse's true column set from the ClickHouse DDL and
asserts the fixtures are a subset of it, end-to-end at the fixture level (no
live infra) so the contract holds before it ever reaches neo4j.
"""

from __future__ import annotations

import re
from pathlib import Path

from data_agent.runtime.retrieval.corpus_loader import load_seed_fixtures

_REPO = Path(__file__).resolve().parents[3]
_FIXTURE_DIR = _REPO / "tests" / "fixtures" / "corpus"
_SQL = _REPO / "docker" / "clickhouse-init" / "hr-warehouse.sql"

_CREATE_TABLE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\.(\w+)\s*\((.*?)\)\s*ENGINE",
    re.DOTALL | re.IGNORECASE,
)


def _warehouse_scope_keys() -> set[str]:
    """Parse the ClickHouse DDL into the set of `database.table.column` keys —
    the exact strings the runtime column-scope is built from."""
    sql = _SQL.read_text(encoding="utf-8")
    keys: set[str] = set()
    for db, table, body in _CREATE_TABLE.findall(sql):
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
    assert "dbpcm_warehouse.employee.Department" in keys
    assert "dbpcm_warehouse.payroll.Amount" in keys
    assert "dbpcm_warehouse.payroll.PayPeriodEndDate" in keys
    # Exactly the two seeded tables, 6 + 5 columns.
    assert len([k for k in keys if k.startswith("dbpcm_warehouse.employee.")]) == 6
    assert len([k for k in keys if k.startswith("dbpcm_warehouse.payroll.")]) == 5


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
    # The catalog column names are case-sensitive (SQL header comment). Prove the
    # fixtures preserve case exactly — a lowercased 'department' would drift.
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    all_keys = {k for bp in blueprints for k in bp.uses}
    assert "dbpcm_warehouse.employee.Department" in all_keys
    assert "dbpcm_warehouse.employee.department" not in all_keys


def test_no_fixture_key_references_an_unseeded_catalog_column() -> None:
    # PayDate / PayPeriodStartDate exist in the catalog but are intentionally NOT
    # seeded (SQL comment). A fixture must not reference them, or scope filtering
    # against the real seeded warehouse would drop the blueprint.
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    all_keys = {k for bp in blueprints for k in bp.uses}
    assert not any("PayDate" in k or "PayPeriodStartDate" in k for k in all_keys)
