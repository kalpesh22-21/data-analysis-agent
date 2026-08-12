"""Finalization enforcement — where `analysisState` gets teeth (Release 1, doc 05).

The invariant these tests defend is SCOPED, and the scope is the point (05 §F.1):

    No intent ends `pending` on any turn that reaches a TERMINAL outcome
    (`done` / `stopped_hard_ceiling`).

A turn abandoned at an `askUser`/budget-cap pause, or one whose resume loses a CAS
race, is a NON-TERMINATED turn and legitimately leaves `pending` state behind —
so `test_an_ask_user_pause_with_pending_intents_is_not_gated` and
`test_an_abandoned_pause_leaves_pending_state_and_does_not_touch_the_next_turn`
are as load-bearing as the refusal tests. An unscoped assertion would fail against
any real store.

Every failure mode covered here is SILENT if it regresses: the feature stays
wired, the model still declares intents, and the turn simply finalizes with work
undone.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.denial_mapping import classify_denial
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.hooks.answer_table import AnswerTableHooks
from data_agent.runtime.loop.agent_loop import (
    FINALIZATION_BLOCKED_PENDING_INTENTS_CODE,
    AgentLoop,
)
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import (
    AnalysisState,
    TrackedIntent,
    TurnMessage,
    live_analysis_state,
)
from data_agent.runtime.session_history import project_history

SESSION_ID = "sess-finalization"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"

# A marker string that appears ONLY inside an intent description, so a leak into
# telemetry / a persisted message is unambiguous wherever it turns up.
HEADCOUNT = "headcount by department"
ATTRITION = "attrition by department"


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return []


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


class CountingStore(InMemorySessionStore):
    """The real in-memory store, counting the calls enforcement could make.

    05 §A/§E: most turns are single-intent, and the fast path must stay an
    `is None` test on a window-local — never a store round-trip at each terminal
    exit. `claims`/`analysis_state_writes` are how that is asserted rather than
    assumed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.claims = 0
        self.analysis_state_writes = 0
        self.session_reads = 0

    async def get_or_create_session(self, session_id: str):  # type: ignore[no-untyped-def]
        self.session_reads += 1
        return await super().get_or_create_session(session_id)

    async def claim_finalization_block(
        self, session_id: str, turn_index: int, window_count: int
    ) -> bool:
        self.claims += 1
        return await super().claim_finalization_block(session_id, turn_index, window_count)

    async def apply_analysis_state(self, session_id, turn_index, merge):  # type: ignore[no-untyped-def]
        self.analysis_state_writes += 1
        return await super().apply_analysis_state(session_id, turn_index, merge)


def _build(
    turns: list[ModelTurnResult],
    *,
    mcp: FakeMCPClient | None = None,
    max_loop_iterations: int = 15,
    max_budget_windows: int = 3,
) -> tuple[AgentLoop, CountingStore, list[tuple[str, dict[str, Any]]], ScriptedModelClient]:
    store = CountingStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    model = ScriptedModelClient(turns)
    dispatcher = ToolDispatcher(mcp or FakeMCPClient(), CATALOG, observer=_observe)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
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
        },
    )
    return loop, store, events, model


# --- scripted call shapes ---------------------------------------------------


def _init_call(call_id: str, *descriptions: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name=STATE,
        arguments={"intents": [{"description": d} for d in descriptions]},
    )


def _update_call(call_id: str, *updates: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=STATE, arguments={"intents": list(updates)})


def _completed(intent_id: str, evidence: str) -> dict[str, Any]:
    return {
        "intent_id": intent_id,
        "status": "completed",
        "evidence_tool_call_id": evidence,
    }


def _answer_call(call_id: str, answer: str = "Here is the answer.") -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id, name=ANSWER, arguments={"answer": answer, "sql": "SELECT 1"}
    )


def _query_call(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="runQuery", arguments={"sql": f"SELECT {call_id}"})


def _query_mcp(count: int = 10, *, row_count: int = 1) -> FakeMCPClient:
    rows = [[1]] * row_count
    return FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["x"], "rows": rows, "row_count": row_count, "truncated": False}
            ]
            * count
        }
    )


