"""QA Layer-1/2: end-to-end bind (and, when a seeded ClickHouse is up, execution)
of the second-order `bp-hires-projection` run-rate PROJECTION blueprint
(D103/D104, second-order-analysis-design §2-§3).

The blueprint carries TWO `relative_window` slots — `window_months` (referenced 3x:
avg denominator, projection denominator, and the trailing-window INTERVAL) and
`horizon_months` (the forward multiplier) — bound as bare NUMBER literals into a
single-row aggregate (`result_grain: []`, so D56 grain-verify is vacuously skipped).

This module binds the REAL fixture template (loaded via `load_seed_fixtures`, never
hand-copied) through the SAME `BlueprintExecutor` path the other blueprint tests use:

  * bind: window_months=6 / horizon_months=6 -> every `{window_months}` -> 6 and
    `{horizon_months}` -> 6, as unquoted NUMBER literals; the repeated slot binds
    at all three sites (a distinct-values variant proves 3x vs 1x unambiguously);
  * bounds: window below min (1) / above max (37) and horizon out of [1,24] resolve
    to AskUser -> ExecPaused, NO dispatch (resolver-level parity too);
  * live (skip-guarded): the bound SQL executes against the seeded dbpcm_warehouse
    and returns a well-formed, non-negative, internally-consistent row.

ADD-only; mirrors `test_executor_windowed_qa.py` / `test_slots_windowed_qa.py` and
does not touch the reviewer-owned executor/slot tests.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    BlueprintExecutor,
    ExecCompleted,
    ExecPaused,
)
from data_agent.runtime.blueprint.models import SlotSpec
from data_agent.runtime.blueprint.slots import AskUser, SlotBinding, resolve_slot
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.corpus_loader import BlueprintSeed, load_seed_fixtures
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "corpus"
_BP_ID = "bp-hires-projection"

_E = "dbpcm_warehouse.employee"
_HIRE_COL = f"{_E}.MostRecentHireDate"
_CODE_COL = f"{_E}.EmployeeCode"
_USES = frozenset({_HIRE_COL, _CODE_COL})

# The catalog the executor binds/scope-checks against (mirrors the seeded schema).
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "MostRecentHireDate": "Nullable(DateTime64(6))"}}
)


def _seed() -> BlueprintSeed:
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    seed = next((b for b in blueprints if b.id == _BP_ID), None)
    assert seed is not None, f"{_BP_ID} missing from the seed corpus"
    return seed


def _detail() -> BlueprintDetail:
    # Wrap the REAL fixture seed in a BlueprintDetail so the bind path exercises the
    # shipped template/slots verbatim (no drift between fixture and test).
    seed = _seed()
    return BlueprintDetail(
        id=seed.id,
        intent=seed.intent,
        slots_summary=seed.slots_summary,
        uses=frozenset(seed.uses),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=list(seed.slots),
        sql_template=seed.sql_template,
        composes=None,
        result_grain=seed.result_grain,
    )


def _slot_spec(name: str) -> SlotSpec:
    seed = _seed()
    raw = next(s for s in seed.slots if s["name"] == name)
    return SlotSpec.parse(raw)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-proj", jwt="jwt-secret", column_scope=frozenset())


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(detail)
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)


async def _bind_sql(slot_bindings: dict[str, Any]) -> str:
    """Drive the executor to a completed bind and return the single node's SQL."""
    detail = _detail()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(
                    ["hires_in_window", "avg_monthly_hires", "projected_next_n_hires"],
                    [[3, 0.5, 3]],
                )
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id=_BP_ID, slot_bindings=slot_bindings, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted), outcome
    return mcp.calls[0].args["sql"]


# ===========================================================================
# The key behavioral test: repeated-slot bind -> NUMBER literals
# ===========================================================================


async def test_projection_binds_both_windows_as_number_literals() -> None:
    # window_months=6, horizon_months=6 (the acceptance example). Every placeholder
    # is gone and the trailing window is a bare `INTERVAL 6 MONTH` (never quoted).
    node_sql = await _bind_sql({"window_months": 6, "horizon_months": 6})
    assert "{window_months}" not in node_sql
    assert "{horizon_months}" not in node_sql
    assert "INTERVAL 6 MONTH" in node_sql
    # NUMBER literal, not a string: no `INTERVAL '6'` and no quoted 6 anywhere.
    assert "INTERVAL '6'" not in node_sql
    assert "'6'" not in node_sql


async def test_projection_repeated_slot_binds_at_all_three_sites() -> None:
    # Distinct values disambiguate the repeated slot: window_months binds 3x, and
    # horizon_months once. Chosen so neither value collides with the literal `1` in
    # `round(..., 1)`: 12 (window) and 3 (horizon) each appear ONLY as bound values.
    node_sql = await _bind_sql({"window_months": 12, "horizon_months": 3})
    assert "{window_months}" not in node_sql
    assert "{horizon_months}" not in node_sql
    # The window value lands at all three of its template sites; the horizon at its one.
    assert node_sql.count("12") == 3, node_sql
    assert node_sql.count("* 3") == 1, node_sql
    assert "INTERVAL 12 MONTH" in node_sql


