"""Call-time intent tagging — the LOOP half (Release 1, 03/04 as amended).

The tool half (resolution, 04's conditions, the reuse escape hatch) lives in
`tests/runtime/composite/test_intent_tagging.py`. This file covers what only the
loop can be wrong about:

  - ⚠ THE STRIP. `serves_intent` is a runtime concept. `runQuery` and
    `getTableSchema` are dispatched to the live MCP server, which rejects an
    argument its own schema does not declare, and `runBlueprint`'s executor
    validates its arguments too. If the loop forgets to remove it, the feature
    breaks every substantive call against the real server while every unit test
    stays green.
  - The tag is validated against the LIVE state and persisted on the entry.
  - An unknown tag drops the TAG, never the WORK.
  - (The pause/resume carry — where no trail entry exists at pause time — is in
    `test_release1_seams_qa.py`, beside the other seam tests that drive the REAL
    `BlueprintExecutor`.)
  - THE LIVE FAILURE, REPLAYED: three intents, three tagged blueprint runs in
    round 1, ONE `updateAnalysisState` closing all three in round 2 with no
    evidence fields. That exact shape produced 9 rejections and 0 completions on
    the citation path.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.loop.agent_loop import AgentLoop, TurnContext
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, live_analysis_state
from tests._blueprint_gate import expand_blueprint

SESSION_ID = "sess-tagging-loop"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return []


class _RecordingBlueprintTool:
    """A stand-in for `RunBlueprintTool` that RECORDS the arguments it is handed.

    The real executor is exercised in `test_release1_seams_qa.py`; what is under
    test here is the loop's dispatch boundary, and the only way to assert "the
    executor never sees the tag" is to look at what the runtime tool was called
    with. Returns an AUTHORITATIVE result, so 04's condition 4 is satisfied and a
    tagged blueprint really can close an intent.
    """

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        self.seen.append(dict(model_args))
        return ToolResult(
            status="ok",
            tool_name="runBlueprint",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=ResultPreview(
                columns=["department", "n"],
                row_count=2,
                truncated=False,
                preview_rows=[["Sales", 3], ["Eng", 5]],
            ),
            result_full={"blueprint_id": model_args.get("id"), "rows": []},
            authoritative=True,
        )


def _build(
    turns: list[ModelTurnResult],
    *,
    mcp: FakeMCPClient | None = None,
    blueprint_tool: _RecordingBlueprintTool | None = None,
    store: InMemorySessionStore | None = None,
) -> tuple[AgentLoop, InMemorySessionStore, list[tuple[str, dict[str, Any]]], FakeMCPClient]:
    store = store if store is not None else InMemorySessionStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, dict(payload)))

    mcp = mcp or FakeMCPClient()
    runtime_tools: dict[str, Any] = {
        STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe),
        ANSWER: AnswerWithTableTool(),
    }
    if blueprint_tool is not None:
        runtime_tools["runBlueprint"] = blueprint_tool
    loop = AgentLoop(
        model_client=ScriptedModelClient(turns),
        tool_dispatcher=ToolDispatcher(mcp, CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=_observe,
        runtime_tools=runtime_tools,
    )
    return loop, store, events, mcp


def _init_call(call_id: str, *descriptions: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name=STATE,
        arguments={"intents": [{"description": d} for d in descriptions]},
    )


def _update_call(call_id: str, *updates: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=STATE, arguments={"intents": list(updates)})


def _tagged_query(call_id: str, intent_id: str | None, sql: str = "SELECT 1") -> ToolCallRequest:
    args: dict[str, Any] = {"sql": sql}
    if intent_id is not None:
        args["serves_intent"] = intent_id
    return ToolCallRequest(id=call_id, name="runQuery", arguments=args)


def _tagged_schema(call_id: str, intent_id: str, table: str = "employee") -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="getTableSchema",
        arguments={
            "database": "dbpcm_warehouse",
            "table": table,
            "serves_intent": intent_id,
        },
    )


def _tagged_blueprint(call_id: str, blueprint_id: str, intent_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="runBlueprint",
        arguments={
            "id": blueprint_id,
            "slot_bindings": {},
            "serves_intent": intent_id,
        },
    )


def _rows() -> dict[str, Any]:
    return {"columns": ["x"], "rows": [[1]], "row_count": 1, "truncated": False}


def _schema() -> dict[str, Any]:
    return {"database": "dbpcm_warehouse", "table": "employee", "columns": ["EmployeeCode"]}


def _events(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


async def _trail(store: InMemorySessionStore) -> dict[str, Any]:
    return {entry.tool_call_id: entry for entry in await store.load_trail(SESSION_ID)}


# ---------------------------------------------------------------------------
# ⚠ The strip
# ---------------------------------------------------------------------------


async def test_the_tag_never_reaches_the_mcp_for_run_query_or_get_table_schema() -> None:
    """THE ONE THAT BREAKS THE LIVE SERVER IF MISSED. Both tools are dispatched to
    the MCP, which rejects an unknown argument — so a tag left on the arguments
    turns every tagged call into a denial, on the release's primary route, while
    every store-level test still passes."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()], "getTableSchema": [_schema()]})
    loop, store, _events_, mcp = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "the employee schema"),
                    _tagged_query("q1", "i1"),
                    _tagged_schema("m1", "i2"),
                ],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _update_call(
                        "s2",
                        {"intent_id": "i1", "status": "completed"},
                        {"intent_id": "i2", "status": "completed"},
                    )
                ],
            ),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=mcp,
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    dispatched = {call.tool_name: call.args for call in mcp.calls}
    assert dispatched["runQuery"] == {"sql": "SELECT 1"}
    assert dispatched["getTableSchema"] == {"database": "dbpcm_warehouse", "table": "employee"}
    assert all("serves_intent" not in call.args for call in mcp.calls)

    # ...and the tag is on the ENTRY instead, where the state tool reads it.
    trail = await _trail(store)
    assert trail["q1"].serves_intent == "i1"
    assert trail["m1"].serves_intent == "i2"
    assert "serves_intent" not in trail["q1"].args
    assert "serves_intent" not in trail["m1"].args

    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.status, i.evidence_tool_call_id) for i in state.intents] == [
        ("completed", "q1"),
        ("completed", "m1"),
    ]