def _events(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


def _nudge_requests(model: ScriptedModelClient) -> list[int]:
    """Indices of the model requests that carried the finalization nudge."""
    return [
        index
        for index, call in enumerate(model.calls)
        if any(
            "re-send your final answer" in str(message.get("content") or "")
            for message in call.messages
        )
    ]


# --- no live state: the fast path ------------------------------------------


async def test_a_turn_with_no_live_state_finalizes_through_both_exits() -> None:
    """05 §A: no live state ⇒ NO ENFORCEMENT. Most turns are single-intent and
    never declare a state at all, so both exits must finalize untouched and the
    check must cost nothing — no claim, no state write, no telemetry."""
    for script, expected in (
        ([ModelTurnResult(assistant_text="plain prose")], "plain prose"),
        (
            [
                ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
            ],
            "Here is the answer.",
        ),
    ):
        loop, store, events, _ = _build(script)
        outcome = await loop.run(
            session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
        )
        assert outcome.status == "done"
        assert outcome.assistant_text == expected
        assert store.claims == 0, "enforcement paid for a store round-trip with no state"
        assert store.analysis_state_writes == 0
        assert not [name for name, _ in events if name.startswith("loop_finalization")]
        assert not _events(events, "loop_intent_force_blocked")


async def test_a_prior_turns_pending_state_does_not_govern_the_next_turn() -> None:
    """05 §A, the failure that matters: turn 0 declares two intents and is
    abandoned at an `askUser`. Turn 1 is an ordinary single-intent question — it
    must finalize, turn 0's record must be untouched, and no telemetry may be
    emitted against it. Without the turn gate, turn 1 is refused, burns its nudge,
    and writes `ENFORCEMENT_EXHAUSTED` onto TURN 0's intents."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", HEADCOUNT, ATTRITION),
                    ToolCallRequest(id="ask", name="askUser", arguments={"question": "which?"}),
                ],
            ),
            ModelTurnResult(assistant_text="an unrelated answer"),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")
    claims_before = store.claims
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="something else"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "an unrelated answer"
    doc = await store.get_or_create_session(SESSION_ID)
    turn0 = live_analysis_state(doc, 0)
    assert turn0 is not None
    assert [i.status for i in turn0.intents] == ["pending", "pending"]
    assert [i.reason_code for i in turn0.intents] == [None, None]
    assert live_analysis_state(doc, 1) is None
    assert store.claims == claims_before
    assert not [name for name, _ in events if name.startswith("loop_finalization")]


async def test_all_intents_completed_finalizes() -> None:
    """The happy path: every intent closed on real evidence, so the terminal exit
    is not touched at all."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", HEADCOUNT), _query_call("q1")],
            ),
            ModelTurnResult(
                assistant_text=None, tool_calls=[_update_call("s2", _completed("i1", "q1"))]
            ),
            ModelTurnResult(assistant_text="both answered"),
        ],
        mcp=_query_mcp(),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "both answered"
    assert store.claims == 0
    assert not [name for name, _ in events if name.startswith("loop_finalization")]


# --- exit #2: answerWithTable ----------------------------------------------


