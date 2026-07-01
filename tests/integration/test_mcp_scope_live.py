"""Layer-2 integration suite — `RealMCPClient` against the LIVE clickhouse-api
MCP (streamable-HTTP), the live token IdP, and real seeded ClickHouse data
(`dbpcm_warehouse.employee`/`payroll`, 5 rows each).

This is the suite that surfaced the two real bugs the fakes hid (see
`src/data_agent/runtime/mcp/real_client.py` FIX 1/FIX 2):
  - `list_tools()` requires a JWT (the live MCP authenticates every request,
    including `tools/list` — no anonymous introspection).
  - The MCP's `[{CODE}]` error marker is wrapped by FastMCP
    (`"Error executing tool <name>: [{CODE}] ..."`), so error-code parsing
    must search the whole string, not just its start.

Skip-guarded on `MCP_TEST_URL` (mirrors `tests/runtime/mcp/test_real_client.py`)
so `uv run pytest` with no live stack configured stays fully green; run with:

    MCP_TEST_URL=http://localhost:18090/mcp uv run pytest tests/integration -v

Assertions here are deterministic MCP/ClickHouse behavior (row counts, column
names, error codes) — NOT LLM output — so exact structural asserts are safe.
"""

from __future__ import annotations

import os
import uuid

import pytest

from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.real_client import RealMCPClient

from .conftest import Mint

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_TEST_URL"),
    reason="Requires a live clickhouse-api MCP stack (set MCP_TEST_URL).",
)

_EMPLOYEE_DB = "dbpcm_warehouse"
_EMPLOYEE_TABLE = "employee"
_EMPLOYEE_FQ = f"{_EMPLOYEE_DB}.{_EMPLOYEE_TABLE}"
_EMPLOYEE_CODE_SCOPE = [f"{_EMPLOYEE_FQ}.EmployeeCode"]

_ALL_SIX_TOOLS = {
    "listDatabases",
    "listTables",
    "getTableSchema",
    "sampleRows",
    "runQuery",
    "explainQuery",
}


def _client() -> RealMCPClient:
    return RealMCPClient(os.environ["MCP_TEST_URL"])


def _session_id() -> str:
    return f"sess-integration-{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# list_tools (FIX 1)
# ---------------------------------------------------------------------------


async def test_list_tools_returns_all_six_tools_with_valid_token(mint: Mint) -> None:
    jwt = await mint()
    tools = await _client().list_tools(jwt=jwt, session_id=_session_id())
    names = {t.name for t in tools}
    assert _ALL_SIX_TOOLS <= names


# ---------------------------------------------------------------------------
# D57 runQuery — column-scope enforcement (FIX 2 for the reject path)
# ---------------------------------------------------------------------------


async def test_run_query_rejects_out_of_scope_column(mint: Mint) -> None:
    jwt = await mint(_EMPLOYEE_CODE_SCOPE)
    with pytest.raises(MCPToolError) as exc_info:
        await _client().call_tool(
            "runQuery",
            {"sql": f"SELECT AnnualSalary FROM {_EMPLOYEE_FQ}"},
            jwt=jwt,
            session_id=_session_id(),
        )
    assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"


async def test_run_query_allowed_scope_returns_rows(mint: Mint) -> None:
    jwt = await mint()  # allow-all
    result = await _client().call_tool(
        "runQuery",
        {"sql": f"SELECT AnnualSalary FROM {_EMPLOYEE_FQ}"},
        jwt=jwt,
        session_id=_session_id(),
    )
    assert result["row_count"] == 5
    assert len(result["rows"]) == 5


async def test_run_query_scope_including_column_returns_rows(mint: Mint) -> None:
    jwt = await mint([f"{_EMPLOYEE_FQ}.EmployeeCode", f"{_EMPLOYEE_FQ}.AnnualSalary"])
    result = await _client().call_tool(
        "runQuery",
        {"sql": f"SELECT AnnualSalary FROM {_EMPLOYEE_FQ}"},
        jwt=jwt,
        session_id=_session_id(),
    )
    assert result["row_count"] == 5


