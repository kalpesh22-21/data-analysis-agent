"""promotion/warehouse_probe.py — the REAL S9 golden-replay probe (D98/D56/D57).

Runs the grain probe through the adopted MCP `runQuery` choke point under a JWT scoped to
EXACTLY the blueprint's declared `uses` footprint (§1), which REUSES the D57 column-scope
teeth verbatim and gives oracle parity: an offline replay that passes travelled the
identical path a live `runBlueprint` would. NEVER RETURNS A VALUE (D98/D17) — only the D56
triple `(row_count, distinct_grain_count, columns)`; it reads the column header and DISCARDS
every row. FAIL-CLOSED BY RAISING (§1.4): any mint, denial, outage or malformed result
raises, and `golden_replay` catches it into a `probe_unavailable` HOLD. The probe SQL comes
from the SHARED `grain_probe` builder the live executor uses, so the two cannot drift.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from data_agent.runtime.blueprint.grain_probe import (
    build_grain_probe_sql,
    map_grain_columns,
    unpack_grain_probe,
)
from data_agent.runtime.mcp.client import MCPClient

from .models import ProbeResult
from .token_minter import TokenMinter

_logger = logging.getLogger(__name__)

# The replay column-signature read fetches a single row so the runQuery result
# carries its `columns` header; the row itself is DISCARDED (never read, D98).
_REPLAY_COLUMN_PROBE_LIMIT = 1


class WarehouseProbeError(Exception):
    """A malformed/unreadable probe result. RAISED (never a value, never a silent
    pass) so `golden_replay` degrades to a clean `probe_unavailable` HOLD (D98)."""


class MCPWarehouseProbe:
    """The real `WarehouseProbe` (S9-design §1).

    Every collaborator is injected — an `MCPClient` (the runQuery transport), a `TokenMinter`
    (the offline scoped JWT), and an optional synthetic-session-id factory — so Layer-1 fakes
    and the live stack travel the identical path.
    """

    def __init__(
        self,
        *,
        mcp_client: MCPClient,
        token_minter: TokenMinter,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._mcp = mcp_client
        self._minter = token_minter
        self._new_session_id = session_id_factory or _default_session_id

    async def run(
        self,
        sql: str,
        *,
        grain_columns: tuple[str, ...],
        column_scope: tuple[str, ...],
    ) -> ProbeResult:
        """Run one golden replay and return the D56 STRUCTURE triple (never a value).

        `column_scope` is the blueprint's declared `uses` footprint: the token is minted to EXACTLY
        that scope (§1.3), so the replay reads only within the declared footprint and the MCP's D57
        teeth reject anything outside it.
        """
        # Mint a JWT scoped to EXACTLY the blueprint's declared footprint (uses),
        # BOUND to a fresh synthetic session id the probe also presents as
        # X-Session-Id (the live MCP's require_sid_binding, §1.3 deviation). Any mint
        # failure RAISES (TokenMintError) → probe_unavailable HOLD (§1.4).
        session_id = self._new_session_id()
        jwt = await self._minter.mint(list(column_scope), session_id=session_id)

        # 1. Run the replay SQL to (a) PROVE it still executes against the live schema
        #    (through the D57-enforced runQuery choke point) and (b) read the actual
        #    OUTPUT column names for the D98 signature check. The rows are DISCARDED —
        #    the probe never reads or returns a value (D98/D17). An MCP denial (e.g.
        #    COLUMN_SCOPE_VIOLATION) RAISES here → probe_unavailable HOLD.
        replay_result = await self._mcp.call_tool(
            "runQuery",
            {"sql": sql, "limit": _REPLAY_COLUMN_PROBE_LIMIT},
            jwt=jwt,
            session_id=session_id,
        )
        columns = _columns_from_result(replay_result)

        # 2. Grain-integrity teeth — only when a grain is declared (§1.2). The grain
        #    probe SQL is built by the SHARED helper (byte-identical to the executor).
        if grain_columns:
            mapped = map_grain_columns(sql, grain_columns)
            if mapped is None:
                # A declared grain column has no matching output column — the check
                # cannot run; verify.py fail-closes on distinct=None (never a false
                # pass). Same posture as the executor's `_verify`.
                return ProbeResult(
                    row_count=0, distinct_grain_count=None, columns=columns
                )
            probe_sql = build_grain_probe_sql(sql, mapped)
            grain_result = await self._mcp.call_tool(
                "runQuery",
                {"sql": probe_sql, "limit": None},
                jwt=jwt,
                session_id=session_id,
            )
            total, distinct = unpack_grain_probe(grain_result)
            if total is None or distinct is None:
                return ProbeResult(
                    row_count=0, distinct_grain_count=None, columns=columns
                )
            return ProbeResult(
                row_count=total, distinct_grain_count=distinct, columns=columns
            )

        # No declared grain → verify.py skips the row-count teeth (grain_checked=False);
        # only the column signature is checked. row_count is unread — report 0.
        return ProbeResult(row_count=0, distinct_grain_count=None, columns=columns)


def _default_session_id() -> str:
    # A synthetic, non-user session id. It is LOAD-BEARING: the token is minted BOUND
    # to it (sid_hash), and the probe sends this SAME id as X-Session-Id, so an MCP
    # running require_sid_binding accepts it (that flag is config, default OFF — see
    # token_minter.py's module docstring; §1.3 deviation). The probe owns both the
    # token and the id, so there is no hijack surface either way.
    return f"learning-replay-{uuid.uuid4().hex}"


def _columns_from_result(raw: Any) -> tuple[str, ...]:
    """Read ONLY the column-name header from a runQuery result — values discarded
    (D98). A non-tabular / header-less result RAISES → probe_unavailable HOLD."""
    if not isinstance(raw, dict):
        raise WarehouseProbeError(
            "runQuery returned a non-tabular result; cannot read the column signature"
        )
    columns = raw.get("columns")
    if not isinstance(columns, list):
        raise WarehouseProbeError("runQuery result carried no columns header")
    return tuple(str(c) for c in columns)


__all__ = ["MCPWarehouseProbe", "WarehouseProbeError"]
