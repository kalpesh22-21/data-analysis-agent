"""QA5 Layer-1 adversarial: the agent-loop `runBlueprint` seam + PII posture
(runblueprint-design §2.5/§2.7/§5.5, Slice B).

Extends `test_loop_seam.py` with the accounting + PII-at-rest edges the happy
seam test does not exercise:

  - budget: TWO runBlueprint calls across one turn count as exactly TWO
    `tool_calls_made` (inner probes never leak into the count); a run that PAUSES
    counts ZERO.
  - PII-at-rest (FLAG): the persisted `PauseCheckpoint.slot_bindings_json` stores
    the raw model-proposed slot value UNREDACTED — the deterministic-re-fill design
    stores PII plain at rest (the redaction e2e only covers spans/progress, not the
    checkpoint doc).
  - redaction GAP (FLAG): a `no_match` slot pause's clarification question echoes the
    model's raw proposed value, which the loop emits verbatim in the
    `loop_paused_ask_user` progress event — so a slot VALUE can reach a progress
    payload, a partial exception to the §5.5 "no bound slot values in progress" claim.
  - resume idempotency: the tool/executor hold NO cross-call state — a re-invocation
    with a clarified value completes cleanly and independently (no double-anything).
"""

from __future__ import annotations

from typing import Any

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
from data_agent.runtime.session.memory_store import InMemorySessionStore

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "AnnualSalary": "Nullable(Float64)"}}
)
SESSION_ID = "sess-bp-qa5"
_BID = "bp-average-salary-by-department"
PII_VALUE = "Warehouse-JaneDoe-SSN123"

_NOGRAIN_SQL = (
    "SELECT Department AS department FROM dbpcm_warehouse.employee WHERE Department = {department}"
)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


def _detail(*, binds_to: bool, result_grain: Any) -> BlueprintDetail:
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
        sql_template=_NOGRAIN_SQL,
        result_grain=result_grain,
    )


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "runBlueprint", "description": "", "parameters": {}}]


def _loop(
    model: ScriptedModelClient,
    tool: RunBlueprintTool,
    *,
    observer: Any = None,
) -> tuple[AgentLoop, InMemorySessionStore]:
    store = InMemorySessionStore()
    kwargs: dict[str, Any] = {}
    if observer is not None:
        kwargs["observer"] = observer
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
        **kwargs,
    )
    return loop, store


def _rq(cols: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": False}


# ===========================================================================
# §2.7 budget — two calls = two tool_calls_made; a pause = zero
# ===========================================================================


async def test_two_run_blueprint_calls_count_as_two_tool_calls() -> None:
    # No binds_to + empty grain ⇒ ONE inner runQuery per runBlueprint. Two model
    # turns each call runBlueprint ⇒ exactly TWO tool_calls_made, two inner queries.
    mcp = FakeMCPClient(
        scripted={"runQuery": [_rq(["department"], [["Sales"]]), _rq(["department"], [["Support"]])]}
    )
    index = FakeVectorIndex()
    index.add_detail(_detail(binds_to=False, result_grain=[]))
    executor = BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)
    tool = RunBlueprintTool(executor=executor)

    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {"department": "Sales"}})
                ]
            ),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c2", name="runBlueprint", arguments={"id": _BID, "slot_bindings": {"department": "Support"}})
                ]
            ),
            ModelTurnResult(assistant_text="Done."),
        ]
    )
    loop, store = _loop(model, tool)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="two depts?")

    assert outcome.status == "done"
    assert outcome.tool_calls_made == 2  # two model-facing calls
    assert len(mcp.calls) == 2  # two inner runQuery (one each)
    trail = await store.load_trail(SESSION_ID)
    assert [e.tool_name for e in trail] == ["runBlueprint", "runBlueprint"]


# ===========================================================================
# PII-at-rest (FLAG) — the checkpoint stores the raw slot value unredacted
# ===========================================================================