# ---------------------------------------------------------------------------
# Read-only enforcement — INSERT/DDL rejected, never executed
# ---------------------------------------------------------------------------


async def test_insert_is_rejected_and_never_executed(mint: Mint) -> None:
    jwt = await mint()  # allow-all — proves the rejection is read-only enforcement,
    # not a column-scope denial
    with pytest.raises(MCPToolError):
        await _client().call_tool(
            "runQuery",
            {"sql": f"INSERT INTO {_EMPLOYEE_FQ} (EmployeeCode) VALUES ('integration-test-leak')"},
            jwt=jwt,
            session_id=_session_id(),
        )
    # Never executed: the seeded row count is unchanged.
    result = await _client().call_tool(
        "runQuery",
        {"sql": f"SELECT count() AS n FROM {_EMPLOYEE_FQ}"},
        jwt=jwt,
        session_id=_session_id(),
    )
    assert result["rows"] == [[5]]


async def test_ddl_is_rejected_and_never_executed(mint: Mint) -> None:
    jwt = await mint()  # allow-all
    with pytest.raises(MCPToolError):
        await _client().call_tool(
            "runQuery",
            {"sql": f"DROP TABLE {_EMPLOYEE_FQ}"},
            jwt=jwt,
            session_id=_session_id(),
        )
    # Never executed: the table still exists with its seeded row count.
    result = await _client().call_tool(
        "runQuery",
        {"sql": f"SELECT count() AS n FROM {_EMPLOYEE_FQ}"},
        jwt=jwt,
        session_id=_session_id(),
    )
    assert result["rows"] == [[5]]


# ---------------------------------------------------------------------------
# D83 getTableSchema — catalog overlay + scope filter
# ---------------------------------------------------------------------------


async def test_get_table_schema_overlay_allow_all(mint: Mint) -> None:
    jwt = await mint()  # allow-all
    schema = await _client().call_tool(
        "getTableSchema",
        {"database": _EMPLOYEE_DB, "table": _EMPLOYEE_TABLE},
        jwt=jwt,
        session_id=_session_id(),
    )
    assert schema["catalogued"] is True
    assert schema["catalog_sha"]
    assert schema["grain"] == ["EmployeeCode"]

    columns = {col["name"]: col for col in schema["columns"]}
    assert "AnnualSalary" in columns
    assert any(col.get("description") for col in schema["columns"])


async def test_get_table_schema_scope_filter_narrow(mint: Mint) -> None:
    jwt = await mint(_EMPLOYEE_CODE_SCOPE)
    schema = await _client().call_tool(
        "getTableSchema",
        {"database": _EMPLOYEE_DB, "table": _EMPLOYEE_TABLE},
        jwt=jwt,
        session_id=_session_id(),
    )
    names = {col["name"] for col in schema["columns"]}
    assert "AnnualSalary" not in names
    assert "EmployeeCode" in names


# ---------------------------------------------------------------------------
# D83 sampleRows — reject outright on any out-of-scope column, allow-all works
# ---------------------------------------------------------------------------


async def test_sample_rows_rejects_when_table_has_out_of_scope_columns(mint: Mint) -> None:
    jwt = await mint(_EMPLOYEE_CODE_SCOPE)
    with pytest.raises(MCPToolError) as exc_info:
        await _client().call_tool(
            "sampleRows",
            {"database": _EMPLOYEE_DB, "table": _EMPLOYEE_TABLE, "limit": 5},
            jwt=jwt,
            session_id=_session_id(),
        )
    assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"


async def test_sample_rows_allow_all_returns_rows(mint: Mint) -> None:
    jwt = await mint()  # allow-all
    result = await _client().call_tool(
        "sampleRows",
        {"database": _EMPLOYEE_DB, "table": _EMPLOYEE_TABLE, "limit": 5},
        jwt=jwt,
        session_id=_session_id(),
    )
    assert result["row_count"] == 5
    assert len(result["rows"]) == 5
