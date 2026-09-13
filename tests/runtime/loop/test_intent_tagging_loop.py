"""Call-time intent tagging — the LOOP half (Release 1, 03/04 as amended).

The tool half (resolution, 04's conditions, the reuse escape hatch) lives in
`tests/runtime/composite/test_intent_tagging.py`. This file covers what only the
loop can be wrong about:

  - ⚠ THE STRIP. `serves_intent` is a runtime concept. `runQuery` and
    `getTableSchema` are dispatched to the live MCP server, which rejects an
    argument its own schema does not declare, and `runBlueprint`'s executor
    validates its arguments too. If the loop forgets to remove it, the feature
    breaks every substantive call against the real server while every unit test
    stays green.
  - The tag is validated against the LIVE state and persisted on the entry.
  - An unknown tag drops the TAG, never the WORK.
  - (The pause/resume carry — where no trail entry exists at pause time — is in
    `test_release1_seams_qa.py`, beside the other seam tests that drive the REAL
    `BlueprintExecutor`.)
  - THE LIVE FAILURE, REPLAYED: three intents, three tagged blueprint runs in
    round 1, ONE `updateAnalysisState` closing all three in round 2 with no
    evidence fields. That exact shape produced 9 rejections and 0 completions on
    the citation path.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.loop.agent_loop import (
    _INTENT_TAG_DROPPED_NOTE as INTENT_TAG_DROPPED_NOTE,
)
from data_agent.runtime.loop.agent_loop import AgentLoop, TurnContext
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, live_analysis_state
from tests._blueprint_gate import expand_blueprint

SESSION_ID = "sess-tagging-loop"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return []


class _RecordingBlueprintTool:
    """A stand-in for `RunBlueprintTool` that RECORDS the arguments it is handed.

    The real executor is exercised in `test_release1_seams_qa.py`; what is under
    test here is the loop's dispatch boundary, and the only way to assert "the
    executor never sees the tag" is to look at what the runtime tool was called
    with. Returns an AUTHORITATIVE result, so 04's condition 4 is satisfied and a
    tagged blueprint really can close an intent.
    """

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
        tool_call_id=None,
    ) -> ToolResult:
        self.seen.append(dict(model_args))
        return ToolResult(
            status="ok",
            tool_name="runBlueprint",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=ResultPreview(
                columns=["department", "n"],
                row_count=2,
                truncated=False,
                preview_rows=[["Sales", 3], ["Eng", 5]],
            ),
            result_full={"blueprint_id": model_args.get("id"), "rows": []},
            authoritative=True,
        )


def _build(
    turns: list[ModelTurnResult],
    *,
    mcp: FakeMCPClient | None = None,
    blueprint_tool: _RecordingBlueprintTool | None = None,
    store: InMemorySessionStore | None = None,
    model: ScriptedModelClient | None = None,
) -> tuple[AgentLoop, InMemorySessionStore, list[tuple[str, dict[str, Any]]], FakeMCPClient]:
    store = store if store is not None else InMemorySessionStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, dict(payload)))

    mcp = mcp or FakeMCPClient()
    runtime_tools: dict[str, Any] = {
        STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe),
        ANSWER: AnswerWithTableTool(),
    }
    if blueprint_tool is not None:
        runtime_tools["runBlueprint"] = blueprint_tool
    loop = AgentLoop(
        model_client=model if model is not None else ScriptedModelClient(turns),
        tool_dispatcher=ToolDispatcher(mcp, CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=_observe,
        runtime_tools=runtime_tools,
    )
    return loop, store, events, mcp


def _init_call(call_id: str, *descriptions: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name=STATE,
        arguments={"intents": [{"description": d} for d in descriptions]},
    )


def _update_call(call_id: str, *updates: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=STATE, arguments={"intents": list(updates)})


def _tagged_query(call_id: str, intent_id: str | None, sql: str = "SELECT 1") -> ToolCallRequest:
    args: dict[str, Any] = {"sql": sql}
    if intent_id is not None:
        args["serves_intent"] = intent_id
    return ToolCallRequest(id=call_id, name="runQuery", arguments=args)


def _tagged_schema(call_id: str, intent_id: str, table: str = "employee") -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="getTableSchema",
        arguments={
            "database": "dbpcm_warehouse",
            "table": table,
            "serves_intent": intent_id,
        },
    )


def _tagged_blueprint(call_id: str, blueprint_id: str, intent_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="runBlueprint",
        arguments={
            "id": blueprint_id,
            "slot_bindings": {},
            "serves_intent": intent_id,
        },
    )


def _rows() -> dict[str, Any]:
    return {"columns": ["x"], "rows": [[1]], "row_count": 1, "truncated": False}


def _schema() -> dict[str, Any]:
    return {"database": "dbpcm_warehouse", "table": "employee", "columns": ["EmployeeCode"]}


def _events(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


async def _trail(store: InMemorySessionStore) -> dict[str, Any]:
    return {entry.tool_call_id: entry for entry in await store.load_trail(SESSION_ID)}


# ---------------------------------------------------------------------------
# ⚠ The strip
# ---------------------------------------------------------------------------


async def test_the_tag_never_reaches_the_mcp_for_run_query_or_get_table_schema() -> None:
    """THE ONE THAT BREAKS THE LIVE SERVER IF MISSED. Both tools are dispatched to
    the MCP, which rejects an unknown argument — so a tag left on the arguments
    turns every tagged call into a denial, on the release's primary route, while
    every store-level test still passes."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()], "getTableSchema": [_schema()]})
    loop, store, _events_, mcp = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "the employee schema"),
                    _tagged_query("q1", "i1"),
                    _tagged_schema("m1", "i2"),
                ],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _update_call(
                        "s2",
                        {"intent_id": "i1", "status": "completed"},
                        {"intent_id": "i2", "status": "completed"},
                    )
                ],
            ),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=mcp,
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    dispatched = {call.tool_name: call.args for call in mcp.calls}
    assert dispatched["runQuery"] == {"sql": "SELECT 1"}
    assert dispatched["getTableSchema"] == {"database": "dbpcm_warehouse", "table": "employee"}
    assert all("serves_intent" not in call.args for call in mcp.calls)

    # ...and the tag is on the ENTRY instead, where the state tool reads it.
    trail = await _trail(store)
    assert trail["q1"].serves_intent == "i1"
    assert trail["m1"].serves_intent == "i2"
    assert "serves_intent" not in trail["q1"].args
    assert "serves_intent" not in trail["m1"].args

    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.status, i.evidence_tool_call_id) for i in state.intents] == [
        ("completed", "q1"),
        ("completed", "m1"),
    ]


