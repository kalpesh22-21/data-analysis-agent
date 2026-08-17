"""The ANSWER-SHAPE exit gate (Release 1, doc 05 §J).

"Presenting a table" is an UNCONDITIONAL prompt rule — a multi-row answer goes
through `answerWithTable` — and it was the last strong rule in the prompt with no
runtime enforcement. Measured across three live suites it failed roughly 5 of 8
expected-table runs, and its worst failure mode was not a judgement call but a
FALSE BELIEF ABOUT TURN MECHANICS: the model ended the turn apologising that it
could no longer call the tool ("...that final table call was not made before the
tool session ended"), on a turn nothing had refused and nothing had ended.

A prompt cannot correct a belief the model holds while it is holding it. The
runtime can: refuse the bare-text finish ONCE and hand back a round.

Two halves of the same contract are load-bearing here, and both are tested:

  - the gate FIRES on a bare-text finish that is holding untabled multi-row
    results, and
  - the gate STAYS SILENT everywhere else — zero-row results (live q6, a correct
    "none found" in prose), single-figure answers, discovery reads, turns that did
    table their answer, pauses, and later turns of the same session.

The silent half is the one that regresses invisibly: a false positive here does
not fail loudly, it just charges every correct prose answer an extra round-trip
and tells the model to table a number.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.loop.finalization import (
    ANSWER_SHAPE_EXHAUSTED_EVENT,
    ANSWER_SHAPE_REFUSED_EVENT,
)
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import FinalizationBlockKind
from data_agent.runtime.session_history import project_history

SESSION_ID = "sess-answer-shape"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "Name": "String"}}
)

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"

# A phrase that appears ONLY in the answer-shape nudge, so its presence in a
# request (or its absence from a persisted message) is unambiguous. Deliberately
# NOT the pending-intents nudge's "re-send your final answer": the two nudges must
# be tellable apart, since precedence between them is part of the contract.
_NUDGE_MARK = "answerWithTable is still available to you"
_ESCAPE_MARK = "re-send your full answer"


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return []


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


class CountingStore(InMemorySessionStore):
    """The real in-memory store, RECORDING THE KIND of every allowance claimed.

    A count would not be enough any more. The two gates hold independent per-window
    allowances (05 §J.3), so what has to be asserted is not "how many claims" but
    "which allowance, in what order" — `["intents", "answer_shape"]` is a turn that
    was refused once by each, which is the sequence live measurement showed and the
    old shared budget could not serve.
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
) -> tuple[AgentLoop, CountingStore, list[tuple[str, dict[str, Any]]], ScriptedModelClient]:
    store = CountingStore()
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
    """One scripted response per call, each with the given number of rows."""
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


def _is_a_user_message(messages: list[dict[str, Any]], content: str) -> bool:
    """The nudge must be a `user`-role message: the base prompt stays the sole
    `system` message (05 §B.2), and a `tool` message cannot stand alone —
    `_assembled_to_canonical` only ever emits one by expanding a trail entry into
    an `assistant(tool_calls) + tool` pair."""
    return any(
        m.get("role") == "user" and str(m.get("content") or "") == content for m in messages
    )


def _requests_carrying_the_nudge(model: ScriptedModelClient) -> list[int]:
    """Indices of the model requests carrying the ANSWER-SHAPE nudge."""
    return [
        index
        for index, call in enumerate(model.calls)
        if any(_NUDGE_MARK in str(message.get("content") or "") for message in call.messages)
    ]


def _requests_carrying_the_intents_nudge(model: ScriptedModelClient) -> list[int]:
    """Indices of the model requests carrying the PENDING-INTENTS nudge.

    Kept apart from the shape nudge deliberately: with two independent allowances, a
    turn can now carry both in one window, and "which nudge, on which round" is the
    assertion that tells a working sequence from a starved one.
    """
    return [
        index
        for index, call in enumerate(model.calls)
        if any(
            "re-send your final answer" in str(message.get("content") or "")
            for message in call.messages
        )
    ]


# --- the gate FIRES ---------------------------------------------------------


