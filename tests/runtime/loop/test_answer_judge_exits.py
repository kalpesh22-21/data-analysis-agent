"""The ANSWER JUDGE at the two TERMINAL EXITS (doc 09 §C.1/§C.2), end to end.

WHAT THESE PIN THAT THE UNIT TESTS CANNOT.

THE JUDGE IS PAID FOR IN MODEL CALLS, so half the contract is about NOT calling it: not
when a free check already won the round, not when the allowance is gone, not when the
wall clock has no room for a rejection to act in, and not on a silent finish. Every one
of those is invisible in the verdict and visible only as `judge.calls_made`.

THE TWO EXITS SHARE ONE ALLOWANCE because they are two doors out of ONE finish — 05 §L.5
records the route between them, the `markdown_table` nudge telling the model to call
`answerWithTable` instead. A model pushed through that door must not be judged twice for
the same answer, and `store.claims` is where that is observable.

THE REFUSAL MECHANICS DIFFER BY EXIT and both are load-bearing. Exit #1 persists nothing,
so the nudge must carry the draft back; exit #2 has a real error channel, so the refusal
is a persisted non-`ok` `ToolResult` whose `denial_detail` is what actually reaches the
model (`ToolResult.user_message` has no `TrailEntry` field at all).
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.denial_mapping import ANSWER_JUDGE_REJECTED_CODE
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.loop.answer_judge import (
    ANSWER_JUDGE_EXHAUSTED_EVENT,
    ANSWER_JUDGE_REFUSED_EVENT,
    ANSWER_JUDGE_SKIPPED_EVENT,
    JudgeBrief,
    JudgeVerdict,
)
from data_agent.runtime.loop.answer_rules import ANSWER_RULE_REFUSED_EVENT
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import FinalizationBlockKind

pytestmark = pytest.mark.usefixtures("blueprint_consulted")

SESSION_ID = "sess-answer-judge-exits"
_E = "hr.employee"
CATALOG = CatalogHandle({_E: {"Department": "String", "Name": "String"}})

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"

RAN_SQL = f"SELECT Department, count() AS n FROM {_E} GROUP BY Department"

_FEEDBACK = "Say which year the figure covers."
# Phrases unique to each message, so a test can say WHICH check fired.
_JUDGE_NUDGE_MARK = "That answer was NOT sent."


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [
        {"type": "function", "name": name, "parameters": {}}
        for name in ("runQuery", "getTableSchema", "listDatabases", "listTables")
    ]


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


class _ScriptedJudge:
    """A judge double with an ordered verdict script. `calls_made` is the point: half of
    this design is about not calling it."""

    def __init__(self, verdicts: list[JudgeVerdict] | None = None) -> None:
        self._verdicts = list(verdicts or [])
        self.briefs: list[JudgeBrief] = []

    async def review(self, brief: JudgeBrief) -> JudgeVerdict:
        self.briefs.append(brief)
        return (
            self._verdicts.pop(0) if self._verdicts else JudgeVerdict(approved=True, reviewed=True)
        )

    @property
    def calls_made(self) -> int:
        return len(self.briefs)


class _CountingStore(InMemorySessionStore):
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


def _rows_mcp(*row_counts: int, first_cell: Any = None) -> FakeMCPClient:
    def _result(n: int) -> dict[str, Any]:
        rows = [[f"D{i}", i] for i in range(n)]
        if rows and first_cell is not None:
            rows[0][1] = first_cell
        return {
            "columns": ["Department", "n"],
            "rows": rows,
            "row_count": n,
            "truncated": False,
        }

    return FakeMCPClient(scripted={"runQuery": [_result(n) for n in row_counts]})


def _build(
    turns: list[ModelTurnResult],
    judge: _ScriptedJudge | None,
    *,
    mcp: FakeMCPClient | None = None,
    max_wall_clock_seconds: float = 60,
    min_headroom_seconds: float = 25.0,
) -> tuple[AgentLoop, _CountingStore, list[tuple[str, dict[str, Any]]], ScriptedModelClient]:
    store = _CountingStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, dict(payload)))

    model = ScriptedModelClient(turns)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(mcp or FakeMCPClient(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_budget_windows=3,
        observer=_observe,
        runtime_tools={
            STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe),
            ANSWER: AnswerWithTableTool(),
        },
        answer_judge=judge,  # type: ignore[arg-type]
        answer_judge_min_headroom_seconds=min_headroom_seconds,
    )
    return loop, store, events, model


def _query(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="runQuery", arguments={"sql": RAN_SQL})


def _answer_table(call_id: str, answer: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name=ANSWER,
        arguments={"answer": answer, "tables": [{"sql": RAN_SQL, "caption": "By dept"}]},
    )


def _events(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


def _carrying(model: ScriptedModelClient, mark: str) -> list[int]:
    return [
        index
        for index, call in enumerate(model.calls)
        if any(mark in str(m.get("content") or "") for m in call.messages)
    ]


def _text_carrying(model: ScriptedModelClient, mark: str) -> str:
    return next(
        str(m.get("content"))
        for call in model.calls
        for m in call.messages
        if mark in str(m.get("content") or "")
    )


def _reject(violation: str = "unrecorded_assumption") -> JudgeVerdict:
    return JudgeVerdict(approved=False, violation=violation, feedback=_FEEDBACK)


# --- the feature absent ------------------------------------------------------


async def test_a_disabled_judge_does_no_store_io_at_a_terminal_exit() -> None:
    """REGRESSION, and the shipped DEFAULT path. Building the brief costs a session load
    plus up to `_MAX_CORROBORATION_READS` KV reads; the first cut did that work FIRST and
    consulted `_judge` afterwards, so every terminal exit of every turn paid for it —
    including with the feature off. `_judge_would_run` now gates both halves.

    Counted at the store rather than asserted on timings: a read is a read."""
    reads: list[str] = []

    class _CountingReads(_CountingStore):
        async def read_full_result(self, session_id: str, ref: str):  # noqa: ANN001, ANN201
            reads.append(ref)
            return await super().read_full_result(session_id, ref)

    store = _CountingReads()
    events: list[tuple[str, dict[str, Any]]] = []
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            # A 3-digit figure, so corroboration WOULD run if it were reached.
            ModelTurnResult(assistant_text="There are 412 active employees."),
        ]
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(
            _rows_mcp(1), CATALOG, observer=lambda e, p: events.append((e, dict(p)))
        ),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=lambda e, p: events.append((e, dict(p))),
        runtime_tools={ANSWER: AnswerWithTableTool()},
        answer_judge=None,
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount?"
    )
    assert outcome.status == "done"
    assert reads == [], "a disabled judge must not read a single full result"


async def test_a_brief_that_cannot_be_built_approves_instead_of_aborting_the_turn() -> None:
    """FAIL-OPEN AT THE CALL SITE, not only inside `review`. `_judge_results` reads the
    session document, and a transient store error there would otherwise propagate out of
    `_run_loop_body` and abort a turn whose answer was already in hand."""

    class _BrokenReads(_CountingStore):
        async def get_or_create_session(self, session_id: str):  # noqa: ANN001, ANN201
            doc = await super().get_or_create_session(session_id)
            if getattr(self, "_armed", False):
                raise RuntimeError("couchbase timed out")
            return doc

    store = _BrokenReads()
    events: list[tuple[str, dict[str, Any]]] = []
    judge = _ScriptedJudge([_reject()])
    model = ScriptedModelClient(
        [ModelTurnResult(assistant_text="Sales is the largest department.")]
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(
            FakeMCPClient(), CATALOG, observer=lambda e, p: events.append((e, dict(p)))
        ),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=lambda e, p: events.append((e, dict(p))),
        runtime_tools={ANSWER: AnswerWithTableTool()},
        answer_judge=judge,  # type: ignore[arg-type]
    )
    # Arm the failure only once the turn is under way, so the loop's own reads succeed.
    original = loop._judge_results  # noqa: SLF001

    async def _boom(*a: Any, **k: Any):  # noqa: ANN202
        raise RuntimeError("couchbase timed out")

    loop._judge_results = _boom  # type: ignore[method-assign]  # noqa: SLF001

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount?"
    )
    assert original is not None
    assert outcome.status == "done", "the turn must not abort"
    assert outcome.assistant_text == "Sales is the largest department."
    assert judge.calls_made == 0
    assert ("loop_answer_judge_failed", {"reason": "brief_failed"}) in events


async def test_no_judge_wired_is_byte_identical_at_both_exits() -> None:
    """`answer_judge=None` is the default and must cost nothing anywhere: no claim, no
    event, no extra round-trip, at either exit."""
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="There are 412 active employees."),
        ],
        None,
        mcp=_rows_mcp(1),
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount?"
    )
    assert outcome.status == "done"
    assert outcome.assistant_text == "There are 412 active employees."
    assert store.claims == []
    assert model.calls_made == 2
    assert [e for e, _ in events if "judge" in e] == []


# --- exit #1 -----------------------------------------------------------------


async def test_exit_one_rejection_hands_back_one_round_and_the_fix_is_accepted() -> None:
    judge = _ScriptedJudge([_reject()])
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="We hired 1,284 people this year."),
            ModelTurnResult(assistant_text="We hired 1,284 people in 2025."),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="hires this year?"
    )
    assert outcome.status == "done"
    assert outcome.assistant_text == "We hired 1,284 people in 2025."
    assert _events(events, ANSWER_JUDGE_REFUSED_EVENT) == [
        {"violation": "unrecorded_assumption", "site": "exit_prose"}
    ]
    assert store.claims == ["answer_judge"]
    assert model.calls_made == 3


async def test_the_exit_one_nudge_carries_the_draft_and_the_feedback() -> None:
    """Exit #1 persists NOTHING — not the refused answer, not the nudge — and D22 discards
    free text around tool calls. Without the echo the model must regenerate blind."""
    judge = _ScriptedJudge([_reject()])
    _loop, _store, _ev, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="We hired 1,284 people this year."),
            ModelTurnResult(assistant_text="We hired 1,284 people in 2025."),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    await _loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="hires this year?"
    )
    nudge = _text_carrying(model, _JUDGE_NUDGE_MARK)
    assert "We hired 1,284 people this year." in nudge
    assert _FEEDBACK in nudge
    assert "The turn is NOT over" in nudge
    assert _carrying(model, _JUDGE_NUDGE_MARK) == [2], "exactly one round-trip"


async def test_a_refused_exit_one_draft_never_reaches_the_user() -> None:
    """`last_assistant_text` rides out on the hard-ceiling and budget-cap paths, so a
    refused draft that survived there would reach the user while never entering history —
    live and history disagreeing on exactly the enforcement path."""
    judge = _ScriptedJudge([_reject()])
    loop, _store, _ev, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="We hired 1,284 people this year."),
            ModelTurnResult(assistant_text="Corrected."),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="hires this year?"
    )
    doc = await _store_of(loop).get_or_create_session(SESSION_ID)
    persisted = [m.content for m in doc.messages if m.role == "assistant"]
    assert persisted == ["Corrected."]


def _store_of(loop: AgentLoop) -> Any:
    return loop._session_store  # noqa: SLF001


async def test_a_cheaper_rule_wins_the_round_and_the_judge_is_not_paid_for() -> None:
    """`not refused_finalization`: the judge is the only check here that costs a model
    call, so a pasted markdown table must cost ZERO judge tokens."""
    judge = _ScriptedJudge()
    loop, store, events, _model = _build(
        [
            ModelTurnResult(assistant_text="| Dept | n |\n| --- | --- |\n| Sales | 3 |"),
            ModelTurnResult(assistant_text="Sales leads."),
        ],
        judge,
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="by department?")
    assert _events(events, ANSWER_RULE_REFUSED_EVENT) == [{"rule": "markdown_table"}]
    assert judge.calls_made == 1, "the SECOND, clean finish is judged; the refused one is not"
    assert store.claims == ["answer_shape"]


async def test_a_silent_finish_never_reaches_the_judge() -> None:
    """§K owns the blank finish and the two conditions are disjoint by construction. The
    ordering is what keeps a silent turn from paying for a model call."""
    judge = _ScriptedJudge()
    loop, _store, events, _model = _build(
        [ModelTurnResult(assistant_text=""), ModelTurnResult(assistant_text="Sales leads.")],
        judge,
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="by department?")
    assert judge.calls_made == 1, "only the non-blank finish is judged"
    assert judge.briefs[0].draft == "Sales leads."


async def test_a_second_finish_skips_the_judge_once_the_allowance_is_spent() -> None:
    """09 §F.1: once the grant is gone a rejection cannot act, so buying the verdict buys
    something nothing may use — and unlike every other check here, buying it costs a model
    call."""
    judge = _ScriptedJudge([_reject()])
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="We hired 1,284 people this year."),
            ModelTurnResult(assistant_text="We hired 1,284 people this year, again."),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="hires this year?"
    )
    assert outcome.status == "done"
    assert judge.calls_made == 1
    assert _events(events, ANSWER_JUDGE_SKIPPED_EVENT) == [{"reason": "allowance_spent"}]
    assert store.claims == ["answer_judge"]


async def test_a_denied_claim_exhausts_and_the_prose_ships() -> None:
    """The runtime never hard-locks a turn (05 §J.5/§L.8), and here that posture is
    load-bearing rather than inherited: this check reads MEANING, so a second refusal
    would be the runtime destroying an answer on the say-so of a model it cannot appeal.

    ⚠ Reached only when the PERSISTED claim is spent and this gate does not know it — the
    resume path. Within one window the `has_spent` skip above pre-empts it."""
    judge = _ScriptedJudge([_reject()])
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="We hired 1,284 people this year."),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    assert await store.claim_finalization_block(SESSION_ID, 0, 1, "answer_judge") is True
    store.claims.clear()

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="hires this year?"
    )
    assert outcome.status == "done"
    assert outcome.assistant_text == "We hired 1,284 people this year."
    assert _events(events, ANSWER_JUDGE_EXHAUSTED_EVENT) == [
        {"violation": "unrecorded_assumption", "site": "exit_prose"}
    ]
    assert model.calls_made == 2


async def test_no_wall_clock_headroom_skips_the_judge_and_ships_the_answer() -> None:
    """09 §H. A rejection issued near the cap buys a regeneration `guard.exceeded` cuts off
    mid-round, after which the turn returns `paused_budget_cap` with the draft already
    cleared: a serviceable answer converted into an empty pause."""
    judge = _ScriptedJudge([_reject()])
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="We hired 1,284 people this year."),
        ],
        judge,
        mcp=_rows_mcp(1),
        max_wall_clock_seconds=30,
        min_headroom_seconds=45.0,
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="hires this year?"
    )
    assert outcome.status == "done"
    assert outcome.assistant_text == "We hired 1,284 people this year."
    assert judge.calls_made == 0
    assert _events(events, ANSWER_JUDGE_SKIPPED_EVENT) == [{"reason": "wall_clock"}]
    assert store.claims == []


# --- the brief ---------------------------------------------------------------


async def test_the_brief_carries_the_date_anchor_the_model_was_given() -> None:
    """ONE derivation, shared (`turn_date_anchor_day`). Two would let the judge grade
    "this year" against a different today than the model was given, and then fault a
    correct answer for a disagreement it created itself."""
    judge = _ScriptedJudge()
    loop, _store, _ev, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="We hired 1,284 people this year."),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="hires this year?"
    )
    (brief,) = judge.briefs
    anchor_line = next(
        str(m["content"])
        for m in model.calls[0].messages
        if "Today's date is" in str(m.get("content") or "")
    )
    assert brief.date_anchor is not None
    assert brief.date_anchor in anchor_line


async def test_the_brief_carries_only_data_bearing_results_of_this_turn() -> None:
    """`DATA_ANSWER_TOOLS` only: those are the results the answer was written FROM. A
    discovery read grounds the MODEL, not the answer, and a wide `getTableSchema` is ~4k
    tokens the judge has no criterion for."""
    judge = _ScriptedJudge()
    loop, _store, _ev, _model = _build(
        [
            ModelTurnResult(
                tool_calls=[
                    _query("q1"),
                    ToolCallRequest(id="s1", name="getTableSchema", arguments={"table": _E}),
                ]
            ),
            ModelTurnResult(assistant_text="Sales leads with 3."),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="by department?")
    (brief,) = judge.briefs
    assert [entry["tool_name"] for entry in brief.results] == ["runQuery"]
    assert brief.sql_executed == (RAN_SQL,)


async def test_the_brief_previews_are_capped_exactly_as_the_models_are() -> None:
    """09 §D.3: a judge holding more rows than the model held faults it for the preview
    cap, which manufactures false rejections."""
    judge = _ScriptedJudge()
    loop, _store, _ev, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            # Exit #2, because a 50-row result answered in bare prose is what the 05 §J
            # shape gate exists to refuse — and it would refuse it before the judge ran.
            ModelTurnResult(tool_calls=[_answer_table("a1", "Sales leads.")]),
        ],
        judge,
        mcp=_rows_mcp(50),
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="by department?")
    (brief,) = judge.briefs
    (entry,) = brief.results
    assert entry["result_preview"]["row_count"] == 50
    assert len(entry["result_preview"]["preview_rows"]) == 20


# --- corroboration (09 §D.4) -------------------------------------------------


async def test_a_figure_present_in_the_full_result_is_reported_as_corroborated() -> None:
    judge = _ScriptedJudge()
    loop, _store, _ev, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="The largest department has 9,184 people."),
        ],
        judge,
        mcp=_rows_mcp(1, first_cell=9184),
    )
    await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="largest department?"
    )
    (brief,) = judge.briefs
    assert brief.figure_corroborated is True


async def test_a_figure_absent_from_the_results_is_not_reported_as_false() -> None:
    """05 §L.7's whole argument. A non-match is not evidence — a derived figure never
    matches literally, rounding and formatting diverge — so reporting `False` would push
    the judge toward `contradicts_result` on exactly the answers §L.7 lists."""
    judge = _ScriptedJudge()
    loop, _store, _ev, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="Headcount rose 12.5% to 9,184 this year."),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="growth?")
    (brief,) = judge.briefs
    assert brief.figure_corroborated is None
    assert brief.figure_corroborated is not False


async def test_prose_with_no_figure_is_not_checked_at_all() -> None:
    judge = _ScriptedJudge()
    loop, _store, _ev, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="No employees match that filter."),
        ],
        judge,
        mcp=_rows_mcp(0),
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="anyone?")
    (brief,) = judge.briefs
    assert brief.figure_corroborated is None


# --- exit #2 -----------------------------------------------------------------


async def test_exit_two_rejection_persists_the_refusal_and_the_turn_continues() -> None:
    """05 §L.9 leaves exit #2 unchecked today, and it is where multi-part answers finish.
    The refusal is a non-`ok` `ToolResult`, so the terminal exit does not fire and the
    PERSISTED entry IS the refusal."""
    judge = _ScriptedJudge([_reject("unexplained_gap")])
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(tool_calls=[_answer_table("a1", "Headcount by department.")]),
            ModelTurnResult(
                tool_calls=[_answer_table("a2", "Headcount and tenure by department.")]
            ),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount and tenure"
    )
    assert outcome.status == "done"
    assert outcome.assistant_text.startswith("I wasn't able to fully verify the answer")
    assert _events(events, ANSWER_JUDGE_REFUSED_EVENT) == [
        {"violation": "unexplained_gap", "site": "exit_table"}
    ]
    assert store.claims == ["answer_judge"]

    doc = await store.get_or_create_session(SESSION_ID)
    refusals = [e for e in doc.tool_trail if e.error_code == ANSWER_JUDGE_REJECTED_CODE]
    assert len(refusals) == 1
    entry = refusals[0]
    assert entry.status == "error"
    assert entry.tool_name == ANSWER
    # `denial_detail` is the ONLY channel `context/budget.py::_render_entry` reads —
    # `ToolResult.user_message` has no `TrailEntry` field at all.
    assert _FEEDBACK in (entry.denial_detail or "")
    # Determined-EMPTY, never `None`: `_compute_turn_provenance_union` is fail-closed and
    # a `None` would collapse the turn's union and drop the user's own answer from replay.
    assert entry.provenance == frozenset()
    # The model's own prose survives in `args`, which is why exit #2 needs no draft echo.
    assert entry.args["answer"] == "Headcount by department."


async def test_the_exit_two_refusal_reaches_the_model_on_the_next_round() -> None:
    judge = _ScriptedJudge([_reject("unexplained_gap")])
    loop, _store, _ev, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(tool_calls=[_answer_table("a1", "Headcount by department.")]),
            ModelTurnResult(tool_calls=[_answer_table("a2", "Headcount and tenure.")]),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount and tenure"
    )
    assert _carrying(model, _FEEDBACK), "the judge's sentence reached the model"


async def test_an_approval_emits_no_refusal_or_exhausted_event_at_either_exit() -> None:
    """REGRESSION, and the defect it pins was found by reading a live Phoenix trace rather
    than by any test here.

    At exit #2 the `else` that emits `loop_answer_judge_exhausted` was bound one level out
    — to `if not approved` instead of to `if may_refuse(...)` — so EVERY APPROVED tabled
    answer published an "exhausted" event carrying an empty `violation`. Offline that is
    silent: the verdict is right, the answer ships, and the only casualty is the metric
    that is supposed to say "the judge was overruled", which now fires on clean turns.

    The assertion is therefore about the WHOLE event set, not about any one event: on an
    approval the judge may emit `..._called` and nothing else."""
    for site_turns in (
        [ModelTurnResult(assistant_text="Sales leads.")],
        [ModelTurnResult(tool_calls=[_answer_table("a1", "Sales leads.")])],
    ):
        judge = _ScriptedJudge()
        loop, store, events, _model = _build(
            [ModelTurnResult(tool_calls=[_query("q1")]), *site_turns], judge, mcp=_rows_mcp(1)
        )
        outcome = await loop.run(
            session_id=SESSION_ID, credentials=_credentials(), user_message="by department?"
        )
        assert outcome.status == "done"
        assert judge.calls_made == 1
        judge_events = [name for name, _ in events if "judge" in name]
        assert judge_events == [], (
            "the loop must emit no judge event on an approval — the judge's own "
            f"`..._called` is emitted inside AnswerJudge, not here. Got: {judge_events}"
        )
        assert store.claims == []


async def test_the_exit_two_brief_carries_the_designated_tables() -> None:
    judge = _ScriptedJudge()
    loop, _store, _ev, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(tool_calls=[_answer_table("a1", "Headcount by department.")]),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="by department?")
    (brief,) = judge.briefs
    assert brief.site == "exit_table"
    assert brief.draft == "Headcount by department."
    assert brief.designated_tables == (("By dept", RAN_SQL),)


async def test_two_answer_calls_in_one_batch_are_both_refused_once() -> None:
    """REGRESSION. A model response carries up to 8 tool calls, and `[answerWithTable,
    answerWithTable]` is the shape 05 §C.2's free-refusal path exists for.

    ⚠ THE DEFECT THIS PINS SHIPPED PAST A FIRST REVIEW. Call A was judged and refused,
    which RECORDS the grant; call B then reached `_judge`, `has_spent` reported the
    allowance gone, the judge was SKIPPED — and a skip returns APPROVED, so B stayed `ok`
    and TERMINATED THE TURN in the very round the judge had refused it. The user got a
    near-copy of the refused answer and the feedback reached the model never. The
    cost-avoidance peek runs before `may_refuse`, so the judge could not reach the free
    path the other gates use.

    The old assertion (`claims.count == 1`) passed over all of that, which is why this
    now asserts the OUTCOME: two refusals, one claim, one judge call, and the turn does
    NOT end in that round."""
    judge = _ScriptedJudge([_reject("unexplained_gap")])
    loop, store, _ev, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(
                tool_calls=[_answer_table("a1", "First."), _answer_table("a2", "Second.")]
            ),
            ModelTurnResult(tool_calls=[_answer_table("a3", "Fixed.")]),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="by department?"
    )

    assert outcome.assistant_text.startswith("I wasn't able to fully verify the answer")
    # The corrected draft had no fresh approval once the review allowance was spent.
    assert store.claims.count("answer_judge") == 1, "one claim"
    assert judge.calls_made == 1, "one model call"

    doc = await store.get_or_create_session(SESSION_ID)
    refusals = [e for e in doc.tool_trail if e.error_code == ANSWER_JUDGE_REJECTED_CODE]
    assert len(refusals) == 2, "BOTH calls of the batch are refused, with the same words"
    assert all(_FEEDBACK in (e.denial_detail or "") for e in refusals)
    assert model.calls_made == 3, "the round was handed back, not consumed"


async def test_a_refusal_by_another_gate_keeps_the_judge_out_of_the_round() -> None:
    """One refusal per round-trip is the rule the whole chain expresses. When an earlier
    gate has already refused in this batch, the judge does not run — which also keeps it
    off `may_refuse`'s free path, where a refusal would be issued with no store claim and
    the window's stated bound of one judge call would quietly become two."""
    judge = _ScriptedJudge([_reject("unexplained_gap")])
    loop, store, events, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1"), _query("q2")]),
            ModelTurnResult(
                tool_calls=[
                    # Designates nothing while multi-row results are in hand: the 08 §O
                    # empty-designation nudge refuses this one and spends `answer_shape`.
                    ToolCallRequest(
                        id="a1",
                        name=ANSWER,
                        arguments={"answer": "Prose only.", "tables": []},
                    ),
                    _answer_table("a2", "With a table."),
                ]
            ),
            ModelTurnResult(tool_calls=[_answer_table("a3", "Fixed.")]),
        ],
        judge,
        mcp=_rows_mcp(3, 3),
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="by department?")
    assert judge.calls_made == 0, "another gate owned this round"
    assert "answer_judge" not in store.claims
    assert {"reason": "round_refused"} in _events(events, ANSWER_JUDGE_SKIPPED_EVENT)


