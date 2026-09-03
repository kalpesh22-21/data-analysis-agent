"""The askUser JUDGE (doc 09 §C.3), end to end through the real loop.

THE CHEAPEST SITE IN THE DESIGN, and these tests are mostly about why. Every other judge
site risks an answer the model already has; this one risks nothing, because the pause has
not happened — the user has seen no question, so a rejection costs one round-trip that is
invisible to them. The tests therefore assert the USER-VISIBLE outcome, not merely that a
nudge appeared: the rejected question must never reach `pending_question`.

TWO ORDERING FACTS CARRY THE REST.

THE JUDGE SEES THE RAW ARGUMENT, BEFORE `scrub_answer_prose`. The scrub already turns
"which AnnualSalary did you mean?" into "which [schema detail withheld] did you mean?" —
05 §L.3's trap, a half-redacted string that is neither usable nor honest. Judging the
scrubbed form would ask the model to repair a string it did not write.

THE STATE CALLS OF THE BATCH ARE ALREADY COMMITTED (03 §E.1). A rejection re-rounds
INSIDE the same window, so an `[updateAnalysisState, askUser]` response must keep its
ledger write — the exact shape that used to lose it entirely.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.loop.answer_judge import (
    ANSWER_JUDGE_SKIPPED_EVENT,
    APPROVED,
    ASK_USER_JUDGE_EXHAUSTED_EVENT,
    ASK_USER_JUDGE_REFUSED_EVENT,
    JudgeBrief,
    JudgeVerdict,
)
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import FinalizationBlockKind

SESSION_ID = "sess-ask-user-judge"
_E = "hr.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "AnnualSalary": "Nullable(Float64)"}})
STATE = "updateAnalysisState"

# The failure this site exists for: a clarifying question only someone holding the schema
# can answer.
_SCHEMA_QUESTION = "Should I use AnnualSalary or HourlyRate?"
_HUMAN_QUESTION = "Do you want their annual salary, or their hourly rate?"

_FEEDBACK = "Name the two things in business terms, not the two fields."
# A phrase unique to the askUser nudge, so a test can say WHICH check fired.
_NUDGE_MARK = "That question was NOT sent."


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return []


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


class _ScriptedJudge:
    """A judge double with an ordered verdict script (09 §L).

    THE COUNT IS THE POINT, not a convenience. Half of this design is about NOT calling
    the judge — once its allowance is spent, and once the wall clock has no room for a
    rejection to act in — and neither skip is observable from the verdict, only from the
    fact that no call was made."""

    def __init__(self, verdicts: list[JudgeVerdict]) -> None:
        self._verdicts = list(verdicts)
        self.briefs: list[JudgeBrief] = []

    async def review(self, brief: JudgeBrief) -> JudgeVerdict:
        self.briefs.append(brief)
        return self._verdicts.pop(0) if self._verdicts else APPROVED

    @property
    def calls_made(self) -> int:
        return len(self.briefs)


class _CountingStore(InMemorySessionStore):
    """The real store, recording the KIND of every allowance claim — the only place the
    judge's charge is observable, and the only way to see that it did NOT take the answer
    judge's grant."""

    def __init__(self) -> None:
        super().__init__()
        self.claims: list[str] = []

    async def claim_finalization_block(
        self,
        session_id: str,
        turn_index: int,
        window_count: int,
        kind: FinalizationBlockKind,
    ) -> bool:
        self.claims.append(kind)
        return await super().claim_finalization_block(session_id, turn_index, window_count, kind)


def _build(
    turns: list[ModelTurnResult],
    judge: _ScriptedJudge | None,
    *,
    max_wall_clock_seconds: float = 60,
    min_headroom_seconds: float = 25.0,
) -> tuple[AgentLoop, _CountingStore, list[tuple[str, dict[str, Any]]], ScriptedModelClient]:
    store = _CountingStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    model = ScriptedModelClient(turns)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_budget_windows=3,
        observer=_observe,
        runtime_tools={STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe)},
        answer_judge=judge,  # type: ignore[arg-type]  # the one method the loop uses
        answer_judge_min_headroom_seconds=min_headroom_seconds,
    )
    return loop, store, events, model