async def test_a_multi_row_prose_finish_is_refused_and_the_table_lands_next_round() -> None:
    """The whole point, end to end: a three-row `runQuery`, a bare-text finish, one
    refusal, and an `answerWithTable` on the round that was handed back.

    The nudge must say the three things the live failure needed said — the turn is
    not over and the tool is available, one table per part, and the escape hatch —
    because the model's stated reason for not calling it was that it COULD not.
    """
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="Sales has 3, Eng has 2, Ops has 1."),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name=ANSWER,
                        arguments={
                            "answer": "Sales has 3, Eng has 2, Ops has 1.",
                            "tables": [
                                {
                                    "sql": "SELECT Department, count() AS n "
                                    "FROM employee GROUP BY Department"
                                }
                            ],
                        },
                    )
                ]
            ),
        ],
        mcp=_rows_mcp("runQuery", 3),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount by department"
    )

    assert outcome.status == "done"
    assert outcome.answer_sql is not None, "the table never landed"
    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 1}]
    assert not _events(events, ANSWER_SHAPE_EXHAUSTED_EVENT)
    assert store.claims == ["answer_shape"], "the gate claimed the wrong allowance"

    # The nudge reached the model exactly once — on the round that was handed back.
    assert _requests_carrying_the_nudge(model) == [2]
    nudge = next(
        str(m.get("content"))
        for m in model.calls[2].messages
        if _NUDGE_MARK in str(m.get("content") or "")
    )
    assert _is_a_user_message(model.calls[2].messages, nudge)
    # (a) the turn is not over and the tool is available — the belief being corrected.
    assert "NOT over" in nudge and "NEXT response" in nudge
    # (b) one table per part, and HOW to name each.
    assert "blueprint_id" in nudge and "sql otherwise" in nudge
    # (c) the escape hatch, without which a legitimate single-figure answer is stuck.
    assert _ESCAPE_MARK in nudge
    # The draft is carried back, or the model must regenerate its answer blind.
    assert "Sales has 3" in nudge


async def test_the_refused_draft_and_the_nudge_are_never_persisted() -> None:
    """05 §B.2, unchanged for this gate: exit #1 persists NOTHING. A persisted
    nudge would appear in `/session/history` as something the user said, and a
    persisted draft would show the user an answer the runtime refused."""
    loop, store, _, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="REFUSED DRAFT"),
            ModelTurnResult(assistant_text="REFUSED DRAFT"),
        ],
        mcp=_rows_mcp("runQuery", 3),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    doc = await store.get_or_create_session(SESSION_ID)
    contents = [m.content for m in doc.messages]
    assert not any(_NUDGE_MARK in c for c in contents), "the nudge was persisted"
    # The FINAL prose is persisted once (it passed on the spent grant) — the
    # REFUSED one is not persisted twice.
    assert contents.count("REFUSED DRAFT") == 1
    history = project_history(doc.messages, doc.tool_trail, frozenset(), doc.pause_checkpoint)
    assert not any(_NUDGE_MARK in str(item) for item in history)


async def test_a_second_prose_finish_passes_and_records_exhausted() -> None:
    """The runtime records what it can and NEVER hard-locks a turn — the same
    posture `ENFORCEMENT_EXHAUSTED` takes for intents. A model that answers in
    prose twice gets its answer through; the operator gets a counter."""
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="Sales leads."),
            ModelTurnResult(assistant_text="Sales leads."),
        ],
        mcp=_rows_mcp("runQuery", 4),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="who leads?"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Sales leads."
    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 1}]
    assert _events(events, ANSWER_SHAPE_EXHAUSTED_EVENT) == [{}]
    # ITS OWN allowance, asked for twice: the second claim is refused by the store,
    # not by a second counter — and the `intents` allowance is never touched.
    assert store.claims == ["answer_shape", "answer_shape"]
    assert _requests_carrying_the_nudge(model) == [2], "the nudge outlived its one round-trip"


async def test_the_count_reported_is_every_untabled_multi_row_call() -> None:
    """`multi_row_calls` is the only thing separating "forgot one table" from
    "abandoned the format" — so it counts CALLS, across round-trips, not results."""
    loop, _, events, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(tool_calls=[_query("q2")]),
            ModelTurnResult(assistant_text="two tables' worth, in prose"),
            ModelTurnResult(assistant_text="two tables' worth, in prose"),
        ],
        mcp=_rows_mcp("runQuery", 3, 5),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 2}]


