"""Unit tests for dispatch/tool_dispatcher.py (Layer 1 — FakeMCPClient, no infra).

Includes the D5 injection-integrity invariant: the JWT/session_id reach the
FakeMCPClient transport boundary but never appear in the ToolResult.
"""

from __future__ import annotations

import dataclasses
import json

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_E = "dbpcm_warehouse.employee"

CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

SECRET_JWT = "eyJhbGciOi.super-secret-jwt-body.sig"
SESSION_ID = "sess-integrity-test"


def _credentials(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=scope)


def _result_to_scannable_json(result: ToolResult) -> str:
    payload = dataclasses.asdict(result)
    # frozenset isn't JSON-serializable; render provenance as a sorted list first.
    if payload.get("provenance") is not None:
        payload["provenance"] = sorted(payload["provenance"])
    return json.dumps(payload, default=str)


async def test_ok_result_never_carries_credentials() -> None:
    mcp_client = FakeMCPClient(
        scripted={
            "getTableSchema": [
                {
                    "database": "dbpcm_warehouse",
                    "table": "employee",
                    "columns": [{"name": "EmployeeCode", "type": "String", "comment": ""}],
                }
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )

    # Transport boundary DID receive the credentials.
    assert len(mcp_client.calls) == 1
    assert mcp_client.calls[0].jwt == SECRET_JWT
    assert mcp_client.calls[0].session_id == SESSION_ID

    # ToolResult NEVER carries them.
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
    assert SESSION_ID not in blob
    assert result.status == "ok"


async def test_denied_result_never_carries_credentials() -> None:
    mcp_client = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "denied")]}
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT Amount FROM payroll"}, _credentials()
    )

    assert result.status == "denied"
    assert result.error_code == "COLUMN_SCOPE_VIOLATION"
    assert result.retryable is False
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
    assert SESSION_ID not in blob


async def test_dispatch_captures_provenance_on_success() -> None:
    raw_result = {
        "columns": ["EmployeeCode", "Department"],
        "rows": [["E1", "Sales"]],
        "row_count": 1,
        "truncated": False,
    }
    mcp_client = FakeMCPClient(scripted={"runQuery": [dict(raw_result)]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    result = await dispatcher.dispatch(
        "runQuery",
        {"sql": "SELECT EmployeeCode, Department FROM employee"},
        _credentials(),
    )
    assert result.status == "ok"
    assert result.provenance == frozenset({(_E, "EmployeeCode"), (_E, "Department")})
    assert result.result_preview is not None
    assert result.result_preview.row_count == 1
    assert result.result_full == raw_result


async def test_preview_truncates_to_preview_row_count() -> None:
    rows = [[f"E{i}"] for i in range(50)]
    mcp_client = FakeMCPClient(
        scripted={
            "sampleRows": [
                {"columns": ["EmployeeCode"], "rows": rows, "row_count": 50, "truncated": False}
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG, preview_row_count=5)
    result = await dispatcher.dispatch(
        "sampleRows", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )
    assert result.result_preview.truncated is True
    assert len(result.result_preview.preview_rows) == 5


async def test_observer_is_called_at_each_stage() -> None:
    events: list[tuple[str, dict]] = []

    def observer(event: str, payload: dict) -> None:
        events.append((event, payload))

    mcp_client = FakeMCPClient(scripted={"listDatabases": [[{"name": "dbpcm_warehouse"}]]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG, observer=observer)
    await dispatcher.dispatch("listDatabases", {}, _credentials())

    event_names = [name for name, _ in events]
    assert event_names == ["tool_dispatch_start", "tool_dispatch_ok"]


async def test_default_observer_is_noop_by_default() -> None:
    """The default observer must not raise or require configuration (Pass-A seam)."""
    mcp_client = FakeMCPClient(scripted={"listDatabases": [[{"name": "dbpcm_warehouse"}]]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    result = await dispatcher.dispatch("listDatabases", {}, _credentials())
    assert result.status == "ok"


async def test_column_scope_denial_surfaces_the_named_column() -> None:
    """For COLUMN_SCOPE_VIOLATION SPECIFICALLY, the denied ToolResult.user_message
    carries the MCP's author-controlled, column-naming detail (exc.message) so the
    model sees WHICH column it lacks and can self-correct on the live turn.
    Column names are catalog metadata (not PII / cell values, D25)."""
    scope_message = (
        "This query needs access to columns outside your permitted scope: "
        "employee.EmployeeStatus. You do not have access to those columns — "
        "remove them from the query, or ask the user to grant access."
    )
    mcp_client = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", scope_message)]}
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT EmployeeStatus FROM employee"}, _credentials()
    )

    assert result.status == "denied"
    assert result.error_code == "COLUMN_SCOPE_VIOLATION"
    assert result.retryable is False
    # The specific out-of-scope column NAME reaches the model.
    assert "employee.EmployeeStatus" in result.user_message
    assert result.user_message == scope_message
    # Still never leaks credentials.
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
    assert SESSION_ID not in blob


async def test_non_scope_denial_stays_generic_canned_message() -> None:
    """Regression guard for B4/D25: a NON-scope MCP error must NOT surface its raw
    exc.message — the model only ever sees the generic canned denial string. Here
    the raw message contains backend detail that must be suppressed."""
    raw_backend_detail = "Code: 47. DB::Exception: Unknown column secret_internal_col"
    mcp_client = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("CLICKHOUSE_QUERY_ERROR", raw_backend_detail)]}
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT bad FROM employee"}, _credentials()
    )

    assert result.status == "denied"
    assert result.error_code == "CLICKHOUSE_QUERY_ERROR"
    # Generic canned message only — the raw backend text must NOT leak.
    assert result.user_message == "That query didn't run correctly. Let me fix it and try again."
    assert "DB::Exception" not in result.user_message
    assert "secret_internal_col" not in result.user_message


async def test_raw_transport_exception_stays_generic_canned_message() -> None:
    """Regression guard for B4/D25: a RAW (non-MCPToolError) transport exception
    must never surface str(exc) — the model only sees the generic canned message."""
    raw_transport_detail = "ConnectionRefusedError: [Errno 61] to 10.0.0.5:8123"

    class _BoomClient(FakeMCPClient):
        async def call_tool(self, tool_name, args, *, jwt, session_id):
            raise RuntimeError(raw_transport_detail)

    dispatcher = ToolDispatcher(_BoomClient(), CATALOG)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT 1"}, _credentials()
    )

    assert result.status == "error"
    assert result.error_code == "INTERNAL_TRANSPORT_ERROR"
    assert result.user_message == (
        "Something went wrong reaching the data warehouse. Please try again."
    )
    assert "ConnectionRefusedError" not in result.user_message
    assert "10.0.0.5" not in result.user_message