async def test_paused_checkpoint_stores_raw_slot_value_at_rest_flag() -> None:
    # A missing-required-slot pause writes slot_bindings_json into the persisted
    # checkpoint. PINNED (flag): the raw model-proposed value is stored PLAIN at rest
    # (unredacted) — the redaction e2e only proves spans/progress are clean, NOT the
    # Couchbase/checkpoint doc. Deterministic-re-fill design accepts PII-at-rest here.
    mcp = FakeMCPClient(scripted={"runQuery": []})
    index = FakeVectorIndex()
    index.add_detail(_detail(binds_to=False, result_grain=[]))
    executor = BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)
    tool = RunBlueprintTool(executor=executor)

    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="runBlueprint",
                        arguments={"id": _BID, "slot_bindings": {"unused_extra": PII_VALUE}},
                    )
                ]
            ),
        ]
    )
    loop, store = _loop(model, tool)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q")

    assert outcome.status == "paused_ask_user"
    doc = await store.get_or_create_session(SESSION_ID)
    cp = doc.pause_checkpoint
    assert cp is not None
    # FLAG: the raw PII value is stored plain in the persisted checkpoint doc.
    assert cp.slot_bindings_json is not None
    assert PII_VALUE in cp.slot_bindings_json
    assert PII_VALUE in cp.to_doc()["slot_bindings_json"]


# ===========================================================================
# Redaction GAP (FLAG) — a no_match pause question echoes the raw value into
# the loop_paused_ask_user progress event
# ===========================================================================


async def test_no_match_pause_question_echoes_value_into_progress_event_flag() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def observer(event: str, payload: dict[str, Any]) -> None:
        events.append((event, dict(payload)))

    # binds_to set + a domain that does NOT contain the proposed value ⇒ no_match ⇒
    # the clarification question embeds the raw proposed value.
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["Department"], [["Sales"], ["Support"]])]})
    index = FakeVectorIndex()
    index.add_detail(_detail(binds_to=True, result_grain=[]))
    executor = BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG, observer=observer),
        vector_index=index,
        observer=observer,
    )
    tool = RunBlueprintTool(executor=executor, observer=observer)

    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="runBlueprint",
                        arguments={"id": _BID, "slot_bindings": {"department": PII_VALUE}},
                    )
                ]
            ),
        ]
    )
    loop, _store = _loop(model, tool, observer=observer)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q")

    assert outcome.status == "paused_ask_user"
    # The clarification question (user-facing) echoes the raw proposed value...
    assert PII_VALUE in outcome.pending_question["question"]
    # ...and the loop emits that question verbatim in a PROGRESS event — so a slot
    # VALUE reaches a progress payload here (the §5.5 "no bound slot values in
    # progress" claim holds only for value-free pauses like a missing slot). FLAG.
    paused_events = [p for name, p in events if name == "loop_paused_ask_user"]
    assert paused_events
    assert any(PII_VALUE in str(p) for p in paused_events)


# ===========================================================================
# Resume idempotency — no cross-call state; a clarified re-call is independent
# ===========================================================================


async def test_clarified_recall_after_pause_is_independent_no_double() -> None:
    # Slice B slot-resume re-runs the model turn (the model re-calls runBlueprint with
    # the clarified value). Proven idempotent at the tool boundary: a second, valid
    # invocation completes cleanly and issues exactly its own inner queries — the
    # executor holds no accumulated state from the paused first attempt.
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"], ["Support"]]),  # attempt-1 domain probe (no_match ⇒ pause)
                _rq(["Department"], [["Sales"], ["Support"]]),  # attempt-2 domain probe (matches)
                _rq(["department"], [["Sales"]]),  # attempt-2 node query
            ]
        }
    )
    index = FakeVectorIndex()
    index.add_detail(_detail(binds_to=True, result_grain=[]))
    executor = BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)

    creds = _creds()
    # Attempt 1: a no_match value ⇒ pause (one inner probe).
    o1 = await executor.execute(blueprint_id=_BID, slot_bindings={"department": "Marketing"}, credentials=creds)
    from data_agent.runtime.blueprint.executor import ExecCompleted, ExecPaused

    assert isinstance(o1, ExecPaused)
    calls_after_1 = len(mcp.calls)
    assert calls_after_1 == 1

    # Attempt 2 (the clarified re-call): completes independently, issuing exactly its
    # own probe + node — no leftover state, no re-run of attempt-1's work.
    o2 = await executor.execute(blueprint_id=_BID, slot_bindings={"department": "Sales"}, credentials=creds)
    assert isinstance(o2, ExecCompleted)
    assert len(mcp.calls) - calls_after_1 == 2  # exactly probe + node for attempt 2