# --- the gate STAYS SILENT --------------------------------------------------


async def test_a_zero_row_result_never_trips_the_gate() -> None:
    """THE q6 REGRESSION. An empty result set is a correct prose answer ("no
    employees match"), and 04 §B.4 already treats it as an answer rather than a
    failure. Refusing it would demand a table of nothing."""
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="No employees match that filter."),
        ],
        mcp=_rows_mcp("runQuery", 0),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="any matches?"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "No employees match that filter."
    assert store.claims == [], "the gate paid a store round-trip on a zero-row turn"
    assert not _events(events, ANSWER_SHAPE_REFUSED_EVENT)
    assert not _events(events, ANSWER_SHAPE_EXHAUSTED_EVENT)
    assert _requests_carrying_the_nudge(model) == []


async def test_a_single_row_result_never_trips_the_gate() -> None:
    """A single figure ("headcount is 412") is not a table, and the prompt does not
    ask for one. `row_count > 1` — not `>= 1` — is where that lives."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="Headcount is 412."),
        ],
        mcp=_rows_mcp("runQuery", 1),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="how many?"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Headcount is 412."
    assert store.claims == []
    assert not _events(events, ANSWER_SHAPE_REFUSED_EVENT)


async def test_discovery_reads_never_trip_the_gate_however_many_rows_they_return() -> None:
    """`sampleRows` is DISCOVERY, not an answer. A model that peeks at ten sample
    rows and then answers a single figure in prose is behaving exactly as the
    prompt asks, and counting that read would refuse it."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name="sampleRows",
                        arguments={"database": "dbpcm_warehouse", "table": "employee"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="The Department column is free text."),
        ],
        mcp=_rows_mcp("sampleRows", 10),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="what's in there?"
    )

    assert outcome.status == "done"
    assert store.claims == []
    assert not _events(events, ANSWER_SHAPE_REFUSED_EVENT)


async def test_a_turn_that_answered_with_a_table_is_untouched() -> None:
    """The gate asks "was this answer tabled", not "was this answer tabled LAST".
    A turn that designated its table and then ends is finished, and must not be
    handed a round to do it again."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name=ANSWER,
                        arguments={
                            "answer": "By department:",
                            "tables": [{"sql": "SELECT 1"}],
                        },
                    )
                ]
            ),
        ],
        mcp=_rows_mcp("runQuery", 6),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="by department"
    )

    assert outcome.status == "done"
    assert outcome.answer_sql is not None
    assert store.claims == []
    assert not _events(events, ANSWER_SHAPE_REFUSED_EVENT)


async def test_the_gate_is_satisfied_by_the_designation_not_by_its_shape() -> None:
    """08 §O: the gate counts `answer_table_succeeded` on a SUCCESSFUL
    `answerWithTable`, whatever the arguments looked like.

    Its two write sites read the tool NAME and the STATUS and nothing else — the
    in-batch one on dispatch, and the trail-walk seed that rebuilds the flag after a
    budget cap or a resume. Neither reads `args`, so the tables-only slim-down
    cannot have moved it. Pinned rather than assumed, because the failure would be
    silent and expensive in exactly one direction: a gate that stopped recognising a
    legacy-shape designation would charge a finished turn an extra round-trip and
    tell the model to table an answer it already tabled — which is the false-positive
    half this file exists to protect, and the half that regresses invisibly.

    So the pre-08-§O top-level shape is used here ON PURPOSE, and the assertion is
    that the gate never noticed the difference.
    """
    loop, store, events, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name=ANSWER,
                        # The retired carrier, folded by `resolve_designations`.
                        arguments={"answer": "By department:", "sql": "SELECT 1"},
                    )
                ]
            ),
        ],
        mcp=_rows_mcp("runQuery", 6),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="by department"
    )

    assert outcome.status == "done"
    assert outcome.answer_sql == "SELECT 1"
    assert store.claims == []
    assert not _events(events, ANSWER_SHAPE_REFUSED_EVENT)


async def test_an_ask_user_pause_holding_multi_row_results_is_not_gated() -> None:
    """The gate lives on the ORDINARY no-tool-calls exit only. A pause is not a
    finish: the turn has not ended, nothing is being presented to anyone, and
    refusing it would inject a nudge the user's answer then has to survive."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="ask", name="askUser", arguments={"question": "which?"})
                ]
            ),
        ],
        mcp=_rows_mcp("runQuery", 7),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="ambiguous"
    )

    assert outcome.status == "paused_ask_user"
    assert store.claims == []
    assert not _events(events, ANSWER_SHAPE_REFUSED_EVENT)


