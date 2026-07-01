"""Layer-2 integration fixtures — a real JWT-minting helper against the live
token IdP (`tests/integration/test_mcp_scope_live.py` and friends).

The whole `tests/integration` package targets a LIVE stack (ClickHouse +
token IdP + the D83-enabled clickhouse-api MCP) that must already be running;
nothing here starts/stops infrastructure. Individual test modules skip-guard
themselves on `MCP_TEST_URL` (mirroring `tests/runtime/mcp/test_real_client.py`)
so `uv run pytest` with no live stack configured stays fully green.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

import httpx
import pytest

# Endpoint/credentials for the token IdP that mints scoped JWTs for the live
# MCP (`POST /token`, `Authorization: Bearer <issuer key>`,
# `{"user_name": ..., "column_scope": [...]}`, `[]` == allow-all per D80(b)).
TOKEN_SERVICE_URL = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")
TOKEN_ISSUER_API_KEY = os.environ.get("TOKEN_ISSUER_API_KEY", "issuer-key-abc123")

Mint = Callable[[list[str] | None], Awaitable[str]]


@pytest.fixture
def mint() -> Mint:
    """Return an async `mint(column_scope) -> jwt` helper.

    `column_scope=None` or `column_scope=[]` mints an allow-all token (D80(b):
    `[]` == allow-all, matching `RuntimeCredentials.column_scope` and
    clickhouse-api's `Principal.column_scope` exactly). A non-empty list mints
    a token scoped to exactly those fully-qualified `database.table.column`
    strings.
    """

    async def _mint(column_scope: list[str] | None = None) -> str:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                TOKEN_SERVICE_URL,
                headers={"Authorization": f"Bearer {TOKEN_ISSUER_API_KEY}"},
                json={"user_name": "alice", "column_scope": column_scope or []},
            )
            response.raise_for_status()
            return response.json()["access_token"]

    return _mint
