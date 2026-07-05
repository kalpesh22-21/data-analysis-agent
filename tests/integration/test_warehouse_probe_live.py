"""Layer-2 integration — the REAL S9 golden-replay probe (`MCPWarehouseProbe`)
end-to-end against the LIVE clickhouse-api MCP → ClickHouse, under a per-blueprint
JWT minted (via the live token IdP) scoped to the blueprint's `uses`.

The proofs only the live stack can give (S9-activation Slice 1):
  * the grain probe runs the `COUNT(*),COUNT(DISTINCT grain)` canary through the real
    `runQuery` choke point and returns a correct `ProbeResult` (5 rows, 5 distinct
    EmployeeCode over the seeded `dbpcm_warehouse.employee`);
  * a replay reading OUTSIDE the minted `uses` is DENIED by the real D57 column-scope
    teeth (COLUMN_SCOPE_VIOLATION) — proving the token is really scoped to `uses`
    (not an allow-all), the whole point of the MCP-via-scope reach decision (§1.1).

Skip-guarded on `MCP_TEST_URL` (mirrors `tests/integration/test_mcp_scope_live.py`)
so `uv run pytest` with no live stack stays green. Run with:

    MCP_TEST_URL=http://localhost:18090/mcp uv run pytest \
        tests/integration/test_warehouse_probe_live.py -v
"""

from __future__ import annotations

import os

import pytest

from data_agent.learning.promotion.token_minter import HttpTokenMinter
from data_agent.learning.promotion.warehouse_probe import MCPWarehouseProbe
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.real_client import RealMCPClient

from .conftest import TOKEN_ISSUER_API_KEY, TOKEN_SERVICE_URL

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_TEST_URL"),
    reason="Requires a live clickhouse-api MCP stack + token IdP (set MCP_TEST_URL).",
)

_EMPLOYEE_FQ = "dbpcm_warehouse.employee"
_EMPLOYEE_CODE_SCOPE = (f"{_EMPLOYEE_FQ}.EmployeeCode",)


def _probe() -> MCPWarehouseProbe:
    return MCPWarehouseProbe(
        mcp_client=RealMCPClient(os.environ["MCP_TEST_URL"]),
        token_minter=HttpTokenMinter(TOKEN_SERVICE_URL, TOKEN_ISSUER_API_KEY),
    )


async def test_probe_runs_grain_probe_end_to_end() -> None:
    """Under a token scoped to EXACTLY the blueprint's `uses` (EmployeeCode), the
    real probe reads the column signature AND runs the grain canary → 5 rows, 5
    distinct EmployeeCode (the seeded grain)."""
    result = await _probe().run(
        f"SELECT EmployeeCode FROM {_EMPLOYEE_FQ}",
        grain_columns=("EmployeeCode",),
        column_scope=_EMPLOYEE_CODE_SCOPE,
    )
    assert result.columns == ("EmployeeCode",)
    assert result.row_count == 5
    assert result.distinct_grain_count == 5


async def test_probe_signature_only_when_grain_unverifiable() -> None:
    """No declared grain → the row-count teeth are skipped; the probe still returns
    the real column signature (proving the replay executed live)."""
    result = await _probe().run(
        f"SELECT EmployeeCode FROM {_EMPLOYEE_FQ}",
        grain_columns=(),
        column_scope=_EMPLOYEE_CODE_SCOPE,
    )
    assert result.columns == ("EmployeeCode",)
    assert result.distinct_grain_count is None


async def test_probe_out_of_uses_column_denied_by_real_scope() -> None:
    """A replay reading a column OUTSIDE the minted `uses` is rejected by the live
    D57 teeth (COLUMN_SCOPE_VIOLATION) — the token is really scoped to `uses`. The
    denial surfaces as a raise → `golden_replay` would HOLD `probe_unavailable`."""
    probe = _probe()
    with pytest.raises(MCPToolError) as exc_info:
        await probe.run(
            f"SELECT AnnualSalary FROM {_EMPLOYEE_FQ}",  # NOT in the minted uses
            grain_columns=(),
            column_scope=_EMPLOYEE_CODE_SCOPE,  # scoped to EmployeeCode only
        )
    assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"
