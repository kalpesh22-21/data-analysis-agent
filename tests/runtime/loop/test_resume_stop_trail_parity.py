"""M2 — "stop" at the budget cap returns what "continue" would have rebuilt.

`resume()` has two branches under a `budget_cap` checkpoint, and until 2026-08-17
they disagreed about a fact NEITHER of them computes for itself. "continue" seeds
its fresh window from the trail (`_compute_turn_assumptions` /
`_compute_turn_answer_tables`), so a table designated and assumptions recorded
BEFORE the pause survive it. "stop" returned `assumptions=None` /
`answer_tables=None`, on the rationale that the in-loop accumulators died with the
prior window — true of the accumulators, but the trail outlives them and is the
fail-closed source of truth for exactly these two facts.

So the user who answered "stop" lost the grid and the assumptions that the user who
answered "continue" kept, from the same trail, on the same turn. That is a product
decision, taken 2026-08-17: the stop path now calls the SAME two producers.

WHAT IS PINNED HERE, and why each half needs pinning:

  1. A stop after a turn whose trail holds a designated `answerWithTable` returns
     those tables AND the assumptions — the regression this fixes, and the one that
     silently comes back if anyone "simplifies" the stop branch back to literals.
  2. The DERIVED envelope fields ride along, because they are projections of the
     designated tables (`answer_envelope`) and not independent accumulations. If
     `answer_tables` were populated and `answer_sql` were not, the UI would have a
     grid it could not page.
  3. An EMPTY trail still returns `None`/`None` — the `[] -> None` fork. Asserting
     only case 1 would pass just as well against a branch that returns `[]`, which
     the UI renders as an empty grid rather than as no grid at all.
  4. The two things that DID NOT change: the `USER_STOPPED` force-block, and the
     silence (`tests/runtime/loop/test_turn_exit_contract.py` owns the no-event
     half; this file re-asserts it locally so a change made HERE cannot be green
     here and red only there).
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.composite.record_assumptions import RecordAssumptionsTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolPause, ToolResult
from data_agent.runtime.loop.agent_loop import AgentLoop, TurnContext
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

SESSION_ID = "sess-resume-stop"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

HEADCOUNT_SQL = f"SELECT Department, count() FROM {_E} GROUP BY Department"
_PROBE_SQL = f"SELECT EmployeeCode FROM {_E}"
ASSUMPTION = "Active employees only."


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return []


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


class _PausingTool:
    """A runtime tool whose only behaviour is to pause — the §2.5 seam in miniature,
    borrowed verbatim in shape from `test_turn_exit_contract.py::_PausingTool`.

    IT IS THE ONLY WAY to get a designated answer table and a pause onto the SAME
    turn, which is what this file needs. `answerWithTable` is TERMINAL, so it cannot
    co-exist with a `done`-deferring exit of its own; and batching it with `askUser`
    dispatches neither (03 §E.1 sends only the STATE calls when a response also
    pauses). A runtime-tool `ToolPause` raised LATER in the same batch is the one
    shape that lets the designation land in the trail and the turn still stop short.
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


def _build(
    turns: list[ModelTurnResult],
    *,
    max_loop_iterations: int = 1,
    max_budget_windows: int = 3,
    runtime_tools: dict[str, Any] | None = None,
) -> tuple[AgentLoop, InMemorySessionStore, list[tuple[str, dict[str, Any]]]]:
    store = InMemorySessionStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    loop = AgentLoop(
        model_client=ScriptedModelClient(turns),
        tool_dispatcher=ToolDispatcher(_query_mcp(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=max_loop_iterations,
        max_wall_clock_seconds=60,
        max_budget_windows=max_budget_windows,
        observer=_observe,
        runtime_tools=runtime_tools
        or {
            "answerWithTable": AnswerWithTableTool(),
            # WIRED, not incidental: the assumptions assertions below are only
            # meaningful if a successful `recordAssumptions` lands an `ok` trail
            # entry for the rebuild to fold. Unregistered, the call would go to the
            # MCP and fail for a reason that has nothing to do with the stop path.
            "recordAssumptions": RecordAssumptionsTool(),
            "resolveValues": _PausingTool(),
        },
    )
    return loop, store, events


def _designating_round() -> ModelTurnResult:
    """Record an assumption, designate a table, then pause — in that order, in ONE
    batch, so both trail entries are written before the pause short-circuits."""
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="k1", name="recordAssumptions", arguments={"assumptions": [ASSUMPTION]}
            ),
            ToolCallRequest(
                id="a1",
                name="answerWithTable",
                arguments={
                    "answer": "By department.",
                    "tables": [{"sql": HEADCOUNT_SQL, "caption": "Headcount"}],
                },
            ),
            ToolCallRequest(id="p1", name="resolveValues", arguments={}),
        ]
    )


def _probe_round(call_id: str) -> ModelTurnResult:
    """A round that does one harmless read and nothing else — it exists only to burn
    the window's single iteration so the loop reaches the budget cap."""
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id=call_id, name="runQuery", arguments={"sql": _PROBE_SQL})],
        usage={"total_tokens": 10},
    )


