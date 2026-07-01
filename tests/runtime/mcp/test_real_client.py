"""Tests for mcp/real_client.py.

`_parse_tool_error_text` is pure (no I/O) and is tested at Layer 1. Everything
else in RealMCPClient requires a live `clickhouse-api` MCP container and is
guarded/skipped unless MCP_TEST_URL is set (design §8).
"""

from __future__ import annotations

import os

import pytest

from data_agent.runtime.mcp.real_client import RealMCPClient, _parse_tool_error_text

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
    tools = await client.list_tools()
    names = {t.name for t in tools}
    assert {"listDatabases", "listTables", "getTableSchema", "sampleRows", "runQuery", "explainQuery"} <= names
