"""Layer-1: the agent-loop mid-DAG approval pause/resume seam (D45, §2.5).

Drives the REAL `AgentLoop` + real `RunBlueprintTool` + real `BlueprintExecutor`
with a `ScriptedModelClient` and the `InMemorySessionStore` fake. Proves:
  - an approval node pauses the turn, writing a `PauseCheckpoint` that carries the
    mid-DAG state (`blueprint_id`, `awaiting_node`, the completed SCALAR outputs);
  - `AgentLoop.resume` RE-ENTERS the executor at `awaiting_node` — completed nodes
    never re-run (exactly-once) — and the turn ends with the model narrating;
  - **restart durability**: a BRAND-NEW `AgentLoop` + executor (a fresh process,
    sharing only the persisted session doc) resumes identically from the store;
  - the CAS-consume makes a double-resume the D45 `AlreadyConsumedError`.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import BlueprintExecutor
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import AlreadyConsumedError, InMemorySessionStore

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "AnnualSalary": "Nullable(Float64)"}}
)
SESSION_ID = "sess-bp-approval"
_BID = "bp-flag-departments"


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail() -> BlueprintDetail:
    return BlueprintDetail(
        id=_BID,
        intent="Flag departments above the company average",
        slots_summary="",
        uses=frozenset({f"{_E}.Department", f"{_E}.AnnualSalary", f"{_E}.EmployeeCode"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        composes=[
            {"order": 0, "output": {"n": "scalar"}, "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "node_kind": "approval",
                "feeds_from": [0],
                "requires_approval": {"prompt": "Flag these departments — proceed?"},
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
):
    index = FakeVectorIndex()
    index.add_detail(_detail())
    executor = BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index
    )
    tool = RunBlueprintTool(executor=executor)
    from data_agent.runtime.loop.agent_loop import AgentLoop

    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={"runBlueprint": tool},
        blueprint_executor=executor,
    )
    return loop


async def test_approval_pause_then_restart_resume_completes() -> None:
    store = InMemorySessionStore()

    # --- process 1: run to the approval pause -------------------------------
    run_model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}})
                ]
            ),
        ]
    )
    run_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})  # node 0 only
    loop1 = _make_loop(store, run_model, run_mcp)

    paused = await loop1.run(session_id=SESSION_ID, credentials=_creds(), user_message="flag depts")
    assert paused.status == "paused_ask_user"
    assert paused.pending_question["show"] == {"$0.n": 42}

    cp = (await store.get_or_create_session(SESSION_ID)).pause_checkpoint
    assert cp is not None
    assert cp.reason == "blueprint_approval"
    assert cp.blueprint_id == _BID
    assert cp.awaiting_node == 1
    assert cp.consumed is False
    assert len(run_mcp.calls) == 1  # only node 0 ran before the pause

    # --- process 2 (RESTART): a fresh loop + executor, shared store only ----
    resume_model = ScriptedModelClient(
        [ModelTurnResult(assistant_text="Flagged 2 departments above the average.")]
    )
    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),   # node 1 query (approved)
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),         # grain probe
            ]
        }
    )
    loop2 = _make_loop(store, resume_model, resume_mcp)

    done = await loop2.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")
    assert done.status == "done"
    assert done.assistant_text == "Flagged 2 departments above the average."

    # node 0 was NOT re-run on resume — only node 1 + the grain probe (exactly-once).
    assert len(resume_mcp.calls) == 2
    assert "count()" not in resume_mcp.calls[0].args["sql"].lower()

    # A verified runBlueprint result landed in the trail for the model to narrate.
    trail = await store.load_trail(SESSION_ID)
    bp_entries = [e for e in trail if e.tool_name == "runBlueprint"]
    assert bp_entries and bp_entries[-1].status == "ok"


async def test_verified_resume_persists_authoritative_marker() -> None:
    """`fix/blueprint-authoritative-stop`: a blueprint that PAUSES for approval and
    then resumes to a D56-verified answer must carry the `authoritative` marker on
    the persisted trail entry — identical to a non-paused verified run. The resume
    path reuses the SAME outcome→ToolResult mapper, so the marker can't drift; a
    regression here would strand exactly the multi-round-trip turn this fix targets
    (the model would re-derive an already-verified answer)."""
    store = InMemorySessionStore()

    run_model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}})
                ]
            ),
        ]
    )
    run_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})
    loop1 = _make_loop(store, run_model, run_mcp)
    paused = await loop1.run(session_id=SESSION_ID, credentials=_creds(), user_message="flag depts")
    assert paused.status == "paused_ask_user"

    resume_model = ScriptedModelClient(
        [ModelTurnResult(assistant_text="Flagged 2 departments above the average.")]
    )
    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    loop2 = _make_loop(store, resume_model, resume_mcp)
    done = await loop2.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")
    assert done.status == "done"

    trail = await store.load_trail(SESSION_ID)
    bp_entries = [e for e in trail if e.tool_name == "runBlueprint"]
    assert bp_entries and bp_entries[-1].status == "ok"
    # The verified-resume result is flagged authoritative on the persisted entry.
    assert bp_entries[-1].authoritative is True


async def test_approval_resume_final_outcome_carries_enrichment() -> None:
    """UI Slice 1 Fix 1: a blueprint that pauses for approval and then RESUMES to a
    verified answer must carry the enriched result fields on the FINAL `done`
    outcome. The resumed loop starts a fresh window, so it seeds its enrichment
    accumulators from the completed blueprint result — without the seed, the
    verified ✓ badge + blueprint chip + SQL + table would silently vanish for
    exactly the approval-gated blueprints."""
    store = InMemorySessionStore()

    run_model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}})
                ]
            ),
        ]
    )
    run_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})
    loop1 = _make_loop(store, run_model, run_mcp)
    paused = await loop1.run(session_id=SESSION_ID, credentials=_creds(), user_message="flag depts")
    assert paused.status == "paused_ask_user"

    resume_model = ScriptedModelClient(
        [ModelTurnResult(assistant_text="Flagged 2 departments above the average.")]
    )
    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    loop2 = _make_loop(store, resume_model, resume_mcp)
    done = await loop2.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")

    assert done.status == "done"
    # The verified-blueprint enrichment SURVIVES the approval-resume boundary.
    assert done.blueprint_use == {"blueprint_id": _BID, "slots": {}}
    assert done.verification == {"passed": True, "method": "blueprint_gate", "grain_checked": True}
    assert done.sql and all(isinstance(s, str) for s in done.sql)
    assert done.result_table is not None
    # Lineage also survives: the runBlueprint trail entry is persisted before the
    # loop re-enters, so the provenance union on the resumed answer is determined.
    assert done.provenance is not None


def _make_loop_ex(
    store: InMemorySessionStore,
    model: ScriptedModelClient,
    bp_mcp: FakeMCPClient,
    loop_mcp: FakeMCPClient,
):
    """Like `_make_loop` but with a SEPARATE MCP for the loop's own dispatcher
    (the model's direct runQuery) vs. the executor's inner blueprint nodes — so a
    runQuery can succeed in the same window a runBlueprint pauses."""
    index = FakeVectorIndex()
    index.add_detail(_detail())
    executor = BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(bp_mcp, CATALOG), vector_index=index
    )
    tool = RunBlueprintTool(executor=executor)
    from data_agent.runtime.loop.agent_loop import AgentLoop

    return AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(loop_mcp, CATALOG),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={"runBlueprint": tool},
        blueprint_executor=executor,
    )


async def test_in_loop_pause_carries_partial_enrichment_from_prior_query() -> None:
    """UI Slice 1 Fix 2: when a runQuery succeeds and THEN a runBlueprint pauses
    (approval) in the same window, the `paused_ask_user` outcome surfaces the
    partial sql/result_table from the query — pause-path symmetry, so the
    runtime-tool pause flavor matches the direct `askUser` pause. blueprint_use /
    verification stay `None` (the blueprint did not produce an answer)."""
    store = InMemorySessionStore()
    query_sql = "SELECT Department FROM dbpcm_warehouse.employee"
    loop_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["Department"], [["Sales"]])]})
    bp_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})  # node 0 only

    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="q1", name="runQuery", arguments={"sql": query_sql}),
                    ToolCallRequest(id="b1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}}),
                ]
            ),
        ]
    )
    loop = _make_loop_ex(store, model, bp_mcp, loop_mcp)

    paused = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="flag depts")

    assert paused.status == "paused_ask_user"
    # The prior runQuery's partial enrichment is surfaced on the runtime-tool pause.
    assert paused.sql == [query_sql]
    assert paused.result_table is not None
    assert paused.result_table.columns == ["Department"]
    # The blueprint did not complete → no chip / badge.
    assert paused.blueprint_use is None
    assert paused.verification is None


async def test_double_resume_is_rejected_exactly_once() -> None:
    store = InMemorySessionStore()
    run_model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}})
                ]
            ),
        ]
    )
    run_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})
    loop1 = _make_loop(store, run_model, run_mcp)
    await loop1.run(session_id=SESSION_ID, credentials=_creds(), user_message="flag depts")

    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    loop2 = _make_loop(
        store,
        ScriptedModelClient([ModelTurnResult(assistant_text="done")]),
        resume_mcp,
    )
    await loop2.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")

    # The checkpoint is consumed — a second resume is the D45 exactly-once guard.
    with pytest.raises(AlreadyConsumedError):
        await loop2.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")


async def test_approval_deny_stops_cleanly_and_model_answers_from_raw_loop() -> None:
    store = InMemorySessionStore()
    run_model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}})
                ]
            ),
        ]
    )
    run_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})
    loop1 = _make_loop(store, run_model, run_mcp)
    await loop1.run(session_id=SESSION_ID, credentials=_creds(), user_message="flag depts")

    resume_mcp = FakeMCPClient(scripted={"runQuery": []})  # deny → nothing dispatches
    resume_model = ScriptedModelClient(
        [ModelTurnResult(assistant_text="Okay, I won't flag anything.")]
    )
    loop2 = _make_loop(store, resume_model, resume_mcp)

    done = await loop2.resume(session_id=SESSION_ID, credentials=_creds(), answer="deny")
    assert done.status == "done"
    assert resume_mcp.calls == []  # the gated node never ran
    trail = await store.load_trail(SESSION_ID)
    bp_entries = [e for e in trail if e.tool_name == "runBlueprint"]
    assert bp_entries and bp_entries[-1].status == "error"  # ABORTED → raw-loop fallback


class _RaisingExecutor:
    """A BlueprintExecutor stand-in whose resume() RAISES — to prove the S3/B4
    guard: after the CAS-consume, a raising executor must NOT abort the turn or
    strand the user with a consumed checkpoint."""

    async def resume(self, **_kwargs: Any):
        raise RuntimeError("neo4j blip on the authoritative re-fetch")


async def test_resume_executor_crash_is_contained_and_loop_continues() -> None:
    store = InMemorySessionStore()
    run_model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}})
                ]
            ),
        ]
    )
    run_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})
    loop1 = _make_loop(store, run_model, run_mcp)
    await loop1.run(session_id=SESSION_ID, credentials=_creds(), user_message="flag depts")

    # Restart with a RAISING executor + a model that answers from the raw loop.
    from data_agent.runtime.context.assembly import ContextAssembler
    from data_agent.runtime.loop.agent_loop import AgentLoop

    resume_model = ScriptedModelClient(
        [ModelTurnResult(assistant_text="The fast path hit an error; here's a raw-loop answer.")]
    )
    loop2 = AgentLoop(
        model_client=resume_model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={},
        blueprint_executor=_RaisingExecutor(),
    )

    # The checkpoint was CAS-consumed; the raising executor is contained → the turn
    # completes (the model answers), never aborts.
    done = await loop2.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve")
    assert done.status == "done"
    trail = await store.load_trail(SESSION_ID)
    bp_entries = [e for e in trail if e.tool_name == "runBlueprint"]
    assert bp_entries and bp_entries[-1].status == "error"
    assert bp_entries[-1].error_code == "RUNTIME_TOOL_INTERNAL_ERROR"
