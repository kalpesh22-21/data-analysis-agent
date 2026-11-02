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
        yield SimpleNamespace(call_tool=call, list_tools=call)

    monkeypatch.setattr(client, "_session", session)
    assert await client.call_tool("runQuery", {}, jwt="secret", session_id="s") == {"rows": []}
    assert len(calls) == 2


@pytest.mark.parametrize("operation", ["runQuery", "list_tools"])
@pytest.mark.parametrize("always_stale", [False, True])
async def test_stale_scope_refreshes_once_with_new_session(monkeypatch, operation, always_stale):
    from unittest.mock import AsyncMock

    from data_agent.runtime.mcp.client import MCPToolError

    client = RealMCPClient("http://test/mcp")
    client.scope_token_provider = AsyncMock(return_value="old")
    client.scope_token_refresher = AsyncMock(return_value="new")
    headers_seen = []

    async def call(*_):
        return SimpleNamespace(isError=False, structuredContent={"rows": []}, tools=[])

    @asynccontextmanager
    async def session(headers):
        headers_seen.append(dict(headers))
        if always_stale or len(headers_seen) == 1:
            raise MCPToolError("SESSION_SCOPE_STALE", "safe")
        yield SimpleNamespace(call_tool=call, list_tools=call)

    monkeypatch.setattr(client, "_session", session)

    async def invoke():
        if operation == "list_tools":
            return await client.list_tools(jwt="jwt", session_id="s")
        return await client.call_tool(operation, {}, jwt="jwt", session_id="s")

    if always_stale:
        with pytest.raises(MCPToolError):
            await invoke()
    else:
        await invoke()
    assert [h["X-Catalog-Scope"] for h in headers_seen] == ["old", "new"]
    client.scope_token_refresher.assert_awaited_once_with(
        jwt="jwt", session_id="s", rejected_token="old"
    )


@pytest.mark.parametrize(
    "code",
    [
        "SESSION_SCOPE_INVALID",
        "SESSION_SCOPE_EXPIRED",
        "COLUMN_SCOPE_VIOLATION",
        "SESSION_BINDING_MISMATCH",
    ],
)
async def test_other_denials_never_refresh(monkeypatch, code):
    from unittest.mock import AsyncMock

    from data_agent.runtime.mcp.client import MCPToolError

    client = RealMCPClient("http://test/mcp")
    client.scope_token_refresher = AsyncMock()

    @asynccontextmanager
    async def session(headers):
        raise MCPToolError(code, "safe")
        yield

    monkeypatch.setattr(client, "_session", session)
    with pytest.raises(MCPToolError):
        await client.call_tool("runQuery", {}, jwt="jwt", session_id="s")
    client.scope_token_refresher.assert_not_awaited()


@pytest.mark.parametrize(
    "status,body,stale",
    [
        (403, b'{"code":"SESSION_SCOPE_STALE"}', True),
        (404, b'{"code":"SESSION_SCOPE_STALE"}', False),
        (403, b'{"code":"SESSION_SCOPE_INVALID"}', False),
        (403, b"<html>SESSION_SCOPE_STALE</html>", False),
        (403, b"[]", False),
        (403, b" " * 9000, False),
    ],
)
async def test_scope_error_decoding_is_bounded_and_exact(status, body, stale):
    from data_agent.runtime.mcp.bounded_http import ResponseGuard
    from data_agent.runtime.mcp.client import MCPToolError

    guard = ResponseGuard(16000)
    response = httpx.Response(
        status,
        content=body,
        request=httpx.Request(
            "POST", "http://test/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
        ),
    )
    if stale:
        with pytest.raises(MCPToolError) as exc:
            await guard.check_response(response)
        assert exc.value.code == "SESSION_SCOPE_STALE"
    else:
        await guard.check_response(response)
        assert guard.error_code is None


@pytest.mark.parametrize(
    "method,payload",
    [
        ("DELETE", {"id": 1}),
        ("GET", {"id": 1}),
        ("POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ],
)
async def test_non_request_scope_errors_never_trigger_recovery(method, payload):
    from data_agent.runtime.mcp.bounded_http import ResponseGuard

    guard = ResponseGuard(16000)
    response = httpx.Response(
        403,
        json={"code": "SESSION_SCOPE_STALE"},
        request=httpx.Request(method, "http://test/mcp", json=payload),
    )
    await guard.check_response(response)
    assert guard.error_code is None
