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


async def _bound(mint: Mint, column_scope: list[str] | None = None) -> tuple[str, str]:
    """Mint a JWT bound to a fresh session id, returning `(jwt, session_id)`.

    The live MCP enforces the X-Session-Id ↔ sid_hash binding by default
    (auth-hardening Slice 1, `require_sid_binding=true`), so every real tool
    call must present a token bound to the exact session id it sends in the
    `X-Session-Id` header. Returning both together keeps them in lock-step.
    """
    session_id = _session_id()
    jwt = await mint(column_scope, session_id=session_id)
    return jwt, session_id


# ---------------------------------------------------------------------------
# list_tools (FIX 1)
# ---------------------------------------------------------------------------


async def test_list_tools_returns_all_six_tools_with_valid_token(mint: Mint) -> None:
    jwt, session_id = await _bound(mint)
    tools = await _client().list_tools(jwt=jwt, session_id=session_id)
    names = {t.name for t in tools}
    assert _ALL_SIX_TOOLS <= names


# ---------------------------------------------------------------------------
# D57 runQuery — column-scope enforcement (FIX 2 for the reject path)
# ---------------------------------------------------------------------------


async def test_run_query_rejects_out_of_scope_column(mint: Mint) -> None:
    jwt, session_id = await _bound(mint, _EMPLOYEE_CODE_SCOPE)
    with pytest.raises(MCPToolError) as exc_info:
        await _client().call_tool(
            "runQuery",
            {"sql": f"SELECT AnnualSalary FROM {_EMPLOYEE_FQ}"},
            jwt=jwt,
            session_id=session_id,
        )
    assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"


async def test_run_query_allowed_scope_returns_rows(mint: Mint) -> None:
    jwt, session_id = await _bound(mint)  # allow-all
    result = await _client().call_tool(
        "runQuery",
        {"sql": f"SELECT AnnualSalary FROM {_EMPLOYEE_FQ}"},
        jwt=jwt,
        session_id=session_id,
    )
    assert result["row_count"] == 5
    assert len(result["rows"]) == 5


async def test_run_query_scope_including_column_returns_rows(mint: Mint) -> None:
    jwt, session_id = await _bound(
        mint, [f"{_EMPLOYEE_FQ}.EmployeeCode", f"{_EMPLOYEE_FQ}.AnnualSalary"]
    )
    result = await _client().call_tool(
        "runQuery",
        {"sql": f"SELECT AnnualSalary FROM {_EMPLOYEE_FQ}"},
        jwt=jwt,
        session_id=session_id,
    )
    assert result["row_count"] == 5


# ---------------------------------------------------------------------------
# Read-only enforcement — INSERT/DDL rejected, never executed
# ---------------------------------------------------------------------------


async def test_insert_is_rejected_and_never_executed(mint: Mint) -> None:
    jwt, session_id = await _bound(mint)  # allow-all — proves the rejection is
    # read-only enforcement, not a column-scope denial
    with pytest.raises(MCPToolError):
        await _client().call_tool(
            "runQuery",
            {"sql": f"INSERT INTO {_EMPLOYEE_FQ} (EmployeeCode) VALUES ('integration-test-leak')"},
            jwt=jwt,
            session_id=session_id,
        )
    # Never executed: the seeded row count is unchanged.
    result = await _client().call_tool(
        "runQuery",
        {"sql": f"SELECT count() AS n FROM {_EMPLOYEE_FQ}"},
        jwt=jwt,
        session_id=session_id,
    )
    assert result["rows"] == [[5]]


async def test_ddl_is_rejected_and_never_executed(mint: Mint) -> None:
    jwt, session_id = await _bound(mint)  # allow-all
    with pytest.raises(MCPToolError):
        await _client().call_tool(
            "runQuery",
            {"sql": f"DROP TABLE {_EMPLOYEE_FQ}"},
            jwt=jwt,
            session_id=session_id,
        )
    # Never executed: the table still exists with its seeded row count.
    result = await _client().call_tool(
        "runQuery",
        {"sql": f"SELECT count() AS n FROM {_EMPLOYEE_FQ}"},
        jwt=jwt,
        session_id=session_id,
    )
    assert result["rows"] == [[5]]


# ---------------------------------------------------------------------------
# D83 getTableSchema — catalog overlay + scope filter
# ---------------------------------------------------------------------------


async def test_get_table_schema_overlay_allow_all(mint: Mint) -> None:
    jwt, session_id = await _bound(mint)  # allow-all
    schema = await _client().call_tool(
        "getTableSchema",
        {"database": _EMPLOYEE_DB, "table": _EMPLOYEE_TABLE},
        jwt=jwt,
        session_id=session_id,
    )
    assert schema["catalogued"] is True
    assert schema["catalog_sha"]
    assert schema["grain"] == ["EmployeeCode"]

    columns = {col["name"]: col for col in schema["columns"]}
    assert "AnnualSalary" in columns
    assert any(col.get("description") for col in schema["columns"])


async def test_get_table_schema_scope_filter_narrow(mint: Mint) -> None:
    jwt, session_id = await _bound(mint, _EMPLOYEE_CODE_SCOPE)
    schema = await _client().call_tool(
        "getTableSchema",
        {"database": _EMPLOYEE_DB, "table": _EMPLOYEE_TABLE},
        jwt=jwt,
        session_id=session_id,
    )
    names = {col["name"] for col in schema["columns"]}
    assert "AnnualSalary" not in names
    assert "EmployeeCode" in names