async def test_exit_two_is_refused_with_a_retryable_error_naming_the_intents() -> None:
    """05 §B.1. The refusal is written INSTEAD of the successful designation, so
    the persisted trail entry IS the refusal — and `denial_detail` is the only
    channel that reaches the model, so it must name what is still pending."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT, ATTRITION)]
            ),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
            ModelTurnResult(assistant_text="second thoughts"),
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="two things"
    )

    # The turn CONTINUED — the refusal is not terminal.
    assert outcome.status == "done"
    assert outcome.assistant_text == "second thoughts"
    doc = await store.get_or_create_session(SESSION_ID)
    entry = next(e for e in doc.tool_trail if e.tool_call_id == "a1")
    assert entry.status == "error"
    assert entry.error_code == FINALIZATION_BLOCKED_PENDING_INTENTS_CODE
    assert entry.denial_detail is not None
    assert "i1" in entry.denial_detail and "i2" in entry.denial_detail
    assert HEADCOUNT in entry.denial_detail
    # `provenance=frozenset()` — determined-empty — is what keeps the entry in
    # scope on its own merits; `None` would be dropped fail-closed from replay.
    assert entry.provenance == frozenset()
    # Retryable, or the turn it exists to keep alive would end here.
    assert classify_denial(entry.error_code).retryable is True
    assert _events(events, "loop_finalization_refused") == [
        {"exit": "answer_with_table", "pending_count": 2}
    ]
    # The designation never became the turn's answer table.
    assert outcome.answer_sql is None


async def test_the_refusal_precedes_answer_sql_resolution_and_its_hooks() -> None:
    """05 §B.1 — WHERE the check sits. `_resolve_answer_sql` fires the two dormant
    `hooks/answer_table.py` seams and can rewrite the result into the
    blueprint-not-run nudge. A designation that is about to be refused must not
    fire the answer-table lifecycle, and checking afterwards would also clobber
    that more actionable message with this less actionable one."""
    fired: list[Any] = []

    def _record(event: Any) -> str | None:
        fired.append(event)
        return None

    store = CountingStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    loop = AgentLoop(
        model_client=ScriptedModelClient(
            [
                ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
                ModelTurnResult(
                    assistant_text=None,
                    tool_calls=[
                        ToolCallRequest(
                            id="a1",
                            name=ANSWER,
                            # Names a blueprint that never ran this turn — the
                            # `ANSWER_TABLE_BLUEPRINT_NOT_RUN` shape.
                            arguments={"answer": "Here it is.", "blueprint_id": "bp-never-ran"},
                        )
                    ],
                ),
                ModelTurnResult(assistant_text="ok"),
            ]
        ),
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=_observe,
        runtime_tools={
            STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe),
            ANSWER: AnswerWithTableTool(),
        },
        answer_table_hooks=AnswerTableHooks(on_unresolved=[_record], on_ephemeral=[_record]),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="one thing")

    doc = await store.get_or_create_session(SESSION_ID)
    entry = next(e for e in doc.tool_trail if e.tool_call_id == "a1")
    assert entry.error_code == FINALIZATION_BLOCKED_PENDING_INTENTS_CODE
    assert not fired, "the answer-table lifecycle fired for a designation being refused"


async def test_a_blank_answer_is_not_a_finalization_and_is_not_refused() -> None:
    """The terminal condition is `ok` + a dict + NON-BLANK `answer`. A call that
    would not have ended the turn is not a finalization, so refusing it would burn
    the window's one forced re-round on nothing."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    ToolCallRequest(
                        id="a1", name=ANSWER, arguments={"answer": "   ", "sql": "SELECT 1"}
                    )
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    doc = await store.get_or_create_session(SESSION_ID)
    designation = next(e for e in doc.tool_trail if e.tool_call_id == "a1")
    assert designation.status == "ok"
    assert designation.error_code is None, "a blank answer was refused as a finalization"
    # Only the later prose exits are refusals; the designation is not one.
    assert all(
        payload["exit"] == "no_tool_calls"
        for payload in _events(events, "loop_finalization_refused")
    )


# --- exit #1: a model turn with no tool calls -------------------------------


async def test_exit_one_refuses_without_persisting_and_nudges_with_the_draft() -> None:
    """05 §B.2/§B.3. Exit #1 has NO error channel, so the refusal is an ephemeral
    `user`-role injection — and it carries the draft back, because nothing on this
    path is persisted and D22 discards free text, so the model would otherwise
    have to regenerate its answer blind."""
    loop, store, events, model = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text="THE DRAFT ANSWER"),
            ModelTurnResult(assistant_text="the real answer"),
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "the real answer"
    # The loop RE-ENTERED: three model round-trips, not two.
    assert model.calls_made == 3
    doc = await store.get_or_create_session(SESSION_ID)
    assistant_messages = [m.content for m in doc.messages if m.role == "assistant"]
    assert assistant_messages == ["the real answer"], "the refused draft was persisted"
    assert _events(events, "loop_finalization_refused") == [
        {"exit": "no_tool_calls", "pending_count": 1}
    ]
    # The nudge quotes the draft back on the re-round.
    nudged = model.calls[2].messages[-1]
    assert nudged["role"] == "user"
    assert "THE DRAFT ANSWER" in nudged["content"]
    assert "i1" in nudged["content"] and HEADCOUNT in nudged["content"]


async def test_the_nudge_is_never_persisted_and_lives_exactly_one_round_trip() -> None:
    """05 §D. Copying `discovery_canonical`'s once-per-window lifetime would repeat
    the nudge forever — including after the intents were closed — and, being
    ephemeral and tail-anchored, would migrate it to be the newest message on every
    rebuild, appearing after tool results it predates."""
    loop, store, _events_, model = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text="draft"),
            ModelTurnResult(assistant_text=None, tool_calls=[_query_call("q1")]),
            ModelTurnResult(assistant_text="final"),
        ],
        mcp=_query_mcp(),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="one thing")

    assert _nudge_requests(model) == [2], "the nudge outlived its one round-trip"
    doc = await store.get_or_create_session(SESSION_ID)
    assert all("re-send your final answer" not in m.content for m in doc.messages)
    history = project_history(doc.messages, doc.tool_trail, frozenset(), doc.pause_checkpoint)
    assert "re-send your final answer" not in str(history)


