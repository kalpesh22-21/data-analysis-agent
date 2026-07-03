"""Shared builders for the S4 generalize tests (design-doc §8 fixture + §9 slugs).

The S3 blueprint PLAN fixture (`tests/fixtures/learning/s3_blueprint_plan.json`)
carries no SQL — S4 reads the ACCEPTED SQL from the session tool trail. These are
the accepted SQLs whose AST-rewrite reproduces the frozen S4 output fixture
(`tests/fixtures/learning/s4_enriched_blueprint.json`), pinned here so the golden
tests are self-contained.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "learning"

# The D69 `database.table` → {column: type} catalog the provenance extractor
# qualifies against. A minimal slice mirroring the payroll worked example (§3.1).
CATALOG: dict[str, dict[str, str]] = {
    "payroll.payroll_fact": {
        "gross_pay": "Float64",
        "department": "String",
        "pay_period": "Date",
        "record_type": "String",
        "region": "String",
    }
}

# The accepted single-blueprint SQL (the §3.1 worked example, pre-generalization).
SINGLE_SQL = (
    "SELECT sum(gross_pay) AS total_earnings FROM payroll.payroll_fact "
    "WHERE department = '0420' AND toYear(pay_period) = 2025 "
    "AND record_type = 'EARNING' AND region = 'NA'"
)

# The two accepted composite-node SQLs (tc1 = dept, tc2 = company).
COMPOSITE_NODE_SQL: dict[str, str] = {
    "tc1": (
        "SELECT sum(gross_pay) AS dept_total FROM payroll.payroll_fact "
        "WHERE department = '0420' AND toYear(pay_period) = 2025"
    ),
    "tc2": (
        "SELECT sum(gross_pay) AS company_total FROM payroll.payroll_fact "
        "WHERE toYear(pay_period) = 2025"
    ),
}


def load_plan() -> dict[str, Any]:
    """The frozen S3 PLAN fixture (single + composite)."""
    return json.loads((_FIXTURES / "s3_blueprint_plan.json").read_text())


def load_expected() -> dict[str, Any]:
    """The frozen S4 output fixture (the generalization contract)."""
    return json.loads((_FIXTURES / "s4_enriched_blueprint.json").read_text())


def single_sql_by_ref() -> dict[str, str | None]:
    return {"tc1": SINGLE_SQL}


def composite_sql_by_ref() -> dict[str, str | None]:
    return dict(COMPOSITE_NODE_SQL)
