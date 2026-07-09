"""Unit tests for provenance/capture.py (Layer 1 — no infra).

runQuery reuses `extract_column_provenance` (already covered by the existing
39-case `tests/sqlparse/test_column_provenance.py` suite, left unmodified);
these tests add the sampleRows declarative all-columns rule and its
uncatalogued-table fail-closed case (design §3.3), plus getTableSchema's
safe-empty (no-provenance) rule — getTableSchema returns MCP-scope-filtered
column METADATA (no cell values), so it carries `frozenset()` provenance and a
fetched schema always survives the D44 replay filter (2026-07-09 fix), unlike
sampleRows which returns real cell values from all columns.
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


async def test_declarative_all_columns_rule() -> None:
    # sampleRows returns real cell values from every column — declarative
    # all-columns provenance (SELECT * semantics).
    provenance = await capture_provenance(
        "sampleRows", {"database": "dbpcm_warehouse", "table": "employee"}, CATALOG, session_id=None
    )
    assert provenance == frozenset({(_E, "EmployeeCode"), (_E, "Department")})


async def test_sample_rows_uncatalogued_table_is_undetermined() -> None:
    provenance = await capture_provenance(
        "sampleRows",
        {"database": "dbpcm_warehouse", "table": "not_a_real_table"},
        CATALOG,
        session_id=None,
    )
    assert provenance is None


async def test_get_table_schema_is_safe_empty_provenance() -> None:
    """getTableSchema returns MCP-scope-filtered column METADATA (no cell
    values), so it is a no-provenance tool: `frozenset()`, ALWAYS replayable
    (empty set ⊆ any scope). This is what keeps a just-fetched schema visible
    to the model under a restricted `column_scope` (2026-07-09 fix) — it is NOT
    declarative all-columns like sampleRows, and it is NOT gated on the table
    being catalogued."""
    provenance = await capture_provenance(
        "getTableSchema",
        {"database": "dbpcm_warehouse", "table": "employee"},
        CATALOG,
        session_id=None,
    )
    assert provenance == frozenset()


async def test_get_table_schema_uncatalogued_table_is_still_safe_empty() -> None:
    """Even an uncatalogued table yields `frozenset()` for getTableSchema (not
    the fail-closed `None` sampleRows gets) — the MCP scope-filters the metadata
    regardless of catalog membership, so there is nothing to fail closed on."""
    provenance = await capture_provenance(
        "getTableSchema",
        {"database": "dbpcm_warehouse", "table": "not_a_real_table"},
        CATALOG,
        session_id=None,
    )
    assert provenance == frozenset()


@pytest.mark.parametrize(
    "tool_name", ["listDatabases", "listTables", "explainQuery", "getTableSchema"]
)
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
