"""Tests for mcp/real_client.py.

`_parse_tool_error_text` is pure (no I/O) and is tested at Layer 1. Everything
else in RealMCPClient requires a live `clickhouse-api` MCP container and is
guarded/skipped unless MCP_TEST_URL is set (design §8).
"""

from __future__ import annotations

import os

import httpx
import pytest

from data_agent.runtime.mcp.real_client import RealMCPClient, _parse_tool_error_text

_TOKEN_SERVICE_URL = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")
_TOKEN_ISSUER_API_KEY = os.environ.get("TOKEN_ISSUER_API_KEY", "issuer-key-abc123")


async def _mint(column_scope: list[str] | None = None) -> str:
    """Mint a JWT from the live token IdP (allow-all when *column_scope* is None/[])."""
    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            _TOKEN_SERVICE_URL,
            headers={"Authorization": f"Bearer {_TOKEN_ISSUER_API_KEY}"},
            json={"user_name": "alice", "column_scope": column_scope or []},
        )
        response.raise_for_status()
        return response.json()["access_token"]

# ---------------------------------------------------------------------------
# Layer 1 — pure parsing logic, no infra
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_text", "expected_code", "expected_message"),
    [
        (
            "[COLUMN_SCOPE_VIOLATION] This query references columns outside your permitted scope.",
            "COLUMN_SCOPE_VIOLATION",
            "This query references columns outside your permitted scope.",
        ),
        (
            "[PARSE_FAILED_CLOSED] The query could not be parsed for column-scope verification.",
            "PARSE_FAILED_CLOSED",
            "The query could not be parsed for column-scope verification.",
        ),
        ("An internal error occurred.", None, "An internal error occurred."),
        ("", None, ""),
        (
            # Real captured text from the live MCP: FastMCP prepends
            # "Error executing tool <name>: " ahead of the domain ToolError's
            # own "[{CODE}] message" — the code must still be found (FIX 2).
            "Error executing tool runQuery: [COLUMN_SCOPE_VIOLATION] This query "
            "references columns outside your permitted scope.",
            "COLUMN_SCOPE_VIOLATION",
            "This query references columns outside your permitted scope.",
        ),
    ],
)
def test_parse_tool_error_text(raw_text: str, expected_code: str | None, expected_message: str) -> None:
    code, message = _parse_tool_error_text(raw_text)
    assert code == expected_code
    assert message == expected_message


# ---------------------------------------------------------------------------
# Layer 2 — real clickhouse-api MCP container required
# ---------------------------------------------------------------------------

pytestmark_live = pytest.mark.skipif(
    not os.environ.get("MCP_TEST_URL"),
    reason="Requires a live clickhouse-api MCP container (set MCP_TEST_URL).",
)


@pytestmark_live
async def test_list_tools_against_live_mcp() -> None:
    client = RealMCPClient(os.environ["MCP_TEST_URL"])
    jwt = await _mint()
    tools = await client.list_tools(jwt=jwt, session_id="sess-real-client-test")
    names = {t.name for t in tools}
    assert {"listDatabases", "listTables", "getTableSchema", "sampleRows", "runQuery", "explainQuery"} <= names
