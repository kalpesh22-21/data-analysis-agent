"""THE TURN-EXIT CONTRACT: what each `TurnOutcome` return site emits, and what it
deliberately does NOT.

Every other loop test drives a FEATURE through an exit and asserts the feature.
These assert the exits THEMSELVES — the three properties that are shared by no
single feature, are true only because of where each exit sits in the body, and
would therefore be silently re-derivable by anyone consolidating the return sites:

  1. THE EXIT'S EVENT IS NOT A FUNCTION OF ITS STATUS. Two `done` returns emit
     `loop_turn_done`; the third (the budget-cap `"stop"` answer, which returns
     from inside `resume()` before `_run_loop` is ever entered) emits NOTHING.
     Anything that derives the event from the status flips that third one on, and
     every `loop_turn_done` consumer starts counting a turn that produced no
     answer of its own.

  2. `provenance` IS DONE-ONLY, and its absence at the three non-`done` exits is a
     DECISION, not a gap. The fail-closed union is the tag on a persisted
     assistant message; a pause has no such message, so a `provenance` there would
     be a lineage claim about an answer that does not exist yet. The pins below
     drive turns whose union IS determined, so "it happened to be `None`" cannot
     pass for the rule.

  3. AN IN-LOOP RUNTIME-TOOL PAUSE CARRIES THE WINDOW'S ANSWER ENVELOPE. The
     envelope is computed from the accumulators at the pause; a designation folded
     EARLIER in the same tool batch must survive the pause that follows it.

None of the three is load-bearing for any single feature, which is exactly why
they need a home: each fails silently, and each is one keystroke of tidying away.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolPause, ToolResult
from data_agent.runtime.loop.agent_loop import AgentLoop, TurnContext
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

SESSION_ID = "sess-turn-exit"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

# A query over a catalogued table, so its `TrailEntry.provenance` is DETERMINED —
# which is what makes the `provenance is None` assertions below mean something.
_SQL = f"SELECT EmployeeCode FROM {_E}"

TOOLS_SCHEMA = [
    {"type": "function", "name": "runQuery", "description": "", "parameters": {}},
    {"type": "function", "name": "askUser", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


def _query_mcp(count: int = 6) -> FakeMCPClient:
    return FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["EmployeeCode"], "rows": [["E1"]], "row_count": 1, "truncated": False}
            ]
            * count
        }
    )


def _query_turn(call_id: str) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id=call_id, name="runQuery", arguments={"sql": _SQL})],
        usage={"total_tokens": 10},
    )


def _build(
    turns: list[ModelTurnResult],
    *,
    mcp: FakeMCPClient | None = None,
    max_loop_iterations: int = 6,
    max_budget_windows: int = 3,
    runtime_tools: dict[str, Any] | None = None,
    store: InMemorySessionStore | None = None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[AgentLoop, InMemorySessionStore, list[tuple[str, dict[str, Any]]]]:
    store = store if store is not None else InMemorySessionStore()
    events = events if events is not None else []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    loop = AgentLoop(
        model_client=ScriptedModelClient(turns),
        tool_dispatcher=ToolDispatcher(mcp or _query_mcp(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=max_loop_iterations,
        max_wall_clock_seconds=60,
        max_budget_windows=max_budget_windows,
        observer=_observe,
        runtime_tools=runtime_tools or {},
    )
    return loop, store, events


def _names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _payload in events]


# --- 1. the exit's event is not a function of its status --------------------


async def test_the_resume_stop_exit_returns_done_and_emits_no_loop_turn_done() -> None:
    """The budget-cap `"stop"` answer is the THIRD `done` return and the only one
    that emits no event at all.

    It returns from inside `resume()` before `_run_loop` is entered: there is no
    window, no accumulators and no model round-trip behind it — the text is canned
    and `tool_calls_made` is 0. `loop_turn_done` means "a window finished a turn",
    and nothing here did, so the silence is correct.

    ASSERTED BECAUSE IT IS DERIVABLE-LOOKING. `status == "done"` is the one thing
    this exit shares with the two that DO emit, so any consolidation that reads the
    event off the status makes this one emit too — and it would look like a fix.
    """
    loop, _store, events = _build(
        [_query_turn("c1")], max_loop_iterations=1, max_budget_windows=3
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="go"
    )
    assert paused.status == "paused_budget_cap"
    assert "loop_paused_budget_cap" in _names(events)

    before = len(events)
    stopped = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="stop"
    )

    assert stopped.status == "done"
    assert stopped.tool_calls_made == 0
    assert stopped.assistant_text == (
        "Stopping here — here is what I found before the budget cap."
    )
    # The whole point: a `done` return that emits NO `loop_turn_done`, ever.
    assert "loop_turn_done" not in _names(events)
    # And nothing else sneaks in either — the only events this resume may emit are
    # the force-block ones, which fire only when an intent is pending (none here).
    assert _names(events[before:]) == []


async def test_the_two_in_body_done_exits_do_emit_loop_turn_done() -> None:
    """The other half of the same contract, so the assertion above cannot pass by
    the event having been deleted outright."""
    loop, _store, events = _build([ModelTurnResult(assistant_text="Here it is.")])

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="go"
    )

    assert outcome.status == "done"
    assert [p for n, p in events if n == "loop_turn_done"] == [{"tool_calls_made": 0}]


# --- 1b. the no-tool-calls exit's append is CONDITIONAL ---------------------


async def test_a_blank_final_answer_persists_no_assistant_message() -> None:
    """The no-tool-calls `done` exit appends its assistant `TurnMessage` only if
    there IS one — and "is there one" is FALSINESS, not `is not None`.

    A model can end a turn with `assistant_text=""` as easily as with `None` (an
    empty completion is a completion), and the two must behave identically: the
    outcome reports the text verbatim, history gets nothing. An empty assistant
    message is not harmless — `context/assembly.py` replays persisted messages into
    every later turn, `session_history` renders them, and a blank one is a blank
    bubble in the transcript plus a content-free entry in the model's context.

    THE `""` CASE IS THE ONE THAT NEEDS PINNING. `None` is caught by any
    `is not None` guard as well as by falsiness, so a guard that has quietly
    weakened from one to the other still passes every `None` test in the suite —
    `""` is the only input that tells them apart.
    """
    for blank in ("", None):
        loop, store, _events = _build([ModelTurnResult(assistant_text=blank)])

        outcome = await loop.run(
            session_id=SESSION_ID, credentials=_credentials(), user_message="go"
        )

        assert outcome.status == "done"
        assert outcome.assistant_text == blank, "the outcome reports it verbatim"
        doc = await store.get_or_create_session(SESSION_ID)
        assert [m.role for m in doc.messages] == ["user"], (
            f"a blank final answer ({blank!r}) must leave NO assistant message"
        )


# --- 2. provenance is done-only ---------------------------------------------


async def test_an_ask_user_pause_carries_no_provenance_even_when_determined() -> None:
    """`paused_ask_user` returns `provenance=None` although this turn's trail union
    is DETERMINED — proved by resuming the same turn to `done` and reading the
    union there. The pause is a partial: it has no persisted assistant message for
    a lineage tag to belong to."""
    loop, _store, _events = _build(
        [
            _query_turn("q1"),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1", name="askUser", arguments={"question": "Which department?"}
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Sales, then."),
        ]
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="go"
    )
    assert paused.status == "paused_ask_user"
    assert paused.provenance is None
    # Best-effort partials DO cross the pause — it is `provenance` alone that does not.
    assert paused.sql_executed == [_SQL]

    done = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="Sales"
    )
    assert done.status == "done"
    # The union the pause declined to surface, on the same turn, from the same trail.
    assert done.provenance is not None
    assert (_E, "EmployeeCode") in done.provenance


async def test_a_hard_ceiling_stop_carries_no_provenance() -> None:
    """`stopped_hard_ceiling` is TERMINAL but not `done`: it force-blocks pending
    intents and returns the best partial. `provenance` stays `None` — there is no
    assistant message it could be the tag of."""
    loop, store, _events = _build(
        [_query_turn("c1")], max_loop_iterations=1, max_budget_windows=1
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="go"
    )

    assert outcome.status == "stopped_hard_ceiling"
    assert outcome.provenance is None
    # The trail DID record a determined read, so `None` above is the rule, not luck.
    trail = await store.load_trail(SESSION_ID)
    assert [e.provenance for e in trail] == [frozenset({(_E, "EmployeeCode")})]


async def test_a_budget_cap_pause_carries_no_provenance() -> None:
    """Same rule at the fourth exit."""
    loop, store, _events = _build(
        [_query_turn("c1")], max_loop_iterations=1, max_budget_windows=3
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="go"
    )

    assert outcome.status == "paused_budget_cap"
    assert outcome.provenance is None
    trail = await store.load_trail(SESSION_ID)
    assert [e.provenance for e in trail] == [frozenset({(_E, "EmployeeCode")})]


# --- 3. an in-loop runtime-tool pause carries the envelope ------------------


class _PausingTool:
    """A runtime tool that does nothing but pause — the §2.5 seam in miniature.

    Registered under `resolveValues` (a real advertised runtime-tool name, so no
    fictional tool is introduced) purely because ANY wired handler may set
    `ToolResult.pause`; the loop's pause branch is not `runBlueprint`-specific and
    this test is about the branch, not about blueprints.
    """

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        return ToolResult(
            status="ok",
            tool_name="resolveValues",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=None,
            result_full=None,
            pause=ToolPause(
                reason="blueprint_slot",
                pending_question={"question": "Which region?", "options": None},
            ),
        )


async def test_an_in_loop_pause_carries_the_envelope_of_the_same_batch() -> None:
    """A designation folded EARLIER in the batch must survive a pause raised LATER
    in it.

    The envelope is read from the accumulators at the pause site, and the fold that
    fills them runs per tool call — so "the batch drained far enough to designate,
    then paused" is the only shape that can tell a live envelope from a stale one.
    The existing `tests/runtime/blueprint/test_loop_resume_dag.py::
    test_in_loop_pause_carries_partial_enrichment_from_prior_query` pins the
    `sql_executed` half of this with an envelope that is entirely `None`; this pins
    the envelope itself with non-`None` contents.
    """
    loop, _store, _events = _build(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name="answerWithTable",
                        arguments={"answer": "By department.", "tables": [{"sql": _SQL}]},
                    ),
                    ToolCallRequest(id="r1", name="resolveValues", arguments={}),
                ]
            ),
        ],
        runtime_tools={
            "answerWithTable": AnswerWithTableTool(),
            "resolveValues": _PausingTool(),
        },
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="go"
    )

    assert paused.status == "paused_ask_user"
    assert paused.pending_question == {"question": "Which region?", "options": None}
    # The envelope of the designation that landed one call earlier in the batch.
    assert paused.answer_sql == _SQL
    assert paused.answer_tables == [
        {"sql": _SQL, "caption": None, "blueprint_use": None, "verification": None}
    ]
    # Still a pause, so still no provenance (rule 2, at the fifth exit).
    assert paused.provenance is None


# --- 4. the exit's event fires AFTER every store write it belongs to --------


class _OrderRecordingStore(InMemorySessionStore):
    """The real in-memory store, logging the two writes an exit can make into the
    SAME list the observer writes to — which is the only way to compare their
    order, since the observer is synchronous and cannot read the store back."""

    def __init__(self, log: list[tuple[str, dict[str, Any]]]) -> None:
        super().__init__()
        self._log = log

    async def write_pause_checkpoint(self, session_id: str, checkpoint: Any) -> None:
        self._log.append(("store:write_pause_checkpoint", {}))
        await super().write_pause_checkpoint(session_id, checkpoint)

    async def append_message(self, session_id: str, message: Any) -> None:
        self._log.append(("store:append_message", {"role": message.role}))
        await super().append_message(session_id, message)


async def test_a_pause_exit_writes_its_checkpoint_before_it_announces_it() -> None:
    """The observer is the seam the SSE layer and the telemetry sink hang off, and
    both may read the session back on the event. Emitting `loop_paused_ask_user`
    before the checkpoint lands would hand them a session with no pending question
    — a race that reproduces only under a real store, i.e. never in this suite.

    So the order is asserted structurally instead: the event must be the LAST thing
    the exit does.
    """
    log: list[tuple[str, dict[str, Any]]] = []
    loop, _store, events = _build(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="a1", name="askUser", arguments={"question": "Which?"})
                ]
            )
        ],
        store=_OrderRecordingStore(log),
        events=log,
    )

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="go"
    )

    assert paused.status == "paused_ask_user"
    tail = [name for name, _p in events][-2:]
    assert tail == ["store:write_pause_checkpoint", "loop_paused_ask_user"]


async def test_a_done_exit_persists_its_answer_before_it_announces_it() -> None:
    """Same rule at the `done` exits, where the write is the assistant `TurnMessage`
    — the one a `loop_turn_done` consumer reading history back would come looking
    for."""
    log: list[tuple[str, dict[str, Any]]] = []
    loop, _store, events = _build(
        [ModelTurnResult(assistant_text="Here it is.")],
        store=_OrderRecordingStore(log),
        events=log,
    )

    done = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")

    assert done.status == "done"
    tail = [name for name, _p in events][-2:]
    assert tail == ["store:append_message", "loop_turn_done"]