async def test_the_tag_never_reaches_the_blueprint_executor() -> None:
    """`runBlueprint` is a RUNTIME tool, so it is intercepted before the dispatcher
    — a strip that only covered the MCP path would leave the tag on its arguments."""
    blueprint = _RecordingBlueprintTool()
    loop, store, _e, _m = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "salary"),
                    _tagged_blueprint("b1", "bp-headcount", "i1"),
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ],
        blueprint_tool=blueprint,
    )
    # The getBlueprint-before-runBlueprint gate (tests/_blueprint_gate.py).
    await expand_blueprint(store, SESSION_ID, "bp-headcount")

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    assert blueprint.seen == [{"id": "bp-headcount", "slot_bindings": {}}]
    assert (await _trail(store))["b1"].serves_intent == "i1"


async def test_two_identically_tagged_reads_still_dedup_to_one_dispatch() -> None:
    """The read-guard signature is computed over the CLEANED arguments. Were it
    computed before the strip, two identical schema fetches tagged for different
    intents would look like different reads and the guard would stop deduping."""
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema()]})
    loop, store, events, mcp = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "the schema", "the same schema"),
                    _tagged_schema("m1", "i1"),
                    _tagged_schema("m2", "i2"),
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=mcp,
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    assert len([c for c in mcp.calls if c.tool_name == "getTableSchema"]) == 1
    assert _events(events, "loop_repeated_idempotent_read_guarded")
    trail = await _trail(store)
    # The guard entry keeps its tag, so the refusal for i2 can name the original
    # rather than claiming nothing was tagged (04 condition 5).
    assert trail["m2"].serves_intent == "i2"