async def test_the_hard_ceiling_path_is_not_gated() -> None:
    """A turn that runs out of iterations never reaches the bare-text exit at all —
    it is terminated from the budget guard. Asserted rather than assumed, because
    the two paths return from within a few lines of each other."""
    loop, store, events, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query(f"q{i}")], usage={"total_tokens": 10})
            for i in range(1, 8)
        ],
        mcp=_rows_mcp("runQuery", *([3] * 7)),
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
    assert store.claims == []
    assert not _events(events, ANSWER_SHAPE_REFUSED_EVENT)
    assert not _events(events, ANSWER_SHAPE_EXHAUSTED_EVENT)


async def test_a_later_turn_is_not_gated_by_an_earlier_turns_rows() -> None:
    """TURN SCOPE, which is also the cross-turn replay protection. Turn 0 runs a
    multi-row query and spends the window's grant; turn 1 is an ordinary follow-up
    answered in prose and must be untouched — a stale refusal cannot be replayed
    onto it, and `claim_finalization_block`'s key is `(turn_index, window)` so
    turn 1 would have its own grant if it ever needed one."""
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="in prose, sorry"),
            ModelTurnResult(assistant_text="in prose, sorry"),
            ModelTurnResult(assistant_text="yes, that is right"),
        ],
        mcp=_rows_mcp("runQuery", 3),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="turn zero")
    claims_after_turn_zero = store.claims
    events.clear()

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="are you sure?"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "yes, that is right"
    assert store.claims == claims_after_turn_zero, "turn 1 asked for a grant it did not need"
    assert not _events(events, ANSWER_SHAPE_REFUSED_EVENT)
    assert not _events(events, ANSWER_SHAPE_EXHAUSTED_EVENT)
    assert _requests_carrying_the_nudge(model) == [2]


async def test_a_budget_cap_resume_reseeds_the_count_from_the_persisted_trail() -> None:
    """THE SEED IS THE TEST, and nothing else in the suite proves it.

    A budget-cap `continue` starts a FRESH `_run_loop_body` with empty window-locals
    (05 §C.1 — the same reason `seen_read_calls` is seeded from the trail). Window 1
    here runs both multi-row queries and then caps; window 2 does nothing but finish
    in prose. If `multi_row_answer_calls` were a plain window-local, it would be `0`
    on the resumed window and the gate would go silent on exactly the long,
    several-table turns it was built for — silently, with a green suite.

    `multi_row_calls: 2` is the assertion that pins it: both calls are recovered
    from the persisted trail, not from anything the resumed window observed.

    (The blueprint resume tests in `tests/runtime/blueprint/` exercise the same seed
    incidentally, but cannot PROVE it — `ScriptedModelClient` does not raise on
    leftover turns, so a seed regression there just consumes one fewer turn.)
    """
    loop, _, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")], usage={"total_tokens": 10}),
            ModelTurnResult(tool_calls=[_query("q2")], usage={"total_tokens": 10}),
            # Window 2, after the resume: prose, refused, then prose again.
            ModelTurnResult(assistant_text="in prose"),
            ModelTurnResult(assistant_text="in prose"),
        ],
        mcp=_rows_mcp("runQuery", 3, 4),
        max_loop_iterations=2,
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="two breakdowns"
    )
    assert paused.status == "paused_budget_cap"

    outcome = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="continue"
    )

    assert outcome.status == "done"
    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 2}], (
        "the resumed window did not recover the turn's multi-row calls from the trail"
    )
    assert _events(events, ANSWER_SHAPE_EXHAUSTED_EVENT) == [{}]
    # Every scripted turn was consumed — so the refusal really did happen in the
    # resumed window, rather than the script simply running short.
    assert model.calls_made == 4


