"""Release-1 SEAM tests — the gaps between 03/04 (state + validators), 05
(enforcement) and the pre-existing pause/resume machinery.

Each deliverable was verified against itself. These are the behaviours one
deliverable ASSUMES another provides, exercised through the real `AgentLoop`,
the real `BlueprintExecutor` and the real stores:

  - the window-local `analysis_state` (05 §E) is loaded once at the top of
    `_run_loop_body` — so every path that RE-ENTERS that function must reload it,
    including the mid-DAG `runBlueprint` resume, which reaches `_run_loop` by a
    completely different route from `askUser`;
  - the finalization counter is keyed by BUDGET WINDOW (05 §C.1), and a blueprint
    resume keeps `window_count` unchanged — so it must NOT hand out a second
    forced re-round;
  - a `runBlueprint` entry written by the RESUME path (`agent_loop.py`, not the
    tool) must still satisfy 04's completion validator: same `turn_index`, `ok`,
    and `authoritative`;
  - the force-block write is DEGRADE-NEVER-FAIL (05 §F) — and a swallowed failure
    must not emit telemetry claiming a transition that never landed;
  - a resume that loses the CAS race is 05 §F.1 case 3: no force-block runs at
    all, and the losing caller must write nothing.

Nothing here needs live infra.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import BlueprintExecutor
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import (
    FINALIZATION_BLOCKED_PENDING_INTENTS_CODE,
    AgentLoop,
)
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import live_analysis_state
from data_agent.runtime.session.store import CASMismatchError

SESSION_ID = "sess-r1-seams"
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
_BID = "bp-flag-departments"

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"
HEADCOUNT = "headcount by department"
ATTRITION = "attrition by department"


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return []


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail() -> BlueprintDetail:
    """A two-node blueprint whose second node needs approval — the shape that
    produces a mid-DAG `ToolPause` and therefore the `_resume_blueprint` path."""
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
            {
                "order": 0,
                "output": {"n": "scalar"},
                "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee",
            },
            {
                "order": 1,
                "node_kind": "approval",
                "feeds_from": [0],
                "requires_approval": {"prompt": "Flag these departments — proceed?"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "GROUP BY Department"
                ),
                "output": {},
            },
        ],
        result_grain=["Department"],
    )


class LateBindingModel(ScriptedModelClient):
    """A `ScriptedModelClient` whose entries may be CALLABLES evaluated at send
    time.

    Needed because `_resume_blueprint` mints the resumed `runBlueprint` trail
    entry's `tool_call_id` with `uuid.uuid4()` — the model cannot cite that id
    from a script fixed before the run. Monkeypatching `uuid` would silently
    change the ids of everything else the loop writes in the same turn.
    """

    def __init__(self, script: list[Any]) -> None:
        super().__init__([])
        self._items = list(script)
        self._index = 0

    async def send_turn(self, messages, tools):  # type: ignore[no-untyped-def]
        assert self._index < len(self._items), "model called more times than scripted"
        item = self._items[self._index]
        self._index += 1
        if callable(item):
            item = item()
            if inspect.isawaitable(item):
                item = await item
        self._script.append(item)
        return await super().send_turn(messages, tools)


def _build(
    script: list[Any],
    *,
    store: InMemorySessionStore | None = None,
    mcp: FakeMCPClient | None = None,
    blueprint_mcp: FakeMCPClient | None = None,
    max_loop_iterations: int = 15,
    max_budget_windows: int = 3,
) -> tuple[AgentLoop, InMemorySessionStore, list[tuple[str, dict[str, Any]]]]:
    store = store if store is not None else InMemorySessionStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    index = FakeVectorIndex()
    index.add_detail(_detail())
    executor = BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(blueprint_mcp or FakeMCPClient(), CATALOG),
        vector_index=index,
    )
    loop = AgentLoop(
        model_client=LateBindingModel(script),
        tool_dispatcher=ToolDispatcher(mcp or FakeMCPClient(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=max_loop_iterations,
        max_wall_clock_seconds=60,
        max_budget_windows=max_budget_windows,
        observer=_observe,
        runtime_tools={
            STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe),
            ANSWER: AnswerWithTableTool(),
            "runBlueprint": RunBlueprintTool(executor=executor),
        },
        blueprint_executor=executor,
    )
    return loop, store, events


def _init_call(call_id: str, *descriptions: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id, name=STATE, arguments={"intents": [{"description": d} for d in descriptions]}
    )


def _update_call(call_id: str, *updates: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=STATE, arguments={"intents": list(updates)})


def _answer_call(call_id: str, answer: str = "Here is the answer.") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=ANSWER, arguments={"answer": answer, "sql": "SELECT 1"})


def _blueprint_call(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id, name="runBlueprint", arguments={"id": _BID, "slot_bindings": {}}
    )


def _events(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


def _resume_mcp() -> FakeMCPClient:
    """Node 1 + the D56 verify probe — what makes the resumed result AUTHORITATIVE,
    which is condition 4 of 04's completion validator."""
    return FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )


