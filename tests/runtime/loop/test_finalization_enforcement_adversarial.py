"""Adversarial finalization enforcement (Release 1, doc 05 §H).

What a model that WANTS to finish does when it is refused. Two of these assert a
guarantee; two assert a KNOWN-PERMITTED evasion, because 03 §C.4 and 04 §B.4 state
plainly what immutability and evidence-backed blocking do and do not close:

    The model cannot SILENTLY DROP an ask. It can still produce a falsifiable
    reason cheaply — `SELECT ... WHERE 1=0` is a valid `REQUIRED_DATA_UNAVAILABLE`
    and one `getTableSchema` against the scratch db is a valid `NO_ACCESS`.

Asserting the permitted routes rather than omitting them is the point: they are
measured (`loop_zero_row_block` vs `loop_zero_row_completion`), not silently
implied to be closed.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    ANALYSIS_STATE_INVALID_CODE,
    MAX_STATE_CALLS,
    UpdateAnalysisStateTool,
)
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.denial_mapping import (
    FINALIZATION_BLOCKED_PENDING_INTENTS_CODE,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.loop.agent_loop import (
    _MAX_SURPLUS_STATE_REJECTIONS,
    AgentLoop,
)
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import (
    FinalizationBlockKind,
    ResultPreview,
    live_analysis_state,
)
from data_agent.runtime.session.store import CASMismatchError

pytestmark = pytest.mark.usefixtures("blueprint_consulted")

SESSION_ID = "sess-finalization-adversarial"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"
HEADCOUNT = "headcount by department"
ATTRITION = "attrition by department"


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [
        {"type": "function", "name": name, "parameters": {}}
        for name in ("runQuery", "getTableSchema", "listDatabases", "listTables")
    ]


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


class _StubSearchBlueprints:
    """A `searchBlueprints` that succeeds — enough to leave an `ok` trail entry the
    model can try to cite. 04 §A deliberately excludes it from the completion
    evidence set: a search result is not evidence that an intent was ANSWERED."""

    tool_name = "searchBlueprints"

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: Any = None,
        tool_call_id=None,
    ) -> ToolResult:
        return ToolResult(
            status="ok",
            tool_name="searchBlueprints",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=ResultPreview(
                columns=["id"], row_count=1, truncated=False, preview_rows=[["bp-1"]]
            ),
            result_full=None,
        )


def _build(
    turns: list[ModelTurnResult],
    *,
    mcp: FakeMCPClient | None = None,
    max_loop_iterations: int = 15,
    max_budget_windows: int = 3,
    store: InMemorySessionStore | None = None,
) -> tuple[AgentLoop, InMemorySessionStore, list[tuple[str, dict[str, Any]]], ScriptedModelClient]:
    store = store if store is not None else InMemorySessionStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    model = ScriptedModelClient(turns)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(mcp or FakeMCPClient(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=max_loop_iterations,
        max_wall_clock_seconds=60,
        max_budget_windows=max_budget_windows,
        observer=_observe,
        runtime_tools={
            STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe),
            ANSWER: AnswerWithTableTool(),
            "searchBlueprints": _StubSearchBlueprints(),
        },
    )
    return loop, store, events, model


def _init_call(call_id: str, *descriptions: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name=STATE,
        arguments={"intents": [{"description": d} for d in descriptions]},
    )


def _update_call(call_id: str, *updates: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=STATE, arguments={"intents": list(updates)})


def _answer_call(call_id: str, answer: str = "Here is the answer.") -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id, name=ANSWER, arguments={"answer": answer, "tables": [{"sql": "SELECT 1"}]}
    )


def _query_call(call_id: str, sql: str = "SELECT 1") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="runQuery", arguments={"sql": sql})


def _query_mcp(count: int = 10, *, row_count: int = 1) -> FakeMCPClient:
    return FakeMCPClient(
        scripted={
            "runQuery": [
                {
                    "columns": ["x"],
                    "rows": [[1]] * row_count,
                    "row_count": row_count,
                    "truncated": False,
                }
            ]
            * count
        }
    )


def _events(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


async def test_two_answers_in_one_batch_are_refused_twice_and_spend_one_block() -> None:
    """05 §C.2 — the cap is consumed PER ROUND-TRIP, not per refused call.

    The dispatch loop processes up to 8 calls from ONE model response. Counting per
    call, `[answerWithTable, answerWithTable]` burns both chances in a single
    round-trip, force-blocks on the second, and finalizes — having been given NO
    re-round at all, with `ENFORCEMENT_EXHAUSTED` written for intents the model was
    never asked twice about."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_answer_call("a1"), _answer_call("a2", answer="Or this one.")],
            ),
            ModelTurnResult(assistant_text="fine, here is the real answer"),
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    # The turn CONTINUED — the model did get its re-round.
    assert outcome.status == "done"
    assert outcome.assistant_text == "fine, here is the real answer"
    doc = await store.get_or_create_session(SESSION_ID)
    refusals = [
        e for e in doc.tool_trail if e.error_code == FINALIZATION_BLOCKED_PENDING_INTENTS_CODE
    ]
    assert [e.tool_call_id for e in refusals] == ["a1", "a2"], "both calls must be refused"
    assert len(_events(events, "loop_finalization_refused")) == 2
    # ...and the counter advanced exactly ONCE.
    assert doc.finalization_blocks == {"0:1:intents": 1}
    assert _events(events, "loop_finalization_block_spent") == [{"window": 1}]
    # No premature exhaustion INSIDE the batch: both refusals land before the next
    # model round-trip, and the eventual `ENFORCEMENT_EXHAUSTED` comes only after
    # the model has actually had its re-round.
    names = [name for name, _ in events]
    last_refusal = len(names) - 1 - names[::-1].index("loop_finalization_refused")
    re_round = [i for i, n in enumerate(names) if n == "loop_model_call_start"][2]
    assert last_refusal < re_round < names.index("loop_enforcement_exhausted")
    assert names.index("loop_intent_force_blocked") > re_round