async def test_a_truncated_draft_echo_is_marked_and_the_hatch_asks_for_the_full_answer() -> None:
    """N1. The echo is the model's ONLY copy of its draft (D22 discards free text
    around tool calls, and exit #1 persists nothing), and the 2000-char slice is
    invisible from the inside. An unmarked cut beside "re-send it unchanged" reads
    as "re-send exactly this" and loses the tail of a long answer silently.

    So: the cut is marked, and the hatch asks for the FULL answer — never for the
    echo verbatim.
    """
    draft = "A" * 2500
    loop, store, _, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text=draft),
            ModelTurnResult(assistant_text="the full answer, again"),
        ],
        mcp=_rows_mcp("runQuery", 3),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    nudge = next(
        str(m.get("content"))
        for m in model.calls[2].messages
        if _NUDGE_MARK in str(m.get("content") or "")
    )
    echoed = nudge.split("You drafted: ", 1)[1].split("\n", 1)[0]
    assert echoed == "A" * 2000 + " …[truncated]"
    assert _ESCAPE_MARK in nudge
    assert "unchanged" not in nudge, "the hatch still demands a verbatim re-send"
    # Belt and braces on the D22 exposure: the truncated echo is never persisted.
    doc = await store.get_or_create_session(SESSION_ID)
    assert not any("[truncated]" in m.content for m in doc.messages)


async def test_a_short_draft_echo_carries_no_truncation_marker() -> None:
    """The marker must mean something. Appending it unconditionally would tell the
    model its complete answer was cut, and the hatch would then be asking it to
    reconstruct text it can see in full."""
    loop, _, _, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="Sales 3, Eng 2."),
            ModelTurnResult(assistant_text="Sales 3, Eng 2."),
        ],
        mcp=_rows_mcp("runQuery", 3),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    nudge = next(
        str(m.get("content"))
        for m in model.calls[2].messages
        if _NUDGE_MARK in str(m.get("content") or "")
    )
    assert "You drafted: Sales 3, Eng 2." in nudge
    assert "[truncated]" not in nudge


async def test_a_refused_round_is_charged_to_the_budget_and_writes_no_ledger_entry() -> None:
    """05 §C.3, inherited: the refused round falls through to `record_iteration`,
    so a forced re-round costs an iteration like any other. Here it costs the LAST
    one, and the turn pauses at the budget cap.

    The second assertion is the interaction that would otherwise be silent. The cap
    handler force-blocks whenever `finalization_refused_this_round` is set — a flag
    this gate now also sets — but the gate only fires when NOTHING is pending, so
    the force-block finds no intent, writes nothing, and emits nothing. A shape
    refusal running into the cap must leave the ledger and the telemetry
    byte-identical to a cap with no refusal at all.
    """
    loop, _, events, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")], usage={"total_tokens": 10}),
            ModelTurnResult(assistant_text="in prose", usage={"total_tokens": 10}),
        ],
        mcp=_rows_mcp("runQuery", 3),
        max_loop_iterations=2,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="by department"
    )

    assert outcome.status == "paused_budget_cap", "the refused round was not charged"
    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 1}]
    assert not _events(events, "loop_intent_force_blocked")
    assert not _events(events, "loop_analysis_state_transition")


# --- precedence, and the two independent allowances -------------------------