# ---------------------------------------------------------------------------
# Seam 1: the window-local state across a mid-DAG blueprint resume
# ---------------------------------------------------------------------------


async def test_the_window_local_state_governs_after_a_blueprint_mid_dag_resume() -> None:
    """05 §E loads `analysis_state` ONCE at the top of `_run_loop_body`. The mid-DAG
    blueprint resume reaches `_run_loop` through `_resume_blueprint`, a different
    route from `run()` and from the `askUser` resume — if that route did not reload
    the local, the resumed window would enforce against `None` and a turn with two
    pending intents would finalize silently."""
    store = InMemorySessionStore()
    loop, _s, events = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", HEADCOUNT, ATTRITION), _blueprint_call("c1")],
            )
        ],
        store=store,
        blueprint_mcp=FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]}),
    )
    paused = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="two things")
    assert paused.status == "paused_ask_user"
    # 03 §E.1: the state call was committed BEFORE the pause.
    assert [i.status for i in live_analysis_state(await store.get_or_create_session(SESSION_ID), 0).intents] == [
        "pending",
        "pending",
    ]

    resumed_loop, _s2, resume_events = _build(
        [
            # The model tries to finalize the moment the blueprint comes back.
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
            ModelTurnResult(assistant_text="ok, I will resolve them"),
        ],
        store=store,
        blueprint_mcp=_resume_mcp(),
    )
    outcome = await resumed_loop.resume(
        session_id=SESSION_ID, credentials=_creds(), answer="approve"
    )

    # Refused, not finalized: the reloaded local governed the resumed window.
    assert _events(resume_events, "loop_finalization_refused") == [
        {"exit": "answer_with_table", "pending_count": 2}
    ]
    trail = await store.load_trail(SESSION_ID)
    refusal = next(e for e in trail if e.tool_call_id == "a1")
    assert refusal.error_code == FINALIZATION_BLOCKED_PENDING_INTENTS_CODE
    assert outcome.status == "done"
    del events


