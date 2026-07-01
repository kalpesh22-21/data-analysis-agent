"""Adversarial graceful-denial coverage: all 7 MCP error codes through the
FULL `ToolDispatcher.dispatch(...)` (not just `classify_denial` in isolation),
plus a probe for a real transport-exception gap (QA hardening pass).

`tests/runtime/dispatch/test_denial_mapping.py` already table-drives
`classify_denial` directly over all 7 codes. This file re-drives the SAME 7
codes through the actual dispatch path (`FakeMCPClient` raising `MCPToolError`
-> `ToolDispatcher.dispatch`) and adds the harsher checks the QA brief calls
for: correct `ToolResult(status=...)`, correct retryable classification,
and a `user_message` that leaks NEITHER the raw internal MCP error code
string NOR any attacker/backend-controlled detail embedded in the raw
`MCPToolError.message` (simulated here with deliberately PII/secret-shaped
text, since a real ClickHouse/JWKS error could plausibly contain connection
strings, credentials, or user data in its message).
"""

from __future__ import annotations

import json

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.denial_mapping import _DENIAL_TABLE
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

CATALOG = CatalogHandle({"dbpcm_warehouse.employee": {"EmployeeCode": "String"}})
SESSION_ID = "sess-denial-adversarial"
JWT = "jwt-secret-should-never-leak"

_ALL_SEVEN_CODES = (
    "COLUMN_SCOPE_VIOLATION",
    "SCRATCH_SESSION_VIOLATION",
    "PARSE_FAILED_CLOSED",
    "DATABASE_NOT_ALLOWED",
    "TABLE_NOT_FOUND",
    "CLICKHOUSE_QUERY_ERROR",
    "CLICKHOUSE_UNAVAILABLE",
)

# A deliberately dangerous-looking raw MCP error message — simulates a real
# backend leaking connection/credential-shaped detail in the ToolError text
# (a realistic adversarial input: the MCP is an external/adopted dependency,
# D75, and its error text is not under this runtime's control).
_DANGEROUS_RAW_MESSAGE = (
    "internal failure: clickhouse://admin:s3cr3t-pw@10.0.0.5:9440/default "
    "user 'jane.doe@example.com' query trace id=abcdef123456"
)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset())


@pytest.mark.parametrize("code", _ALL_SEVEN_CODES)
async def test_all_seven_codes_produce_correct_tool_result_via_real_dispatch(code: str) -> None:
    mcp = FakeMCPClient(
        scripted={"runQuery": [MCPToolError(code, f"[{code}] {_DANGEROUS_RAW_MESSAGE}")]}
    )
    dispatcher = ToolDispatcher(mcp, CATALOG)

    result = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())

    expected = _DENIAL_TABLE[code]
    assert result.status == "denied"
    assert result.error_code == code
    assert result.retryable is expected.retryable
    assert result.user_message == expected.user_message
    # Never gated by capture_provenance/preview on the denial path.
    assert result.provenance is None
    assert result.result_preview is None
    assert result.result_full is None


@pytest.mark.parametrize("code", _ALL_SEVEN_CODES)
async def test_user_message_never_leaks_raw_backend_detail(code: str) -> None:
    """Even when the raw `MCPToolError.message` carries connection strings,
    credentials, or a user's PII (as a real backend error plausibly could),
    the `ToolResult.user_message` the model/UI ultimately sees must be the
    clean, canned, code-keyed string — never a substring of the raw detail."""
    mcp = FakeMCPClient(scripted={"runQuery": [MCPToolError(code, _DANGEROUS_RAW_MESSAGE)]})
    dispatcher = ToolDispatcher(mcp, CATALOG)

    result = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())

    blob = json.dumps(
        {
            "user_message": result.user_message,
            "error_code": result.error_code,
            "retryable": result.retryable,
        },
        default=str,
    )
    assert "s3cr3t-pw" not in blob
    assert "10.0.0.5" not in blob
    assert "jane.doe@example.com" not in blob
    assert "admin" not in blob
    assert code not in result.user_message  # raw code string never in the user-facing text


async def test_unparseable_mcp_error_code_is_classified_conservatively_via_real_dispatch() -> None:
    """`MCPToolError(code=None, ...)` — an unexpected/internal MCP error whose
    `[{CODE}]` prefix could not be parsed — must still be dispatched
    gracefully (not propagate as an unhandled exception), classified as
    non-retryable, with a generic user message."""
    mcp = FakeMCPClient(
        scripted={"runQuery": [MCPToolError(None, _DANGEROUS_RAW_MESSAGE)]}
    )
    dispatcher = ToolDispatcher(mcp, CATALOG)

    result = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())
    assert result.status == "denied"
    assert result.error_code == "UNKNOWN_ERROR"
    assert result.retryable is False
    assert "s3cr3t-pw" not in result.user_message


# ---------------------------------------------------------------------------
# B4 fix — a raw (non-MCPToolError) transport exception during
# `MCPClient.call_tool` IS now gracefully handled (was a strict-xfail defect
# probe during the QA hardening pass; flipped to a normal passing assertion
# once tool_dispatcher.py grew a broad `except Exception` fallback).
# ---------------------------------------------------------------------------


async def test_raw_transport_exception_is_gracefully_denied_not_propagated() -> None:
    """`ToolResult.status` is typed as `Literal["ok", "denied", "error"]`
    (dispatch/tool_dispatcher.py), and design §3.4/06 "Graceful denial"
    requires every tool-call failure to surface as a clean `ToolResult`
    rather than crash the turn. `RealMCPClient.call_tool` can itself raise a
    RAW (non-`MCPToolError`) exception for a genuine transport/connectivity
    failure — e.g. `httpx.ConnectError`, `asyncio.TimeoutError`, or a
    `json.JSONDecodeError` from a malformed MCP response — none of which
    carry the `[{CODE}]` prefix `MCPToolError` expects, because they never
    reach the point where the MCP itself formats a `ToolError`.

    `ToolDispatcher.dispatch` now catches ANY such exception via a broad
    `except Exception` fallback (below the specific `MCPToolError` handler),
    degrading to `ToolResult(status="error", error_code=
    "INTERNAL_TRANSPORT_ERROR", ...)` with a generic, PII-safe `user_message`
    — never the raw exception text — instead of propagating and crashing the
    turn (which used to also leak `str(exc)` verbatim over SSE via `app.py`'s
    last-resort handler).
    """
    mcp = FakeMCPClient(scripted={"runQuery": [ConnectionError("connection reset by peer")]})
    dispatcher = ToolDispatcher(mcp, CATALOG)

    result = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())
    assert result.status == "error"
    assert result.error_code == "INTERNAL_TRANSPORT_ERROR"
    assert result.retryable is False
    assert result.user_message
    assert "connection reset by peer" not in (result.user_message or "")
    # Never gated by capture_provenance/preview on the error path either.
    assert result.provenance is None
    assert result.result_preview is None
    assert result.result_full is None
