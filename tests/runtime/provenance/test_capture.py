"""Unit tests for provenance/capture.py (Layer 1 — no infra).

runQuery reuses `extract_column_provenance` (already covered by the existing
39-case `tests/sqlparse/test_column_provenance.py` suite, left unmodified);
these tests add the sampleRows/getTableSchema declarative rule and the
uncatalogued-table fail-closed cases (design §3.3).
"""

from __future__ import annotations

import pytest

from data_agent.runtime.provenance.capture import capture_provenance
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_P = "dbpcm_warehouse.payroll"
_E = "dbpcm_warehouse.employee"

CATALOG = CatalogHandle(
    {
        _P: {
            "EmployeeCode": "String",
            "Amount": "Nullable(Decimal(18, 6))",
            "RegisterType": "String",
        },
        _E: {"EmployeeCode": "String", "Department": "Nullable(String)"},
    }
)


async def test_run_query_reuses_extractor() -> None:
    provenance = await capture_provenance(
        "runQuery",
        {"sql": "SELECT EmployeeCode, Amount FROM payroll WHERE RegisterType = 'EARN'"},
        CATALOG,
        session_id=None,
    )
    assert provenance == frozenset({(_P, "EmployeeCode"), (_P, "Amount"), (_P, "RegisterType")})


async def test_run_query_parse_failure_is_undetermined() -> None:
    provenance = await capture_provenance(
        "runQuery",
        {"sql": "SELECT * FROM generateRandom('a UInt8', 1, 10, 2)"},
        CATALOG,
        session_id=None,
    )
    assert provenance is None


async def test_run_query_no_columns_referenced_is_empty_not_none() -> None:
    provenance = await capture_provenance(
        "runQuery", {"sql": "SELECT 1"}, CATALOG, session_id=None
    )
    assert provenance == frozenset()


@pytest.mark.parametrize("tool_name", ["sampleRows", "getTableSchema"])
async def test_declarative_all_columns_rule(tool_name: str) -> None:
    provenance = await capture_provenance(
        tool_name, {"database": "dbpcm_warehouse", "table": "employee"}, CATALOG, session_id=None
    )
    assert provenance == frozenset({(_E, "EmployeeCode"), (_E, "Department")})


@pytest.mark.parametrize("tool_name", ["sampleRows", "getTableSchema"])
async def test_uncatalogued_table_is_undetermined(tool_name: str) -> None:
    provenance = await capture_provenance(
        tool_name, {"database": "dbpcm_warehouse", "table": "not_a_real_table"}, CATALOG, session_id=None
    )
    assert provenance is None


@pytest.mark.parametrize("tool_name", ["listDatabases", "listTables", "explainQuery"])
async def test_no_provenance_tools_return_empty_frozenset(tool_name: str) -> None:
    provenance = await capture_provenance(tool_name, {}, CATALOG, session_id=None)
    assert provenance == frozenset()


async def test_unknown_tool_name_is_conservatively_undetermined() -> None:
    provenance = await capture_provenance("someFutureTool", {}, CATALOG, session_id=None)
    assert provenance is None


async def test_run_query_scratch_session_violation_is_undetermined() -> None:
    """A ScratchSessionError (subclass of ProvenanceExtractionError) also fails closed."""
    provenance = await capture_provenance(
        "runQuery",
        {
            "sql": (
                "SELECT s.EmployeeCode FROM scratch.s_sess_other_data AS s "
                "JOIN employee AS e ON s.EmployeeCode = e.EmployeeCode"
            )
        },
        CATALOG,
        session_id="sess_mine",
    )
    assert provenance is None