async def test_a_blank_answer_call_is_not_a_finalization_and_is_not_judged() -> None:
    """The terminal condition is mirrored exactly: a call that would NOT have ended the
    turn is not a finalization and must not be judged as one — judging it would spend the
    window's allowance on a habit call and leave the real finish unjudgeable."""
    judge = _ScriptedJudge()
    loop, _store, _ev, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name=ANSWER,
                        arguments={"answer": "", "tables": [{"sql": RAN_SQL}]},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Sales leads."),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="by department?")
    assert [b.site for b in judge.briefs] == ["exit_prose"]


# --- the shared allowance ----------------------------------------------------


async def test_the_two_exits_share_one_allowance() -> None:
    """09 §F: two doors out of ONE finish, not two complaints. 05 §L.5 records the route —
    a form nudge at exit #1 telling the model to call `answerWithTable` instead — and a
    model pushed through it must not be judged twice for the same answer."""
    judge = _ScriptedJudge([_reject(), _reject("unexplained_gap")])
    loop, store, events, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="Sales leads with 3 people."),
            ModelTurnResult(tool_calls=[_answer_table("a1", "Sales leads with 3 people.")]),
        ],
        judge,
        mcp=_rows_mcp(1),
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="by department?"
    )
    assert outcome.status == "done"
    assert store.claims.count("answer_judge") == 1, "one claim across BOTH exits"
    assert [p["site"] for p in _events(events, ANSWER_JUDGE_REFUSED_EVENT)] == ["exit_prose"]
    assert _events(events, ANSWER_JUDGE_SKIPPED_EVENT) == [{"reason": "allowance_spent"}]
