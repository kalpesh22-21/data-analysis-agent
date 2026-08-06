"""Layer-1: Slice C at the executor — an OMITTED optional slot applies its
`optional_pattern` at bind time (single-node AND DAG), instead of leaving an
unbound `{token}` that fell back to the raw loop.

Proofs:
  - single-node OMITTED optional slot (`optional_pattern: 'TRUE'`) → the node SQL
    renders `WHERE TRUE`, the grain still verifies, NO ExecFailed;
  - single-node PROVIDED optional slot → the value binds as a typed literal;
  - single-node OMITTED optional slot with NO pattern → fail-closed (raw loop);
  - DAG: the optional slot referenced in ONE node, omitted → the pattern is applied
    in THAT node only, the DAG verifies;
  - grain preserved: an omitted `department` filter returns all departments and the
    D56 grain probe still passes (`result_grain: [Department]`).
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    SLOT_INVALID_CODE,
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
_SAL_COL = f"{_E}.AnnualSalary"
_CODE_COL = f"{_E}.EmployeeCode"

CATALOG = CatalogHandle(
    {
        _E: {
            "EmployeeCode": "String",
            "Department": "Nullable(String)",
            "AnnualSalary": "Nullable(Float64)",
        }
    }
)

# A single-node aggregate GROUPED by Department (grain = [Department]) whose ONLY
# filter is the optional `{department}` slot. Omitted → `WHERE TRUE` = all depts.
_BY_DEPT_SQL = (
    "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary "
    "FROM dbpcm_warehouse.employee WHERE Department = {department} GROUP BY Department"
)


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-bp-c", jwt="jwt-secret", column_scope=scope)


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(
    *,
    slots: list[dict[str, Any]] | None = None,
    sql_template: str | None = _BY_DEPT_SQL,
    composes: list[dict[str, Any]] | None = None,
    result_grain: Any = None,
    uses: frozenset[str] = frozenset({_DEPT_COL, _SAL_COL, _CODE_COL}),
    bid: str = "bp-c",
) -> BlueprintDetail:
    return BlueprintDetail(
        id=bid,
        intent="optional-slot runtime",
        slots_summary="department",
        uses=uses,
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=slots,
        sql_template=sql_template,
        composes=composes,
        result_grain=result_grain if result_grain is not None else ["Department"],
    )


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    idx = FakeVectorIndex()
    idx.add_detail(detail)
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=idx)


_OPTIONAL_DEPT = {
    "name": "department",
    "type": "string",
    "required": False,
    "optional_pattern": "TRUE",
}


# -- single-node: omitted optional slot applies its pattern -------------------


async def test_single_node_omitted_optional_applies_pattern_and_verifies() -> None:
    detail = _detail(slots=[_OPTIONAL_DEPT])
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department", "avg_salary"], [["Sales", 1.0], ["Eng", 2.0]]),  # node
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),  # grain probe: 2 rows, 2 distinct
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-c", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["status"] == "verified"
    # Grain still checked and OK — omitting the filter returns ALL departments,
    # each a distinct grain row (D56 preserved).
    assert outcome.result_full["verify"]["grain_checked"] is True
    assert outcome.result_full["verify"]["grain_ok"] is True
    assert outcome.result_full["row_count"] == 2
    # No domain probe fired (the slot was absent) — only node + grain.
    assert len(mcp.calls) == 2
    node_sql = mcp.calls[0].args["sql"]
    assert "WHERE TRUE" in node_sql
    assert "{department}" not in node_sql
    assert "Department = " not in node_sql  # NOT a value substitution


async def test_single_node_provided_optional_binds_value() -> None:
    detail = _detail(slots=[_OPTIONAL_DEPT])
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department", "avg_salary"], [["Sales", 1.0]]),  # node
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),  # grain probe
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-c", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    node_sql = mcp.calls[0].args["sql"]
    assert "Department = 'Sales'" in node_sql
    assert "WHERE TRUE" not in node_sql


async def test_single_node_omitted_optional_period_range_applies_to_both_tokens() -> None:
    # A period_range slot occupies `{window_start}`/`{window_end}` (two predicates).
    # Omitted → BOTH arms become the pattern (`TRUE AND TRUE`) — the fix keys the
    # pattern by `slot_token_names`, not the bare slot name (else it silently fell to
    # the raw loop because `window` alone is never a referenced token).
    sql = (
        "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary "
        "FROM dbpcm_warehouse.employee "
        "WHERE AnnualSalary >= {window_start} AND AnnualSalary < {window_end} "
        "GROUP BY Department"
    )
    detail = _detail(
        slots=[{"name": "window", "type": "period_range", "required": False, "optional_pattern": "TRUE"}],
        sql_template=sql,
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department", "avg_salary"], [["Sales", 1.0], ["Eng", 2.0]]),  # node
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),  # grain probe
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-c", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    node_sql = mcp.calls[0].args["sql"]
    assert "WHERE TRUE AND TRUE" in node_sql
    assert "{window_start}" not in node_sql and "{window_end}" not in node_sql


async def test_single_node_omitted_optional_without_pattern_fails_closed() -> None:
    # An optional slot with NO optional_pattern, omitted, whose token IS referenced
    # → the {token} stays unbound → SLOT_INVALID (retryable) → raw-loop fallback.
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": False}]  # no pattern
    )
    mcp = FakeMCPClient(scripted={"runQuery": []})  # nothing may dispatch
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-c", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert outcome.retryable is True
    assert mcp.calls == []  # the half-bound query was NEVER dispatched


# -- DAG: pattern applied only in the referencing node -----------------------

_COMPANY_AVG_SQL = "SELECT AVG(AnnualSalary) AS company_avg FROM dbpcm_warehouse.employee"
_ABOVE_AVG_FILTERED_SQL = (
    "SELECT Department AS department FROM dbpcm_warehouse.employee "
    "WHERE Department = {department} GROUP BY Department "
    "HAVING AVG(AnnualSalary) > {company_avg}"
)


def _dag_detail() -> BlueprintDetail:
    return _detail(
        sql_template=None,
        slots=[_OPTIONAL_DEPT],
        composes=[
            {"order": 0, "output": {"company_avg": "scalar"}, "sql_template": _COMPANY_AVG_SQL},
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"company_avg": "$0.company_avg"},
                "sql_template": _ABOVE_AVG_FILTERED_SQL,
                "output": {},
            },
        ],
        result_grain=["Department"],
        bid="bp-c-dag",
    )


async def test_dag_omitted_optional_applies_pattern_in_referencing_node_only() -> None:
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["company_avg"], [[55000.0]]),          # node 0 scalar
                _rq(["department"], [["Sales"], ["Eng"]]),  # node 1 filtered table
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),        # grain probe
            ]
        }
    )
    idx = FakeVectorIndex()
    idx.add_detail(_dag_detail())
    executor = BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=idx)

    outcome = await executor.execute(
        blueprint_id="bp-c-dag", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["status"] == "verified"
    assert outcome.result_full["row_count"] == 2
    # node 0 never referenced {department} — untouched by the pattern.
    node0_sql = mcp.calls[0].args["sql"]
    assert "TRUE" not in node0_sql.upper().replace("ANNUALSALARY", "")
    # node 1's `Department = {department}` predicate was replaced by the pattern;
    # the consumed scalar `{company_avg}` still bound normally.
    node1_sql = mcp.calls[1].args["sql"]
    assert "WHERE TRUE" in node1_sql
    assert "{department}" not in node1_sql
    assert "55000.0" in node1_sql  # the scalar consume still bound


async def test_dag_provided_optional_binds_value_in_node() -> None:
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["company_avg"], [[55000.0]]),
                _rq(["department"], [["Sales"]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    idx = FakeVectorIndex()
    idx.add_detail(_dag_detail())
    executor = BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=idx)

    outcome = await executor.execute(
        blueprint_id="bp-c-dag", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    node1_sql = mcp.calls[1].args["sql"]
    assert "Department = 'Sales'" in node1_sql
    assert "WHERE TRUE" not in node1_sql