async def test_a_resumed_blueprint_entry_is_valid_completion_evidence() -> None:
    """The 03/04 × blueprint seam. `_resume_blueprint` writes the `runBlueprint`
    trail entry ITSELF (not through the tool), with a freshly minted
    `tool_call_id`, `args={"id":…, "resumed": True}` and the `authoritative`
    marker copied from the outcome. 04's completion validator requires all of
    `turn_index` match + `status == "ok"` + `authoritative is True` — if the resume
    path dropped any of them, every blueprint that paused for approval would be
    UNCITABLE, and the intent it answered could only end `ENFORCEMENT_EXHAUSTED`."""
    store = InMemorySessionStore()
    loop, _s, _e = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", HEADCOUNT), _blueprint_call("c1")],
            )
        ],
        store=store,
        blueprint_mcp=FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]}),
    )
    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="one thing")

    async def _cite_the_resumed_entry() -> ModelTurnResult:
        trail = await store.load_trail(SESSION_ID)
        resumed = [e for e in trail if e.tool_name == "runBlueprint" and e.args.get("resumed")]
        assert resumed, "the resume path wrote no runBlueprint trail entry"
        return ModelTurnResult(
            assistant_text=None,
            tool_calls=[
                _update_call(
                    "s2",
                    {
                        "intent_id": "i1",
                        "status": "completed",
                        "evidence_tool_call_id": resumed[-1].tool_call_id,
                    },
                ),
                _answer_call("a1", answer="Flagged 2 departments."),
            ],
        )

    resumed_loop, _s2, events = _build(
        [_cite_the_resumed_entry], store=store, blueprint_mcp=_resume_mcp()
    )
    outcome = await resumed_loop.resume(
        session_id=SESSION_ID, credentials=_creds(), answer="approve"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Flagged 2 departments."
    assert not _events(events, "loop_finalization_refused")
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [("completed", None)]
    # And the route is derivable from the telemetry: blueprint, not ad-hoc.
    assert _events(events, "loop_intent_completed") == [
        {"intent_id": "i1", "evidence_tool_name": "runBlueprint"}
    ]


async def test_a_blueprint_resume_does_not_hand_out_a_second_forced_reround() -> None:
    """05 §C.1 is why the counter is PERSISTED and keyed by window. A mid-DAG
    blueprint resume re-enters `_run_loop_body` with `window_count` UNCHANGED, so a
    counter local to that function would reset here and forced re-rounds would be
    unbounded — user-paced, but unbounded. The window's one re-round is spent
    before the pause; after the resume the same window must go straight to
    `ENFORCEMENT_EXHAUSTED`."""
    store = InMemorySessionStore()
    loop, _s, events = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            # Spend the window's re-round, then pause mid-DAG in the same batch.
            ModelTurnResult(
                assistant_text=None, tool_calls=[_answer_call("a1"), _blueprint_call("c1")]
            ),
        ],
        store=store,
        blueprint_mcp=FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]}),
    )
    paused = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="one thing")
    assert paused.status == "paused_ask_user"
    assert _events(events, "loop_finalization_block_spent") == [{"window": 1}]
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.finalization_blocks == {"0:1": 1}

    resumed_loop, _s2, resume_events = _build(
        [ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a2")])],
        store=store,
        blueprint_mcp=_resume_mcp(),
    )
    outcome = await resumed_loop.resume(
        session_id=SESSION_ID, credentials=_creds(), answer="approve"
    )

    assert outcome.status == "done"
    # No second re-round was granted, and the counter did not advance again.
    assert not _events(resume_events, "loop_finalization_refused")
    assert not _events(resume_events, "loop_finalization_block_spent")
    assert (await store.get_or_create_session(SESSION_ID)).finalization_blocks == {"0:1": 1}
    assert _events(resume_events, "loop_enforcement_exhausted") == [{"intent_count": 1}]
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [
        ("blocked", "ENFORCEMENT_EXHAUSTED")
    ]


# ---------------------------------------------------------------------------
# Seam 2: the budget-cap escapes in combination
# ---------------------------------------------------------------------------


async def test_a_budget_cap_during_a_refused_round_terminally_disposes_a_live_turn() -> None:
    """MEASURED, and it is the sharpest edge of 05 §F's fourth forced path.

    An ORDINARY budget-cap pause is explicitly NOT finalization (§G): the turn is
    still running and the user may answer "continue". The fourth path is gated on
    `finalization_refused_this_round` — but a budget-cap pause is a budget-cap
    pause either way, so the gate means a REFUSED round writes a TERMINAL
    disposition (`BUDGET_EXHAUSTED`) onto a turn that is still alive.

    The consequence, asserted here rather than assumed: after the user answers
    "continue" and is granted a fresh window, nothing is pending any more, so
    enforcement no longer applies and the model finalizes freely — with every
    intent recorded `BUDGET_EXHAUSTED` even though the budget was extended and the
    work could have been done. The ledger and the answer disagree.
    """
    store = InMemorySessionStore()
    loop, _s, events = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
        ],
        store=store,
        max_loop_iterations=2,
        max_budget_windows=3,
    )
    paused = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="one thing")

    assert paused.status == "paused_budget_cap"
    assert _events(events, "loop_finalization_refused") == [
        {"exit": "answer_with_table", "pending_count": 1}
    ]
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [("blocked", "BUDGET_EXHAUSTED")]

    # The user grants more budget. The disposition is NOT restored, and the next
    # window finalizes with no enforcement at all.
    resumed_loop, _s2, resume_events = _build(
        [ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a2", answer="Done.")])],
        store=store,
        max_loop_iterations=5,
        max_budget_windows=3,
    )
    outcome = await resumed_loop.resume(
        session_id=SESSION_ID, credentials=_creds(), answer="continue"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Done."
    assert not _events(resume_events, "loop_finalization_refused")
    final = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.status, i.reason_code) for i in final.intents] == [("blocked", "BUDGET_EXHAUSTED")]


