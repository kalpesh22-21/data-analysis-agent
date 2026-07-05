"""promotion/token_minter.py — the offline JWT minter for the S9 golden replay.

The real `WarehouseProbe` (`warehouse_probe.py`) runs the golden-replay grain probe
through the adopted MCP `runQuery` choke point, under a JWT minted PER BLUEPRINT and
scoped to EXACTLY that blueprint's declared `uses` footprint (S9-design §1.3). This
module is the thin transport that mints that token against the token IdP's
`POST /token` (`clickhouse-api/app/token_service.py`), mirroring `runtime/mcp/
scratch_client.py`'s httpx posture.

Load-bearing choices (S9-design §1.3, with the Slice-1 binding deviation noted below):
  * `column_scope = <blueprint.uses>` — the exact `["db.table.column", …]` grammar the
    MCP enforces (D57). The replay then reads ONLY within the declared footprint
    (D89 scope-honesty); the MCP's D57 teeth are the backstop if the mint is wrong.
  * **Session-BOUND (deviation from §1.3).** The design assumed a session-LESS mint
    (no `sid_hash`) so the probe could send an arbitrary `X-Session-Id`. The live MCP
    runs `require_sid_binding=true` — an unbound token + an `X-Session-Id` header is
    rejected 403 `SESSION_BINDING_MISMATCH` (see `test_mcp_scope_live.py::
    test_unbound_token_with_session_header_rejected`), and `RealMCPClient` always sends
    the header. So the probe mints a token BOUND to its OWN synthetic session id and
    sends that SAME id — the probe owns both (no hijack surface), scope stays `uses`.
  * A SHORT TTL bounds token exposure (the probe fires once and discards the token).
  * ANY mint failure RAISES (`TokenMintError`) — the probe lets it propagate so
    `golden_replay` degrades to a clean `probe_unavailable` HOLD (D98), never a leak.

Two implementations, one interface (the `FakeMCPClient` pattern): the real
`HttpTokenMinter` here (Layer 2), and a fake in the S9 test helpers (Layer 1).
"""

from __future__ import annotations

from typing import Protocol

import httpx


class TokenMintError(Exception):
    """The offline token mint was rejected or returned no token. RAISED so the probe
    fails closed to `probe_unavailable` (never a value, never a silent pass)."""


class TokenMinter(Protocol):
    """Mints a JWT scoped to `column_scope` for one offline golden replay, bound to
    `session_id` (the synthetic session the probe also sends as `X-Session-Id`)."""

    async def mint(self, column_scope: list[str], *, session_id: str) -> str: ...


class HttpTokenMinter:
    """Real `TokenMinter` over the token IdP's `POST /token` (Layer 2+).

    `token_endpoint` is the FULL mint URL (e.g. `http://token:8000/token`), guarded
    by the static issuer API key. The mint is session-BOUND (§1.3 deviation): it
    stamps a `sid_hash` for the caller-supplied synthetic `session_id` — the live MCP's
    `require_sid_binding` rejects an unbound token the moment `X-Session-Id` is sent,
    and `RealMCPClient` always sends it. Short-TTL by design (§1.3).
    """

    def __init__(
        self,
        token_endpoint: str,
        api_key: str,
        *,
        user_name: str = "learning-scheduler",
        ttl_seconds: int = 300,
        timeout: float = 30.0,
    ) -> None:
        self._endpoint = token_endpoint
        self._api_key = api_key
        self._user_name = user_name
        self._ttl_seconds = ttl_seconds
        self._timeout = timeout

    async def mint(self, column_scope: list[str], *, session_id: str) -> str:
        # BACKSTOP (D57/D80b): an EMPTY column_scope mints an ALLOW-ALL (unrestricted)
        # token at the IdP + MCP — the learning plane must NEVER do that. Refuse to
        # mint rather than run a model/extraction-derived replay SQL against live
        # ClickHouse with no scope. golden_replay short-circuits on empty `uses` before
        # reaching here; this is the defense-in-depth second gate.
        if not column_scope:
            raise TokenMintError(
                "refusing to mint an allow-all token (empty column_scope)"
            )
        # `session_id` binds the token (sid_hash) to the synthetic session the probe
        # also sends as X-Session-Id — required by the live MCP's require_sid_binding
        # (§1.3 deviation). `column_scope` is EXACTLY the blueprint's `uses` footprint.
        body = {
            "user_name": self._user_name,
            "column_scope": list(column_scope),
            "ttl_seconds": self._ttl_seconds,
            "session_id": session_id,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=body,
                )
        except httpx.HTTPError as exc:  # transport failure — token IdP down/unreachable
            raise TokenMintError(f"token mint transport error: {exc}") from exc
        if resp.status_code >= 400:
            # A 401 (bad issuer key), 422 (bad scope), or 5xx — never leak the body.
            raise TokenMintError(f"token mint returned HTTP {resp.status_code}")
        try:
            body_json = resp.json()
        except ValueError as exc:
            raise TokenMintError("token mint returned a non-JSON body") from exc
        token = body_json.get("access_token") if isinstance(body_json, dict) else None
        if not isinstance(token, str) or not token:
            raise TokenMintError("token mint returned no access_token")
        return token


__all__ = ["HttpTokenMinter", "TokenMintError", "TokenMinter"]