async def test_dropping_the_hard_intent_does_not_let_the_turn_finalize() -> None:
    """03 §C.4 end-to-end: merge-by-id means an intent the model simply stops
    mentioning KEEPS its disposition — there is no "full replace" shape to shorten
    — and a re-declaration is rejected outright. Either way the turn is still
    refused for the intent it tried to drop."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", HEADCOUNT, ATTRITION), _query_call("q1")],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    # (a) omit i2 entirely — the "drop" — and (b) try to re-declare
                    # the whole set as a single easy intent.
                    _update_call(
                        "s2",
                        {
                            "intent_id": "i1",
                            "status": "completed",
                            "evidence_tool_call_id": "q1",
                        },
                    ),
                    ToolCallRequest(
                        id="s3",
                        name=STATE,
                        arguments={"intents": [{"description": "just the headcount"}]},
                    ),
                    _answer_call("a1"),
                ],
            ),
            ModelTurnResult(assistant_text="ok, I give up"),
        ],
        mcp=_query_mcp(),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="two things"
    )

    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    # The dropped intent survives, with its ORIGINAL description.
    assert [i.intent_id for i in state.intents] == ["i1", "i2"]
    assert state.intents[1].description == ATTRITION
    # The re-declaration was rejected, not applied.
    redeclare = next(e for e in doc.tool_trail if e.tool_call_id == "s3")
    assert redeclare.error_code == ANALYSIS_STATE_INVALID_CODE
    assert ("loop_analysis_state_rejected", {"reason": "second_initialize", "intent_count": 1}) in (
        events
    )
    # And the finalization was refused for exactly the intent it tried to drop.
    refusal = next(e for e in doc.tool_trail if e.tool_call_id == "a1")
    assert refusal.error_code == FINALIZATION_BLOCKED_PENDING_INTENTS_CODE
    assert "i2" in refusal.denial_detail
    assert outcome.assistant_text == "ok, I give up"
    assert _events(events, "loop_enforcement_exhausted") == [{"intent_count": 1}]


async def test_completing_an_intent_on_a_search_result_is_rejected_and_still_refused() -> None:
    """04 §A condition 3: `searchBlueprints` is not evidence that an intent was
    ANSWERED. The state update is rejected, the intent stays `pending`, and the
    finalization in the same batch is refused.

    STRUCTURAL SINCE 01a §14, not a validation failure. The model cannot name a
    call any more, and `searchBlueprints` is neither taggable (`serves_intent` is
    advertised on three tools, and it is not one) nor in the auto-bind pool (which
    is built from the calls the validators ACCEPT). So the search result is not
    refused as evidence — it is never a candidate, and the intent reads as
    unanswered, which is what it is. The legacy citation is left in the payload on
    purpose: it must be dropped, not honoured.
    """
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", HEADCOUNT),
                    ToolCallRequest(
                        id="sb1", name="searchBlueprints", arguments={"query": "headcount"}
                    ),
                ],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _update_call(
                        "s2",
                        {
                            "intent_id": "i1",
                            "status": "completed",
                            "evidence_tool_call_id": "sb1",
                        },
                    ),
                    _answer_call("a1"),
                ],
            ),
            ModelTurnResult(assistant_text="giving up"),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="one thing")

    doc = await store.get_or_create_session(SESSION_ID)
    rejected = next(e for e in doc.tool_trail if e.tool_call_id == "s2")
    assert rejected.error_code == ANALYSIS_STATE_INVALID_CODE
    assert "nothing this turn answered it" in rejected.denial_detail
    refusal = next(e for e in doc.tool_trail if e.tool_call_id == "a1")
    assert refusal.error_code == FINALIZATION_BLOCKED_PENDING_INTENTS_CODE
    assert _events(events, "loop_finalization_refused") == [
        {"exit": "answer_with_table", "pending_count": 1}
    ]


async def test_manufactured_block_evidence_is_permitted_today_and_measured() -> None:
    """KNOWN-OPEN (03 §C.4 / 04 §B.4): `SELECT ... WHERE 1=0` returns zero rows,
    which is a valid `REQUIRED_DATA_UNAVAILABLE`, so the model can close an intent
    it did not answer and finalize cleanly.

    This is NOT a guarantee failure — zero rows is also how an honest query answers
    "nobody", which is precisely why the two cases are mechanically
    indistinguishable and the mitigation is the `loop_zero_row_block` vs
    `loop_zero_row_completion` ratio rather than a structural rule. Asserted here so
    it is measured rather than assumed closed."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", HEADCOUNT),
                    _query_call("q0", sql="SELECT 1 WHERE 1=0"),
                ],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _update_call(
                        "s2",
                        {
                            "intent_id": "i1",
                            "status": "blocked",
                            "reason_code": "REQUIRED_DATA_UNAVAILABLE",
                            "evidence_tool_call_id": "q0",
                        },
                    ),
                    _answer_call("a1", answer="Nothing to report."),
                ],
            ),
        ],
        mcp=_query_mcp(row_count=0),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Nothing to report."
    assert not _events(events, "loop_finalization_refused")
    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [
        ("blocked", "REQUIRED_DATA_UNAVAILABLE")
    ]
    # THE MEASUREMENT — the 04 §B.4 ratio.
    assert _events(events, "loop_zero_row_block") == [{"intent_id": "i1"}]
    assert not _events(events, "loop_zero_row_completion")


