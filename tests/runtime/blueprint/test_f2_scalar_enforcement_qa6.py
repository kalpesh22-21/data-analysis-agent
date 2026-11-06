"""QA6 Layer-1: F2 scalar-intermediate enforcement + table-intermediate rejection.

runblueprint-design §2.4 (F2): Phase-1 passes ONLY scalar intermediates; a table
intermediate needs scratch materialization no Phase-1 MCP tool can do → the DAG is
`RUN_BLUEPRINT_UNSUPPORTED` (raw-loop fallback). And a node consumed as a scalar
must be a genuine single cell — 0 rows fail-closed; NULL is a valid cell (never binds a broken
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
# Scalar cell integrity: 0 rows fail-closed; NULL is a valid cell (these DO work today).
# ---------------------------------------------------------------------------


async def test_scalar_node_zero_rows_fails_closed() -> None:
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["tok"], [])]})  # 0 rows
    outcome = await _executor(mcp, _scalar_consumer_dag()).execute(
        blueprint_id="bp-f2", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert len(mcp.calls) == 1  # consumer never dispatched on an empty scalar


async def test_scalar_node_null_cell_binds_sql_null() -> None:
    mcp = FakeMCPClient(scripted={"runQuery": [
        _rq(["tok"], [[None]]), _rq(["department"], []),
        _rq(["__bp_n", "__bp_d"], [[0, 0]]),
    ]})
    outcome = await _executor(mcp, _scalar_consumer_dag()).execute(
        blueprint_id="bp-f2", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["scalar_bindings"] == [dict(step=2, parameter="tok", source_step=1, source_output="tok", value=None)]
    assert "NULL" in mcp.calls[1].args["sql"]
    assert "\'NULL\'" not in mcp.calls[1].args["sql"]


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


def test_nullable_multi_scalar_survives_checkpoint_and_keeps_row_shape():
    from data_agent.runtime.blueprint.executor import (
        _dumps_completed,
        _extract_scalar_output,
        _load_completed_into_outputs,
        _node_record,
        _rehydrate_completed,
    )
    from data_agent.runtime.blueprint.models import Node
    from data_agent.runtime.blueprint.when import evaluate_when

    node = Node(order=0, output={"salary": "scalar", "hourly": "scalar"})
    for values in ([100000, None], [None, None]):
        output = _extract_scalar_output(node, _rq(["salary", "hourly"], [values]))
        assert output == dict(zip(["salary", "hourly"], values, strict=True))
        completed = {0: _node_record(output, frozenset(), "SELECT 1")}
        env = {}
        _load_completed_into_outputs(_rehydrate_completed(_dumps_completed(completed)), env)
        assert env["$0.hourly"] is None
        assert evaluate_when("count($0) == 1", env)
        assert evaluate_when("empty($0.hourly)", env)
    assert _extract_scalar_output(node, _rq(["salary", "wrong"], [[100000, None]])) is None
    assert _extract_scalar_output(node, _rq(["salary", "hourly"], [])) is None
    assert _extract_scalar_output(node, _rq(["salary", "hourly"], [[1, None], [2, None]])) is None


def test_null_when_comparison_requires_explicit_empty_guard():
    import pytest

    from data_agent.runtime.blueprint.when import WhenClauseError, evaluate_when
    env = {"$0.hourly": None}
    with pytest.raises(WhenClauseError, match="NULL scalar"):
        evaluate_when("$0.hourly > 0", env)
    assert not evaluate_when("not empty($0.hourly) and $0.hourly > 0", env)


def test_scalar_lineage_uses_executed_steps_and_survives_checkpoint():
    from data_agent.runtime.blueprint.executor import (
        _dumps_completed,
        _node_record,
        _rehydrate_completed,
        _scalar_binding_receipts,
    )
    from data_agent.runtime.blueprint.models import Node
    nodes = [Node(order=2, output={'salary': 'scalar', 'hourly': 'scalar'}),
             Node(order=3),  # skipped node has no executed SQL
             Node(order=7, consumes={'s': '$2.salary', 'h': '$2.hourly', 'unused': '$2.salary'},
                  sql_template='SELECT {s}, {h}')]
    running = {2: _node_record({'salary': 107000, 'hourly': None}, frozenset(), 'SELECT averages'),
               3: _node_record({}, frozenset(), None),
               7: _node_record({}, frozenset(), 'SELECT 107000, NULL')}
    expected = [dict(step=2, parameter='s', source_step=1, source_output='salary', value=107000),
                dict(step=2, parameter='h', source_step=1, source_output='hourly', value=None)]
    assert _scalar_binding_receipts(nodes, running) == expected
    assert _scalar_binding_receipts(nodes, _rehydrate_completed(_dumps_completed(running))) == expected
