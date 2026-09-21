"""Receive limits run before MCP JSON/SSE parsing, including SDK task failures."""

import asyncio
import json

import httpx
import pytest

from data_agent.runtime.dispatch.denial_mapping import DenialKind, classify_denial
from data_agent.runtime.mcp.bounded_http import ResponseGuard
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.real_client import RealMCPClient


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            self.reads += 1
            yield part

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "headers,parts,reads,code",
    [
        ({"content-length": "11"}, [b"x" * 11], 0, "RESULT_TOO_LARGE"),
        ({}, [b"12345", b"67890", b"x", b"never read"], 3, "RESULT_TOO_LARGE"),
        ({"content-length": "2"}, [b"x" * 11], 1, "RESULT_TOO_LARGE"),
        ({"content-encoding": "gzip"}, [b"compressed"], 0, "MCP_RESPONSE_UNSUPPORTED_ENCODING"),
    ],
)
async def test_rejects_before_buffering_or_decoding(headers, parts, reads, code):
    stream = Chunks(parts)
    guard = ResponseGuard(10)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers=headers, stream=stream)
    )
    async with httpx.AsyncClient(
        transport=transport, event_hooks={"response": [guard.check_response]}
    ) as client:
        with pytest.raises(httpx.ReadError):
            await client.get("http://fixture/mcp")
    assert stream.reads == reads
    assert stream.closed
    with pytest.raises(MCPToolError) as caught:
        guard.raise_if_failed()
    assert caught.value.code == code


async def test_exact_limit_is_accepted_and_resets_per_response():
    guard = ResponseGuard(10)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Chunks([b"12345", b"67890"]))
    )
    async with httpx.AsyncClient(
        transport=transport, event_hooks={"response": [guard.check_response]}
    ) as client:
        for _ in range(2):
            assert (await client.get("http://fixture/mcp")).content == b"1234567890"
    guard.raise_if_failed()


@pytest.mark.parametrize("method", ["call_tool", "list_tools"])
@pytest.mark.parametrize("wire_format", ["json", "sse"])
async def test_sdk_failures_keep_size_code(monkeypatch, method, wire_format):
    body = Chunks([b"x" * 200, b"x" * 200, b"not consumed"])

    def handle(request):
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["authorization"] == "Bearer fixture"
        if request.method != "POST":
            return httpx.Response(405)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    },
                },
            )
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json" if wire_format == "json" else "text/event-stream"
            },
            stream=body,
        )

    monkeypatch.setattr(
        "httpx._client.AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    client = RealMCPClient("http://fixture/mcp", max_response_bytes=256)
    with pytest.raises(MCPToolError) as caught:
        if method == "call_tool":
            await asyncio.wait_for(
                client.call_tool("runQuery", {}, jwt="fixture", session_id="fixture"), 2
            )
        else:
            await asyncio.wait_for(client.list_tools(jwt="fixture", session_id="fixture"), 2)
    assert caught.value.code == "RESULT_TOO_LARGE"
    assert body.reads == 2
    assert body.closed


def test_size_denial_is_a_repairable_limit_not_judged_work():
    denial = classify_denial("RESULT_TOO_LARGE")
    assert denial.kind == DenialKind.GATE
    assert denial.retryable


async def test_normal_sdk_result_still_returns_without_waiting_for_watcher(monkeypatch):
    payload = {"columns": ["headcount"], "rows": [[3]], "row_count": 1, "truncated": False}

    def handle(request):
        if request.method != "POST":
            return httpx.Response(405)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif message["method"] == "tools/list":
            result = {"tools": [{"name": "runQuery", "inputSchema": {"type": "object"}}]}
        else:
            result = {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload,
                "isError": False,
            }
        body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=Chunks([body[:20], body[20:]])
        )

    monkeypatch.setattr(
        "httpx._client.AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    client = RealMCPClient("http://fixture/mcp", max_response_bytes=1024)
    result = await asyncio.wait_for(
        client.call_tool("runQuery", {}, jwt="fixture", session_id="fixture"), 2
    )
    assert result == payload