async def test_the_intents_nudge_no_longer_starves_the_shape_gate() -> None:
    """THE SEQUENCE THAT MEASURED BADLY, and the reason the allowances were split
    (05 §J.3, revised 2026-08-12 on live data).

    On a multi-intent question the two gates do not compete — they fire in SEQUENCE:

      1. the model finishes in prose with an intent still pending  -> INTENTS nudge
      2. it closes the ledger and finishes in prose again          -> SHAPE nudge
      3. it calls answerWithTable                                  -> done, tabled

    Under one shared allowance, step 1 consumed it and step 2 could only emit
    `loop_answer_shape_exhausted` and let untabled prose through — 2 of 4 live
    three-part runs (traces `900a85a4`, `16f090db`). The gate was starved on
    precisely the question it exists for, which is what a shared budget buys when
    the two complaints arrive one after the other rather than together.

    Precedence is unchanged and is asserted here too: in step 1 BOTH conditions
    hold, and the intents refusal is the one that fires.
    """
    loop, store, events, model = _build(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name=STATE,
                        arguments={"intents": [{"description": "headcount by department"}]},
                    ),
                    _query("q1"),
                ]
            ),
            # (1) Bare-text finish: intent pending AND three rows untabled. BOTH
            # gates qualify; the intents one wins.
            ModelTurnResult(assistant_text="Sales 3, Eng 2, Ops 1."),
            # (2) Closes the ledger...
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="s2",
                        name=STATE,
                        arguments={
                            "intents": [
                                {
                                    "intent_id": "i1",
                                    "status": "completed",
                                    "evidence_tool_call_id": "q1",
                                }
                            ]
                        },
                    )
                ]
            ),
            # ...and finishes in prose again. Nothing is pending now, so the SHAPE
            # gate reaches its own, untouched allowance.
            ModelTurnResult(assistant_text="Sales 3, Eng 2, Ops 1."),
            # (3) The table finally lands.
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name=ANSWER,
                        arguments={
                            "answer": "Sales 3, Eng 2, Ops 1.",
                            "tables": [
                                {
                                    "sql": "SELECT Department, count() "
                                    "FROM employee GROUP BY Department"
                                }
                            ],
                        },
                    )
                ]
            ),
        ],
        mcp=_rows_mcp("runQuery", 3),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount by department"
    )

    assert outcome.status == "done"
    assert outcome.answer_sql is not None, "the table never landed — the gate was starved"
    # Precedence: the intents refusal fired on the round where both qualified, and
    # its payload is byte-identical to a turn where only it qualifies.
    assert _events(events, "loop_finalization_refused") == [
        {"exit": "no_tool_calls", "pending_count": 1}
    ]
    # ...and the shape gate got its own round LATER, from its own allowance.
    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 1}]
    assert not _events(events, ANSWER_SHAPE_EXHAUSTED_EVENT), "the gate was starved again"
    # TWO claims, of DIFFERENT kinds, in that order — the whole change in one line.
    assert store.claims == ["intents", "answer_shape"]
    # Two nudges, in the right order and one round-trip each: the intents nudge on
    # request 2, the shape nudge on request 4.
    assert _requests_carrying_the_intents_nudge(model) == [2]
    assert _requests_carrying_the_nudge(model) == [4]


async def test_each_turn_gets_its_own_shape_allowance() -> None:
    """CROSS-TURN REPLAY SAFETY for the new claim, which is the same trap the
    intents allowance already fell into once: `window_count` restarts at 1 on every
    external turn while `finalization_blocks` persists for the whole session, so a
    key without the turn index would let turn 0's spent allowance silence turn 1.

    Both turns here run a multi-row query and finish in prose, and BOTH must be
    refused once.
    """
    loop, store, events, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(assistant_text="turn zero, in prose"),
            ModelTurnResult(assistant_text="turn zero, in prose"),
            ModelTurnResult(tool_calls=[_query("q2")]),
            ModelTurnResult(assistant_text="turn one, in prose"),
            ModelTurnResult(assistant_text="turn one, in prose"),
        ],
        mcp=_rows_mcp("runQuery", 3, 4),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="turn zero")
    events.clear()
    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="turn one")

    assert outcome.status == "done"
    # Turn 1 was refused ON ITS OWN MERITS — one multi-row call, not turn 0's.
    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 1}]
    assert store.claims == ["answer_shape", "answer_shape", "answer_shape", "answer_shape"]
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.finalization_blocks == {"0:1:answer_shape": 1, "1:1:answer_shape": 1}


# ---------------------------------------------------------------------------
# THE EMPTY DESIGNATION (08 §O) — the same complaint, arriving through exit #2
#
# Both probes below were run against the live loop before this section existed and
# both passed silently, which is why the section exists. The gate above watches
# exit #1 (a model turn with NO tool calls); `answerWithTable` is exit #2, and a
# call that designates nothing walks straight through it.
# ---------------------------------------------------------------------------