async def test_two_answers_in_one_batch_with_the_counter_spent_terminate_once() -> None:
    """The counter is spent in window 1, so the FIRST `answerWithTable` of a
    `[answerWithTable, answerWithTable]` batch force-blocks and finalizes. The
    second must find nothing pending and must NOT force-block again: one
    `loop_enforcement_exhausted`, one transition per intent, one terminal exit."""
    store = InMemorySessionStore()
    loop, _s, events = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _answer_call("a2", answer="first"),
                    _answer_call("a3", answer="second"),
                ],
            ),
        ],
        store=store,
    )
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="one thing")

    assert outcome.status == "done"
    # Round 2 refused once (spending the window's block); round 3 exhausted once.
    assert _events(events, "loop_finalization_refused") == [
        {"exit": "answer_with_table", "pending_count": 1}
    ]
    assert _events(events, "loop_enforcement_exhausted") == [{"intent_count": 1}]
    assert _events(events, "loop_intent_force_blocked") == [
        {"intent_id": "i1", "reason_code": "ENFORCEMENT_EXHAUSTED"}
    ]
    assert len(_events(events, "loop_analysis_state_transition")) == 1
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [
        ("blocked", "ENFORCEMENT_EXHAUSTED")
    ]


# ---------------------------------------------------------------------------
# Seam 3: the force-block write itself failing
# ---------------------------------------------------------------------------


class _ForceBlockFailsStore(InMemorySessionStore):
    """A store whose `apply_analysis_state` fails for the FORCED path only.

    `_force_block_pending_intents` is degrade-never-fail by design — it runs on
    terminal paths already returning a result, so a store failure is logged and
    swallowed. The thing that must not degrade with it is the TELEMETRY: an event
    reporting a transition that was never written is worse than no event, because
    07's coverage report is computed from exactly these events.
    """

    def __init__(self) -> None:
        super().__init__()
        self.forced_attempts = 0

    async def apply_analysis_state(self, session_id, turn_index, merge):  # type: ignore[no-untyped-def]
        doc = await self.get_or_create_session(session_id)
        state = live_analysis_state(doc, turn_index)
        if state is not None and any(i.status == "pending" for i in state.intents):
            probe = merge(state)
            if all(i.reason_code == "ENFORCEMENT_EXHAUSTED" for i in probe.intents):
                self.forced_attempts += 1
                raise RuntimeError("couchbase is down")
        return await super().apply_analysis_state(session_id, turn_index, merge)


