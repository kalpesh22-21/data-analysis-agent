"""Bound MCP response streams before HTTPX or the SDK accumulates/parses them."""

from __future__ import annotations

import asyncio

import httpx

from .client import MCPToolError

DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ResponseGuard:
    """One SDK operation; retain failures even when SDK tasks wrap/swallow errors."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self.error_code: str | None = None
        self.failed = asyncio.Event()

    def fail(self, code: str) -> None:
        self.error_code = self.error_code or code
        self.failed.set()
        raise httpx.ReadError("MCP response rejected by receive limit")

    async def watch(self) -> None:
        await self.failed.wait()
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self.error_code:
            raise MCPToolError(self.error_code, "MCP response could not be accepted safely.")

    def client_factory(self, headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={**(headers or {}), "Accept-Encoding": "identity"},
            timeout=timeout or httpx.Timeout(30, read=300),
            auth=auth,
            follow_redirects=True,
            event_hooks={"response": [self.check_response]},
        )

    async def check_response(self, response: httpx.Response) -> None:
        # Do not let compressed content expand inside HTTPX before the byte check.
        # We request identity; a server ignoring that contract is rejected unopened.
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"", "identity"}:
            await response.aclose()
            self.fail("MCP_RESPONSE_UNSUPPORTED_ENCODING")
        try:
            length = int(response.headers.get("content-length", ""))
        except ValueError:
            length = 0
        if length > self.max_bytes:
            await response.aclose()
            self.fail("RESULT_TOO_LARGE")
        response.stream = _BoundedStream(response.stream, self)


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, guard: ResponseGuard) -> None:
        self.inner, self.guard = inner, guard

    async def __aiter__(self):
        received = 0
        async for chunk in self.inner:
            received += len(chunk)
            if received > self.guard.max_bytes:
                await self.inner.aclose()
                self.guard.fail("RESULT_TOO_LARGE")
            yield chunk

    async def aclose(self) -> None:
        await self.inner.aclose()
