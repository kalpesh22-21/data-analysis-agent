"""QA6 Layer-1: D45 mid-DAG pause/resume durability under HOSTILE checkpoints.

runblueprint-design §2.5: resume is stateless — everything to continue lives in
the checkpoint the loop passes back, so a fresh process resumes identically. This
suite attacks the resume re-entry (`BlueprintExecutor.resume`) with corrupted /
partial / tampered checkpoint state: it must fail-CLOSED (raw loop) or degrade
SAFELY (re-run the read-only DAG), never crash, and a tampered scalar carried in
the checkpoint must still bind as an escaped literal (never SQL).

All fakes.
"""

from __future__ import annotations

import json
from typing import Any

import sqlglot
from sqlglot import exp

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecPaused,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "AnnualSalary": "Nullable(Float64)"}}
)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-qa6-pr", jwt="jwt", column_scope=frozenset())


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(composes: list[dict[str, Any]]) -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-pr",
        intent="pause/resume probe",
        slots_summary="",
        uses=frozenset({f"{_E}.Department", f"{_E}.AnnualSalary", f"{_E}.EmployeeCode"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        composes=composes,
        result_grain=["Department"],
    )


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(detail)
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)


def _approval_detail() -> BlueprintDetail:
    return _detail(
        [
            {"order": 0, "output": {"n": "scalar"}, "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "node_kind": "approval",
                "feeds_from": [0],
                "requires_approval": {"prompt": "Proceed?"},
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ]
    )


# ---------------------------------------------------------------------------
# Corrupted / partial checkpoint → SAFE degrade, never a crash.
# ---------------------------------------------------------------------------


async def test_resume_with_malformed_completed_nodes_json_does_not_crash() -> None:
    # Garbage completed-nodes payload → rehydrate to empty → the read-only DAG
    # re-runs from the top (idempotent, §2.5), completing cleanly, never crashing.
    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["n"], [[42]]),                          # node 0 re-runs (empty rehydrate)
                _rq(["department"], [["Sales"], ["Eng"]]),   # node 1 (approved)
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),         # grain probe
            ]
        }
    )
    outcome = await _executor(resume_mcp, _approval_detail()).resume(
        blueprint_id="bp-pr",
        slot_bindings={},
        completed_nodes_json="}{ not json at all",
        awaiting_node=1,
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecCompleted)


async def test_resume_with_missing_completed_nodes_does_not_crash() -> None:
    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["n"], [[42]]),
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    outcome = await _executor(resume_mcp, _approval_detail()).resume(
        blueprint_id="bp-pr",
        slot_bindings={},
        completed_nodes_json=None,
        awaiting_node=1,
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecCompleted)


async def test_resume_with_bad_awaiting_node_repauses_not_crash() -> None:
    # An awaiting_node that matches no approval node → the approval gate is not
    # satisfied → re-pause (never crash, never silently run the gated query).
    resume_mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(resume_mcp, _approval_detail()).resume(
        blueprint_id="bp-pr",
        slot_bindings={},
        completed_nodes_json='[{"order": 0, "output": {"n": 42}}]',
        awaiting_node=99,  # no such approval node
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecPaused)
    assert outcome.reason == "blueprint_approval"
    # The gated query never dispatched on a mismatched awaiting_node.
    assert all("GROUP BY Department" not in c.args["sql"] for c in resume_mcp.calls)


async def test_resume_single_node_checkpoint_is_fail_closed() -> None:
    # A checkpoint pointing a mid-DAG resume at a SINGLE-node blueprint is corrupt
    # (a single node has no mid-DAG pause) → UNSUPPORTED (raw loop), never a crash.
    single = BlueprintDetail(
        id="bp-pr",
        intent="single",
        slots_summary="",
        uses=frozenset({f"{_E}.Department"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        sql_template="SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
        result_grain=["Department"],
    )
    resume_mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(resume_mcp, single).resume(
        blueprint_id="bp-pr",
        slot_bindings={},
        completed_nodes_json='[{"order": 0, "output": {}}]',
        awaiting_node=1,
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecFailed)
    assert resume_mcp.calls == []


# ---------------------------------------------------------------------------
# Tampered checkpoint SCALAR → still binds as an escaped literal, never SQL.
# ---------------------------------------------------------------------------


def _approval_consume_detail() -> BlueprintDetail:
    # node 1 is an APPROVAL node that ALSO queries, consuming node 0's scalar.
    return _detail(
        [
            {
                "order": 0,
                "output": {"tok": "scalar"},
                "sql_template": "SELECT Department AS tok FROM dbpcm_warehouse.employee LIMIT 1",
            },
            {
                "order": 1,
                "node_kind": "approval",
                "feeds_from": [0],
                "requires_approval": {"prompt": "Proceed?"},
                "consumes": {"tok": "$0.tok"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "WHERE Department = {tok} GROUP BY Department"
                ),
                "output": {},
            },
        ]
    )


async def test_tampered_checkpoint_scalar_binds_as_escaped_literal() -> None:
    payload = "evil'); DROP TABLE employee;--"
    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"]]),      # node 1 (approved, consumes the scalar)
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),   # grain probe
            ]
        }
    )
    outcome = await _executor(resume_mcp, _approval_consume_detail()).resume(
        blueprint_id="bp-pr",
        slot_bindings={},
        # A TAMPERED completed-nodes scalar (as if an attacker edited the store).
        completed_nodes_json=json.dumps([{"order": 0, "output": {"tok": payload}}]),
        awaiting_node=1,
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecCompleted)
    node1_sql = resume_mcp.calls[0].args["sql"]
    # The tampered scalar bound as ONE string literal — the injection is inert data.
    tree = sqlglot.parse_one(node1_sql, dialect="clickhouse")
    literals = [n.this for n in tree.walk() if isinstance(n, exp.Literal) and n.args.get("is_string")]
    assert payload in literals
    assert len(sqlglot.parse(node1_sql, dialect="clickhouse")) == 1  # no smuggled 2nd statement


async def test_fresh_executor_resume_completed_node_not_rerun() -> None:
    # Restart durability: a brand-new executor resumes from checkpoint state ONLY;
    # the completed node (0) is NOT re-run — only node 1 + the grain probe.
    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    outcome = await _executor(resume_mcp, _approval_detail()).resume(
        blueprint_id="bp-pr",
        slot_bindings={},
        completed_nodes_json='[{"order": 0, "output": {"n": 42}}]',
        awaiting_node=1,
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecCompleted)
    assert len(resume_mcp.calls) == 2  # node 0 NOT re-run
    assert "count()" not in resume_mcp.calls[0].args["sql"].lower()