async def test_a_failing_force_block_returns_the_answer_and_claims_no_transition() -> None:
    store = _ForceBlockFailsStore()
    loop, _s, events = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a2", answer="Done.")]),
        ],
        store=store,
    )
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="one thing")

    # The user still gets their answer — losing the forced disposition is bad,
    # aborting the answer to record it is worse.
    assert outcome.status == "done"
    assert outcome.assistant_text == "Done."
    assert store.forced_attempts == 1
    # ...but nothing may CLAIM the write landed.
    assert not _events(events, "loop_intent_force_blocked")
    assert not [
        payload
        for payload in _events(events, "loop_analysis_state_transition")
        if payload["reason_code"] == "ENFORCEMENT_EXHAUSTED"
    ]
    # And the ledger honestly still says `pending` — the invariant is BROKEN here,
    # visibly, which is the point: a swallowed store failure must not look clean.
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [i.status for i in state.intents] == ["pending"]


# ---------------------------------------------------------------------------
# Seam 4: the resume that loses the CAS race (05 §F.1 case 3)
# ---------------------------------------------------------------------------


async def test_a_resume_that_loses_the_cas_race_force_blocks_nothing() -> None:
    """05 §F.1 case 3, and the reason the invariant is SCOPED. `resume()` raises
    from `resume_checkpoint` BEFORE any escape is reachable, so the losing side of
    a race has no path to a force-block — its turn simply never terminated. The
    winner's `"stop"` is what writes `USER_STOPPED`; the loser must write nothing
    and must emit nothing."""
    store = InMemorySessionStore()
    loop, _s, events = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
        ],
        store=store,
        max_loop_iterations=2,
        max_budget_windows=3,
    )
    # Reach a budget-cap pause WITHOUT a refusal, so nothing is force-blocked yet.
    loop._max_loop_iterations = 1  # noqa: SLF001 - the cap is what makes the pause
    paused = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="one thing")
    assert paused.status == "paused_budget_cap"
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [i.status for i in state.intents] == ["pending"]

    # Two resumers read the same CAS; the first to write wins (the in-memory
    # store's version counter is a faithful CAS emulation — see its docstring).
    winner_loop, _s2, winner_events = _build([], store=store)
    loser_loop, _s3, loser_events = _build([], store=store)
    doc_a, cas_a = await store.get_session_with_cas(SESSION_ID)
    doc_b, cas_b = await store.get_session_with_cas(SESSION_ID)
    assert cas_a == cas_b
    del doc_a, doc_b

    done = await winner_loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="stop")
    assert done.status == "done"
    assert _events(winner_events, "loop_intent_force_blocked") == [
        {"intent_id": "i1", "reason_code": "USER_STOPPED"}
    ]

    with pytest.raises((CASMismatchError, Exception)) as raised:
        await loser_loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="stop")
    assert raised.type.__name__ in {"CASMismatchError", "AlreadyConsumedError"}
    # The loser wrote nothing and claimed nothing.
    assert not loser_events
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [("blocked", "USER_STOPPED")]
    del events


# ---------------------------------------------------------------------------
# Seam 5: ordering inside one model response — askUser past the dispatch cap
# ---------------------------------------------------------------------------


async def test_ask_user_past_the_dispatch_cap_still_pauses_the_turn() -> None:
    """WAS a strict xfail — a Release-1 regression, now fixed.

    `ask_user_call` had come to be selected from `other_calls`, which is TRUNCATED to
    `max_tool_calls_per_iteration` by the 03 §E.2 partition. Measured with cap 2 and
    the batch `[runQuery, runQuery, askUser]`: the turn did NOT pause, both queries
    dispatched, and the user was never asked. The scan runs over the RAW
    `result.tool_calls` again (agent_loop.py, beside the state partition, for the
    identical reason — capping silently discards the overflow)."""
    store = InMemorySessionStore()
    loop, _s, _events = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    ToolCallRequest(id="q1", name="runQuery", arguments={"sql": "SELECT 1"}),
                    ToolCallRequest(id="q2", name="runQuery", arguments={"sql": "SELECT 2"}),
                    ToolCallRequest(id="ask", name="askUser", arguments={"question": "which?"}),
                ],
            ),
            ModelTurnResult(assistant_text="answered without asking"),
        ],
        store=store,
        mcp=FakeMCPClient(scripted={"runQuery": [_rq(["x"], [[1]]), _rq(["x"], [[2]])]}),
    )
    loop._max_tool_calls_per_iteration = 2  # noqa: SLF001 - the cap under test

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="ambiguous")

    assert outcome.status == "paused_ask_user", (
        "an askUser beyond the per-iteration cap was silently discarded: the model's "
        "request to pause and ask the user never reached the user"
    )


