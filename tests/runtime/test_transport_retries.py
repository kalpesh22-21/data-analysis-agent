from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from data_agent.runtime.mcp.real_client import RealMCPClient


@pytest.mark.parametrize(
    "status,tool,expected",
    [(503, "runQuery", 2), (500, "getTableSchema", 2), (403, "runQuery", 1), (502, "writeData", 1)],
)
async def test_bounded_read_transport_retries(status, tool, expected, monkeypatch):
    client = RealMCPClient("http://test/mcp")
    calls = []

    @asynccontextmanager
    async def session(_):
        calls.append(1)
        raise httpx.HTTPStatusError(
            "private error",
            request=httpx.Request("POST", "http://test"),
            response=httpx.Response(status),
        )
        yield

    monkeypatch.setattr(client, "_session", session)
    with pytest.raises(httpx.HTTPStatusError):
        await client.call_tool(tool, {}, jwt="secret", session_id="s")
    assert len(calls) == expected


async def test_retry_recovers_grouped_sdk_transport_failure(monkeypatch):
    client = RealMCPClient("http://test/mcp")
    calls = []

    async def call(*_):
        return SimpleNamespace(isError=False, structuredContent={"rows": []})

    @asynccontextmanager
    async def session(_):
        calls.append(1)
        if len(calls) == 1:
            raise ExceptionGroup("sdk", [httpx.ConnectError("private")])
        yield SimpleNamespace(call_tool=call)

    monkeypatch.setattr(client, "_session", session)
    assert await client.call_tool("runQuery", {}, jwt="secret", session_id="s") == {"rows": []}
    assert len(calls) == 2
