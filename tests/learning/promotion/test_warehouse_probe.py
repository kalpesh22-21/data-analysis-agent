"""Layer-1 — the REAL S9 golden-replay probe (`promotion/warehouse_probe.py`).

Infra-free: the probe is driven by a FAKE `MCPClient` (scripted runQuery results) +
a FAKE `TokenMinter` (records the minted scope, no real IdP). The proofs:

  * `S9-probe-never-returns-value` — the probe returns ONLY (row_count,
    distinct_grain_count, columns); a real row VALUE in the scripted result NEVER
    appears in the `ProbeResult` (D98/D17).
  * `S9-probe-scoped-to-uses` (mint-args half; L2 proves real D57) — the minted
    `column_scope` is EXACTLY the blueprint's `generalization.uses`.
  * a mint / MCP failure RAISES → `golden_replay` degrades to `probe_unavailable`.
"""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from data_agent.learning.candidate.generalization import BlueprintGeneralization
from data_agent.learning.promotion.replay import golden_replay
from data_agent.learning.promotion.token_minter import HttpTokenMinter, TokenMintError
from data_agent.learning.promotion.warehouse_probe import (
    MCPWarehouseProbe,
    WarehouseProbeError,
)
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient

from .helpers import make_blueprint_candidate


class FakeTokenMinter:
    """A `TokenMinter` double — records every minted `column_scope` (so a test can
    assert the replay was scoped to the blueprint's `uses`) and can be scripted to
    RAISE (an IdP failure)."""

    def __init__(self, *, token: str = "jwt-scoped", fail: Exception | None = None) -> None:
        self._token = token
        self._fail = fail
        self.minted_scopes: list[list[str]] = []
        self.minted_sessions: list[str] = []

    async def mint(self, column_scope: list[str], *, session_id: str) -> str:
        self.minted_scopes.append(list(column_scope))
        self.minted_sessions.append(session_id)
        if self._fail is not None:
            raise self._fail
        return self._token


def _runquery_result(columns: list[str], rows: list[list]) -> dict:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


# --- S9-probe-never-returns-value -----------------------------------------------


async def test_probe_returns_only_structure_never_a_value() -> None:
    """A scripted result carrying a real salary VALUE must never leak into the
    ProbeResult — the probe reads only the column header + counts (D98)."""
    secret_value = 987654.32
    mcp = FakeMCPClient(
        scripted={"runQuery": [_runquery_result(["total_earnings"], [[secret_value]])]}
    )
    minter = FakeTokenMinter()
    probe = MCPWarehouseProbe(mcp_client=mcp, token_minter=minter)

    result = await probe.run(
        "SELECT sum(gross_pay) AS total_earnings FROM db.t",
        grain_columns=(),
        column_scope=("db.t.gross_pay",),
    )

    assert result.columns == ("total_earnings",)
    assert result.distinct_grain_count is None
    # D98: NO row value ever leaves the probe.
    assert "987654" not in repr(result)
    assert secret_value not in (result.row_count, result.distinct_grain_count)


async def test_probe_runs_the_grain_probe_when_grain_declared() -> None:
    """A declared grain → a SECOND runQuery (the COUNT(*)/COUNT(DISTINCT) canary);
    the probe returns the two counts, still never a value."""
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _runquery_result(["department"], [["engineering"]]),  # replay col read
                _runquery_result(["__bp_n", "__bp_d"], [[5, 5]]),  # the grain probe
            ]
        }
    )
    probe = MCPWarehouseProbe(mcp_client=mcp, token_minter=FakeTokenMinter())

    result = await probe.run(
        "SELECT department FROM db.t GROUP BY department",
        grain_columns=("department",),
        column_scope=("db.t.department",),
    )

    assert (result.row_count, result.distinct_grain_count) == (5, 5)
    assert result.columns == ("department",)
    # the second call WAS the grain probe (built by the shared helper).
    grain_sql = mcp.calls[1].args["sql"].lower()
    assert "count(*)" in grain_sql and "count(distinct department)" in grain_sql


async def test_probe_holds_grain_unverifiable_when_column_unmappable() -> None:
    """A declared grain column with no matching output → distinct=None (verify.py
    fail-closes, never a false pass) — parity with the executor."""
    mcp = FakeMCPClient(scripted={"runQuery": [_runquery_result(["total"], [[1]])]})
    probe = MCPWarehouseProbe(mcp_client=mcp, token_minter=FakeTokenMinter())

    result = await probe.run(
        "SELECT sum(x) AS total FROM db.t",
        grain_columns=("headcount",),  # not an output column
        column_scope=("db.t.x",),
    )
    assert result.distinct_grain_count is None
    assert len(mcp.calls) == 1  # the grain probe was NOT dispatched