async def test_the_tag_never_reaches_the_blueprint_executor() -> None:
    """`runBlueprint` is a RUNTIME tool, so it is intercepted before the dispatcher
    — a strip that only covered the MCP path would leave the tag on its arguments."""
    blueprint = _RecordingBlueprintTool()
    loop, store, _e, _m = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "salary"),
                    _tagged_blueprint("b1", "bp-headcount", "i1"),
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ],
        blueprint_tool=blueprint,
    )
    # The getBlueprint-before-runBlueprint gate (tests/_blueprint_gate.py).
    await expand_blueprint(store, SESSION_ID, "bp-headcount")

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    assert blueprint.seen == [{"id": "bp-headcount", "slot_bindings": {}}]
    assert (await _trail(store))["b1"].serves_intent == "i1"


async def test_two_identically_tagged_reads_still_dedup_to_one_dispatch() -> None:
    """The read-guard signature is computed over the CLEANED arguments. Were it
    computed before the strip, two identical schema fetches tagged for different
    intents would look like different reads and the guard would stop deduping."""
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema()]})
    loop, store, events, mcp = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "the schema", "the same schema"),
                    _tagged_schema("m1", "i1"),
                    _tagged_schema("m2", "i2"),
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=mcp,
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    assert len([c for c in mcp.calls if c.tool_name == "getTableSchema"]) == 1
    assert _events(events, "loop_repeated_idempotent_read_guarded")
    trail = await _trail(store)
    # The guard entry keeps its tag, so the refusal for i2 can name the original
    # rather than claiming nothing was tagged (04 condition 5).
    assert trail["m2"].serves_intent == "i2"


# ---------------------------------------------------------------------------
# Lenient validation — drop the tag, never the work
# ---------------------------------------------------------------------------


async def test_an_unknown_tag_drops_the_tag_and_still_runs_the_query() -> None:
    """Degrade-not-fail, never silently. Refusing a real query over a bookkeeping
    typo would be strictly worse than an untagged entry — it is exactly the failure
    mode this whole change exists to remove."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()]})
    loop, store, events, mcp = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "salary"),
                    _tagged_query("q1", "i7"),  # no such intent
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=mcp,
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    assert [c.args for c in mcp.calls] == [{"sql": "SELECT 1"}]
    trail = await _trail(store)
    assert trail["q1"].status == "ok"
    assert trail["q1"].serves_intent is None
    # Never silently: the drop is reported, by RULE NAME. The offending value is
    # NOT emitted — an invalid tag is arbitrary model text (D25).
    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "runQuery", "reason": "unknown_intent_id"}
    ]


async def test_a_hostile_tag_value_never_reaches_telemetry() -> None:
    """D25. A VALID tag is a runtime-assigned `intent_id` and is safe to emit (it
    already rides `loop_intent_completed.intent_id`). A DROPPED one is whatever the
    model typed — here the user's own question — so the drop event reports the RULE
    NAME and the tool, and nothing else."""
    hostile = "the average salary of every employee in Sales"
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()]})
    loop, _s, events, mcp = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "salary"),
                    _tagged_query("q1", hostile),
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=mcp,
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    dropped = _events(events, "loop_intent_tag_dropped")
    assert dropped == [{"tool_name": "runQuery", "reason": "unknown_intent_id"}]
    assert hostile not in json.dumps(events, default=str)


async def test_a_tag_on_a_single_intent_turn_is_dropped_not_fatal() -> None:
    """No `updateAnalysisState` at all — the single-deliverable shape the prompt
    tells the model NOT to track. A tag there means nothing and must cost nothing."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()]})
    loop, store, events, mcp = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_query("q1", "i1")]),
            ModelTurnResult(assistant_text="42"),
        ],
        mcp=mcp,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "done"
    assert [c.args for c in mcp.calls] == [{"sql": "SELECT 1"}]
    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "runQuery", "reason": "no_live_state"}
    ]


