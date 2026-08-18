"""Layer-1: the leaf path IS the DAG path (cleanup T4.2).

`BlueprintExecutor.execute` no longer carries a hand-copied single-node branch: a
leaf blueprint's top-level `sql_template` is synthesized into a one-node DAG and
walked by `_execute_dag`. These two tests pin what the collapse could silently
change and the existing suites do not already cover:

  - ORDERING: the S-read-only gate now lives in `_execute_dag`, and it must fire
    BEFORE `_resolve_all_slots` — a poisoned (DDL) template whose slot carries a
    `binds_to` must be rejected without ever firing the DISTINCT domain probe.
    The `test_executor_b3.py` fixture has `slots=[]`, so it cannot see this.
  - RESULT SHAPE: a single-node run finishes through the DAG `_finalize`, whose
    `sql` list is built from `node_sqls` rather than a literal `[node_sql]` —
    pin that it is still exactly one entry, equal to `terminal_sql`.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
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
_DEPT_COL = f"{_E}.Department"

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
    return RuntimeCredentials(session_id="s-bp", jwt="jwt", column_scope=frozenset())


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(
    *, slots: list[dict[str, Any]], sql_template: str, result_grain: Any = None
) -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-leaf",
        intent="x",
        slots_summary="department",
        uses=frozenset({_DEPT_COL, f"{_E}.EmployeeCode", f"{_E}.AnnualSalary"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=slots,
        sql_template=sql_template,
        result_grain=result_grain if result_grain is not None else [],
    )


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    idx = FakeVectorIndex()
    idx.add_detail(detail)
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=idx)


async def test_ddl_template_with_a_binds_to_slot_is_unsupported_before_any_probe() -> None:
    # The gate ordering trap: this template PARSES but is a DROP, and its slot
    # declares `binds_to`, so a gate placed after slot resolution would already
    # have fired a DISTINCT domain probe against the warehouse before rejecting
    # the blueprint. UNSUPPORTED with NOTHING dispatched is the only safe shape.
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}],
        sql_template="DROP TABLE dbpcm_warehouse.employee",
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["Department"], [["Warehouse"]])]})

    outcome = await _executor(mcp, detail).execute(
        blueprint_id=detail.id,
        slot_bindings={"department": "Warehouse"},
        credentials=_creds(),
    )

    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    assert mcp.calls == []  # no domain probe, no node query — nothing ran


async def test_single_node_sql_list_is_exactly_the_terminal_sql() -> None:
    # Through the collapsed path the transparency `sql` list comes from the DAG
    # walker's `node_sqls`; for a leaf blueprint it must still be the ONE node
    # SQL, identical to `terminal_sql` (what `answerWithTable` re-pages from).
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}],
        sql_template=(
            "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary "
            "FROM dbpcm_warehouse.employee WHERE Department = {department} "
            "GROUP BY Department"
        ),
        result_grain=["Department"],
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Warehouse"], ["Sales"]]),  # slot domain probe
                _rq(["department", "avg_salary"], [["Warehouse", 50000.0]]),  # node
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),  # grain probe
            ]
        }
    )

    outcome = await _executor(mcp, detail).execute(
        blueprint_id=detail.id,
        slot_bindings={"department": "Warehouse"},
        credentials=_creds(),
    )

    assert isinstance(outcome, ExecCompleted)
    terminal_sql = outcome.result_full["terminal_sql"]
    assert outcome.result_full["sql"] == [terminal_sql]
    assert len(outcome.result_full["sql"]) == 1
    assert terminal_sql == mcp.calls[1].args["sql"]  # the node query, not the probes
