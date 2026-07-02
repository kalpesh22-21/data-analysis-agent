"""Layer-1: the table-intermediate materialize-and-join path (table-intermediate
design §2.2/§Q2, Slice 2). All fakes, no infra.

Proves the load-bearing properties the design pins:
  - a table-output node consumed downstream is MATERIALIZED to a session-scoped
    scratch table via the D93 side-channel (native rows, session-derived name);
  - the consumer's `scratch.<placeholder>` FROM/JOIN token is AST-rewritten to the
    RETURNED scratch identifier (runtime-controlled, never model text / a cell);
  - an adversarial upstream cell rides as DATA, never becomes SQL;
  - provenance = warehouse columns ∪ (scratch columns EXCLUDED, D69/OQ-4);
  - an oversized intermediate + no scratch_client both fail closed to the raw loop;
  - the credential boundary: only jwt + session_id reach the scratch transport.
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
from data_agent.runtime.mcp.scratch_client import FakeScratchClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_W = "dbpcm_warehouse"
_EMP = f"{_W}.employee"
_PAY = f"{_W}.payroll"

CATALOG = CatalogHandle(
    {
        _EMP: {
            "EmployeeCode": "String",
            "Department": "Nullable(String)",
        },
        _PAY: {
            "EmployeeCode": "String",
            "RegisterType": "Nullable(String)",
            "Amount": "Nullable(Float64)",
        },
    }
)

# An underscore-free, identifier-safe sid (the Slice-2 contract) so the fake's
# `scratch.s_<sid>_bp_<n>` name parses AND passes the tightened D64 read gate.
_SID = "sdagtest"

_USES = frozenset(
    {
        f"{_PAY}.EmployeeCode",
        f"{_PAY}.RegisterType",
        f"{_PAY}.Amount",
        f"{_EMP}.EmployeeCode",
        f"{_EMP}.Department",
    }
)

_PRODUCER_SQL = (
    "SELECT toString(p.EmployeeCode) AS EmployeeCode, toFloat64(SUM(p.Amount)) AS earnings "
    "FROM dbpcm_warehouse.payroll AS p WHERE p.RegisterType = 'EARN' GROUP BY p.EmployeeCode"
)
_CONSUMER_SQL = (
    "SELECT e.Department AS department, SUM(x.earnings) AS total_earnings "
    "FROM scratch.emp_earnings AS x "
    "JOIN dbpcm_warehouse.employee AS e ON e.EmployeeCode = x.EmployeeCode "
    "WHERE e.Department = {department} GROUP BY e.Department"
)


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=_SID, jwt="jwt-secret", column_scope=scope)


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail() -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-table",
        intent="Total earnings by department via a scratch join",
        slots_summary="department",
        uses=_USES,
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=[
            {
                "name": "department",
                "type": "string",
                "required": True,
                "binds_to": f"{_EMP}.Department",
            }
        ],
        uses_rules=None,
        sql_template=None,
        composes=[
            {"order": 0, "output": {"emp_earnings": "table"}, "sql_template": _PRODUCER_SQL},
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"emp_earnings": "$0"},
                "output": {},
                "sql_template": _CONSUMER_SQL,
            },
        ],
        result_grain=["Department"],
    )


def _executor(
    mcp: FakeMCPClient,
    *,
    scratch_client: Any,
    scratch_max_rows: int = 10_000,
) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(_detail())
    return BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG),
        vector_index=index,
        scratch_client=scratch_client,
        scratch_max_rows=scratch_max_rows,
    )


def _happy_mcp() -> FakeMCPClient:
    return FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"], ["Eng"]]),        # department domain probe
                _rq(["EmployeeCode", "earnings"], [["1001", 100.0], ["1002", 200.0]]),  # node 0
                _rq(["department", "total_earnings"], [["Sales", 300.0]]),  # node 1 (JOIN)
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),               # grain probe
            ]
        }
    )


async def test_table_intermediate_materializes_and_join_is_rewritten() -> None:
    mcp = _happy_mcp()
    scratch = FakeScratchClient()
    executor = _executor(mcp, scratch_client=scratch)

    outcome = await executor.execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )

    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["status"] == "verified"

    # Exactly one materialize, with the producer's native rows + inferred types.
    mats = [c for c in scratch.calls if c.op == "materialize"]
    assert len(mats) == 1
    mat = mats[0]
    assert mat.rows == [["1001", 100.0], ["1002", 200.0]]  # native data, not SQL
    assert mat.columns == [
        {"name": "EmployeeCode", "type": "String"},   # toString join key → String
        {"name": "earnings", "type": "Float64"},        # toFloat64 measure → Float64
    ]

    # The consumer's SQL was rewritten to the RETURNED scratch identifier — the
    # placeholder `scratch.emp_earnings` is gone, the real materialized name is in.
    consumer_sql = mcp.calls[2].args["sql"]
    assert "emp_earnings" not in consumer_sql
    assert f"s_{_SID}_bp_" in consumer_sql
    assert "scratch" in consumer_sql
    assert "{department}" not in consumer_sql and "'Sales'" in consumer_sql


async def test_materialize_rides_only_jwt_and_session_id_no_ch_credential() -> None:
    scratch = FakeScratchClient()
    executor = _executor(_happy_mcp(), scratch_client=scratch)
    await executor.execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    mat = next(c for c in scratch.calls if c.op == "materialize")
    # The runtime forwards ONLY the JWT + the session-derived name (invariant #8):
    assert mat.jwt == "jwt-secret"
    assert mat.session_id == _SID
    # The returned table name is session-scoped (D64 read gate will accept it).
    assert mat.table == f"scratch.s_{_SID}_bp_{1:032x}"


async def test_adversarial_cell_is_data_never_sql() -> None:
    hostile = "x'); DROP TABLE payroll; --"
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),
                _rq(["EmployeeCode", "earnings"], [[hostile, 100.0]]),
                _rq(["department", "total_earnings"], [["Sales", 100.0]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    scratch = FakeScratchClient()
    executor = _executor(mcp, scratch_client=scratch)
    outcome = await executor.execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    # The hostile string went to materialize as a native row cell (DATA)…
    mat = next(c for c in scratch.calls if c.op == "materialize")
    assert mat.rows == [[hostile, 100.0]]
    # …and NEVER appears in the consumer SQL text (it is a scratch column value).
    assert "DROP TABLE" not in mcp.calls[2].args["sql"]


async def test_provenance_is_warehouse_only_scratch_columns_excluded() -> None:
    scratch = FakeScratchClient()
    executor = _executor(_happy_mcp(), scratch_client=scratch)
    outcome = await executor.execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert outcome.provenance is not None
    # Warehouse columns from BOTH nodes are present…
    assert (_PAY, "Amount") in outcome.provenance
    assert (_EMP, "Department") in outcome.provenance
    assert (_EMP, "EmployeeCode") in outcome.provenance
    # …and NO scratch column leaked into the union (D69/OQ-4 exclusion).
    assert not any(db.startswith("scratch.") for db, _ in outcome.provenance)


async def test_no_scratch_client_wired_is_unsupported() -> None:
    outcome = await _executor(_happy_mcp(), scratch_client=None).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE


async def test_truncated_intermediate_fails_closed_before_materialize() -> None:
    # The MCP hard-caps runQuery rows regardless of caller LIMIT, so a >cap producer
    # arrives PRE-TRUNCATED (truncated=True) with the surviving rows well under the
    # row cap. Materializing it would build a PARTIAL scratch table → the JOIN would
    # under-count → a silently wrong "verified" answer. It MUST fail closed to the
    # raw loop BEFORE any materialize (the row-cap guard alone never sees this).
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),  # department domain probe
                {  # node 0 producer — truncated by the MCP row cap, rows well under cap
                    "columns": ["EmployeeCode", "earnings"],
                    "rows": [["1001", 100.0], ["1002", 200.0]],
                    "row_count": 2,
                    "truncated": True,
                },
            ]
        }
    )
    scratch = FakeScratchClient()
    executor = _executor(mcp, scratch_client=scratch)
    outcome = await executor.execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    # The partial intermediate is NEVER materialized (no partial scratch table).
    assert not any(c.op == "materialize" for c in scratch.calls)


async def test_oversized_intermediate_fails_closed_to_raw_loop() -> None:
    scratch = FakeScratchClient()
    executor = _executor(_happy_mcp(), scratch_client=scratch, scratch_max_rows=1)
    outcome = await executor.execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    # The over-cap intermediate is never materialized (no runaway POST).
    assert not any(c.op == "materialize" for c in scratch.calls)