async def test_the_state_block_precedes_the_question_and_the_nudge_is_last() -> None:
    """05 §D.1 — THIS deliverable owns the splice order. `_insert_retrieval` (and
    03's state block with it) anchors on the LAST `user` message, so appending the
    nudge first would make the nudge that anchor and land the state block AFTER the
    question instead of before it."""
    loop, _store, _events_, model = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text="draft"),
            ModelTurnResult(assistant_text="final"),
        ]
    )

    await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="THE QUESTION"
    )

    request = model.calls[2].messages
    contents = [str(m.get("content") or "") for m in request]
    state_block = next(i for i, c in enumerate(contents) if "[Analysis state" in c)
    question = next(i for i, c in enumerate(contents) if c == "THE QUESTION")
    nudge = next(i for i, c in enumerate(contents) if "re-send your final answer" in c)
    assert state_block < question, "the nudge stole the anchor from the state block"
    assert nudge == len(request) - 1, "the nudge must be the tail"


# --- the block counter ------------------------------------------------------


async def test_a_refused_round_is_charged_to_the_budget() -> None:
    """05 §C.3. Exit #1 sits BEFORE `guard.record_iteration`, so a `continue` that
    skipped it would make the forced re-round free — no iteration, no tokens —
    leaving the 60-second wall clock as the only backstop, and every resume
    restarts that. Here one iteration is the whole window, so the refused round
    itself trips the cap."""
    loop, store, events, model = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text="draft"),
        ],
        max_loop_iterations=2,
        max_budget_windows=2,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "paused_budget_cap"
    assert model.calls_made == 2, "the refused round was not charged (it looped on)"
    # The refused draft must NOT reach the user on the pause path (§B.3).
    assert outcome.assistant_text is None
    # 05 §F, fourth forced path: the cap was reached DURING a refused round.
    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert [i.status for i in state.intents] == ["blocked"]
    assert [i.reason_code for i in state.intents] == ["BUDGET_EXHAUSTED"]
    assert _events(events, "loop_intent_force_blocked") == [
        {"intent_id": "i1", "reason_code": "BUDGET_EXHAUSTED"}
    ]


async def test_the_second_attempt_is_enforcement_exhausted_and_finalization_proceeds() -> None:
    """The window's one forced re-round is spent, the intents are still pending, so
    the runtime records the disposition it CAN establish and lets the turn finish.

    `ENFORCEMENT_EXHAUSTED` means "enforcement could not establish a disposition" —
    NOT that the system proved the intent impossible."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT, ATTRITION)]
            ),
            ModelTurnResult(assistant_text="draft"),
            ModelTurnResult(assistant_text="still no closure"),
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="two things"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "still no closure"
    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert [i.status for i in state.intents] == ["blocked", "blocked"]
    assert {i.reason_code for i in state.intents} == {"ENFORCEMENT_EXHAUSTED"}
    # Runtime-forced blocks cite NO evidence — that is what distinguishes them
    # from a model-declared block in the ledger.
    assert all(i.evidence_tool_call_id is None for i in state.intents)
    assert _events(events, "loop_enforcement_exhausted") == [{"intent_count": 2}]
    assert doc.finalization_blocks == {"0:1": 1}


async def test_the_counter_is_not_reset_by_an_ask_user_resume() -> None:
    """05 §C.1 — the reason the counter is PERSISTED. `_run_loop_body` is re-entered
    on every resume while `window_count` stands still, so a counter local to it
    resets on every `askUser` and every mid-DAG blueprint resume, and forced
    re-rounds become unbounded (user-paced, but unbounded)."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
            ModelTurnResult(assistant_text="draft one"),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    ToolCallRequest(id="ask", name="askUser", arguments={"question": "which?"})
                ],
            ),
            ModelTurnResult(assistant_text="draft two"),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="one thing")
    outcome = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="department"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "draft two", (
        "the resumed window granted a SECOND forced re-round — the counter reset"
    )
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.finalization_blocks == {"0:1": 1}
    assert len(_events(events, "loop_finalization_block_spent")) == 1
    assert _events(events, "loop_enforcement_exhausted") == [{"intent_count": 1}]


# --- runtime-forced terminal escapes (05 §F) --------------------------------


