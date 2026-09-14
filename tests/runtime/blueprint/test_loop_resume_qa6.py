"""QA6 Layer-1: loop-level pause/resume budget + trail invariants (D45/§2.5/§2.7).

Drives the real `AgentLoop` + `RunBlueprintTool` + `BlueprintExecutor` with a
scripted model and the in-memory store. Pins the accounting the design promises:
  - the PAUSED runBlueprint call writes NO trail entry and is NOT counted as a
    tool call (the loop returns before `tool_calls_made += 1` / `append_trail`);
  - completion (on resume) writes EXACTLY ONE `runBlueprint` trail entry;
  - the inner per-node `runQuery` calls never surface as model-facing trail
    entries (they are the tool's implementation, §2.7);
  - a double-resume is the D45 exactly-once `AlreadyConsumedError`.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import BlueprintExecutor
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import AlreadyConsumedError, InMemorySessionStore
from tests._blueprint_gate import expand_blueprint
from tests.runtime.final_answer import final_answer

pytestmark = pytest.mark.usefixtures("answer_tools", "blueprint_consulted")

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
SESSION_ID = "sess-qa6"
_BID = "bp-flag"


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


def _prose_resume_script(text: str) -> list[ModelTurnResult]:
    """Propose prose, then select the verified resumed blueprint. Two consumed rounds prove the persisted multi-row result reached shape review."""
    return [
        final_answer(assistant_text=text),
        ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id="select_resumed",
                    name="answerWithTable",
                    arguments={"answer": text, "blueprint_id": _BID},
                )
            ]
        ),
    ]


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail() -> BlueprintDetail:
    return BlueprintDetail(
        id=_BID,
        intent="Flag departments",
        slots_summary="",
        uses=frozenset({f"{_E}.Department", f"{_E}.AnnualSalary", f"{_E}.EmployeeCode"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        composes=[
            {
                "order": 0,
                "output": {"n": "scalar"},
                "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee",
            },
            {
                "order": 1,
                "node_kind": "approval",
                "feeds_from": [0],
                "requires_approval": {"prompt": "Proceed?"},
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ],
        result_grain=["Department"],
    )


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "runBlueprint", "description": "", "parameters": {}}]


def _make_loop(
    store: InMemorySessionStore, model: ScriptedModelClient, mcp: FakeMCPClient
) -> AgentLoop:
    index = FakeVectorIndex()
    index.add_detail(_detail())
    executor = BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)
    tool = RunBlueprintTool(executor=executor)
    return AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={"runBlueprint": tool},
        blueprint_executor=executor,
    )


def _run_model() -> ScriptedModelClient:
    return ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}}
                    )
                ]
            )
        ]
    )


async def test_paused_call_records_pause_without_counting_completed_execution() -> None:
    store = InMemorySessionStore()
    run_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})
    loop = _make_loop(store, _run_model(), run_mcp)
    # The getBlueprint-before-runBlueprint gate: the turn must already hold a
    # successful expansion of the blueprint about to run (tests/_blueprint_gate.py).
    await expand_blueprint(store, SESSION_ID, _BID)

    paused = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="flag depts")

    assert paused.status == "paused_ask_user"
    # The paused runBlueprint call is NOT counted (loop returns before += 1).
    assert paused.tool_calls_made == 0
    # Record the unfinished call explicitly for conversation replay.
    trail = await store.load_trail(SESSION_ID)
    assert [e.error_code for e in trail if e.tool_name == "runBlueprint"] == ["TOOL_PAUSED"]
    # The inner node-0 runQuery ran but is the tool's implementation — never a
    # model-facing trail entry.
    assert [e for e in trail if e.tool_name == "runQuery"] == []


async def test_completion_on_resume_writes_exactly_one_runblueprint_entry() -> None:
    store = InMemorySessionStore()
    run_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})
    # The gate (tests/_blueprint_gate.py) — the expansion precedes the run.
    await expand_blueprint(store, SESSION_ID, _BID)
    await _make_loop(store, _run_model(), run_mcp).run(
        session_id=SESSION_ID, credentials=_creds(), user_message="flag depts"
    )

    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    resume_model = ScriptedModelClient(_prose_resume_script("Flagged 2 departments."))
    done = await _make_loop(store, resume_model, resume_mcp).resume(
        session_id=SESSION_ID, credentials=_creds(), answer="approve"
    )
    assert done.status == "done"
    # BOTH scripted turns were consumed, so `_prose_resume_script`'s claim is real:
    # the resumed blueprint's two rows DID reach the answer-shape gate through the
    # persisted trail, and the first prose finish was refused. `ScriptedModelClient`
    # does not raise on leftover turns, so without this the helper's second turn
    # could go unused and the docstring would quietly become fiction.
    assert resume_model.calls_made == 2

    trail = await store.load_trail(SESSION_ID)
    bp_entries = [e for e in trail if e.tool_name == "runBlueprint" and e.status == "ok"]
    # EXACTLY ONE runBlueprint entry across the whole pause+resume turn — the
    # completion, not the pause (§2.7: the DAG is one tool call, counted once).
    assert len(bp_entries) == 1
    assert bp_entries[0].status == "ok"
    # The inner grain-probe / node runQuery calls never leak into the trail.
    assert [e for e in trail if e.tool_name == "runQuery"] == []


async def test_double_resume_is_exactly_once() -> None:
    store = InMemorySessionStore()
    run_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})
    # The gate (tests/_blueprint_gate.py) — the expansion precedes the run.
    await expand_blueprint(store, SESSION_ID, _BID)
    await _make_loop(store, _run_model(), run_mcp).run(
        session_id=SESSION_ID, credentials=_creds(), user_message="flag depts"
    )

    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    loop2 = _make_loop(store, ScriptedModelClient(_prose_resume_script("done")), resume_mcp)
    await loop2.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")
    n_calls_after_first = len(resume_mcp.calls)

    # A second resume is refused (CAS-consumed) — the completed DAG is not re-run.
    with pytest.raises(AlreadyConsumedError):
        await loop2.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")
    assert len(resume_mcp.calls) == n_calls_after_first  # no extra dispatch