# ---------------------------------------------------------------------------
# Lenient validation — drop the tag, never the work
# ---------------------------------------------------------------------------


async def test_an_unknown_tag_drops_the_tag_and_still_runs_the_query() -> None:
    """Degrade-not-fail, never silently. Refusing a real query over a bookkeeping
    typo would be strictly worse than an untagged entry — it is exactly the failure
    mode this whole change exists to remove."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()]})
    loop, store, events, mcp = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "salary"),
                    _tagged_query("q1", "i7"),  # no such intent
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=mcp,
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    assert [c.args for c in mcp.calls] == [{"sql": "SELECT 1"}]
    trail = await _trail(store)
    assert trail["q1"].status == "ok"
    assert trail["q1"].serves_intent is None
    # Never silently: the drop is reported, by RULE NAME. The offending value is
    # NOT emitted — an invalid tag is arbitrary model text (D25).
    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "runQuery", "reason": "unknown_intent_id"}
    ]


async def test_a_hostile_tag_value_never_reaches_telemetry() -> None:
    """D25. A VALID tag is a runtime-assigned `intent_id` and is safe to emit (it
    already rides `loop_intent_completed.intent_id`). A DROPPED one is whatever the
    model typed — here the user's own question — so the drop event reports the RULE
    NAME and the tool, and nothing else."""
    hostile = "the average salary of every employee in Sales"
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()]})
    loop, _s, events, mcp = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "salary"),
                    _tagged_query("q1", hostile),
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=mcp,
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    dropped = _events(events, "loop_intent_tag_dropped")
    assert dropped == [{"tool_name": "runQuery", "reason": "unknown_intent_id"}]
    assert hostile not in json.dumps(events, default=str)


async def test_a_tag_on_a_single_intent_turn_is_dropped_not_fatal() -> None:
    """No `updateAnalysisState` at all — the single-deliverable shape the prompt
    tells the model NOT to track. A tag there means nothing and must cost nothing."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()]})
    loop, store, events, mcp = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_query("q1", "i1")]),
            ModelTurnResult(assistant_text="42"),
        ],
        mcp=mcp,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "done"
    assert [c.args for c in mcp.calls] == [{"sql": "SELECT 1"}]
    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "runQuery", "reason": "no_live_state"}
    ]