def _empty_answer(call_id: str = "a1", answer: str = "Sales has 3, Eng 2, Ops 1.") -> ToolCallRequest:
    """`answerWithTable` that names NO table — the shape 08 §O made likelier, since
    `tables` is REQUIRED and a model that cannot omit a declared key sends `[]`."""
    return ToolCallRequest(id=call_id, name=ANSWER, arguments={"answer": answer, "tables": []})


async def test_an_answer_with_no_table_is_nudged_and_the_table_lands_next_round() -> None:
    """PROBE A. `{answer: <prose>, tables: []}` on a turn holding a 6-row result.

    Measured on the live loop before the fix: `status=done`, no tables, and ZERO
    events or log lines. The call succeeded (the tool is stateless and refuses
    nothing), carried non-blank prose, and therefore TERMINATED the turn — through
    the one exit the 05 §J gate does not watch. The user asked for a breakdown,
    the turn held six rows of it, and the answer was prose with no grid.

    This is `_answer_table_blueprint_not_run`'s failure one step earlier: there the
    model named a table that could not be resolved, here it named none at all. So
    it takes the same treatment — a retryable refusal, before the trail entry is
    written, so the persisted entry IS the nudge and the model reads it next round.
    """
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(tool_calls=[_empty_answer()]),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a2",
                        name=ANSWER,
                        arguments={
                            "answer": "Sales has 3, Eng 2, Ops 1.",
                            "tables": [{"sql": f"SELECT Department, count() FROM {_E} GROUP BY 1"}],
                        },
                    )
                ]
            ),
        ],
        mcp=_rows_mcp("runQuery", 6),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount by department"
    )

    assert outcome.status == "done"
    assert outcome.answer_sql is not None, "the table never landed"
    # It shares the shape gate's allowance, so it reports through the same event.
    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 1}]
    assert store.claims == ["answer_shape"]
    # …and the empty designation itself is reported where it happened.
    assert _events(events, "loop_answer_table_empty_designation") == [{}]
    # The refusal is PERSISTED as the entry, so the model reads it on the rebuild.
    trail = await store.load_trail(SESSION_ID)
    refusals = [e for e in trail if e.error_code == "ANSWER_TABLE_NO_TABLE_DESIGNATED"]
    assert len(refusals) == 1
    assert refusals[0].status == "error"
    assert "Every table goes in `tables`" in (refusals[0].denial_detail or "")


async def test_the_empty_designation_passes_once_its_allowance_is_spent() -> None:
    """NEVER A HARD LOCK. The allowance is shared with the shape gate, so a model
    that empties the array twice is refused once and then let through — the same
    posture `ENFORCEMENT_EXHAUSTED` takes for intents. The event still fires on the
    second one, so the behaviour stays visible after the grant is gone: an
    allowance is a bound on REFUSALS, never on reporting.
    """
    loop, store, events, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(tool_calls=[_empty_answer("a1")]),
            ModelTurnResult(tool_calls=[_empty_answer("a2")]),
        ],
        mcp=_rows_mcp("runQuery", 6),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount by department"
    )

    assert outcome.status == "done"
    assert outcome.answer_sql is None, "there was never a table to show"
    assert outcome.assistant_text == "Sales has 3, Eng 2, Ops 1."
    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 1}]
    assert _events(events, ANSWER_SHAPE_EXHAUSTED_EVENT) == [{}]
    # Reported BOTH times — the second call was let through, not un-noticed.
    assert _events(events, "loop_answer_table_empty_designation") == [{}, {}]