def _ask(
    question: str, call_id: str = "a1", options: list[str] | None = None
) -> ToolCallRequest:
    arguments: dict[str, Any] = {"question": question}
    if options is not None:
        arguments["options"] = options
    return ToolCallRequest(id=call_id, name="askUser", arguments=arguments)


def _turn(*calls: ToolCallRequest, text: str | None = None) -> ModelTurnResult:
    return ModelTurnResult(assistant_text=text, tool_calls=list(calls))


def _events(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


def _nudge(model: ScriptedModelClient) -> str:
    return next(
        str(m.get("content"))
        for call in model.calls
        for m in call.messages
        if _NUDGE_MARK in str(m.get("content") or "")
    )


def _reject() -> JudgeVerdict:
    return JudgeVerdict(
        approved=False, violation="non_contextual_question", feedback=_FEEDBACK
    )


# --- the feature absent ------------------------------------------------------


async def test_no_judge_wired_pauses_exactly_as_before() -> None:
    """`answer_judge=None` is the DEFAULT and must be byte-identical to the loop before
    this feature: one round-trip, one pause, no claim, no event."""
    loop, store, events, model = _build([_turn(_ask(_SCHEMA_QUESTION))], None)
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )
    assert outcome.status == "paused_ask_user"
    assert outcome.pending_question == {"question": _SCHEMA_QUESTION, "options": None}
    assert store.claims == []
    assert model.calls_made == 1
    assert [e for e, _ in events if "judge" in e] == []


# --- the rejection -----------------------------------------------------------


async def test_a_schema_worded_question_is_refused_and_never_reaches_the_user() -> None:
    """THE WHOLE POINT OF THIS SITE. The rejected question must not appear in
    `pending_question`, in the checkpoint, or anywhere the user can see — and the
    replacement pauses normally on the round that was handed back."""
    judge = _ScriptedJudge([_reject()])
    loop, store, events, model = _build(
        [_turn(_ask(_SCHEMA_QUESTION)), _turn(_ask(_HUMAN_QUESTION, "a2"))], judge
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )

    assert outcome.status == "paused_ask_user"
    assert outcome.pending_question == {"question": _HUMAN_QUESTION, "options": None}
    assert _SCHEMA_QUESTION not in str(outcome.pending_question)
    assert _events(events, ASK_USER_JUDGE_REFUSED_EVENT) == [
        {"violation": "non_contextual_question"}
    ]
    assert store.claims == ["ask_user_judge"]
    assert model.calls_made == 2, "one round-trip handed back, and only one"


async def test_the_nudge_echoes_the_question_and_carries_the_feedback() -> None:
    """The `askUser` call is INTERCEPTED and never persisted, and D22 discards the free
    text around it — so without the echo the model is asked to rewrite something it can
    no longer see."""
    judge = _ScriptedJudge([_reject()])
    _loop, _store, _events_, model = _build(
        [_turn(_ask(_SCHEMA_QUESTION)), _turn(_ask(_HUMAN_QUESTION, "a2"))], judge
    )
    await _loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )
    nudge = _nudge(model)
    assert _SCHEMA_QUESTION in nudge
    assert _FEEDBACK in nudge
    assert "The turn is NOT over" in nudge


async def test_the_nudge_lives_exactly_one_round_trip() -> None:
    """Same lifetime as every other finalization nudge (05 §D). A nudge that persisted
    would repeat forever, and — being ephemeral and appended at the tail — would migrate
    to be the newest message on every rebuild."""
    judge = _ScriptedJudge([_reject()])
    _loop, _store, _events_, model = _build(
        [
            _turn(_ask(_SCHEMA_QUESTION)),
            _turn(text="Let me check the schema first."),
            _turn(_ask(_HUMAN_QUESTION, "a2")),
        ],
        judge,
    )
    await _loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )
    carrying = [
        index
        for index, call in enumerate(model.calls)
        if any(_NUDGE_MARK in str(m.get("content") or "") for m in call.messages)
    ]
    assert carrying == [1]


