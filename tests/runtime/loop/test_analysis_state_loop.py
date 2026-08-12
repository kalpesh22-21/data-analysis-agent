"""`updateAnalysisState` inside the agent loop (Release 1, doc 03 §E/§F).

These are the tests that matter most in the deliverable, because every failure
they cover is SILENT: the feature stays wired, the tool still exists, the model
still calls it — and the state simply never lands, so enforcement passes on a
turn it should have refused.

  - PARTITION BEFORE CAP: `result.tool_calls[:max_tool_calls_per_iteration]`
    discards the overflow with no error, so partitioning the CAPPED list loses a
    state call that follows eight substantive ones.
  - ORDER: state calls dispatch first regardless of array position.
  - PAUSE: `askUser` short-circuits the whole response before dispatch, so the
    state write in a `[updateAnalysisState, askUser]` batch was discarded whole.
  - BOUND: the cap exemption is not unlimited.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    ANALYSIS_STATE_INVALID_CODE,
    ANALYSIS_STATE_LATE_INIT_CODE,
    MAX_STATE_CALLS,
    UpdateAnalysisStateTool,
)
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import live_analysis_state

SESSION_ID = "sess-analysis-loop"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

STATE = "updateAnalysisState"


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return []


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


def _build(
    turns: list[ModelTurnResult], *, mcp: FakeMCPClient | None = None
) -> tuple[AgentLoop, InMemorySessionStore, list[tuple[str, dict[str, Any]]]]:
    store = InMemorySessionStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    dispatcher = ToolDispatcher(mcp or FakeMCPClient(), CATALOG, observer=_observe)
    loop = AgentLoop(
        model_client=ScriptedModelClient(turns),
        tool_dispatcher=dispatcher,
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=_observe,
        runtime_tools={
            STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe)
        },
    )
    return loop, store, events


def _state_call(call_id: str, *descriptions: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name=STATE,
        arguments={"intents": [{"description": d} for d in descriptions]},
    )


def _finalizing_turns() -> list[ModelTurnResult]:
    """TWO plain-prose turns, because 05's finalization enforcement is now live.

    Every script below leaves its intents `pending` (these are 03's tests — they
    exercise partitioning, ordering and bounds, not evidence), so the FIRST attempt
    to finish is refused and the model is given one forced re-round; the second is
    force-blocked as `ENFORCEMENT_EXHAUSTED` and the turn ends. Scripting one turn
    would exhaust `ScriptedModelClient` on the re-round, which is a test-authoring
    failure, not a runtime one. See `test_finalization_enforcement.py` for the
    tests that assert this behaviour rather than merely surviving it.
    """
    return [
        ModelTurnResult(assistant_text="done"),
        ModelTurnResult(assistant_text="done"),
    ]


def _query_call(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="runQuery", arguments={"sql": "SELECT 1"})


def _query_mcp(count: int = 10) -> FakeMCPClient:
    return FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["x"], "rows": [[1]], "row_count": 1, "truncated": False}
            ]
            * count
        }
    )


async def test_the_partition_runs_before_the_cap() -> None:
    """8 substantive calls + a TRAILING state call. `max_tool_calls_per_iteration`
    is 8, so partitioning the already-capped list would drop the state call before
    any reordering could help — and drop it SILENTLY: no error, no trail entry.
    The turn would then run completely unprotected."""
    calls = [_query_call(f"call_{n}") for n in range(8)]
    calls.append(_state_call("call_state", "headcount", "attrition"))
    loop, store, _ = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=calls),
            *_finalizing_turns(),
        ],
        mcp=_query_mcp(),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert state is not None, "the trailing state call was discarded by the cap"
    assert [i.intent_id for i in state.intents] == ["i1", "i2"]


async def test_state_calls_dispatch_before_everything_else_in_the_batch() -> None:
    """The state call is LAST in the array and must still run FIRST — otherwise
    the `runQuery` beside it would trip the late-init boundary and the
    initialization it is racing would be refused."""
    loop, store, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _query_call("call_q"),
                    _state_call("call_state", "headcount"),
                ],
            ),
            *_finalizing_turns(),
        ],
        mcp=_query_mcp(),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    doc = await store.get_or_create_session(SESSION_ID)
    assert live_analysis_state(doc, 0) is not None
    # And the ordering is visible in the trail, which is what the boundary reads.
    assert [e.tool_name for e in doc.tool_trail] == [STATE, "runQuery"]


async def test_a_state_call_that_follows_a_query_in_an_earlier_round_is_late() -> None:
    """The boundary is real, not defeated by the reorder: the reorder only covers
    ONE response. A `runQuery` in a previous round-trip still locks."""
    loop, store, events = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_query_call("call_q")]),
            ModelTurnResult(
                assistant_text=None, tool_calls=[_state_call("call_state", "headcount")]
            ),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=_query_mcp(),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    doc = await store.get_or_create_session(SESSION_ID)
    assert live_analysis_state(doc, 0) is None
    state_entry = next(e for e in doc.tool_trail if e.tool_name == STATE)
    assert state_entry.error_code == ANALYSIS_STATE_LATE_INIT_CODE
    assert ("loop_analysis_state_late_init_rejected",
            {"proposed_count": 1, "blocking_tool_name": "runQuery"}) in events


async def test_state_is_committed_before_the_ask_user_pause() -> None:
    """03 §E.1 — the natural round-1 shape for "three asks, one ambiguous".

    `askUser` pauses the whole response before anything is dispatched, so this
    batch used to discard the state write entirely: no trail entry, no tool
    result, and D22 discards the surrounding free text, so after the resume the
    model had no record it ever tried."""
    loop, store, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _state_call("call_state", "headcount", "attrition", "salary bands"),
                    ToolCallRequest(
                        id="call_ask",
                        name="askUser",
                        arguments={"question": "Which department?"},
                    ),
                ],
            )
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="three things"
    )

    assert outcome.status == "paused_ask_user"
    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert state is not None, "the state write was discarded by the askUser short-circuit"
    assert len(state.intents) == 3
    assert doc.pause_checkpoint is not None and not doc.pause_checkpoint.consumed


async def test_the_ask_user_pause_still_dispatches_nothing_else() -> None:
    """The E.1 change is narrow: ONLY state calls are committed before the pause.
    Everything else still waits for the resume, exactly as before."""
    loop, store, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _query_call("call_q"),
                    _state_call("call_state", "headcount"),
                    ToolCallRequest(
                        id="call_ask", name="askUser", arguments={"question": "which?"}
                    ),
                ],
            )
        ],
        mcp=_query_mcp(),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="q"
    )

    assert outcome.status == "paused_ask_user"
    doc = await store.get_or_create_session(SESSION_ID)
    assert [e.tool_name for e in doc.tool_trail] == [STATE]


async def test_surplus_state_calls_are_rejected_not_silently_dropped() -> None:
    """The exemption is BOUNDED (03 §E.2): unbounded, N state calls are N full
    session-doc CAS read-modify-writes and N trail entries in the pinned budget
    region. The surplus gets a REJECTION the model can see, not a silent drop."""
    assert MAX_STATE_CALLS == 2
    loop, store, events = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _state_call("call_s1", "headcount"),
                    ToolCallRequest(
                        id="call_s2",
                        name=STATE,
                        arguments={"intents": [{"intent_id": "i1", "status": "pending"}]},
                    ),
                    ToolCallRequest(
                        id="call_s3",
                        name=STATE,
                        arguments={"intents": [{"intent_id": "i1", "status": "pending"}]},
                    ),
                ],
            ),
            *_finalizing_turns(),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    doc = await store.get_or_create_session(SESSION_ID)
    surplus = next(e for e in doc.tool_trail if e.tool_call_id == "call_s3")
    assert surplus.status == "error"
    assert surplus.error_code == ANALYSIS_STATE_INVALID_CODE
    assert surplus.denial_detail  # the model is told why, on the channel it reads
    assert ("loop_analysis_state_rejected",
            {"reason": "surplus_state_call", "intent_count": 0}) in events
    # The two accepted calls still landed.
    assert live_analysis_state(doc, 0) is not None


async def test_a_state_call_is_exempt_from_the_per_iteration_cap() -> None:
    """8 substantive calls all dispatch AND the state call dispatches — the state
    call does not consume one of the eight slots."""
    calls = [_state_call("call_state", "headcount")]
    calls += [_query_call(f"call_{n}") for n in range(8)]
    loop, store, _ = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=calls),
            *_finalizing_turns(),
        ],
        mcp=_query_mcp(),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    doc = await store.get_or_create_session(SESSION_ID)
    assert len([e for e in doc.tool_trail if e.tool_name == "runQuery"]) == 8
    assert len([e for e in doc.tool_trail if e.tool_name == STATE]) == 1


async def test_the_tool_self_emits_its_dispatch_events() -> None:
    """It takes `observer` and `tracer` and emits its own dispatch events, like
    the retrieval read tools. `_run_runtime_tool` emits NOTHING on a runtime
    tool's behalf, so copying `RecordAssumptionsTool()` would have shipped a
    silently-mute tool."""
    loop, _store, events = _build(
        [
            ModelTurnResult(
                assistant_text=None, tool_calls=[_state_call("call_state", "headcount")]
            ),
            *_finalizing_turns(),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    names = [name for name, payload in events if payload.get("tool_name") == STATE]
    assert names == ["tool_dispatch_start", "tool_dispatch_ok"]


async def test_evidence_from_a_previous_round_trip_validates() -> None:
    """The end-to-end shape the tool description teaches: query in round 1, cite it
    in round 2. This is what proves the tool reads the trail ITSELF — a snapshot
    taken above the round-trip loop would hold nothing from this window and every
    citation would fail as "unknown id" while looking correctly wired."""
    loop, store, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_state_call("call_state", "headcount"), _query_call("call_q")],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_s2",
                        name=STATE,
                        arguments={
                            "intents": [
                                {
                                    "intent_id": "i1",
                                    "status": "completed",
                                    "evidence_tool_call_id": "call_q",
                                }
                            ]
                        },
                    )
                ],
            ),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=_query_mcp(),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert state.intents[0].status == "completed"
    assert state.intents[0].evidence_tool_call_id == "call_q"


async def test_state_from_an_abandoned_turn_does_not_govern_the_next_turn() -> None:
    """The A.1 failure that matters: turn 0 is multi-intent, the model pauses on
    `askUser`, the user abandons it and asks something new. Turn 1 must begin with
    NO live state — otherwise enforcement refuses an unrelated single-intent turn
    and writes its verdict onto turn 0's record."""
    loop, store, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _state_call("call_state", "headcount", "attrition"),
                    ToolCallRequest(
                        id="call_ask", name="askUser", arguments={"question": "which?"}
                    ),
                ],
            ),
            ModelTurnResult(assistant_text="a plain answer"),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")
    await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="something else"
    )

    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.analysis_state is not None  # still on the doc, as history
    assert live_analysis_state(doc, 0) is not None
    assert live_analysis_state(doc, 1) is None  # and inert for the new turn