# ---------------------------------------------------------------------------
# Seam 6: hostile intra-batch ordering around the terminal exit
# ---------------------------------------------------------------------------


async def test_an_answer_first_batch_still_commits_the_state_before_the_answer() -> None:
    """The hostile array order for 05 §G. The model puts `answerWithTable` FIRST
    and the state update that closes the last intent SECOND. Dispatch walks
    `capped_tool_calls` in array order, so without 03 §E.2's partition the answer
    would be refused for an intent the same response was about to close — costing a
    round-trip and burning the window's one forced re-round on nothing."""
    store = InMemorySessionStore()
    loop, _s, events = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", HEADCOUNT),
                    ToolCallRequest(id="q1", name="runQuery", arguments={"sql": "SELECT 1"}),
                ],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _answer_call("a1", answer="12 people."),
                    _update_call(
                        "s2",
                        {
                            "intent_id": "i1",
                            "status": "completed",
                            "evidence_tool_call_id": "q1",
                        },
                    ),
                ],
            ),
        ],
        store=store,
        mcp=FakeMCPClient(scripted={"runQuery": [_rq(["x"], [[1]])]}),
    )
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="one thing")

    assert outcome.status == "done"
    assert outcome.assistant_text == "12 people."
    assert not _events(events, "loop_finalization_refused")
    assert not _events(events, "loop_finalization_block_spent")
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [i.status for i in state.intents] == ["completed"]


async def test_a_surplus_state_call_cannot_launder_a_pending_intent_past_the_answer() -> None:
    """Three state calls plus an answer, in the worst order. The THIRD state call is
    the one that would close the last intent — and it is the surplus, rejected by
    `MAX_STATE_CALLS`. The answer must therefore still be refused, not finalized on
    a write that never landed. (`_refreshed_analysis_state` returning `None` for a
    rejected call is what makes the enforcement local hold its ground.)"""
    store = InMemorySessionStore()
    loop, _s, events = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", HEADCOUNT, ATTRITION),
                    ToolCallRequest(id="q1", name="runQuery", arguments={"sql": "SELECT 1"}),
                ],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _answer_call("a1"),
                    _update_call(
                        "s2",
                        {
                            "intent_id": "i1",
                            "status": "completed",
                            "evidence_tool_call_id": "q1",
                        },
                    ),
                    ToolCallRequest(id="s3", name=STATE, arguments={"intents": []}),
                    _update_call(
                        "s4",
                        {
                            "intent_id": "i2",
                            "status": "completed",
                            "evidence_tool_call_id": "q1",
                        },
                    ),
                ],
            ),
            ModelTurnResult(assistant_text="giving up"),
        ],
        store=store,
        mcp=FakeMCPClient(scripted={"runQuery": [_rq(["x"], [[1]])]}),
    )
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="two things")

    assert _events(events, "loop_finalization_refused") == [
        {"exit": "answer_with_table", "pending_count": 1}
    ]
    surplus = [
        payload
        for payload in _events(events, "loop_analysis_state_rejected")
        if payload["reason"] == "surplus_state_call"
    ]
    assert surplus == [{"reason": "surplus_state_call", "intent_count": 0}]
    # i2 was NEVER completed by the discarded surplus call. The turn ends with the
    # runtime's own disposition on it, which is the honest record.
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [
        ("completed", None),
        ("blocked", "ENFORCEMENT_EXHAUSTED"),
    ]
    assert outcome.status == "done"


