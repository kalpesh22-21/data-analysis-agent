"""The EMPTY-ANSWER exit gate (Release 1, doc 05 §K).

A model can end a round-trip with NO tool calls and NO text at all, and before
this gate existed that outcome was the only one in the runtime that produced no
artifact of any kind: exit #1 persisted nothing (`assistant_text or None`), the
`result` event carried `assistant_text: null`, the UI rendered `text || ""` beside
`status: done`, and NO event fired. A total failure was indistinguishable from an
answer that had not loaded, and the only witness was a person looking at the
screen.

Three causes produce it and are identical from inside the loop — a genuinely empty
completion, a completion the provider cut short (`incomplete_reason`), and a
REFUSAL whose text the parser used to drop (fixed in `model/openai_client.py`, and
pinned in `tests/runtime/model/test_openai_client.py`). The gate is deliberately
blind to which: the response to all three is the same one round handed back.

Both halves of the contract are load-bearing and both are tested here:

  - the gate FIRES on a silent finish, and a SECOND silent finish is substituted
    with prose rather than shipped blank, and
  - the gate STAYS SILENT everywhere else — above all on the response shape it
    most resembles and that most round-trips have: TOOL CALLS WITH NO PROSE, which
    is normal and must never be charged a round-trip.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.answer_scrub import ANSWER_PROSE_REDACTED_EVENT, scrub_answer_prose
from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.loop.finalization import (
    ANSWER_SHAPE_REFUSED_EVENT,
    EMPTY_ANSWER_EXHAUSTED_EVENT,
    EMPTY_ANSWER_FALLBACK_TEXT,
    EMPTY_ANSWER_REFUSED_EVENT,
)
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import FinalizationBlockKind
from data_agent.runtime.session_history import project_history

SESSION_ID = "sess-empty-answer"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "Name": "String"}}
)

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"

# A phrase that appears ONLY in the empty-answer nudge, so its presence in a
# request (or its absence from a persisted message) is unambiguous. Deliberately
# distinct from the other two nudges' marks — precedence between the three is part
# of the contract, so a test must be able to say WHICH one fired.
_NUDGE_MARK = "Your last response was EMPTY"
_SHORTER_MARK = "Keep this one SHORT"


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return []


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


class CountingStore(InMemorySessionStore):
    """The real in-memory store, RECORDING THE KIND of every allowance claim ATTEMPT.

    ATTEMPTS, NOT GRANTS, and the tests below read it that way: a gate whose grant
    is already spent still asks the store on every qualifying round and is refused,
    so `claims` shows `["answer_shape", "answer_shape", ...]` for one granted
    refusal followed by one exhausted one. What it proves is WHICH GATE ASKED, in
    what order — the only place the independence of the allowances is visible.

    `store.claims == []` is the assertion carrying the silent half of this
    contract: a gate that stays quiet must also not touch the store, because a
    claim it does not use is an allowance the NEXT round cannot spend.
    """

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
    *,
    mcp: FakeMCPClient | None = None,
    max_loop_iterations: int = 15,
    store: CountingStore | None = None,
) -> tuple[AgentLoop, CountingStore, list[tuple[str, dict[str, Any]]], ScriptedModelClient]:
    store = store if store is not None else CountingStore()
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
        max_budget_windows=3,
        observer=_observe,
        runtime_tools={
            STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe),
            ANSWER: AnswerWithTableTool(),
        },
    )
    return loop, store, events, model


def _rows_mcp(tool: str, *row_counts: int) -> FakeMCPClient:
    return FakeMCPClient(
        scripted={
            tool: [
                {
                    "columns": ["Department", "n"],
                    "rows": [[f"D{i}", i] for i in range(n)],
                    "row_count": n,
                    "truncated": False,
                }
                for n in row_counts
            ]
        }
    )


def _query(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="runQuery",
        arguments={"sql": f"SELECT Department, count() AS n FROM {_E} GROUP BY Department"},
    )


def _events(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


def _requests_carrying_the_nudge(model: ScriptedModelClient) -> list[int]:
    return [
        index
        for index, call in enumerate(model.calls)
        if any(_NUDGE_MARK in str(message.get("content") or "") for message in call.messages)
    ]


def _nudge_text(model: ScriptedModelClient, request_index: int) -> str:
    return next(
        str(m.get("content"))
        for m in model.calls[request_index].messages
        if _NUDGE_MARK in str(m.get("content") or "")
    )


# --- the gate FIRES ---------------------------------------------------------


async def test_a_silent_finish_is_refused_and_the_answer_lands_next_round() -> None:
    """The whole point, end to end: an empty response, one refusal, and a real
    answer on the round that was handed back — accepted VERBATIM, with no trace of
    the intervention left behind."""
    loop, store, events, model = _build(
        [
            ModelTurnResult(assistant_text=None),
            ModelTurnResult(assistant_text="Sales has 3 people."),
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="how many in sales"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Sales has 3 people.", (
        "the recovered answer must reach the user unchanged"
    )
    assert _events(events, EMPTY_ANSWER_REFUSED_EVENT) == [{"incomplete_reason": ""}]
    assert not _events(events, EMPTY_ANSWER_EXHAUSTED_EVENT)
    assert store.claims == ["empty_answer"]
    # ONE round-trip of nudge lifetime (05 §D): request 1 carries it, and the
    # request after the recovery would too if the nudge had been threaded.
    assert _requests_carrying_the_nudge(model) == [1]


async def test_whitespace_only_prose_is_treated_as_silence() -> None:
    """`.strip()`, not truthiness. `"   "` renders exactly as blank as `""` does,
    and a gate that tests only falsiness lets the whitespace case through — where
    it becomes a blank bubble AND a content-free assistant message replayed into
    every later turn."""
    loop, _store, events, _model = _build(
        [ModelTurnResult(assistant_text="  \n\t "), ModelTurnResult(assistant_text="Answered.")]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="q"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Answered."
    assert len(_events(events, EMPTY_ANSWER_REFUSED_EVENT)) == 1


async def test_a_second_silent_finish_is_substituted_and_persisted() -> None:
    """The exhausted path, and the one difference from every other exhausted gate:
    there is NO answer in hand to let through.

    So the substitution is the contract — the user is told what happened, and
    history holds the same string. A live answer that `/session/history` does not
    have is the divergence `_finish` scrubs before persisting to prevent, and "the
    turn is missing from history entirely" is the worst version of it.
    """
    loop, store, events, model = _build(
        [ModelTurnResult(assistant_text=""), ModelTurnResult(assistant_text="")]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="q"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == EMPTY_ANSWER_FALLBACK_TEXT
    assert _events(events, EMPTY_ANSWER_EXHAUSTED_EVENT) == [{"incomplete_reason": ""}]
    # TWO claims, ONE allowance — the second is refused by the store, not by a
    # local counter (05 §C.1: a local one resets on every resume).
    assert store.claims == ["empty_answer", "empty_answer"]
    assert len(_requests_carrying_the_nudge(model)) == 1, "the nudge outlived its round-trip"

    doc = await store.get_or_create_session(SESSION_ID)
    assistant = [m for m in doc.messages if m.role == "assistant"]
    assert [m.content for m in assistant] == [EMPTY_ANSWER_FALLBACK_TEXT]
    # And the nudge itself is never persisted — it is control flow, not something
    # the user said.
    history = project_history(doc.messages, doc.tool_trail, frozenset(), doc.pause_checkpoint)
    assert not any(_NUDGE_MARK in str(item) for item in history)


async def test_an_incomplete_reason_rides_both_events_and_changes_the_ask() -> None:
    """`incomplete_reason` is telemetry, not control flow (05 §K.3): the DECISION is
    identical either way, and the only thing it changes is what the nudge asks for.

    A completion cut off at the token cap does not need to be told it was silent —
    it needs to be told to be SHORTER. And an operator reading the span needs the
    word itself, because "the model chose to say nothing" and "the answer was cut
    off" call for opposite fixes.
    """
    loop, _store, events, model = _build(
        [
            ModelTurnResult(assistant_text=None, incomplete_reason="max_output_tokens"),
            ModelTurnResult(assistant_text=None, incomplete_reason="max_output_tokens"),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert _events(events, EMPTY_ANSWER_REFUSED_EVENT) == [
        {"incomplete_reason": "max_output_tokens"}
    ]
    assert _events(events, EMPTY_ANSWER_EXHAUSTED_EVENT) == [
        {"incomplete_reason": "max_output_tokens"}
    ]
    nudge = _nudge_text(model, 1)
    assert "max_output_tokens" in nudge
    assert _SHORTER_MARK in nudge


async def test_the_payload_carries_an_empty_string_not_none_for_an_ordinary_completion() -> None:
    """The observer's allowlist filter keeps `str | int | float | bool` and DROPS
    everything else, so a `None` here would vanish from the span — making "ordinary
    completion" indistinguishable from "attribute missing" for the one field these
    events exist to carry."""
    loop, _store, events, _model = _build(
        [ModelTurnResult(assistant_text=None), ModelTurnResult(assistant_text="ok")]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    payload = _events(events, EMPTY_ANSWER_REFUSED_EVENT)[0]
    assert payload["incomplete_reason"] == ""
    assert payload["incomplete_reason"] is not None


# --- the gate STAYS SILENT --------------------------------------------------


async def test_tool_calls_with_no_prose_are_untouched() -> None:
    """THE FALSE POSITIVE THAT WOULD MATTER MOST. Empty text alongside tool calls is
    what a normal round-trip looks like — the model is working, not silent. A gate
    that fired here would charge an extra round-trip to nearly every turn in the
    system and tell a working model it had said nothing.

    `store.claims == []` is the sharper half: no claim, so nothing was spent.
    """
    loop, store, events, _model = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="Sales 3, Eng 2, Ops 1."),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="q"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Sales 3, Eng 2, Ops 1."
    assert not _events(events, EMPTY_ANSWER_REFUSED_EVENT)
    assert store.claims == []


async def test_ordinary_prose_is_untouched() -> None:
    """The other silent half: a turn that answers normally must not see the store
    at all."""
    loop, store, events, _model = _build([ModelTurnResult(assistant_text="Here it is.")])

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="q"
    )

    assert outcome.assistant_text == "Here it is."
    assert not _events(events, EMPTY_ANSWER_REFUSED_EVENT)
    assert store.claims == []


async def test_the_shape_gate_takes_precedence_and_keeps_its_own_allowance() -> None:
    """PRECEDENCE: the empty-answer branch is LAST in the chain, so a silent finish
    holding untabled multi-row results is the answer-shape gate's complaint — the
    more specific one, about work that exists and was not presented.

    And the allowances stay independent: the shape refusal must not consume the
    empty-answer grant, or the silent finish that follows it would go unchallenged
    — which is exactly the starvation §J.3 was written about, arriving one gate
    later.
    """
    loop, store, events, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            # Silent finish holding three untabled rows: BOTH gates qualify.
            ModelTurnResult(assistant_text=None),
            # Silent again, and now the shape gate's grant is spent — so the
            # empty-answer gate takes its own.
            ModelTurnResult(assistant_text=None),
            ModelTurnResult(assistant_text="Sales 3, Eng 2, Ops 1."),
        ],
        mcp=_rows_mcp("runQuery", 3),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="by department"
    )

    assert outcome.status == "done"
    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 1}]
    assert _events(events, EMPTY_ANSWER_REFUSED_EVENT) == [{"incomplete_reason": ""}]
    # The order of the two GRANTED refusals is the precedence claim: the shape gate
    # spoke first, and the empty-answer gate still had its own grant afterwards.
    # (`claims` records attempts, so the spent shape allowance appears again on each
    # later qualifying round — see `CountingStore`.)
    assert store.claims[0] == "answer_shape"
    assert store.claims.count("empty_answer") == 1, "the empty-answer grant was starved"


async def test_the_intents_nudge_takes_precedence_over_silence() -> None:
    """Same precedence rule at the top of the chain: a pending intent is the most
    specific complaint of the three, and it wins even when the finish is silent.

    The empty-answer allowance is untouched by that refusal — asserted through
    `store.claims`, which is the only place the independence is visible.
    """
    loop, store, events, _model = _build(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name=STATE,
                        arguments={"intents": [{"description": "headcount by department"}]},
                    )
                ]
            ),
            # Silent finish with an intent pending: the intents refusal fires.
            ModelTurnResult(assistant_text=None),
            ModelTurnResult(assistant_text="Sales 3."),
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount"
    )

    assert outcome.status == "done"
    assert _events(events, "loop_finalization_refused") == [
        {"exit": "no_tool_calls", "pending_count": 1}
    ]
    assert not _events(events, EMPTY_ANSWER_REFUSED_EVENT)
    # The intents gate asked (and, on the later round where the intent was still
    # pending, asked again and was refused). The empty-answer allowance was never
    # touched — it is the silent finish's own, and this turn spent none of it.
    assert set(store.claims) == {"intents"}


async def test_a_later_turn_of_the_same_session_is_refused_on_its_own_merits() -> None:
    """Cross-turn replay safety for the new key. `finalization_blocks` persists for
    the whole session while `window_count` restarts at 1 on every external turn, so
    a key without the turn index would silently deny turn 2 the re-round it never
    had (05 §C.1 — the bug the intents allowance already fell into once)."""
    store = CountingStore()
    loop, _store, events, _model = _build(
        [ModelTurnResult(assistant_text=None), ModelTurnResult(assistant_text="one")],
        store=store,
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q1")

    loop2, _store2, events2, _model2 = _build(
        [ModelTurnResult(assistant_text=None), ModelTurnResult(assistant_text="two")],
        store=store,
    )
    outcome = await loop2.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="q2"
    )

    assert outcome.assistant_text == "two"
    assert len(_events(events, EMPTY_ANSWER_REFUSED_EVENT)) == 1
    assert len(_events(events2, EMPTY_ANSWER_REFUSED_EVENT)) == 1, (
        "the second turn was denied a re-round it never had"
    )
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.finalization_blocks == {"0:1:empty_answer": 1, "1:1:empty_answer": 1}


# --- the nudge's shape ------------------------------------------------------


async def test_the_nudge_is_an_ephemeral_user_message_at_the_tail() -> None:
    """05 §K.5 / §B.2: the correction is a `user`-role message spliced at the TAIL
    of the next request, and nowhere else.

    THE ROLE IS THE PART THAT NEEDS PINNING, and no other test in this file reads
    it. There is no error channel at exit #1 — nothing was called, so there is
    nothing to attach a denial to — and a synthetic `tool` message cannot stand
    alone (`_assembled_to_canonical` only ever emits one by expanding a trail entry
    into an `assistant(tool_calls) + tool` PAIR). An `assistant`-role nudge would be
    worse than useless: it would read as the model's own words, i.e. as the very
    silence being corrected, and it would be indistinguishable from a draft.

    THE TAIL IS THE OTHER HALF: the nudge must follow the current question, not
    precede it, or the model reads a correction about a response it has not given
    yet.
    """
    loop, _store, _events, model = _build(
        [ModelTurnResult(assistant_text=None), ModelTurnResult(assistant_text="Sales has 3.")]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="how many")

    nudged = model.calls[1].messages[-1]
    assert nudged["role"] == "user", "the nudge must not arrive in the model's own voice"
    assert _NUDGE_MARK in str(nudged["content"])
    # NO DRAFT ECHO, the one structural difference from the other two nudges (§K.5):
    # there is nothing to echo, so the message must be self-sufficient — it names
    # the two acceptable shapes of a next response instead of asking for a repair.
    assert "You drafted:" not in str(nudged["content"])
    assert "the final answer as TEXT" in str(nudged["content"])
    # And it is gone from the round after that — no test above reads the request
    # that FOLLOWS the recovery, because the recovery ends the turn.
    assert _requests_carrying_the_nudge(model) == [1]


# --- the budget seams (05 §C.3, §K.7) ---------------------------------------


async def test_a_refused_round_is_charged_to_the_budget_and_writes_no_ledger_entry() -> None:
    """05 §C.3, inherited: the refused round falls through to `record_iteration`, so
    a forced re-round costs an iteration like any other. Here it costs the LAST one
    and the turn pauses at the budget cap — which is the interaction worth pinning,
    because the cap handler force-blocks whenever `finalization_refused_this_round`
    is set, and this gate now also sets that flag.

    The gate fires only when NOTHING is pending, so the force-block must find no
    intent, write no ledger entry and emit nothing: a silent-finish refusal running
    into the cap has to leave the ledger and the telemetry byte-identical to a cap
    with no refusal at all.

    AND THE PAUSE MUST NOT CARRY THE FALLBACK. `EMPTY_ANSWER_FALLBACK_TEXT`
    substitutes at the `done` exit only — announcing "I could not answer" on a turn
    that is pausing to ASK the user whether to continue would be a lie the user is
    about to disprove by answering "continue".
    """
    loop, store, events, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")], usage={"total_tokens": 10}),
            ModelTurnResult(assistant_text=None, usage={"total_tokens": 10}),
        ],
        mcp=_rows_mcp("runQuery", 1),
        max_loop_iterations=2,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="how many in sales"
    )

    assert outcome.status == "paused_budget_cap", "the refused round was not charged"
    assert _events(events, EMPTY_ANSWER_REFUSED_EVENT) == [{"incomplete_reason": ""}]
    assert not _events(events, EMPTY_ANSWER_EXHAUSTED_EVENT)
    assert not _events(events, "loop_intent_force_blocked")
    assert not _events(events, "loop_analysis_state_transition")
    assert outcome.assistant_text != EMPTY_ANSWER_FALLBACK_TEXT
    assert store.claims == ["empty_answer"]
    doc = await store.get_or_create_session(SESSION_ID)
    assert [m.role for m in doc.messages] == ["user"], (
        "a pause persists no assistant message — least of all the fallback"
    )


async def test_a_budget_cap_resume_grants_a_fresh_empty_answer_allowance() -> None:
    """05 §C.1: a `continue` answer grants a FRESH WINDOW, and the allowance key is
    `(turn_index, window_count, kind)` — so the resumed window gets its own
    `empty_answer` grant even though window 1 spent one on the same turn.

    THIS IS THE LONG TURN'S ONLY PROTECTION. A turn that caps once has already
    proved it is the kind of turn this gate exists for, and an allowance carried
    across the resume would mean the FIRST silent finish after a cap is shipped
    blank — with `loop_empty_answer_exhausted` recorded for a gate that had never
    actually spoken in that window. The two persisted keys are the proof, because
    a window-only or turn-only key would show one entry here, not two.
    """
    store = CountingStore()
    loop, _store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")], usage={"total_tokens": 10}),
            # Window 1: silent, refused — and the charge trips the cap.
            ModelTurnResult(assistant_text=None, usage={"total_tokens": 10}),
            # Window 2, after the resume: silent again on a brand-new grant...
            ModelTurnResult(assistant_text=None, usage={"total_tokens": 10}),
            # ...and the round handed back lands the answer.
            ModelTurnResult(assistant_text="Sales has 3 people.", usage={"total_tokens": 10}),
        ],
        mcp=_rows_mcp("runQuery", 1),
        max_loop_iterations=2,
        store=store,
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="how many in sales"
    )
    assert paused.status == "paused_budget_cap"

    outcome = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="continue"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Sales has 3 people."
    assert len(_events(events, EMPTY_ANSWER_REFUSED_EVENT)) == 2, (
        "the resumed window was denied a grant it had never spent"
    )
    assert not _events(events, EMPTY_ANSWER_EXHAUSTED_EVENT)
    assert store.claims == ["empty_answer", "empty_answer"]
    # Every scripted turn consumed — so the second refusal really happened in the
    # resumed window rather than the script running short.
    assert model.calls_made == 4
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.finalization_blocks == {"0:1:empty_answer": 1, "0:2:empty_answer": 1}


async def test_a_budget_cap_stop_resume_is_not_gated() -> None:
    """05 §K.7: the `"stop"` resume returns from inside `resume()` BEFORE
    `_run_loop_body` is entered — it never reaches exit #1, so no gate can see it.

    Worth asserting rather than assuming, because the pause it resumes FROM was
    caused by a silent finish this gate refused. The user's own words end the turn
    here, and the substitution must not overwrite them with an apology for a
    silence the user has already responded to.
    """
    store = CountingStore()
    loop, _store, events, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")], usage={"total_tokens": 10}),
            ModelTurnResult(assistant_text=None, usage={"total_tokens": 10}),
        ],
        mcp=_rows_mcp("runQuery", 1),
        max_loop_iterations=2,
        store=store,
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="how many in sales"
    )
    assert paused.status == "paused_budget_cap"
    claims_at_the_pause = list(store.claims)
    events.clear()

    outcome = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="stop"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text is not None
    assert outcome.assistant_text.startswith("Stopping here")
    assert outcome.assistant_text != EMPTY_ANSWER_FALLBACK_TEXT
    assert store.claims == claims_at_the_pause, "the stop resume claimed an allowance"
    assert not _events(events, EMPTY_ANSWER_REFUSED_EVENT)
    assert not _events(events, EMPTY_ANSWER_EXHAUSTED_EVENT)


async def test_the_hard_ceiling_path_is_not_gated() -> None:
    """05 §K.7: a turn that runs out of windows is terminated from the budget guard
    and never reaches the bare-text exit, so neither the refusal nor the
    substitution can fire there.

    Asserted rather than assumed because the two returns sit a few lines apart and
    both carry an `assistant_text` that may legitimately be `None` — the hard
    ceiling's is a partial-results stop, NOT a silent finish, and substituting the
    fallback into it would tell the user "I could not answer" about a turn that was
    cut off mid-work with findings in hand.
    """
    loop, store, events, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query(f"q{i}")], usage={"total_tokens": 10})
            for i in range(1, 9)
        ],
        mcp=_rows_mcp("runQuery", *([1] * 8)),
        max_loop_iterations=2,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="never ends"
    )
    while outcome.status == "paused_budget_cap":
        outcome = await loop.resume(
            session_id=SESSION_ID, credentials=_credentials(), answer="continue"
        )

    assert outcome.status == "stopped_hard_ceiling"
    assert outcome.assistant_text != EMPTY_ANSWER_FALLBACK_TEXT
    assert store.claims == []
    assert not _events(events, EMPTY_ANSWER_REFUSED_EVENT)
    assert not _events(events, EMPTY_ANSWER_EXHAUSTED_EVENT)


async def test_an_ask_user_pause_with_no_prose_is_not_gated() -> None:
    """05 §K.7: a pause is not a finish. The turn has not ended, nothing is being
    presented, and the model's silence beside an `askUser` call is not silence at
    all — the question IS the communication.

    Refusing it would inject a nudge that the user's answer then has to survive,
    and would tell a model that just asked a clarifying question that it had said
    nothing.
    """
    loop, store, events, _model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    ToolCallRequest(id="ask", name="askUser", arguments={"question": "which?"})
                ],
            ),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="ambiguous"
    )

    assert outcome.status == "paused_ask_user"
    assert outcome.assistant_text != EMPTY_ANSWER_FALLBACK_TEXT
    assert store.claims == []
    assert not _events(events, EMPTY_ANSWER_REFUSED_EVENT)
    assert not _events(events, EMPTY_ANSWER_EXHAUSTED_EVENT)


# --- the fallback text itself -----------------------------------------------


def test_the_fallback_text_survives_the_prose_scrub_byte_identical() -> None:
    """05 §K.6: "the fallback carries NO IDENTIFIER SHAPES, so the scrub passes it
    through untouched" — a claim about a constant, so it is pinned against the real
    scrubber rather than by reading the string.

    It matters because the substitution is inserted at `_finish`'s *assistant_text*
    argument, ABOVE the scrub — deliberately, so that one scrubbed string reaches
    both the user and history. A fallback containing a backticked or quoted
    identifier shape would therefore come out wearing `[schema detail withheld]`,
    which is a second defect in the first one's clothes: the message explaining a
    failure would itself look like a redaction failure, and it would emit a
    `loop_answer_prose_redacted` event that made the disclosure rate lie.

    Both provenance modes are checked: `None` (what the pause exits pass) and a
    populated USES set, which is what arms the double-quoted arm.
    """
    for provenance in (None, frozenset({(_E, "Department"), (_E, "Name")})):
        scrubbed, redactions = scrub_answer_prose(
            EMPTY_ANSWER_FALLBACK_TEXT, provenance=provenance
        )
        assert scrubbed == EMPTY_ANSWER_FALLBACK_TEXT, (
            f"the fallback was mangled by the scrub under provenance={provenance!r}"
        )
        assert redactions == 0


async def test_the_substituted_fallback_emits_no_redaction_event() -> None:
    """The end-to-end half of the test above: the substitution really does go
    through the scrub on the live path, and comes out the other side unchanged and
    uncounted.

    `loop_answer_prose_redacted` fires ONLY when something was redacted, so the
    event rate IS the disclosure rate — a fallback that tripped it would put a
    permanent false floor under that signal, on exactly the turns an operator is
    most likely to be reading.
    """
    loop, _store, events, _model = _build(
        [ModelTurnResult(assistant_text=None), ModelTurnResult(assistant_text=None)]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="q"
    )

    assert outcome.assistant_text == EMPTY_ANSWER_FALLBACK_TEXT
    assert not _events(events, ANSWER_PROSE_REDACTED_EVENT)


async def test_the_fallback_reads_back_from_history_exactly_as_it_was_shown() -> None:
    """LIVE/HISTORY PARITY, through the projection the UI actually reloads (05 §K.6).

    `test_a_second_silent_finish_is_substituted_and_persisted` reads the store's
    raw messages; this reads `project_history`, which is what `GET /session/history`
    returns — the D44 scope filters, the turn join and all. The bug being closed was
    precisely a live/history DISAGREEMENT (a blank bubble live, and NOTHING at all
    in history), so a test that never runs the projection cannot see a regression
    that drops the message one layer above the store.

    The three assertions are one claim: the turn EXISTS in history, its answer is
    the same string the user was shown, and there is exactly one answer for it —
    not the empty message the old `or None` guard existed to prevent, and not two
    messages from the substitution being appended beside a real one.
    """
    # The SECOND silence is `None`, and that is the sharp input: `persist_text` is
    # read as a "should I write anything" flag, so the old `result.assistant_text or
    # None` guard still wrote the substitution for a whitespace-only draft (`"  "` is
    # truthy) and dropped the turn entirely only for `None`/`""`. A parity test built
    # on whitespace passes against the very shape it exists to reject.
    loop, store, _events, _model = _build(
        [ModelTurnResult(assistant_text="  \t"), ModelTurnResult(assistant_text=None)]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="how many in sales"
    )
    assert outcome.assistant_text == EMPTY_ANSWER_FALLBACK_TEXT

    doc = await store.get_or_create_session(SESSION_ID)
    history = project_history(doc.messages, doc.tool_trail, frozenset(), doc.pause_checkpoint)

    assert [turn["question"] for turn in history["turns"]] == ["how many in sales"]
    assert [turn["answer"] for turn in history["turns"]] == [outcome.assistant_text], (
        "history and the live turn disagree — the exact divergence §K.6 closes"
    )
    assert len([m for m in doc.messages if m.role == "assistant"]) == 1


async def test_a_spent_intents_grant_does_not_silence_the_empty_answer_gate() -> None:
    """THE PLACEMENT DECISION, at the top of the chain (05 §K.4).

    `test_the_shape_gate_takes_precedence_and_keeps_its_own_allowance` proves the
    same rule one branch lower, but this is the branch where the `elif` would be
    most tempting and where its failure mode is worst: the pending-intents `else` is
    not a no-op. It force-blocks every surviving intent `ENFORCEMENT_EXHAUSTED`,
    writes the ledger and emits — a full, terminal-looking disposition — and THEN
    lets the finish proceed. As an `elif`, the round that wrote all of that would
    also be the round on which a totally silent response sailed through: the user
    would get a blank bubble on the one turn the runtime had just recorded as
    unfinishable.

    So the sequence here is the measured one, moved up a gate: refuse for intents
    (grant spent), silence again, close the ledger, and STILL get a word in about
    the silence — on this gate's own untouched allowance.
    """
    loop, store, events, _model = _build(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name=STATE,
                        arguments={"intents": [{"description": "headcount by department"}]},
                    )
                ]
            ),
            # Round 1: silent, intents pending -> the INTENTS refusal (more specific).
            ModelTurnResult(assistant_text=None),
            # Round 2: silent again, intents still pending -> the intents grant is
            # spent, so that branch force-blocks instead of refusing. Nothing has
            # been refused this round, so the silence finally gets answered.
            ModelTurnResult(assistant_text=None),
            ModelTurnResult(assistant_text="Sales 3, Eng 2."),
        ]
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Sales 3, Eng 2.", (
        "the answer arrived on the round the empty-answer gate handed back"
    )
    assert len(_events(events, "loop_finalization_refused")) == 1
    assert len(_events(events, "loop_enforcement_exhausted")) == 1
    assert _events(events, EMPTY_ANSWER_REFUSED_EVENT) == [{"incomplete_reason": ""}], (
        "the silent finish went unchallenged on the round the intents ledger closed"
    )
    assert not _events(events, EMPTY_ANSWER_EXHAUSTED_EVENT)
    # The intents gate asked twice (granted, then refused); the empty-answer gate
    # asked once, on its own untouched allowance, and was granted.
    assert store.claims == ["intents", "intents", "empty_answer"]
