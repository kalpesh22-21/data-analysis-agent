"""Transport recovery is internal, bounded and shares the logical call deadline."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import httpx
import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.denial_mapping import classify_denial
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp import real_client
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

CREDS = RuntimeCredentials(session_id="retry-test", jwt="private-token", column_scope=frozenset())
RESULT = {"database": "warehouse", "table": "test", "columns": []}


def scripted_client(monkeypatch, outcomes):
    client = RealMCPClient("http://unused.invalid/mcp")
    calls = []
    sessions = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[])

        async def call_tool(self, name, args):
            calls.append((name, args))
            value = outcomes.pop(0)
            if isinstance(value, BaseException):
                raise value
            return SimpleNamespace(isError=False, structuredContent=value)

    @asynccontextmanager
    async def session(headers):
        sessions.append(dict(headers))
        yield Session()

    monkeypatch.setattr(client, "_session", session)
    return client, calls, sessions


@pytest.mark.parametrize("error", [
    httpx.ConnectError("private transport detail"),
    httpx.WriteError("lost write"),
    ConnectionResetError("reset"),
    TimeoutError("socket timeout"),
    anyio.EndOfStream(),
    anyio.BrokenResourceError(),
    ExceptionGroup("sdk", [anyio.EndOfStream(), httpx.ReadError("lost")]),
])
async def test_read_recovers_inside_one_dispatch_with_backoff(monkeypatch, error):
    client, calls, sessions = scripted_client(monkeypatch, [error, RESULT])
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(real_client.asyncio, "sleep", sleep)
    events = []
    dispatcher = ToolDispatcher(client, CatalogHandle({}), observer=lambda e, p: events.append(e))
    result = await dispatcher.dispatch("getTableSchema", {"table": "test"}, CREDS)
    assert result.status == "ok"
    assert calls == [("getTableSchema", {"table": "test"})] * 2
    assert len(sessions) == 2 and sessions[0] == sessions[1]
    assert delays == [0.2]
    assert events.count("tool_dispatch_start") == 1
    assert events.count("tool_dispatch_ok") == 1
    assert "tool_dispatch_error" not in events


async def test_exhaustion_is_not_retried_by_dispatcher_or_offered_to_model(monkeypatch):
    client, calls, _ = scripted_client(monkeypatch, [ConnectionError("private"), ConnectionError("private"), RESULT])
    result = await ToolDispatcher(client, CatalogHandle({})).dispatch("getTableSchema", {}, CREDS)
    assert len(calls) == 2
    assert result.error_code == "INTERNAL_TRANSPORT_ERROR"
    assert result.retryable is False
    assert result.result_full is None
    assert "retry" not in result.user_message.lower()
    assert "private" not in result.user_message
    replay = classify_denial(result.error_code)
    assert replay.retryable is False and replay.user_message == result.user_message


@pytest.mark.parametrize("error", [
    MCPToolError("COLUMN_SCOPE_VIOLATION", "denied"),
    MCPToolError("SQL_EXECUTION_ERROR", "bad SQL"),
    ValueError("malformed response"),
    ExceptionGroup("mixed", [ConnectionError(), ValueError()]),
    httpx.HTTPStatusError("denied", request=httpx.Request("GET", "http://unused.invalid"), response=httpx.Response(403)),
])
async def test_non_transport_failures_are_not_retried(monkeypatch, error):
    client, calls, _ = scripted_client(monkeypatch, [error, RESULT])
    with pytest.raises(type(error)):
        await client.call_tool("getTableSchema", {}, jwt=CREDS.jwt, session_id=CREDS.session_id)
    assert len(calls) == 1


async def test_write_tool_is_not_replayed_after_uncertain_execution(monkeypatch):
    client, calls, _ = scripted_client(monkeypatch, [ConnectionError(), RESULT])
    with pytest.raises(ConnectionError):
        await client.call_tool("createTable", {}, jwt=CREDS.jwt, session_id=CREDS.session_id)
    assert len(calls) == 1


async def test_backoff_is_cancelled_by_original_dispatch_deadline(monkeypatch):
    client, calls, _ = scripted_client(monkeypatch, [ConnectionError(), RESULT])
    result = await ToolDispatcher(client, CatalogHandle({}), tool_call_timeout_seconds=0.02).dispatch("getTableSchema", {}, CREDS)
    assert result.error_code == "TOOL_CALL_TIMEOUT"
    assert len(calls) == 1  # deadline expires during the 200ms backoff


async def test_caller_cancellation_does_not_start_another_attempt(monkeypatch):
    client, calls, _ = scripted_client(monkeypatch, [asyncio.CancelledError(), RESULT])
    with pytest.raises(asyncio.CancelledError):
        await client.call_tool("getTableSchema", {}, jwt=CREDS.jwt, session_id=CREDS.session_id)
    assert len(calls) == 1
