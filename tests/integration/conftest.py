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

from data_agent.learning.promotion.token_minter import TenantClaims
from data_agent.runtime.config import RuntimeSettings

# Endpoint/credentials for the token IdP that mints scoped JWTs for the live
# MCP (`POST /token`, `Authorization: Bearer <issuer key>`,
# `{"user_name": ..., "column_scope": [...]}`, `[]` == allow-all per D80(b)).
TOKEN_SERVICE_URL = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")
TOKEN_ISSUER_API_KEY = os.environ.get("TOKEN_ISSUER_API_KEY", "issuer-key-abc123")

# The warehouse TENANT claims every minted token must carry. The MCP maps
# clientcode/proc_center/jti onto the paycom_* ClickHouse settings its row policies
# read (`app/config.py::CLICKHOUSE_TENANT_SETTINGS`); a token WITHOUT them is rejected
# `403 MISSING_TENANT_CLAIM` at auth, before any tool runs — so every live test here
# was failing at the transport, not exercising the thing it names. Resolved through
# `RuntimeSettings` so this reads the SAME TENANT_* env vars `ui/server.py` and the
# learning scheduler read; the defaults are the seeded local tenant
# (docker/clickhouse-init/hr-4tables-snake-migration.sql).
_TENANT_SETTINGS = RuntimeSettings(_env_file=None)
TENANT = TenantClaims(
    clientcode=_TENANT_SETTINGS.tenant_client_code,
    proc_center=_TENANT_SETTINGS.tenant_proc_center,
    jti=_TENANT_SETTINGS.tenant_jti,
)

Mint = Callable[..., Awaitable[str]]


@pytest.fixture
def mint() -> Mint:
    """Return an async `mint(column_scope=None, session_id=None) -> jwt` helper.

    `column_scope=None` or `column_scope=[]` mints an allow-all token (D80(b):
    `[]` == allow-all, matching `RuntimeCredentials.column_scope` and
    clickhouse-api's `Principal.column_scope` exactly). A non-empty list mints
    a token scoped to exactly those fully-qualified `database.table.column`
    strings.

    `session_id`, when supplied, is threaded into the mint request so the token
    carries a `sid_hash` claim (auth-hardening Slice 1). The live MCP enforces
    that any `X-Session-Id` header matches this claim, so a session-bound token
    MUST be sent with the matching `session_id` (see `test_mcp_scope_live.py`).
    Omitting it mints an unbound token (rejected by the live MCP the moment an
    `X-Session-Id` header is present — the default `require_sid_binding=true`).
    """

    async def _mint(
        column_scope: list[str] | None = None,
        session_id: str | None = None,
    ) -> str:
        body: dict = {
            "user_name": "alice",
            "column_scope": column_scope or [],
            # See `TENANT` above — without these the MCP 403s at auth.
            "claims": TENANT.as_claims(),
        }
        if session_id is not None:
            body["session_id"] = session_id
        async with httpx.AsyncClient() as client:
            response = await client.post(
                TOKEN_SERVICE_URL,
                headers={"Authorization": f"Bearer {TOKEN_ISSUER_API_KEY}"},
                json=body,
            )
            response.raise_for_status()
            return response.json()["access_token"]

    return _mint