async def test_declaring_and_tagging_in_the_same_message_works() -> None:
    """03 §E.2 dispatches every state call FIRST, so the ids exist by the time the
    tagged calls in the same batch are reached. If the tag were validated against a
    snapshot taken before the batch, the natural round-1 shape (declare + do the
    work) would silently lose every tag."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows(), _rows()]})
    loop, store, _e, mcp = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "salary"),
                    _tagged_query("q1", "i1"),
                    _tagged_query("q2", "i2", sql="SELECT 2"),
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=mcp,
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    trail = await _trail(store)
    assert (trail["q1"].serves_intent, trail["q2"].serves_intent) == ("i1", "i2")


# ---------------------------------------------------------------------------
# K2/G1 — the drop the model could not see (live-eval L5)
#
# `no_live_state` is the ONE drop reason that is not a typo: the model's tag was
# well-formed and its intents were real, it just had not declared them yet. The
# observer event recorded it; nothing told the MODEL. Measured consequence: it
# believed its work was tracked, never called `updateAnalysisState`, and the turn
# finished untracked. The note below is that missing feedback.
# ---------------------------------------------------------------------------


def _runtime_notes(model: ScriptedModelClient, request_index: int) -> list[tuple[str, str]]:
    """`(tool_call_id, runtime_note)` for every tool result carrying the K2 note in
    the Nth request the model was handed — i.e. what the model can actually READ,
    after assembly and budget trimming, not what the loop intended to send."""
    found: list[tuple[str, str]] = []
    for message in model.calls[request_index].messages:
        if message.get("role") != "tool":
            continue
        try:
            content = json.loads(message["content"])
        except (TypeError, ValueError):
            continue  # a withheld/verbatim sentinel, never JSON
        if isinstance(content, dict) and "runtime_note" in content:
            found.append((message["tool_call_id"], content["runtime_note"]))
    return found


async def test_a_tag_dropped_for_no_live_state_tells_the_model_on_the_next_round() -> None:
    """THE FIX. The tag rides a `getTableSchema` — a NON-substantive call, which is
    the whole point: the late-init door is still open there, so the correction is
    still actionable. The note must reach the model as part of the tool result for
    the call it is about, on the very next round-trip, and must then GO AWAY: it is
    ephemeral, and a note re-attached on every rebuild would still be nagging after
    the model had complied."""
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema()], "runQuery": [_rows()]})
    model = ScriptedModelClient(
        [
            # Round 1: tag it, with no analysisState anywhere in sight.
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_schema("m1", "i1")]),
            # Round 2 sees the note; do some untagged work so a round 3 happens.
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_query("q1", None)]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, _store, events, mcp = _build([], mcp=mcp, model=model)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "done"
    # The observer event is UNCHANGED — the note is additive, not a replacement.
    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "getTableSchema", "reason": "no_live_state"}
    ]
    # Request 1 could not carry it: the call it is about had not been made yet.
    assert _runtime_notes(model, 0) == []
    # Request 2 — on the result of `m1`, the call that was tagged.
    assert _runtime_notes(model, 1) == [("m1", INTENT_TAG_DROPPED_NOTE)]
    # ...and it names the repair AND the deadline, which is what makes it
    # actionable rather than a scolding.
    assert "updateAnalysisState" in INTENT_TAG_DROPPED_NOTE
    assert "before your next substantive call" in INTENT_TAG_DROPPED_NOTE
    # Request 3 — gone. EXACTLY ONE ROUND-TRIP.
    assert _runtime_notes(model, 2) == []


async def test_the_note_rides_its_own_key_and_never_the_authoritative_one() -> None:
    """`note` (do not re-derive this blueprint result) and `window_note` (what period
    these rows cover) are independent of a dropped tag and of each other, so the
    corrective note gets its OWN key. Appending it to `note` would trade a
    do-not-re-derive instruction for a bookkeeping one on the exact result that most
    needs the first."""
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema()]})
    model = ScriptedModelClient(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_schema("m1", "i1")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, _store, _e, mcp = _build([], mcp=mcp, model=model)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="one thing")

    tagged = [
        json.loads(m["content"])
        for m in model.calls[1].messages
        if m.get("role") == "tool" and m.get("tool_call_id") == "m1"
    ]
    assert len(tagged) == 1
    assert tagged[0]["runtime_note"] == INTENT_TAG_DROPPED_NOTE
    assert "note" not in tagged[0]
    assert "window_note" not in tagged[0]


async def test_a_tag_dropped_against_a_live_state_gets_no_note() -> None:
    """The note says "no intents are declared yet". When a state DOES exist that is
    simply false — the tag was a typo (`unknown_intent_id`) or not a string, and
    telling the model to declare intents it already declared would send it to
    re-initialize, which the runtime then REFUSES as a duplicate init. So the note
    is bound to `no_live_state` alone, not to "a tag was dropped"."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()]})
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call("s1", "headcount", "salary"),
                    _tagged_query("q1", "i7"),  # a live state exists; the id is a typo
                ],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, _store, events, mcp = _build([], mcp=mcp, model=model)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "runQuery", "reason": "unknown_intent_id"}
    ]
    assert all(_runtime_notes(model, index) == [] for index in range(len(model.calls)))


