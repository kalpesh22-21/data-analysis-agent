"""ScratchClient — the runtime side of the D93 scratch-write side-channel.

The executor materializes an already-scope-checked table-output node result into a
session-partitioned scratch table (``scratch.s_<sid>_bp_<uuid>``) so a downstream node
can ``JOIN`` it. Not an MCP tool (D19): plain HTTP routes on the MCP host behind the
same ``JWTAuthMiddleware``, riding the same credential binding. The client holds NO
ClickHouse write credential of its own (invariant #8) — the privilege is the MCP's
server-side scratch-only grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ._transport import SideChannelError, error_from_response
from ._transport import side_channel_headers as _headers


class ScratchClientError(SideChannelError):
    """A scratch materialize/drop call was rejected or failed.

        Carries the endpoint's stable *code* (e.g. ``SCRATCH_TOO_LARGE``) when available.
        Its OWN class because the consequence is specific: this one fails the composite
        node back to the raw loop rather than degrading a cached artifact.
    """


class ScratchClientProtocol(Protocol):
    """The interface the executor depends on (real + fake share it)."""

    async def materialize(
        self,
        columns: list[dict[str, str]],
        rows: list[list[Any]],
        *,
        jwt: str,
        session_id: str,
    ) -> str: ...

    async def drop(self, table: str, *, jwt: str, session_id: str) -> None: ...


class ScratchClient:
    """Real ``ScratchClient`` over the live MCP scratch side-channel (Layer 2+)."""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        # base_url is the …/scratch/v1 base (no trailing slash).
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def materialize(
        self,
        columns: list[dict[str, str]],
        rows: list[list[Any]],
        *,
        jwt: str,
        session_id: str,
    ) -> str:
        """POST the columns+rows, return the RETURNED ``scratch.s_<sid>_bp_<uuid>``.

                The rows go over the wire as native JSON DATA — the endpoint bulk-inserts them
                via the driver, never string-interpolated into SQL (invariant #4). The returned
                table name is used VERBATIM (the runtime never reconstructs it): its
                ``s_<sid>_`` prefix is derived server-side from X-Session-Id.
        """
        payload = {"columns": columns, "rows": rows}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base_url}/materialize",
                json=payload,
                headers=_headers(jwt, session_id),
            )
        if resp.status_code >= 400:
            raise _error_from_response(resp)
        body = resp.json()
        table = body.get("table") if isinstance(body, dict) else None
        if not isinstance(table, str) or not table:
            raise ScratchClientError(None, "Scratch materialize returned no table name.")
        return table

    async def drop(self, table: str, *, jwt: str, session_id: str) -> None:
        """Best-effort drop (TTL is the real cleanup); errors are non-fatal and the caller
                may swallow them.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base_url}/drop",
                json={"table": table},
                headers=_headers(jwt, session_id),
            )
        if resp.status_code >= 400:
            raise _error_from_response(resp)


def _error_from_response(resp: httpx.Response) -> SideChannelError:
    return error_from_response(resp, error_class=ScratchClientError, description="scratch endpoint")


@dataclass
class RecordedScratchCall:
    op: str  # "materialize" | "drop"
    columns: list[dict[str, str]] | None
    rows: list[list[Any]] | None
    table: str | None
    jwt: str
    session_id: str


class FakeScratchClient:
    """Layer-1 ``ScratchClient`` double — records calls, returns deterministic names.

        ``materialize`` hands back a ``scratch.s_<session_id>_bp_<n>`` name matching the
        endpoint's naming contract, so the D64 read gate accepts the rewritten JOIN for
        this session. Every call is recorded so tests can assert the credential boundary.
    """

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[RecordedScratchCall] = []
        self._counter = 0
        self._fail = fail

    async def materialize(
        self,
        columns: list[dict[str, str]],
        rows: list[list[Any]],
        *,
        jwt: str,
        session_id: str,
    ) -> str:
        self._counter += 1
        table = f"scratch.s_{session_id}_bp_{self._counter:032x}"
        self.calls.append(
            RecordedScratchCall(
                op="materialize",
                columns=[dict(c) for c in columns],
                rows=[list(r) for r in rows],
                table=table,
                jwt=jwt,
                session_id=session_id,
            )
        )
        if self._fail is not None:
            raise self._fail
        return table

    async def drop(self, table: str, *, jwt: str, session_id: str) -> None:
        self.calls.append(
            RecordedScratchCall(
                op="drop",
                columns=None,
                rows=None,
                table=table,
                jwt=jwt,
                session_id=session_id,
            )
        )


__all__ = [
    "FakeScratchClient",
    "RecordedScratchCall",
    "ScratchClient",
    "ScratchClientError",
    "ScratchClientProtocol",
]