async def _pause_at_the_budget_cap_with_a_designated_table(
    loop: AgentLoop,
) -> None:
    """Drive the turn to a `budget_cap` checkpoint with an `answerWithTable` and a
    `recordAssumptions` already `ok` in its trail.

    Two pauses, because one cannot do it: the FIRST (a runtime-tool `ToolPause`) is
    what lets the designation be written without the turn ending, and the SECOND is
    the budget cap the `"stop"` answer belongs to. Both are on the SAME turn_index,
    which is what makes the trail rebuild find them.
    """
    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount?"
    )
    assert paused.status == "paused_ask_user"
    # The in-window envelope, for contrast with the stop outcome below: the live
    # window HAD these, and the whole bug was that the next window's stop did not.
    assert paused.answer_sql == HEADCOUNT_SQL

    capped = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="EMEA"
    )
    assert capped.status == "paused_budget_cap"


async def test_a_stop_returns_the_answer_tables_and_assumptions_from_the_trail() -> None:
    """M2, the behaviour change: the stop path calls the same two trail producers
    the continue path does, so the user who stops keeps the grid."""
    loop, _store, _events = _build([_designating_round(), _probe_round("q1")])
    await _pause_at_the_budget_cap_with_a_designated_table(loop)

    stopped = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="stop"
    )

    assert stopped.status == "done"
    assert stopped.assumptions == [ASSUMPTION]
    assert stopped.answer_tables == [
        {
            "sql": HEADCOUNT_SQL,
            "caption": "Headcount",
            "blueprint_use": None,
            "verification": None,
        }
    ]


async def test_a_stops_derived_envelope_fields_follow_its_rebuilt_tables() -> None:
    """`answer_sql` is a PROJECTION of `answer_tables[0]` (`answer_envelope`), not an
    independent accumulation — so it rides along with the rebuild or the UI is handed
    a grid with nothing to page it by. `blueprint_use`/`verification` stay `None`
    here because the designated table is raw SQL and carries neither: the rebuild
    reports what the trail holds and invents no badge."""
    loop, _store, _events = _build([_designating_round(), _probe_round("q1")])
    await _pause_at_the_budget_cap_with_a_designated_table(loop)

    stopped = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="stop"
    )

    assert stopped.answer_sql == HEADCOUNT_SQL
    assert stopped.blueprint_use is None
    assert stopped.verification is None


async def test_a_stop_over_an_empty_trail_still_returns_none_not_empty() -> None:
    """The `[] -> None` fork, at the exit that just started reading a list.

    `_compute_turn_answer_tables` returns `[]` and `_compute_turn_assumptions`
    returns `[]` for a turn that designated and assumed nothing; both must reach the
    outcome as `None`, because §1 fork 1 has the UI treat "no table" and "empty
    table" identically and only `None` says the first. This is the assertion that
    fails if the branch is written as a straight pass-through of the producers.
    """
    loop, _store, _events = _build([_probe_round("q1")])

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount?"
    )
    assert paused.status == "paused_budget_cap"

    stopped = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="stop"
    )

    assert stopped.status == "done"
    assert stopped.assumptions is None
    assert stopped.answer_tables is None
    assert stopped.answer_sql is None


async def test_the_stop_exit_still_emits_nothing_and_makes_no_extra_round_trip() -> None:
    """The half of the exit contract this change must NOT move.

    `test_turn_exit_contract.py::test_the_resume_stop_exit_returns_done_and_emits_no
    _loop_turn_done` owns this rule; it is re-asserted here because the change adds
    two store reads to this branch, and "reads the trail" is one refactor away from
    "goes through `_finish`", which emits. `tool_calls_made == 0` is the same pin
    from the other side: the trail rebuild is a READ, and must never look like the
    turn did more work.
    """
    loop, _store, events = _build([_designating_round(), _probe_round("q1")])
    await _pause_at_the_budget_cap_with_a_designated_table(loop)

    before = len(events)
    stopped = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="stop"
    )

    assert stopped.tool_calls_made == 0
    assert stopped.assistant_text == (
        "Stopping here — here is what I found before the budget cap."
    )
    assert [name for name, _p in events[before:]] == []


async def test_the_rebuild_does_not_persist_a_second_assistant_message() -> None:
    """The stop exit does not route through `_finish` and must not start: it appends
    NO assistant `TurnMessage`, so the rebuilt table is a live-outcome enrichment
    only and history is left to reconstruct itself from the same trail.

    Pinned because the natural way to "make stop match continue" is to reach for the
    shared finisher, which persists — and a persisted canned "Stopping here" line
    would then replay into every later turn's model context.
    """
    loop, store, _events = _build([_designating_round(), _probe_round("q1")])
    await _pause_at_the_budget_cap_with_a_designated_table(loop)

    await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="stop")

    doc = await store.get_or_create_session(SESSION_ID)
    assert [m.role for m in doc.messages] == ["user", "user", "user"], (
        "the question and the two resume answers ('EMEA', 'stop') — the resume "
        "consumes a checkpoint by appending the answer as a user message — and NO "
        "assistant message"
    )
