"""QA6 Layer-1: F2 scalar-intermediate enforcement + table-intermediate rejection.

runblueprint-design §2.4 (F2): Phase-1 passes ONLY scalar intermediates; a table
intermediate needs scratch materialization no Phase-1 MCP tool can do → the DAG is
`RUN_BLUEPRINT_UNSUPPORTED` (raw-loop fallback). And a node consumed as a scalar
must be a genuine single cell — 0 rows / NULL fail-closed (never binds a broken
literal); >1 row / >1 column are the "silent row[0][0]" hazard the brief pins.

All fakes.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    SLOT_INVALID_CODE,
    UNSUPPORTED_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {
        _E: {
            "EmployeeCode": "String",
            "Department": "Nullable(String)",
            "AnnualSalary": "Nullable(Float64)",
        }
    }
)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-qa6-f2", jwt="jwt", column_scope=frozenset())


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(composes: list[dict[str, Any]], result_grain: Any = None) -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-f2",
        intent="f2 probe",
        slots_summary="",
        uses=frozenset({f"{_E}.Department", f"{_E}.AnnualSalary", f"{_E}.EmployeeCode"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        composes=composes,
        result_grain=result_grain if result_grain is not None else ["Department"],
    )


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(detail)
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)


def _scalar_consumer_dag() -> BlueprintDetail:
    return _detail(
        composes=[
            {
                "order": 0,
                "output": {"tok": "scalar"},
                "sql_template": "SELECT Department AS tok FROM dbpcm_warehouse.employee LIMIT 1",
            },
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"tok": "$0.tok"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "WHERE Department = {tok} GROUP BY Department"
                ),
                "output": {},
            },
        ]
    )


# ---------------------------------------------------------------------------
# Table-intermediate DAG → UNSUPPORTED (F2). Never executes a scratch JOIN.
# ---------------------------------------------------------------------------


async def test_table_intermediate_consumed_downstream_is_unsupported() -> None:
    # Node 0 declares a TABLE output and is CONSUMED by node 1 (feeds_from) — this
    # needs scratch materialization (F2). Must reject BEFORE any node dispatches.
    detail = _detail(
        composes=[
            {
                "order": 0,
                "output": {"rows": "table"},
                "sql_template": "SELECT Department AS department, count() AS n FROM dbpcm_warehouse.employee GROUP BY Department",
            },
            {
                "order": 1,
                "feeds_from": [0],
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-f2", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    assert mcp.calls == []  # rejected structurally, nothing ran


async def test_terminal_table_output_is_fine_only_consumed_table_is_rejected() -> None:
    # A TERMINAL node's table result is returned, not passed — a table output that
    # feeds nobody is allowed (only a CONSUMED table is out of scope).
    detail = _detail(
        composes=[
            {
                "order": 0,
                "output": {"n": "scalar"},
                "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee",
            },
            {
                "order": 1,
                "feeds_from": [0],
                "output": {"rows": "table"},  # terminal table — returned, not passed
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
            },
        ]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["n"], [[10]]),
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-f2", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)


# ---------------------------------------------------------------------------
# Scalar cell integrity: 0 rows / NULL fail-closed (these DO work today).
# ---------------------------------------------------------------------------


async def test_scalar_node_zero_rows_fails_closed() -> None:
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["tok"], [])]})  # 0 rows
    outcome = await _executor(mcp, _scalar_consumer_dag()).execute(
        blueprint_id="bp-f2", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert len(mcp.calls) == 1  # consumer never dispatched on an empty scalar


async def test_scalar_node_null_cell_fails_closed() -> None:
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["tok"], [[None]])]})
    outcome = await _executor(mcp, _scalar_consumer_dag()).execute(
        blueprint_id="bp-f2", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert len(mcp.calls) == 1


# ---------------------------------------------------------------------------
# FIXED (Slice-C review Blocker 1): a scalar-declared node that FANS OUT (>1 row)
# or returns >1 column is now fail-closed — `_extract_scalar_output` returns a
# sentinel and the executor maps it to SLOT_INVALID BEFORE the consumer dispatches
# (the D56 grain gate only runs on the TERMINAL node, so a fanned-out INTERMEDIATE
# scalar must be caught here, not silently bound via rows[0][0]).
# ---------------------------------------------------------------------------


async def test_scalar_node_multi_row_should_fail_closed() -> None:
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["tok"], [["Sales"], ["Eng"]]),  # FAN-OUT: 2 rows for a scalar
                _rq(["department"], [["Sales"]]),      # consumer runs on rows[0] silently
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    outcome = await _executor(mcp, _scalar_consumer_dag()).execute(
        blueprint_id="bp-f2", slot_bindings={}, credentials=_creds()
    )
    # Fail-closed rather than silently bind an arbitrary fanned-out cell.
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert len(mcp.calls) == 1  # consumer never dispatched


async def test_scalar_node_multi_column_should_fail_closed() -> None:
    detail = _detail(
        composes=[
            {
                "order": 0,
                "output": {"tok": "scalar"},
                # two output columns but a single declared scalar → ambiguous cell
                "sql_template": "SELECT Department AS tok, count() AS extra FROM dbpcm_warehouse.employee GROUP BY Department LIMIT 1",
            },
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"tok": "$0.tok"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "WHERE Department = {tok} GROUP BY Department"
                ),
                "output": {},
            },
        ]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["tok", "extra"], [["Sales", 3]]),  # >1 column
                _rq(["department"], [["Sales"]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-f2", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert len(mcp.calls) == 1