async def test_no_drop_at_all_leaves_every_tool_result_untouched() -> None:
    """The negative case that makes the three above mean something: an ordinary
    tagged turn must render byte-identically to before this slice existed."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()]})
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", "headcount", "salary"), _tagged_query("q1", "i1")],
            ),
            ModelTurnResult(assistant_text="done"),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, _store, events, mcp = _build([], mcp=mcp, model=model)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    assert _events(events, "loop_intent_tag_dropped") == []
    assert all(_runtime_notes(model, index) == [] for index in range(len(model.calls)))


async def test_two_tags_dropped_in_one_round_produce_exactly_one_note() -> None:
    """The note is advice about the TURN, not about a call. Attaching it per dropped
    call would put the identical paragraph on every tool result in a batch — the
    model tags all its work at once, so the natural shape is several — turning one
    correction into a wall of repetition it is likelier to skim past.

    BOTH calls are `getTableSchema`: the batch has to stay clear of
    `SUBSTANTIVE_TOOLS` or the suppression gate below would empty the set and this
    test would pass for the wrong reason."""
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema(), _schema()]})
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _tagged_schema("m1", "i1"),
                    _tagged_schema("m2", "i2", table="department"),
                ],
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, _store, events, mcp = _build([], mcp=mcp, model=model)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    # BOTH drops are still reported to the observer — the once-per-round rule is
    # about what the MODEL is shown, and must not cost telemetry fidelity.
    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "getTableSchema", "reason": "no_live_state"},
        {"tool_name": "getTableSchema", "reason": "no_live_state"},
    ]
    assert _runtime_notes(model, 1) == [("m1", INTENT_TAG_DROPPED_NOTE)]


async def test_a_tagged_substantive_call_gets_no_note_because_it_shuts_the_door_itself() -> None:
    """THE BLOCKER THE REVIEW CAUGHT. `runQuery(serves_intent=…)` with no state drops
    the tag for `no_live_state` — but the very same call is SUBSTANTIVE, so by the
    time the model could read a note, `find_locking_tool` reports the turn locked and
    a first `updateAnalysisState` is refused NON-RETRYABLY. Sending the note here
    would upgrade a silent drop into an instruction to earn a refusal. No true advice
    exists in this state, so the correct output is the pre-slice one: drop silently,
    report to telemetry, say nothing to the model."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()]})
    model = ScriptedModelClient(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_query("q1", "i1")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, _store, events, mcp = _build([], mcp=mcp, model=model)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )

    assert outcome.status == "done"
    # Telemetry is UNCHANGED by the suppression — the drop is still reported.
    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "runQuery", "reason": "no_live_state"}
    ]
    assert all(_runtime_notes(model, index) == [] for index in range(len(model.calls)))


async def test_a_mixed_batch_suppresses_the_note_even_though_the_tagged_call_was_metadata() -> None:
    """The ordering case, and why the suppression is folded at the END of the batch
    rather than at the drop site. The tagged `getTableSchema` is selected for the note
    BEFORE the untagged `runQuery` beside it has been seen — yet that runQuery shuts
    the same door, so by the next round the advice is false. Draining the batch first
    makes the two orderings agree."""
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema()], "runQuery": [_rows()]})
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_tagged_schema("m1", "i1"), _tagged_query("q1", None)],
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, _store, events, mcp = _build([], mcp=mcp, model=model)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="two things")

    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "getTableSchema", "reason": "no_live_state"}
    ]
    assert all(_runtime_notes(model, index) == [] for index in range(len(model.calls)))