# --- S9-probe-scoped-to-uses (mint-args half) -----------------------------------


async def test_minted_scope_equals_generalization_uses() -> None:
    """Through `golden_replay`, the minted `column_scope` is EXACTLY the blueprint's
    declared `uses` footprint (D87/OQ-1) — the replay reads only there."""
    env = make_blueprint_candidate()
    gen = BlueprintGeneralization.from_doc(env.payload["generalization"])
    mcp = FakeMCPClient(
        scripted={"runQuery": [_runquery_result(["total_earnings"], [[1.0]])]}
    )
    minter = FakeTokenMinter()
    probe = MCPWarehouseProbe(mcp_client=mcp, token_minter=minter)

    outcome = await golden_replay(env, probe=probe)

    assert outcome.passed  # signature (total_earnings) matches the fixture
    assert minter.minted_scopes == [list(gen.uses)]
    # the JWT + the bound session id reached the transport boundary (D5).
    assert mcp.calls[0].jwt == "jwt-scoped"
    assert mcp.calls[0].session_id == minter.minted_sessions[0]


# --- fail-closed: any mint / MCP failure RAISES → probe_unavailable HOLD ---------


async def test_mint_failure_raises_and_golden_replay_holds() -> None:
    env = make_blueprint_candidate()
    probe = MCPWarehouseProbe(
        mcp_client=FakeMCPClient(scripted={"runQuery": []}),
        token_minter=FakeTokenMinter(fail=TokenMintError("token IdP down")),
    )
    outcome = await golden_replay(env, probe=probe)
    assert not outcome.passed
    assert outcome.reason == "probe_unavailable"


async def test_mcp_denial_raises_and_golden_replay_holds() -> None:
    """A COLUMN_SCOPE_VIOLATION (the live D57 teeth) surfaces as a raise from the
    probe → golden_replay degrades to a clean `probe_unavailable` HOLD (never a value
    leak, never a crash into the cron guard)."""
    env = make_blueprint_candidate()
    mcp = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "outside scope")]}
    )
    probe = MCPWarehouseProbe(mcp_client=mcp, token_minter=FakeTokenMinter())
    outcome = await golden_replay(env, probe=probe)
    assert not outcome.passed
    assert outcome.reason == "probe_unavailable"


async def test_probe_raises_directly_on_mint_error() -> None:
    probe = MCPWarehouseProbe(
        mcp_client=FakeMCPClient(scripted={"runQuery": []}),
        token_minter=FakeTokenMinter(fail=TokenMintError("boom")),
    )
    with pytest.raises(TokenMintError):
        await probe.run("SELECT 1 AS a", grain_columns=(), column_scope=())


# --- S9-empty-uses-refuses-allow-all (D57/D80b backstop) ------------------------


async def test_empty_uses_refuses_allow_all_and_holds_no_uses_scope() -> None:
    """An EMPTY `uses` would mint an ALLOW-ALL token (D80b) — the learning plane must
    NEVER do that. golden_replay short-circuits to `no_uses_scope` BEFORE any mint/MCP
    call (never the misleading `probe_unavailable`)."""
    env = make_blueprint_candidate()
    payload = copy.deepcopy(env.payload)
    payload["generalization"]["uses"] = []  # the empty-footprint hazard
    env = replace(env, payload=payload)

    mcp = FakeMCPClient(scripted={"runQuery": []})
    minter = FakeTokenMinter()
    probe = MCPWarehouseProbe(mcp_client=mcp, token_minter=minter)

    outcome = await golden_replay(env, probe=probe)

    assert not outcome.passed
    assert outcome.reason == "no_uses_scope"
    assert mcp.calls == []  # ZERO warehouse calls
    assert minter.minted_scopes == []  # ZERO mints — never an allow-all token


async def test_http_minter_refuses_empty_scope() -> None:
    """Defense-in-depth: even called directly, the real minter REFUSES an empty scope
    (no httpx call) rather than mint an unrestricted token."""
    minter = HttpTokenMinter("http://token.invalid/token", "issuer-key")
    with pytest.raises(TokenMintError):
        await minter.mint([], session_id="sess-x")


async def test_probe_raises_on_headerless_result() -> None:
    """A malformed runQuery result (no columns header) RAISES — fail-closed, never a
    silent pass with empty columns."""
    mcp = FakeMCPClient(scripted={"runQuery": [{"rows": [[1]]}]})
    probe = MCPWarehouseProbe(mcp_client=mcp, token_minter=FakeTokenMinter())
    with pytest.raises(WarehouseProbeError):
        await probe.run("SELECT 1 AS a", grain_columns=(), column_scope=())