async def test_the_hard_ceiling_force_blocks_with_budget_exhausted() -> None:
    """`stopped_hard_ceiling` IS a terminal outcome, so the scoped invariant
    applies there too."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
        ],
        max_loop_iterations=1,
        max_budget_windows=1,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "stopped_hard_ceiling"
    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [
        ("blocked", "BUDGET_EXHAUSTED")
    ]
    assert _events(events, "loop_analysis_state_transition") == [
        {
            "intent_id": "i1",
            "from_status": "pending",
            "to_status": "blocked",
            "reason_code": "BUDGET_EXHAUSTED",
        }
    ]


async def test_a_stop_answer_at_the_budget_cap_force_blocks_with_user_stopped() -> None:
    """05 §F — the THIRD `done` return, and the one that inherits nothing: it
    returns from inside `resume()` before `_run_loop` is ever entered, so it needs
    its own force-block call."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT)]),
        ],
        max_loop_iterations=1,
        max_budget_windows=2,
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )
    assert paused.status == "paused_budget_cap"
    outcome = await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="stop")

    assert outcome.status == "done"
    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [("blocked", "USER_STOPPED")]
    assert _events(events, "loop_intent_force_blocked") == [
        {"intent_id": "i1", "reason_code": "USER_STOPPED"}
    ]


async def test_a_stop_answer_does_not_rewrite_an_earlier_turns_state() -> None:
    """The §A turn gate applies on the `"stop"` path too: a state left behind by an
    abandoned EARLIER turn must not be stamped `USER_STOPPED` by a later one."""
    store = CountingStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    loop = AgentLoop(
        model_client=ScriptedModelClient(
            [ModelTurnResult(assistant_text=None, tool_calls=[_query_call("q1")])]
        ),
        tool_dispatcher=ToolDispatcher(_query_mcp(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=1,
        max_wall_clock_seconds=60,
        max_budget_windows=2,
        observer=_observe,
    )
    # Turn 0 happened and left a pending state behind; the run below is turn 1.
    await store.append_message(
        SESSION_ID,
        TurnMessage(turn_index=0, role="user", content="an older question", ts="2026-08-11T00:00:00+00:00"),
    )
    await store.apply_analysis_state(
        SESSION_ID,
        0,
        lambda _current: AnalysisState(
            turn_index=0,
            intents=(TrackedIntent(intent_id="i1", description=HEADCOUNT, status="pending"),),
        ),
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="a new question"
    )
    assert paused.status == "paused_budget_cap"
    await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="stop")

    doc = await store.get_or_create_session(SESSION_ID)
    assert [(i.status, i.reason_code) for i in doc.analysis_state.intents] == [
        ("pending", None)
    ]
    assert not _events(events, "loop_intent_force_blocked")


async def test_an_ask_user_pause_with_pending_intents_is_not_gated() -> None:
    """05 §G. A pause is NOT finalization: it returns a non-`done` status with
    intents legitimately pending, and the turn is not over."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", HEADCOUNT, ATTRITION),
                    ToolCallRequest(id="ask", name="askUser", arguments={"question": "which?"}),
                ],
            )
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="two things"
    )

    assert outcome.status == "paused_ask_user"
    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert [i.status for i in state.intents] == ["pending", "pending"]
    assert store.claims == 0
    assert not [name for name, _ in events if name.startswith("loop_finalization")]
    assert not _events(events, "loop_intent_force_blocked")


async def test_an_ordinary_budget_cap_pause_is_not_gated() -> None:
    """The budget-cap force-block is scoped to a REFUSED round (05 §F row 4). An
    ordinary cap pause is §G: the user may still answer "continue", and stamping a
    terminal disposition on a turn that is still running would be wrong."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None, tool_calls=[_init_call("s1", HEADCOUNT), _query_call("q1")]
            ),
        ],
        mcp=_query_mcp(),
        max_loop_iterations=1,
        max_budget_windows=2,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "paused_budget_cap"
    doc = await store.get_or_create_session(SESSION_ID)
    assert [i.status for i in live_analysis_state(doc, 0).intents] == ["pending"]
    assert not _events(events, "loop_intent_force_blocked")


# --- the batch that closes and finalizes in one response --------------------


async def test_closing_the_last_intent_and_answering_in_one_response_finalizes() -> None:
    """03 §E.2 commits `updateAnalysisState` BEFORE `answerWithTable` in the same
    response, and 05 §E refreshes the enforcement local from that result — so the
    model closes its last intent and finalizes in ONE round rather than being
    refused into an extra one."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", HEADCOUNT), _query_call("q1")],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _answer_call("a1", answer="Headcount is 42."),
                    _update_call("s2", _completed("i1", "q1")),
                ],
            ),
        ],
        mcp=_query_mcp(),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Headcount is 42."
    assert not _events(events, "loop_finalization_refused")
    assert store.claims == 0
    doc = await store.get_or_create_session(SESSION_ID)
    assert [i.status for i in live_analysis_state(doc, 0).intents] == ["completed"]