async def test_the_gate_is_turn_scoped_so_an_earlier_rounds_query_still_suppresses() -> None:
    """The door is shut by the TURN's trail, not by this round's batch. A `runQuery`
    in round 1 locks late init for good, so a tag dropped in round 2 — on a metadata
    call, with the batch itself perfectly innocent — still has no true advice to
    offer. A per-round flag would re-open the note here and send the model at a
    refusal two rounds after the call that caused it."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()], "getTableSchema": [_schema()]})
    model = ScriptedModelClient(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_query("q1", None)]),
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_schema("m1", "i1")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, _store, events, mcp = _build([], mcp=mcp, model=model)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="one thing")

    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "getTableSchema", "reason": "no_live_state"}
    ]
    assert all(_runtime_notes(model, index) == [] for index in range(len(model.calls)))


async def test_the_gate_survives_a_pause_because_it_is_seeded_from_the_trail() -> None:
    """THE WINDOW BOUNDARY, which is where a plain window-local would silently
    re-open the hole. A resume enters a FRESH `_run_loop_body`, so an unseeded flag
    reads False again — while the `runQuery` from before the pause is still in the
    turn's trail and still has late init locked. The gate is therefore seeded with
    `find_locking_tool`, the runtime's own predicate, over the session doc the window
    already loads. Delete that seed and this is the test that goes red."""
    mcp = FakeMCPClient(scripted={"runQuery": [_rows()], "getTableSchema": [_schema()]})
    model = ScriptedModelClient(
        [
            # Window 1: an untagged query shuts the door for the whole turn...
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_query("q1", None)]),
            # ...then the model pauses, ending this window.
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    ToolCallRequest(id="a1", name="askUser", arguments={"question": "which?"})
                ],
            ),
            # Window 2, a fresh loop body: a tag with still no state anywhere.
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_schema("m1", "i1")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, _store, events, mcp = _build([], mcp=mcp, model=model)

    paused = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="one thing"
    )
    assert paused.status == "paused_ask_user"
    resumed = await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="Sales")

    assert resumed.status == "done"
    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "getTableSchema", "reason": "no_live_state"}
    ]
    assert all(_runtime_notes(model, index) == [] for index in range(len(model.calls)))


async def test_the_note_leads_to_an_accepted_late_declaration() -> None:
    """THE NOTE'S ADVICE MUST BE TRUE. It tells the model to declare its intents
    now, before its next substantive call — which is a lie if the `getTableSchema`
    that triggered the note has itself already locked initialization. It has not:
    `SUBSTANTIVE_TOOLS` is the four-tool locking set and `getTableSchema` is
    deliberately outside it. This test is the guard on that coupling — move
    `getTableSchema` into the locking set and the runtime starts refusing exactly
    the call the note asks for."""
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema(), _schema()]})
    model = ScriptedModelClient(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_schema("m1", "i1")]),
            # The model complies: it declares, having run only a metadata read.
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", "the department schema")],
            ),
            # ...and now the tag is honoured.
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_tagged_schema("m2", "i1", table="department")],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_update_call("s2", {"intent_id": "i1", "status": "completed"})],
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, store, events, mcp = _build([], mcp=mcp, model=model)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="two things"
    )

    assert outcome.status == "done"
    assert _runtime_notes(model, 1) == [("m1", INTENT_TAG_DROPPED_NOTE)]
    # The declaration the note asked for was ACCEPTED — no late-init trip.
    assert not _events(events, "loop_analysis_state_late_init_rejected")
    assert not _events(events, "loop_analysis_state_rejected")
    # Only the first, pre-declaration tag was ever dropped.
    assert _events(events, "loop_intent_tag_dropped") == [
        {"tool_name": "getTableSchema", "reason": "no_live_state"}
    ]
    # The re-tagged call is now real evidence, so the intent closes on its own
    # merits — the note's advice paid off end-to-end.
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.intent_id, i.status, i.evidence_tool_call_id) for i in state.intents] == [
        ("i1", "completed", "m2")
    ]


async def test_a_late_declaration_still_finalizes_through_the_pending_intents_gate() -> None:
    """THE INTERACTION THAT COULD HAVE HARD-LOCKED THE TURN. The note pushes the
    model to declare intents LATE — so a turn that would have ended untracked and
    unimpeded now ends with pending intents, which is precisely what the
    finalization gate refuses. That must remain a ONE-TIME refusal: the enforcement
    allowance is spent, the second attempt is granted, and the turn terminates with
    the model's answer. If the note could turn a finishing turn into a
    non-terminating one, it would be a worse bug than the one it fixes.
    """
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema()]})
    model = ScriptedModelClient(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_tagged_schema("m1", "i1")]),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", "headcount", "salary")],
            ),
            # Answers with both intents still pending -> refused ONCE.
            ModelTurnResult(assistant_text="a premature answer"),
            # Re-sent -> the allowance grants it and the turn ends.
            ModelTurnResult(assistant_text="the final answer"),
        ]
    )
    loop, store, events, mcp = _build([], mcp=mcp, model=model)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="two things"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "the final answer"
    assert _runtime_notes(model, 1) == [("m1", INTENT_TAG_DROPPED_NOTE)]
    # Refused exactly once, then force-blocked and released — never a loop.
    assert _events(events, "loop_finalization_refused") == [
        {"exit": "no_tool_calls", "pending_count": 2}
    ]
    assert len(_events(events, "loop_finalization_block_spent")) == 1
    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [i.status for i in state.intents] == ["blocked", "blocked"]


# ---------------------------------------------------------------------------
# THE LIVE FAILURE, REPLAYED
# ---------------------------------------------------------------------------


async def test_the_live_three_intent_failure_now_completes_with_zero_rejections() -> None:
    """The measured shape, verbatim: three deliverables declared, three blueprints
    run and tagged in round 1, ONE `updateAnalysisState` closing all three in round
    2 with NO evidence field on any of them.

    On the citation path this turn produced rejection after rejection — the model
    cited the blueprint id, then the tool name, then a hallucinated call id, then
    `""` — and the intents stayed `pending` until enforcement force-blocked them.
    Here: three completions, zero rejections, and each intent bound to the
    blueprint run that actually served it.
    """
    blueprint = _RecordingBlueprintTool()
    loop, store, events, _m = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _init_call(
                        "s1",
                        "Active headcount by department.",
                        "Average salary by department.",
                        "Projected hires over the next 6 months.",
                    ),
                    _tagged_blueprint("b1", "bp-active-headcount-by-department", "i1"),
                    _tagged_blueprint("b2", "bp-average-salary-by-department", "i2"),
                    _tagged_blueprint("b3", "bp-hires-projection", "i3"),
                ],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _update_call(
                        "s2",
                        {"intent_id": "i1", "status": "completed"},
                        {"intent_id": "i2", "status": "completed"},
                        {"intent_id": "i3", "status": "completed"},
                    ),
                    ToolCallRequest(
                        id="a1",
                        name=ANSWER,
                        arguments={
                            "answer": "Headcount and average salary by department, "
                            "plus the six-month hiring projection.",
                            "tables": [{"sql": "SELECT 1"}],
                        },
                    ),
                ],
            ),
        ],
        blueprint_tool=blueprint,
    )
    # The getBlueprint-before-runBlueprint gate: the live three-intent shape now
    # expands all three blueprints before running them (tests/_blueprint_gate.py).
    for blueprint_id in (
        "bp-active-headcount-by-department",
        "bp-average-salary-by-department",
        "bp-hires-projection",
    ):
        await expand_blueprint(store, SESSION_ID, blueprint_id)

    outcome = await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(),
        user_message=(
            "Give me the active headcount by department, the average salary by "
            "department, and the projected hires over the next 6 months."
        ),
    )

    assert outcome.status == "done"
    assert not _events(events, "loop_analysis_state_rejected"), "a completion was refused"
    assert not _events(events, "loop_finalization_refused")
    assert not _events(events, "loop_enforcement_exhausted")

    state = live_analysis_state(await store.get_or_create_session(SESSION_ID), 0)
    assert [(i.intent_id, i.status, i.evidence_tool_call_id) for i in state.intents] == [
        ("i1", "completed", "b1"),
        ("i2", "completed", "b2"),
        ("i3", "completed", "b3"),
    ]
    assert [
        (p["intent_id"], p["evidence_tool_name"], p["evidence_binding"])
        for p in _events(events, "loop_intent_completed")
    ] == [
        ("i1", "runBlueprint", "tagged"),
        ("i2", "runBlueprint", "tagged"),
        ("i3", "runBlueprint", "tagged"),
    ]