async def test_projection_digit_string_binds_as_number() -> None:
    # A pure-digit string still binds as a NUMBER literal into the INTERVAL.
    node_sql = await _bind_sql({"window_months": "6", "horizon_months": "6"})
    assert "INTERVAL 6 MONTH" in node_sql
    assert "'6'" not in node_sql


# ===========================================================================
# Slot bounds -> AskUser (pause), never a bind. Resolver-level + executor-level.
# ===========================================================================


@pytest.mark.parametrize("bad", [1, 37])  # min_value=2, max_value=36 (both inclusive)
def test_window_out_of_bounds_resolves_to_askuser(bad: int) -> None:
    out = resolve_slot(bad, _slot_spec("window_months"))
    assert isinstance(out, AskUser) and out.reason == "invalid"


@pytest.mark.parametrize("bad", [0, 25])  # min_value=1, max_value=24 (both inclusive)
def test_horizon_out_of_bounds_resolves_to_askuser(bad: int) -> None:
    out = resolve_slot(bad, _slot_spec("horizon_months"))
    assert isinstance(out, AskUser) and out.reason == "invalid"


def test_window_and_horizon_at_bounds_bind() -> None:
    # Both inclusive bounds bind (the fence-post pin: 2/36 and 1/24 are IN range).
    w = _slot_spec("window_months")
    h = _slot_spec("horizon_months")
    assert isinstance(resolve_slot(2, w), SlotBinding)
    assert isinstance(resolve_slot(36, w), SlotBinding)
    assert isinstance(resolve_slot(1, h), SlotBinding)
    assert isinstance(resolve_slot(24, h), SlotBinding)


@pytest.mark.parametrize(
    "bindings",
    [
        {"window_months": 1, "horizon_months": 6},   # window below min
        {"window_months": 37, "horizon_months": 6},  # window above max
        {"window_months": 6, "horizon_months": 0},   # horizon below min
        {"window_months": 6, "horizon_months": 25},  # horizon above max
    ],
)
async def test_out_of_bounds_slot_pauses_before_any_dispatch(bindings: dict[str, Any]) -> None:
    # Through the executor: an out-of-range window/horizon -> AskUser -> PAUSE, and
    # NO node SQL is ever dispatched (no wrong-answer query runs).
    detail = _detail()
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id=_BP_ID, slot_bindings=bindings, credentials=_creds()
    )
    assert isinstance(outcome, ExecPaused), outcome
    assert outcome.reason == "blueprint_slot"
    assert mcp.calls == []


async def test_missing_required_window_pauses_before_dispatch() -> None:
    detail = _detail()
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id=_BP_ID, slot_bindings={"window_months": 6}, credentials=_creds()
    )
    assert isinstance(outcome, ExecPaused)
    assert mcp.calls == []


# ===========================================================================
# Live layer (skip-guarded): the bound SQL executes on the seeded warehouse
# ===========================================================================

_CH_URL = os.environ.get("CLICKHOUSE_TEST_URL", "http://localhost:8123")


def _ch_query(sql: str) -> dict[str, Any] | None:
    """Return the JSON result of `sql` against the seeded ClickHouse, or None when
    no warehouse is reachable (so the live assertions SKIP rather than fail)."""
    body = (sql.rstrip().rstrip(";") + "\nFORMAT JSON").encode("utf-8")
    req = urllib.request.Request(_CH_URL + "/", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (localhost only)
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _warehouse_reachable() -> bool:
    probe = _ch_query("SELECT count() AS n FROM dbpcm_warehouse.employee")
    return probe is not None and probe.get("rows", 0) == 1


async def test_bound_sql_executes_on_seeded_warehouse() -> None:
    if not _warehouse_reachable():
        pytest.skip(
            "no seeded ClickHouse reachable at "
            f"{_CH_URL} (dbpcm_warehouse.employee) — live projection assertion skipped"
        )

    # Bind the REAL fixture template (window=6, horizon=6) through the executor, then
    # run THAT exact bound SQL against the live seeded warehouse.
    node_sql = await _bind_sql({"window_months": 6, "horizon_months": 6})
    result = _ch_query(node_sql)
    assert result is not None, "bound projection SQL failed to execute on the warehouse"

    assert result["rows"] == 1, "run-rate projection is a single-row aggregate"
    cols = [c["name"] for c in result["meta"]]
    assert cols == ["hires_in_window", "avg_monthly_hires", "projected_next_n_hires"]

    row = result["data"][0]
    hires = float(row["hires_in_window"])
    avg_monthly = float(row["avg_monthly_hires"])
    projected = float(row["projected_next_n_hires"])

    # Well-formed + non-negative counts (a projection is never a negative headcount).
    assert hires >= 0
    assert avg_monthly >= 0
    assert projected >= 0
    # Internal consistency: avg = hires / window(6); projected = round(avg * horizon(6)).
    assert avg_monthly == pytest.approx(hires / 6.0, abs=0.05)  # authored round(...,1)
    assert projected == pytest.approx(avg_monthly * 6.0, abs=1.0)  # authored round(...)