async def test_declaring_and_tagging_in_the_same_message_works() -> None:
    """03 §E.2 dispatches every state call FIRST, so the ids exist by the time the
    tagged calls in the same batch are reached. If the tag were validated against a
    snapshot taken before the batch, the natural round-1 shape (declare + do the
    work) would silently lose every tag."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows(), _rows()]})
    loop, store, _e, mcp = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "salary"),
                    _tagged_query("q1", "i1"),
                    _tagged_query("q2", "i2", sql="SELECT 2"),
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=mcp,
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    trail = await _trail(store)
    assert (trail["q1"].serves_intent, trail["q2"].serves_intent) == ("i1", "i2")


# ---------------------------------------------------------------------------
# THE LIVE FAILURE, REPLAYED
# ---------------------------------------------------------------------------


async def test_the_live_three_intent_failure_now_completes_with_zero_rejections() -> None:
    """The measured shape, verbatim: three deliverables declared, three blueprints
    run and tagged in round 1, ONE `updateAnalysisState` closing all three in round
    2 with NO evidence field on any of them.

    On the citation path this turn produced rejection after rejection — the model
    cited the blueprint id, then the tool name, then a hallucinated call id, then
    `""` — and the intents stayed `pending` until enforcement force-blocked them.
    Here: three completions, zero rejections, and each intent bound to the
    blueprint run that actually served it.
    """
    blueprint = _RecordingBlueprintTool()
    loop, store, events, _m = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call(
                        "s1",
                        "Active headcount by department.",
                        "Average salary by department.",
                        "Projected hires over the next 6 months.",
                    ),
                    _tagged_blueprint("b1", "bp-active-headcount-by-department", "i1"),
                    _tagged_blueprint("b2", "bp-average-salary-by-department", "i2"),
                    _tagged_blueprint("b3", "bp-hires-projection", "i3"),
                ],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _update_call(
                        "s2",
                        {"intent_id": "i1", "status": "completed"},
                        {"intent_id": "i2", "status": "completed"},
                        {"intent_id": "i3", "status": "completed"},
                    ),
                    ToolCallRequest(
                        id="a1",
                        name=ANSWER,
                        arguments={
                            "answer": "Headcount and average salary by department, "
                            "plus the six-month hiring projection.",
                            "sql": "SELECT 1",
                        },
                    ),
                ],
            ),
        ],
        blueprint_tool=blueprint,
    )
    # The getBlueprint-before-runBlueprint gate: the live three-intent shape now
    # expands all three blueprints before running them (tests/_blueprint_gate.py).
    for blueprint_id in (
        "bp-active-headcount-by-department",
        "bp-average-salary-by-department",
        "bp-hires-projection",
    ):
        await expand_blueprint(store, SESSION_ID, blueprint_id)

    outcome = await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(),
        user_message=(
            "Give me the active headcount by department, the average salary by "
            "department, and the projected hires over the next 6 months."
        ),
    )

    assert outcome.status == "done"
    assert not _events(events, "loop_analysis_state_rejected"), "a completion was refused"
    assert not _events(events, "loop_finalization_refused")
    assert not _events(events, "loop_enforcement_exhausted")

    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.intent_id, i.status, i.evidence_tool_call_id) for i in state.intents] == [
        ("i1", "completed", "b1"),
        ("i2", "completed", "b2"),
        ("i3", "completed", "b3"),
    ]
    assert [
        (p["intent_id"], p["evidence_tool_name"], p["evidence_binding"])
        for p in _events(events, "loop_intent_completed")
    ] == [
        ("i1", "runBlueprint", "tagged"),
        ("i2", "runBlueprint", "tagged"),
        ("i3", "runBlueprint", "tagged"),
    ]