async def test_a_pending_intent_at_the_hard_ceiling_is_force_blocked_exactly_once() -> None:
    """A refused `answerWithTable` and the hard ceiling in the same round. The turn
    must terminate ONCE, and the intent must be written ONCE — a double force-block
    (from the refusal path and the ceiling path both) would emit two transitions for
    one intent and record the second reason over the first."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
        ],
        max_loop_iterations=2,
        max_budget_windows=1,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "stopped_hard_ceiling"
    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [("blocked", "BUDGET_EXHAUSTED")]
    assert _events(events, "loop_intent_force_blocked") == [
        {"intent_id": "i1", "reason_code": "BUDGET_EXHAUSTED"}
    ]
    assert len(_events(events, "loop_analysis_state_transition")) == 1
    assert len([name for name, _ in events if name == "loop_hard_ceiling_stop"]) == 1


async def test_an_abandoned_pause_leaves_pending_state_and_does_not_touch_the_next_turn() -> None:
    """05 §F.1, case 1 — and the reason the invariant is SCOPED. The checkpoint was
    written and `resume()` was never called, so this turn never terminated: its
    intents stay `pending`, legitimately, and 07's assertion must exclude it. The
    NEXT turn is unaffected."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", HEADCOUNT, ATTRITION),
                    ToolCallRequest(id="ask", name="askUser", arguments={"question": "which?"}),
                ],
            ),
            ModelTurnResult(assistant_text="an answer to a different question"),
        ]
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="two things"
    )
    assert paused.status == "paused_ask_user"
    # The user never answers; they ask something else instead.
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="never mind, what is X?"
    )

    assert outcome.status == "done"
    doc = await store.get_or_create_session(SESSION_ID)
    turn0 = live_analysis_state(doc, 0)
    assert [i.status for i in turn0.intents] == ["pending", "pending"]
    assert live_analysis_state(doc, 1) is None
    assert not _events(events, "loop_intent_force_blocked")
    assert not [name for name, _ in events if name.startswith("loop_finalization")]


# ---------------------------------------------------------------------------
# The store fails at the worst possible moment
# ---------------------------------------------------------------------------


class _ClaimRaisingStore(InMemorySessionStore):
    """A store whose `claim_finalization_block` always raises, as the real one can:
    `CASMismatchError` after five lost CAS retries (a concurrent resume racing this
    turn is enough) or a transient connection error.

    In-memory and scripted doubles cannot fail, so this whole path is green by
    construction without a double like this one — it only bites against a real store,
    at the exact moment the model has a finished answer."""

    def __init__(self) -> None:
        super().__init__()
        self.claim_attempts = 0

    async def claim_finalization_block(
        self,
        session_id: str,
        turn_index: int,
        window_count: int,
        kind: FinalizationBlockKind,
    ) -> bool:
        self.claim_attempts += 1
        raise CASMismatchError("five CAS retries lost to a concurrent resume")