async def test_the_judge_sees_the_raw_question_not_the_scrubbed_one() -> None:
    """ORDERING IS THE POINT (09 §C.3). `scrub_answer_prose` would hand the judge
    "which [schema detail withheld] did you mean?", which is exactly the string the judge
    exists to prevent the model from producing — and asking it to repair a redaction it
    did not write."""
    judge = _ScriptedJudge([_reject()])
    loop, _store, _events_, _model = _build(
        [_turn(_ask(_SCHEMA_QUESTION)), _turn(_ask(_HUMAN_QUESTION, "a2"))], judge
    )
    await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )
    (brief,) = judge.briefs
    assert brief.site == "ask_user"
    assert brief.pending_question == _SCHEMA_QUESTION
    assert "withheld" not in brief.pending_question


async def test_the_judge_sees_structured_options_for_code_and_placement_checks() -> None:
    judge = _ScriptedJudge([APPROVED])
    choices = ["Jane Doe (E1042)", "Sam Lee (E1043)"]
    loop, _store, _events_, _model = _build(
        [_turn(_ask("Which employee?", options=choices))], judge
    )
    await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="show employee payroll"
    )
    (brief,) = judge.briefs
    assert brief.pending_question == "Which employee?"
    assert brief.pending_options == tuple(choices)


async def test_a_batched_state_call_survives_the_rejection() -> None:
    """03 §E.1: when a response pauses, ONLY the state calls are dispatched — and they are
    dispatched BEFORE the pause is honoured. A rejection re-rounds inside the same window,
    so that ledger write must still be on the trail; losing it is the pre-existing failure
    the batch ordering was introduced to fix, arriving by a new route."""
    judge = _ScriptedJudge([_reject()])
    loop, _store, _events_, _model = _build(
        [
            _turn(
                ToolCallRequest(
                    id="s1",
                    name=STATE,
                    arguments={
                        "intents": [
                            {"description": "average salary in Sales"},
                            {"description": "headcount in Sales"},
                        ]
                    },
                ),
                _ask(_SCHEMA_QUESTION),
            ),
            _turn(_ask(_HUMAN_QUESTION, "a2")),
        ],
        judge,
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="salary and headcount"
    )
    assert outcome.status == "paused_ask_user"
    doc = await _loop_doc(loop)
    assert doc.analysis_state is not None
    assert len(doc.analysis_state.intents) == 2
    (brief,) = judge.briefs
    assert [i[1] for i in brief.intents] == [
        "average salary in Sales",
        "headcount in Sales",
    ], "the judge sees the ledger the same batch just wrote"


async def _loop_doc(loop: AgentLoop) -> Any:
    return await loop._session_store.get_or_create_session(SESSION_ID)  # noqa: SLF001


# --- the allowance -----------------------------------------------------------


async def test_a_denied_claim_exhausts_and_the_pause_proceeds() -> None:
    """THE RUNTIME NEVER HARD-LOCKS A TURN (05 §J.5). Refusing again would spend the
    window on a disagreement; suppressing the pause would end the turn with neither an
    answer nor a question. So the question ships, and the event is what makes that visible.

    ⚠ REACHED ONLY WHEN THE PERSISTED CLAIM IS SPENT AND THIS GATE DOES NOT KNOW IT — i.e.
    the window's allowance went in an EARLIER `_run_loop_body` invocation, which is the
    askUser-resume path (`window_count` is unchanged across an askUser resume, and a fresh
    gate is built on re-entry). Within ONE invocation the `has_spent` skip pre-empts this
    branch entirely and no judge call is made at all — the test below pins that. Both
    behaviours are wanted: the skip is the cheap common case, and this is the honest
    handler for the case the skip cannot see. Set up here by spending the claim directly,
    which is exactly the state a resume would find."""
    judge = _ScriptedJudge([_reject()])
    loop, store, events, model = _build([_turn(_ask(_SCHEMA_QUESTION))], judge)
    assert await store.claim_finalization_block(SESSION_ID, 0, 1, "ask_user_judge") is True
    store.claims.clear()

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )
    assert outcome.status == "paused_ask_user"
    assert outcome.pending_question == {"question": _SCHEMA_QUESTION, "options": None}
    assert _events(events, ASK_USER_JUDGE_EXHAUSTED_EVENT) == [
        {"violation": "non_contextual_question"}
    ]
    assert store.claims == ["ask_user_judge"], "it asked, and was refused"
    assert judge.calls_made == 1
    assert model.calls_made == 1


