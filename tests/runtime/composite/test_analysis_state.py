"""Unit tests for the `updateAnalysisState` tool (Release 1, doc 03 §F).

Layer 1: `InMemorySessionStore`, no loop, no model. The tool is driven directly
with an explicit `TurnContext`, which is exactly how the loop drives it.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    ANALYSIS_STATE_INVALID_CODE,
    ANALYSIS_STATE_LATE_INIT_CODE,
    SUBSTANTIVE_TOOLS,
    UpdateAnalysisStateTool,
    find_locking_tool,
)
from data_agent.runtime.loop.agent_loop import TurnContext
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import (
    ResultPreview,
    TrailEntry,
    live_analysis_state,
)

SESSION_ID = "sess-analysis-state"
TURN = 2


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


def _tool(store: InMemorySessionStore, events: list[tuple[str, dict[str, Any]]] | None = None):
    def _observe(event: str, payload: dict[str, Any]) -> None:
        if events is not None:
            events.append((event, payload))

    return UpdateAnalysisStateTool(session_store=store, observer=_observe)


async def _entry(
    store: InMemorySessionStore,
    tool_call_id: str,
    tool_name: str,
    *,
    status: str = "ok",
    error_code: str | None = None,
    row_count: int | None = 1,
    authoritative: bool = False,
    turn_index: int = TURN,
) -> None:
    preview = (
        None
        if row_count is None
        else ResultPreview(columns=["x"], row_count=row_count, truncated=False, preview_rows=[])
    )
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=turn_index,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args={},
            status=status,
            error_code=error_code,
            provenance=frozenset(),
            result_preview=preview,
            result_full_ref=None,
            ts="2026-08-11T00:00:00+00:00",
            authoritative=authoritative,
        ),
    )


def _init_args(*descriptions: str) -> dict[str, Any]:
    return {"intents": [{"description": d} for d in descriptions]}


async def test_initialize_assigns_sequential_ids_and_returns_them() -> None:
    """The initialize RESULT is the only way the model learns the ids, so a bare
    confirmation is not enough."""
    store = InMemorySessionStore()
    result = await _tool(store).run(
        _init_args("headcount by department", "average salary by department"),
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )

    assert result.status == "ok"
    assert result.provenance == frozenset()  # 03 §C.5 — determined-empty, NOT None
    assert [i["intent_id"] for i in result.result_full["intents"]] == ["i1", "i2"]
    assert result.result_full["intents"][0]["status"] == "pending"
    # The ids reach the model through the tool MESSAGE too, not just result_full.
    assert [row[0] for row in result.result_preview.preview_rows] == ["i1", "i2"]

    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, TURN)
    assert state is not None
    assert state.turn_index == TURN
    assert [i.description for i in state.intents] == [
        "headcount by department",
        "average salary by department",
    ]


async def test_mode_is_inferred_from_live_analysis_state_not_declared() -> None:
    """A state from ANOTHER turn is history: the same payload initializes again on
    a new turn rather than being refused as "already declared"."""
    store = InMemorySessionStore()
    tool = _tool(store)
    await tool.run(_init_args("a", "b"), _credentials(), turn=TurnContext(turn_index=TURN))

    later = await tool.run(
        _init_args("something else"), _credentials(), turn=TurnContext(turn_index=TURN + 1)
    )
    assert later.status == "ok"
    assert [i["intent_id"] for i in later.result_full["intents"]] == ["i1"]

    doc = await store.get_or_create_session(SESSION_ID)
    assert live_analysis_state(doc, TURN) is None  # the old state is inert
    assert live_analysis_state(doc, TURN + 1) is not None


async def test_update_is_batched_across_several_intents_in_one_call() -> None:
    """Batching is load-bearing: with automatic blueprint-flipping cut, one call
    per intent reintroduces the bookkeeping the trim exists to remove."""
    store = InMemorySessionStore()
    tool = _tool(store)
    await tool.run(_init_args("a", "b", "c"), _credentials(), turn=TurnContext(turn_index=TURN))
    await _entry(store, "call_q", "runQuery")
    await _entry(store, "call_denied", "runQuery", status="denied",
                 error_code="COLUMN_SCOPE_VIOLATION", row_count=None)

    result = await tool.run(
        {
            "intents": [
                {"intent_id": "i1", "status": "completed", "evidence_tool_call_id": "call_q"},
                {
                    "intent_id": "i2",
                    "status": "blocked",
                    "reason_code": "NO_ACCESS",
                    "evidence_tool_call_id": "call_denied",
                },
            ]
        },
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )

    assert result.status == "ok"
    by_id = {i["intent_id"]: i for i in result.result_full["intents"]}
    assert by_id["i1"]["status"] == "completed"
    assert by_id["i2"]["reason_code"] == "NO_ACCESS"
    # An intent the call did not mention keeps its previous disposition.
    assert by_id["i3"]["status"] == "pending"


async def test_an_unmentioned_intent_is_never_dropped() -> None:
    """The guarantee stated accurately: the model cannot SILENTLY DROP an ask.
    Merge-by-id means there is no "full replace" shape to shorten."""
    store = InMemorySessionStore()
    tool = _tool(store)
    await tool.run(_init_args("easy", "hard"), _credentials(), turn=TurnContext(turn_index=TURN))
    await _entry(store, "call_q", "runQuery")

    result = await tool.run(
        {"intents": [{"intent_id": "i1", "status": "completed",
                      "evidence_tool_call_id": "call_q"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert [i["intent_id"] for i in result.result_full["intents"]] == ["i1", "i2"]
    assert result.result_full["intents"][1]["status"] == "pending"


async def test_late_init_is_rejected_after_a_substantive_call() -> None:
    store = InMemorySessionStore()
    events: list[tuple[str, dict[str, Any]]] = []
    await _entry(store, "call_q", "runQuery")

    result = await _tool(store, events).run(
        _init_args("headcount", "attrition"), _credentials(), turn=TurnContext(turn_index=TURN)
    )

    assert result.status == "error"
    assert result.error_code == ANALYSIS_STATE_LATE_INIT_CODE
    assert result.retryable is False  # the boundary has passed; no retry helps
    # 03 §C.3 / spec §5.1: the detail carries the PROPOSED descriptions, on the
    # only channel that reaches the model.
    assert "headcount" in result.denial_detail
    assert "attrition" in result.denial_detail
    assert ("loop_analysis_state_late_init_rejected",
            {"proposed_count": 2, "blocking_tool_name": "runQuery"}) in events
    # A rejected init leaves the turn running UNPROTECTED — it does not error it.
    doc = await store.get_or_create_session(SESSION_ID)
    assert live_analysis_state(doc, TURN) is None


async def test_late_init_is_permitted_after_a_metadata_call() -> None:
    """EVERYTHING outside the locking set is non-locking. An implementer guessing
    "unlisted = locking" would make `getTableSchema` — and `recordAssumptions` —
    block initialization."""
    store = InMemorySessionStore()
    await _entry(store, "call_schema", "getTableSchema")
    await _entry(store, "call_search", "searchBlueprints")
    await _entry(store, "call_assume", "recordAssumptions")

    result = await _tool(store).run(
        _init_args("headcount"), _credentials(), turn=TurnContext(turn_index=TURN)
    )
    assert result.status == "ok"


async def test_the_locking_set_is_exactly_four_tools() -> None:
    assert SUBSTANTIVE_TOOLS == frozenset(
        {"runQuery", "runBlueprint", "sampleRows", "resolveValues"}
    )


async def test_a_prior_turns_substantive_call_does_not_lock_this_turn() -> None:
    store = InMemorySessionStore()
    await _entry(store, "call_old", "runQuery", turn_index=TURN - 1)
    trail = await store.load_trail(SESSION_ID)
    assert find_locking_tool(trail, TURN) is None

    result = await _tool(store).run(
        _init_args("headcount"), _credentials(), turn=TurnContext(turn_index=TURN)
    )
    assert result.status == "ok"


async def test_evidence_must_come_from_a_prior_round_trip() -> None:
    """State calls are dispatched FIRST, so `[runQuery, updateAnalysisState(
    evidence=<that call>)]` can never validate. The rejection has to say so."""
    store = InMemorySessionStore()
    tool = _tool(store)
    await tool.run(_init_args("headcount"), _credentials(), turn=TurnContext(turn_index=TURN))

    result = await tool.run(
        {"intents": [{"intent_id": "i1", "status": "completed",
                      "evidence_tool_call_id": "call_not_yet"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert result.error_code == ANALYSIS_STATE_INVALID_CODE
    assert result.retryable is True
    assert "has not run yet" in result.denial_detail


async def test_a_missing_turn_context_refuses_rather_than_guessing() -> None:
    """Writing state under a guessed turn index would land it where
    `live_analysis_state` makes it inert — enforcement would then pass while
    looking wired."""
    store = InMemorySessionStore()
    result = await _tool(store).run(_init_args("headcount"), _credentials())
    assert result.status == "error"
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.analysis_state is None


async def test_citing_a_prior_turns_call_is_diagnosed_precisely() -> None:
    """The tool hands the validators the WHOLE trail, not one pre-filtered to the
    turn. Both filter internally, and the unfiltered list is what lets them tell
    "that call belongs to an earlier turn" — precisely diagnosable — from "no such
    call anywhere", which is genuinely ambiguous. Pre-filtering collapses the two
    into the weaker message and the model is told to do the wrong thing."""
    store = InMemorySessionStore()
    await _entry(store, "call_old", "runQuery", turn_index=TURN - 1)
    tool = _tool(store)
    await tool.run(_init_args("headcount"), _credentials(), turn=TurnContext(turn_index=TURN))

    result = await tool.run(
        {"intents": [{"intent_id": "i1", "status": "completed",
                      "evidence_tool_call_id": "call_old"}]},
        _credentials(),
        turn=TurnContext(turn_index=TURN),
    )
    assert result.error_code == ANALYSIS_STATE_INVALID_CODE
    assert "belongs to an earlier turn" in result.denial_detail