async def test_a_failing_block_claim_finalizes_the_turn_instead_of_aborting_it() -> None:
    """Degrade-not-fail, never silently — the same posture
    `_force_block_pending_intents` already takes.

    The exception used to propagate out of `_run_loop_body` and abort the turn WITH
    THE ANSWER IN HAND. It is now caught, logged server-side, reported on an observer
    event, and treated as `False`: the runtime records the disposition it can
    (`ENFORCEMENT_EXHAUSTED`) and the user gets their answer. Treating it as `True`
    would grant a re-round whose consumption was never persisted — unbounded
    re-rounds, bounded only by the budget window."""
    store = _ClaimRaisingStore()
    loop, _s, events, _ = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1", "12 people.")]),
        ],
        store=store,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert store.claim_attempts == 1
    # The turn finished. It did NOT raise out of the loop.
    assert outcome.status == "done"
    assert outcome.assistant_text == "12 people."
    # NEVER SILENTLY: the failure is on the wire as its own event.
    assert _events(events, "loop_finalization_block_claim_failed") == [
        {"turn_index": 0, "window": 1, "reason": "store_error"}
    ]
    # Treated as `False`, so the runtime recorded a disposition rather than looping.
    assert not _events(events, "loop_finalization_refused")
    assert _events(events, "loop_enforcement_exhausted") == [{"intent_count": 1}]
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [
        ("blocked", "ENFORCEMENT_EXHAUSTED")
    ]


# ---------------------------------------------------------------------------
# Adversarial VOLUME on the cap-exempt partition
# ---------------------------------------------------------------------------


class _TrailCountingStore(InMemorySessionStore):
    """Counts `append_trail_entry` calls — each is a full CAS read-modify-write
    against the session document, which is the cost being bounded."""

    def __init__(self) -> None:
        super().__init__()
        self.trail_writes = 0

    async def append_trail_entry(self, session_id: str, entry: Any) -> None:
        self.trail_writes += 1
        await super().append_trail_entry(session_id, entry)


async def test_twenty_state_calls_in_one_response_cost_a_bounded_number_of_writes() -> None:
    """`updateAnalysisState` is EXEMPT from `max_tool_calls_per_iteration`, and
    `MAX_STATE_CALLS` bounded the state WRITES at two — but every surplus call still
    got a rejection entry, and each of those is a full CAS read-modify-write plus an
    entry pinned in the current-turn budget region. A degenerate response cost O(N)
    store writes in one round-trip while over-cap `other_calls` were simply dropped.

    The first `_MAX_SURPLUS_STATE_REJECTIONS` surplus calls are still rejected WITH an
    entry, so the model learns why; the rest are dropped exactly as over-cap
    `other_calls` are."""
    store = _TrailCountingStore()
    surplus = [
        _update_call(f"s{i}", {"intent_id": "i1", "status": "pending"}) for i in range(2, 21)
    ]
    loop, _s, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", HEADCOUNT), *surplus],
            ),
            # Refused (i1 is still pending), then allowed through once the window's
            # one forced re-round is spent. Neither round persists a trail entry —
            # exit #1 persists nothing by design — so the count below stays clean.
            ModelTurnResult(assistant_text="done anyway"),
            ModelTurnResult(assistant_text="done anyway"),
        ],
        store=store,
    )
    assert len(surplus) + 1 == 20

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="one thing")

    # MAX_STATE_CALLS (2) dispatched + _MAX_SURPLUS_STATE_REJECTIONS (2) rejected.
    assert store.trail_writes == MAX_STATE_CALLS + _MAX_SURPLUS_STATE_REJECTIONS
    assert store.trail_writes < 20, "the response's call count still drove the write count"
    doc = await store.get_or_create_session(SESSION_ID)
    assert len(doc.tool_trail) == MAX_STATE_CALLS + _MAX_SURPLUS_STATE_REJECTIONS
    # The model IS told why, at least twice — it is not a silent drop.
    surplus_events = [
        payload
        for payload in _events(events, "loop_analysis_state_rejected")
        if payload["reason"] == "surplus_state_call"
    ]
    assert len(surplus_events) == _MAX_SURPLUS_STATE_REJECTIONS
    rejected = [e for e in doc.tool_trail if e.error_code == ANALYSIS_STATE_INVALID_CODE]
    assert len(rejected) == _MAX_SURPLUS_STATE_REJECTIONS
