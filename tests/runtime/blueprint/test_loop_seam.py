"""Layer-1: the agent-loop seam for `runBlueprint` (runblueprint-design §2.5/§2.7).

Drives the REAL `AgentLoop` with a `ScriptedModelClient` + the real
`RunBlueprintTool` wired into the `runtime_tools` registry. Proves:
  - one `runBlueprint` call = exactly ONE `tool_calls_made`, even though the
    executor issues several inner runQuery probes (they are the tool's
    implementation, invisible to the loop's budget, §2.7);
  - a slot-resolution `askUser` → the §2.5 PAUSING-runtime-tool seam: the loop
    writes a `PauseCheckpoint` (reason "blueprint_slot", carrying blueprint_id +
    raw slot_bindings) and returns `paused_ask_user` — the SAME terminal contract
    as `askUser`, with NO trail entry persisted for the un-completed tool;
  - an advertised-but-UNWIRED `runBlueprint` → a clean `RUN_BLUEPRINT_UNAVAILABLE`
    local error (never an incoherent MCP unknown-tool denial), turn survives.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import BlueprintExecutor, ExecPaused
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
from data_agent.runtime.session.memory_store import InMemorySessionStore
from tests._blueprint_gate import expand_blueprint

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "AnnualSalary": "Nullable(Float64)"}}
)
SESSION_ID = "sess-bp-loop"
_BID = "bp-average-salary-by-department"

_AVG_SQL = (
    "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary, "
    "COUNT(DISTINCT EmployeeCode) AS headcount "
    "FROM dbpcm_warehouse.employee WHERE Department = {department} GROUP BY Department"
)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


def _detail(binds_to: bool) -> BlueprintDetail:
    slot: dict[str, Any] = {"name": "department", "type": "string", "required": True}
    if binds_to:
        slot["binds_to"] = f"{_E}.Department"
    return BlueprintDetail(
        id=_BID,
        intent="Average annual salary by department",
        slots_summary="department",
        uses=frozenset({f"{_E}.Department", f"{_E}.AnnualSalary", f"{_E}.EmployeeCode"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=[slot],
        sql_template=_AVG_SQL,
        result_grain=["Department"],
    )


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "runBlueprint", "description": "", "parameters": {}}]


def _loop(
    model: ScriptedModelClient, *, runtime_tools: dict | None = None
) -> tuple[AgentLoop, InMemorySessionStore]:
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(FakeMCPClient(), CATALOG)  # loop-level dispatcher (unused here)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools=runtime_tools or {},
    )
    return loop, store


def _run_blueprint_tool_with_real_executor() -> tuple[RunBlueprintTool, FakeMCPClient]:
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["Department"], "rows": [["Sales"]], "row_count": 1, "truncated": False},
                {"columns": ["department", "avg_salary", "headcount"], "rows": [["Sales", 60000.0, 4]], "row_count": 1, "truncated": False},
                {"columns": ["__bp_n", "__bp_d"], "rows": [[1, 1]], "row_count": 1, "truncated": False},
            ]
        }
    )
    index = FakeVectorIndex()
    index.add_detail(_detail(binds_to=True))
    executor = BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)
    return RunBlueprintTool(executor=executor), mcp


# ---------------------------------------------------------------------------
# §2.7 — one runBlueprint = one tool_calls_made (inner probes not double-counted)
# ---------------------------------------------------------------------------


async def test_one_run_blueprint_is_one_tool_call_despite_inner_probes() -> None:
    tool, mcp = _run_blueprint_tool_with_real_executor()
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {"department": "Sales"}})
                ]
            ),
            ModelTurnResult(assistant_text="The average salary in Sales is $60,000."),
        ]
    )
    loop, store = _loop(model, runtime_tools={"runBlueprint": tool})
    # The getBlueprint-before-runBlueprint gate: this turn must already hold a
    # successful expansion of the blueprint the model is about to run. Seeded as
    # the real trail entry the gate reads, so the predicate under test is the
    # production one (tests/_blueprint_gate.py).
    await expand_blueprint(store, SESSION_ID, _BID)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="avg salary in Sales?")

    assert outcome.status == "done"
    assert outcome.tool_calls_made == 1  # ONE model-facing call...
    assert len(mcp.calls) == 3  # ...despite THREE inner runQuery probes
    trail = await store.load_trail(SESSION_ID)
    # The seeded getBlueprint leads; the point of the assertion is unchanged —
    # ONE runBlueprint entry despite three inner runQuery probes.
    assert [e.tool_name for e in trail] == ["getBlueprint", "runBlueprint"]
    assert trail[0].status == "ok"


# ---------------------------------------------------------------------------
# §2.5 — the pausing-runtime-tool seam (slot-resolution askUser)
# ---------------------------------------------------------------------------


class _PausingTool:
    tool_name = "runBlueprint"

    async def run(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials, turn=None
    ):
        from data_agent.runtime.dispatch.tool_dispatcher import ToolPause, ToolResult

        paused = ExecPaused(
            reason="blueprint_slot",
            pending_question={"question": "Which department did you mean?", "options": ["Sales", "Support"]},
            blueprint_id=model_args["id"],
            slot_bindings_json='{"department": "S"}',
        )
        return ToolResult(
            status="ok",
            tool_name="runBlueprint",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=None,
            result_full=None,
            pause=ToolPause(
                reason=paused.reason,
                pending_question=paused.pending_question,
                blueprint_id=paused.blueprint_id,
                slot_bindings_json=paused.slot_bindings_json,
            ),
        )


async def test_slot_askuser_pauses_the_turn_via_the_seam() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {"department": "S"}})
                ]
            ),
        ]
    )
    loop, store = _loop(model, runtime_tools={"runBlueprint": _PausingTool()})
    # The getBlueprint-before-runBlueprint gate: this turn must already hold a
    # successful expansion of the blueprint the model is about to run. Seeded as
    # the real trail entry the gate reads, so the predicate under test is the
    # production one (tests/_blueprint_gate.py).
    await expand_blueprint(store, SESSION_ID, _BID)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="avg salary?")

    assert outcome.status == "paused_ask_user"
    assert outcome.pending_question["question"] == "Which department did you mean?"
    assert outcome.tool_calls_made == 0  # the un-completed tool did not count

    doc = await store.get_or_create_session(SESSION_ID)
    cp = doc.pause_checkpoint
    assert cp is not None
    assert cp.reason == "blueprint_slot"
    assert cp.blueprint_id == _BID
    assert cp.slot_bindings_json == '{"department": "S"}'
    assert cp.consumed is False
    # No trail entry was persisted for the paused (un-completed) tool call — the
    # seeded getBlueprint expansion is the only entry there is.
    trail = await store.load_trail(SESSION_ID)
    assert [e.tool_name for e in trail] == ["getBlueprint"]


async def test_pause_checkpoint_round_trips_blueprint_fields() -> None:
    # The additive PauseCheckpoint fields survive the store's doc (de)serialization.
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}})
                ]
            ),
        ]
    )
    loop, store = _loop(model, runtime_tools={"runBlueprint": _PausingTool()})
    # The getBlueprint-before-runBlueprint gate: this turn must already hold a
    # successful expansion of the blueprint the model is about to run. Seeded as
    # the real trail entry the gate reads, so the predicate under test is the
    # production one (tests/_blueprint_gate.py).
    await expand_blueprint(store, SESSION_ID, _BID)
    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q")

    # Force a full doc round-trip to prove the wire encoding carries the new fields.
    doc = await store.get_or_create_session(SESSION_ID)
    from data_agent.runtime.session.models import PauseCheckpoint

    reloaded = PauseCheckpoint.from_doc(doc.pause_checkpoint.to_doc())
    assert reloaded.blueprint_id == _BID
    assert reloaded.reason == "blueprint_slot"


# ---------------------------------------------------------------------------
# §6 — advertised-but-unwired runBlueprint → clean local unavailable
# ---------------------------------------------------------------------------


async def test_unwired_run_blueprint_is_clean_unavailable() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}})
                ]
            ),
            ModelTurnResult(assistant_text="I'll answer from the raw tools instead."),
        ]
    )
    loop, store = _loop(model, runtime_tools={})  # runBlueprint NOT wired

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q")

    assert outcome.status == "done"
    trail = await store.load_trail(SESSION_ID)
    assert trail[0].tool_name == "runBlueprint"
    assert trail[0].status == "error"
    assert trail[0].error_code == "RUN_BLUEPRINT_UNAVAILABLE"