async def test_two_state_calls_in_one_batch_are_applied_in_order_and_the_local_follows() -> None:
    """The second state call of a batch must MERGE onto the first's write, not onto
    the document read before the batch began — and the enforcement local must end up
    holding the SECOND call's result, or the answer in the same batch would be
    refused against a state that is one call stale."""
    store = InMemorySessionStore()
    loop, _s, events = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", HEADCOUNT, ATTRITION),
                    ToolCallRequest(id="q1", name="runQuery", arguments={"sql": "SELECT 1"}),
                ],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _answer_call("a1", answer="both done"),
                    _update_call(
                        "s2",
                        {
                            "intent_id": "i1",
                            "status": "completed",
                            "evidence_tool_call_id": "q1",
                        },
                    ),
                    _update_call(
                        "s3",
                        {
                            "intent_id": "i2",
                            "status": "completed",
                            "evidence_tool_call_id": "q1",
                        },
                    ),
                ],
            ),
        ],
        store=store,
        mcp=FakeMCPClient(scripted={"runQuery": [_rq(["x"], [[1]])]}),
    )
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="two things")

    assert outcome.status == "done"
    assert outcome.assistant_text == "both done"
    assert not _events(events, "loop_finalization_refused")
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [i.status for i in state.intents] == ["completed", "completed"]


# ---------------------------------------------------------------------------
# Seam 7: the forced-re-round counter across TURNS
# ---------------------------------------------------------------------------


async def test_a_second_turn_gets_its_own_forced_reround() -> None:
    """WAS a strict xfail — high-blast-radius defect, now fixed.

    `SessionDoc.finalization_blocks` was keyed by BUDGET WINDOW NUMBER ALONE
    (`{"1": 1}`, 05 §C.1) and is never cleared at a turn boundary, while
    `window_count` RESTARTS AT 1 for every new external turn — so turn 1's window 1
    collided with turn 0's. Measured: turn 0 spent its re-round normally; turn 1's
    FIRST finalization attempt was refused a re-round outright, emitting no
    `loop_finalization_refused`, building no nudge, and writing
    `ENFORCEMENT_EXHAUSTED` for an intent the model was never asked twice about —
    which also inflates 07's headline metric. Same class as the bug
    `live_analysis_state` fixed: a per-turn value persisted on the session doc with
    no turn gate. The key is now `(turn_index, window)`
    (`session/models.py::finalization_block_key`)."""
    store = InMemorySessionStore()
    turn_zero, _s, zero_events = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
            ModelTurnResult(assistant_text="giving up"),
        ],
        store=store,
    )
    await turn_zero.run(session_id=SESSION_ID, credentials=_creds(), user_message="first ask")
    assert _events(zero_events, "loop_finalization_block_spent") == [{"window": 1}]
    assert (await store.get_or_create_session(SESSION_ID)).finalization_blocks == {"0:1": 1}

    # A brand-new, unrelated question. It declares its own intents and makes its
    # FIRST finalization attempt with one still pending — it has never been given a
    # forced re-round, so it must get one.
    turn_one, _s2, one_events = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s2", ATTRITION)]),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a2")]),
            ModelTurnResult(assistant_text="second answer"),
        ],
        store=store,
    )
    await turn_one.run(session_id=SESSION_ID, credentials=_creds(), user_message="second ask")

    assert _events(one_events, "loop_finalization_refused") == [
        {"exit": "answer_with_table", "pending_count": 1}
    ], (
        "the second turn's first finalization attempt was force-blocked without "
        "ever being refused once — its forced re-round was consumed by a PREVIOUS "
        "turn that happened to use the same window number"
    )