async def test_the_second_call_is_skipped_when_the_allowance_is_already_gone() -> None:
    """09 §F.1. Once the grant is spent a rejection cannot act, so buying the verdict is
    buying something nothing is permitted to use — and unlike every other gate's
    predicate, this one costs a MODEL CALL. The skip is what makes the judge affordable on
    a wall-clock-bounded turn."""
    judge = _ScriptedJudge([_reject()])
    loop, store, events, model = _build(
        [
            _turn(_ask(_SCHEMA_QUESTION)),
            _turn(_ask("And which Rate column?", "a2")),
        ],
        judge,
    )
    # Spend the allowance on the first rejection, then let the model ask again.
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )
    assert outcome.status == "paused_ask_user"
    assert judge.calls_made == 1, "the second finish never reached the model"
    assert _events(events, ANSWER_JUDGE_SKIPPED_EVENT) == [{"reason": "allowance_spent"}]
    assert store.claims == ["ask_user_judge"], "no second claim attempt"


async def test_the_ask_user_judge_never_spends_the_answer_judges_grant() -> None:
    """09 §F: two kinds, deliberately. A rejected ANSWER earlier in the window must not
    silence the check that keeps a schema-worded question off the user's screen."""
    judge = _ScriptedJudge([_reject()])
    loop, store, _events_, _model = _build(
        [_turn(_ask(_SCHEMA_QUESTION)), _turn(_ask(_HUMAN_QUESTION, "a2"))], judge
    )
    await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )
    assert store.claims == ["ask_user_judge"]
    assert "answer_judge" not in store.claims


# --- the wall clock ----------------------------------------------------------


async def test_no_wall_clock_headroom_skips_the_judge_entirely() -> None:
    """09 §H, the failure mode most likely to make this feature net-negative. A rejection
    issued near the cap buys a regeneration the guard cuts off mid-round, and the turn
    then pauses with nothing to show. Below the headroom the judge does not run AT ALL —
    not "runs and is ignored", which would still pay for the call."""
    judge = _ScriptedJudge([_reject()])
    loop, store, events, model = _build(
        [_turn(_ask(_SCHEMA_QUESTION))],
        judge,
        max_wall_clock_seconds=30,
        min_headroom_seconds=45.0,  # more headroom demanded than the window can ever hold
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )
    assert outcome.status == "paused_ask_user"
    assert outcome.pending_question == {"question": _SCHEMA_QUESTION, "options": None}
    assert judge.calls_made == 0
    assert _events(events, ANSWER_JUDGE_SKIPPED_EVENT) == [{"reason": "wall_clock"}]
    assert store.claims == []
    assert model.calls_made == 1


# --- approval ----------------------------------------------------------------


async def test_an_approved_question_pauses_with_no_claim_and_no_extra_round() -> None:
    """The common path. Approval must cost exactly one judge call and nothing else — no
    allowance, no event, no second round-trip."""
    judge = _ScriptedJudge([APPROVED])
    loop, store, events, model = _build([_turn(_ask(_HUMAN_QUESTION))], judge)
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )
    assert outcome.status == "paused_ask_user"
    assert outcome.pending_question == {"question": _HUMAN_QUESTION, "options": None}
    assert judge.calls_made == 1
    assert store.claims == []
    assert _events(events, ASK_USER_JUDGE_REFUSED_EVENT) == []
    assert model.calls_made == 1


async def test_the_scrub_still_runs_on_an_approved_question() -> None:
    """The judge does not REPLACE `scrub_answer_prose` (ISSUES I1) — it reduces how often
    the scrub has anything to do. A judge that approves an identifier-bearing question
    must not disarm the redaction behind it."""
    judge = _ScriptedJudge([APPROVED])
    loop, _store, events, _model = _build(
        [_turn(_ask(f"Which value of {_E}.AnnualSalary did you mean?"))], judge
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="average salary in Sales"
    )
    assert outcome.pending_question is not None
    assert _E not in outcome.pending_question["question"]
    assert _events(events, "loop_answer_prose_redacted")