# ---------------------------------------------------------------------------
# D83 sampleRows — reject outright on any out-of-scope column, allow-all works
# ---------------------------------------------------------------------------


async def test_sample_rows_rejects_when_table_has_out_of_scope_columns(mint: Mint) -> None:
    jwt, session_id = await _bound(mint, _EMPLOYEE_CODE_SCOPE)
    with pytest.raises(MCPToolError) as exc_info:
        await _client().call_tool(
            "sampleRows",
            {"database": _EMPLOYEE_DB, "table": _EMPLOYEE_TABLE, "limit": 5},
            jwt=jwt,
            session_id=session_id,
        )
    assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"


async def test_sample_rows_allow_all_returns_rows(mint: Mint) -> None:
    jwt, session_id = await _bound(mint)  # allow-all
    result = await _client().call_tool(
        "sampleRows",
        {"database": _EMPLOYEE_DB, "table": _EMPLOYEE_TABLE, "limit": 5},
        jwt=jwt,
        session_id=session_id,
    )
    assert result["row_count"] == 5
    assert len(result["rows"]) == 5


# ---------------------------------------------------------------------------
# X-Session-Id ↔ sid_hash binding — the hijack invariant (auth-hardening Slice 1)
#
# THE POINT: a caller holding userA's valid JWT must NOT be able to present
# userB's session_id in the X-Session-Id header to read userB's session/scratch
# data (D64). The live MCP rejects the mismatch at the middleware layer (HTTP
# 403 SESSION_BINDING_MISMATCH) — BEFORE any tool or the scratch extractor runs.
#
# The rejection is an HTTP-transport failure (it precedes the MCP protocol), so
# these use raw httpx to assert the exact status + error code, rather than the
# RealMCPClient (whose MCPToolError only surfaces in-tool errors).
# ---------------------------------------------------------------------------


async def test_hijack_cross_session_rejected_before_tool(mint: Mint) -> None:
    """userA JWT (bound to session A) + session B in X-Session-Id → 403, no tool runs."""
    import httpx

    sid_a = _session_id()
    sid_b = _session_id()
    jwt_a = await mint(session_id=sid_a)  # bound to A only

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            os.environ["MCP_TEST_URL"],
            headers={
                "Authorization": f"Bearer {jwt_a}",
                "X-Session-Id": sid_b,  # the hijack: another user's session
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "runQuery",
                    "arguments": {"sql": f"SELECT EmployeeCode FROM {_EMPLOYEE_FQ}"},
                },
            },
        )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"


async def test_own_session_passes_binding_and_reaches_tool(mint: Mint) -> None:
    """userA JWT + userA session_id → binding passes; the request reaches the tool
    and executes (positive control for the hijack test above)."""
    jwt, session_id = await _bound(mint)  # allow-all, bound to its own session
    result = await _client().call_tool(
        "runQuery",
        {"sql": f"SELECT EmployeeCode FROM {_EMPLOYEE_FQ}"},
        jwt=jwt,
        session_id=session_id,
    )
    assert result["row_count"] == 5


async def test_unbound_token_with_session_header_rejected(mint: Mint) -> None:
    """A token carrying NO sid_hash claim + any X-Session-Id → 403 (fail-closed)."""
    import httpx

    jwt_unbound = await mint()  # no session_id → no sid_hash claim

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            os.environ["MCP_TEST_URL"],
            headers={
                "Authorization": f"Bearer {jwt_unbound}",
                "X-Session-Id": _session_id(),
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "runQuery",
                    "arguments": {"sql": f"SELECT EmployeeCode FROM {_EMPLOYEE_FQ}"},
                },
            },
        )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"


# ---------------------------------------------------------------------------
# Omit-the-header scratch bypass (reviewer S1 / QA TestOmitHeaderBypass) — live
#
# The binding check only fires when X-Session-Id is PRESENT. A caller could try
# to defeat scratch isolation by DROPPING the header entirely (session context
# becomes None) and reading `scratch.s_<victimSession>_*` directly. The D64
# extractor now FAILS CLOSED on any scratch reference when the session is None
# (provenance.py::_validate_scratch_name), so the victim's scratch is never read.
# ---------------------------------------------------------------------------


def _sse_tool_result(resp) -> dict:
    """Extract the JSON-RPC result object from an MCP streamable-HTTP SSE body."""
    import json

    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())["result"]
    raise AssertionError(f"no SSE data line in response: {resp.text!r}")


async def test_omit_header_cannot_read_cross_session_scratch(mint: Mint) -> None:
    """Attacker holds a valid (allow-all) token, OMITS X-Session-Id, and targets a
    victim session's scratch table → rejected fail-closed; PII never returned."""
    import httpx

    jwt = await mint(session_id="sess-attacker-A")  # a valid, session-bound token
    victim_scratch = "scratch.s_sess_victim_B_payroll"

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            os.environ["MCP_TEST_URL"],
            headers={
                "Authorization": f"Bearer {jwt}",
                # NO X-Session-Id header — the omit-the-header bypass.
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "runQuery",
                    "arguments": {"sql": f"SELECT secret_col FROM {victim_scratch}"},
                },
            },
        )

    # The request is admitted by the middleware (no header → binding not fired),
    # but the D64 extractor fails closed on the scratch reference → tool error.
    assert resp.status_code == 200, resp.text
    tool_result = _sse_tool_result(resp)
    assert tool_result["isError"] is True
    error_text = tool_result["content"][0]["text"]
    assert "SCRATCH_SESSION_VIOLATION" in error_text, error_text