async def test_an_empty_designation_does_not_disarm_the_gate_for_the_prose_finish() -> None:
    """PROBE B, and the second half of the same defect.

    `answer_table_succeeded` was set on ANY successful `answerWithTable`, read from
    the tool name and status alone. So a mid-turn call with a BLANK answer and an
    empty array — which does not terminate the turn, because the terminal condition
    needs non-blank prose — disarmed the gate with zero designations, and the
    model's later bare-prose finish then sailed through exit #1 unrefused. Two
    silent paths, one flag.

    The flag now means what its name says: the user has a grid. A blank-answer call
    is used here deliberately, so the failure being tested is the FLAG and not the
    terminal nudge (which needs non-blank prose to fire on).
    """
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            # Blank `answer` — does not terminate, and designates nothing.
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="a1", name=ANSWER, arguments={"answer": "", "tables": []})]
            ),
            ModelTurnResult(assistant_text="Sales has 3, Eng 2, Ops 1."),
            ModelTurnResult(assistant_text="Sales has 3, Eng 2, Ops 1."),
        ],
        mcp=_rows_mcp("runQuery", 6),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount by department"
    )

    assert outcome.status == "done"
    # THE GATE STILL FIRED on the prose finish — the empty call did not disarm it.
    assert _events(events, ANSWER_SHAPE_REFUSED_EVENT) == [{"multi_row_calls": 1}]
    # Two claims: refused on the first prose finish, spent on the second (the model
    # repeated itself), which is the ordinary refuse-then-exhaust sequence.
    assert store.claims == ["answer_shape", "answer_shape"]
    assert _events(events, ANSWER_SHAPE_EXHAUSTED_EVENT) == [{}]
    assert _requests_carrying_the_nudge(model), "the shape nudge never reached the model"


async def test_a_blank_answer_call_that_designates_a_table_still_disarms_the_gate() -> None:
    """The other side of the flag rule, so it is not read as "only terminal calls
    count". A blank-`answer` call does not end the turn, but the tables it
    designated DO reach the user through the envelope — which is the whole thing
    the gate protects — so the gate is done for this turn.
    """
    loop, store, events, _ = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name=ANSWER,
                        arguments={"answer": "", "tables": [{"sql": "SELECT 1"}]},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Sales has 3, Eng 2, Ops 1."),
        ],
        mcp=_rows_mcp("runQuery", 6),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount by department"
    )

    assert outcome.status == "done"
    assert outcome.answer_sql == "SELECT 1"
    assert not _events(events, ANSWER_SHAPE_REFUSED_EVENT)
    assert store.claims == []


async def test_a_turn_holding_no_multi_row_result_is_never_nudged_for_an_empty_table() -> None:
    """THE FALSE-POSITIVE GUARD, and it is the half that regresses invisibly.

    A zero-row result is a correct answer in prose — live q6, "none found" — and a
    turn holding nothing multi-row has no table being withheld. Nudging it would
    charge a right answer an extra round-trip and tell the model to put a number in
    a grid, which is exactly the harm 05 §J's silent half exists to prevent. So the
    nudge is scoped to `multi_row_answer_calls > 0` and this turn ends first time.
    """
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(tool_calls=[_empty_answer(answer="Nobody matched.")]),
        ],
        mcp=_rows_mcp("runQuery", 0),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="who left last month?"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Nobody matched."
    assert len(model.calls) == 2, "the empty answer must have ended the turn first time"
    assert store.claims == []
    assert not _events(events, ANSWER_SHAPE_REFUSED_EVENT)
    # The event still fires — it records the shape, and the nudge decides on it.
    assert _events(events, "loop_answer_table_empty_designation") == [{}]


async def test_a_dropped_table_is_not_treated_as_an_empty_designation() -> None:
    """The distinction `carried_designation` exists to make, asserted directly.

    An empty resolved list has two causes needing opposite handling: the model named
    NOTHING (nudge — it can fix that), or it named something the runtime then
    dropped for a reason the model cannot act on. Here the designated query reads a
    column outside the caller's scope, so the table is dropped by
    `is_answer_table_in_scope` — the model's payload was correct and telling it to
    "put each table in `tables`" would be a lie it cannot act on.
    """
    scoped = RuntimeCredentials(
        session_id=SESSION_ID, jwt="jwt", column_scope=frozenset({f"{_E}.Department"})
    )
    loop, store, events, model = _build(
        [
            ModelTurnResult(tool_calls=[_query("q1")]),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name=ANSWER,
                        arguments={
                            "answer": "By department.",
                            "tables": [{"sql": f"SELECT Name FROM {_E}"}],
                        },
                    )
                ]
            ),
        ],
        mcp=_rows_mcp("runQuery", 6),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=scoped, user_message="headcount by department"
    )

    assert outcome.status == "done"
    assert len(model.calls) == 2, "a dropped table must not be nudged as an empty one"
    assert not _events(events, "loop_answer_table_empty_designation")
    assert store.claims == []
