"""Unit tests for loop/agent_loop.py — the full turn state machine (Layer 1, all fakes).

Driven entirely by `ScriptedModelClient` + `FakeMCPClient` + `InMemorySessionStore`,
matching design §8's Layer-1 test matrix: normal termination, model-`askUser`
pause/resume, budget-cap pause with exactly-one-fresh-window-per-continue, the
hard outer ceiling, D5 injection integrity, and trail persistence.
"""

from __future__ import annotations

import json

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.store import AlreadyConsumedError, CASMismatchError

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

SECRET_JWT = "eyJhbGciOi.super-secret-jwt-body.sig"
SESSION_ID = "sess-loop-test"

TOOLS_SCHEMA = [
    {"type": "function", "name": "listDatabases", "description": "", "parameters": {}},
    {"type": "function", "name": "runQuery", "description": "", "parameters": {}},
    {
        "type": "function",
        "name": "askUser",
        "description": "",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
    },
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=scope)


def _build_loop(
    *,
    model_client: ScriptedModelClient,
    mcp_client: FakeMCPClient,
    store: InMemorySessionStore | None = None,
    max_loop_iterations: int = 15,
    max_wall_clock_seconds: float = 60,
    max_budget_windows: int = 3,
) -> tuple[AgentLoop, InMemorySessionStore]:
    store = store or InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    assembler = ContextAssembler(store, history_token_budget=100_000)
    loop = AgentLoop(
        model_client=model_client,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=max_loop_iterations,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_budget_windows=max_budget_windows,
    )
    return loop, store


# ---------------------------------------------------------------------------
# Termination: normal
# ---------------------------------------------------------------------------


async def test_normal_termination_tool_call_free_response() -> None:
    model = ScriptedModelClient([ModelTurnResult(assistant_text="Here is your answer.")])
    mcp = FakeMCPClient()
    loop, store = _build_loop(model_client=model, mcp_client=mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="How many employees?"
    )

    assert outcome.status == "done"
    assert outcome.assistant_text == "Here is your answer."
    assert outcome.tool_calls_made == 0

    doc = await store.get_or_create_session(SESSION_ID)
    roles = [m.role for m in doc.messages]
    assert roles == ["user", "assistant"]


# ---------------------------------------------------------------------------
# Termination: model-invoked askUser pause + resume
# ---------------------------------------------------------------------------


async def test_ask_user_pause_writes_checkpoint_and_never_reaches_dispatcher() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    ToolCallRequest(id="call_1", name="askUser", arguments={"question": "Which department?"})
                ],
            )
        ]
    )
    mcp = FakeMCPClient()  # no scripted responses — askUser must never reach it
    loop, store = _build_loop(model_client=model, mcp_client=mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Show me payroll."
    )

    assert outcome.status == "paused_ask_user"
    assert outcome.pending_question == {"question": "Which department?", "options": None}
    assert mcp.calls == []  # askUser never dispatched

    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.pause_checkpoint is not None
    assert doc.pause_checkpoint.reason == "askUser"
    assert doc.pause_checkpoint.consumed is False


async def test_ask_user_resume_round_trip_threads_answer_and_completes() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="call_1", name="askUser", arguments={"question": "Which dept?"})]
            ),
            ModelTurnResult(assistant_text="Using Sales, here is the answer."),
        ]
    )
    mcp = FakeMCPClient()
    loop, store = _build_loop(model_client=model, mcp_client=mcp)

    paused = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="Show payroll.")
    assert paused.status == "paused_ask_user"

    resumed = await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="Sales")
    assert resumed.status == "done"
    assert resumed.assistant_text == "Using Sales, here is the answer."

    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.pause_checkpoint.consumed is True
    contents = [m.content for m in doc.messages]
    assert "Sales" in contents


async def test_second_concurrent_resume_raises_already_consumed() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="call_1", name="askUser", arguments={"question": "Which dept?"})]
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient()
    loop, store = _build_loop(model_client=model, mcp_client=mcp)
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="Show payroll.")

    await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="Sales")
    try:
        await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="Sales again")
        raise AssertionError("expected AlreadyConsumedError")
    except AlreadyConsumedError:
        pass


async def test_resume_with_stale_cas_raises_cas_mismatch() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="call_1", name="askUser", arguments={"question": "Which dept?"})]
            ),
        ]
    )
    mcp = FakeMCPClient()
    loop, store = _build_loop(model_client=model, mcp_client=mcp)
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="Show payroll.")

    _, stale_cas = await store.get_session_with_cas(SESSION_ID)
    # A concurrent write bumps the version out from under us.
    await store.bump_last_activity(SESSION_ID)

    try:
        await store.resume_checkpoint(SESSION_ID, stale_cas, "Sales")
        raise AssertionError("expected CASMismatchError")
    except CASMismatchError:
        pass


# ---------------------------------------------------------------------------
# Termination: budget-cap pause + hard outer ceiling (D47/D55)
# ---------------------------------------------------------------------------


def _forever_tool_call_turn(call_id: str) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id=call_id, name="listDatabases", arguments={})],
        usage={"total_tokens": 10},
    )


async def test_budget_cap_pause_grants_exactly_one_fresh_window_then_hard_ceiling() -> None:
    # max_loop_iterations=1 -> every window exceeds after exactly one tool call.
    model = ScriptedModelClient(
        [
            _forever_tool_call_turn("c1"),
            _forever_tool_call_turn("c2"),
            _forever_tool_call_turn("c3"),
        ]
    )
    mcp = FakeMCPClient(scripted={"listDatabases": [[{"name": "dbpcm_warehouse"}] for _ in range(3)]})
    loop, store = _build_loop(
        model_client=model,
        mcp_client=mcp,
        max_loop_iterations=1,
        max_wall_clock_seconds=999,
        max_budget_windows=3,
    )

    window1 = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")
    assert window1.status == "paused_budget_cap"
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.pause_checkpoint.budget_window_count == 1

    window2 = await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="continue")
    assert window2.status == "paused_budget_cap"
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.pause_checkpoint.budget_window_count == 2  # exactly one fresh window granted

    window3 = await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="continue")
    assert window3.status == "stopped_hard_ceiling"  # hard ceiling (max_budget_windows=3)

    # Exactly 3 model round-trips total — never more than one send_turn per window.
    assert len(model.calls) == 3


async def test_budget_cap_stop_answer_ends_turn_with_best_partial() -> None:
    model = ScriptedModelClient([_forever_tool_call_turn("c1")])
    mcp = FakeMCPClient(scripted={"listDatabases": [[{"name": "dbpcm_warehouse"}]]})
    loop, store = _build_loop(
        model_client=model, mcp_client=mcp, max_loop_iterations=1, max_budget_windows=3
    )

    paused = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")
    assert paused.status == "paused_budget_cap"

    stopped = await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="stop")
    assert stopped.status == "done"
    assert len(model.calls) == 1  # no further model round-trip after "stop"


# ---------------------------------------------------------------------------
# D5 injection integrity — credentials never reach the model, but DO reach MCP
# ---------------------------------------------------------------------------


async def test_credentials_never_appear_in_any_model_payload_across_multi_tool_call_turn() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="call_1", name="listDatabases", arguments={}),
                    ToolCallRequest(id="call_2", name="listDatabases", arguments={}),
                ]
            ),
            ModelTurnResult(assistant_text="All done."),
        ]
    )
    mcp = FakeMCPClient(
        scripted={"listDatabases": [[{"name": "a"}], [{"name": "b"}]]}
    )
    loop, store = _build_loop(model_client=model, mcp_client=mcp)

    await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(),
        user_message=f"session {SESSION_ID} please",
    )

    # Transport boundary DID receive credentials.
    assert len(mcp.calls) == 2
    assert all(c.jwt == SECRET_JWT for c in mcp.calls)
    assert all(c.session_id == SESSION_ID for c in mcp.calls)

    # Every payload ever handed to the model must never carry them.
    for recorded_turn in model.calls:
        blob = json.dumps(recorded_turn.messages, default=str)
        assert SECRET_JWT not in blob
        tools_blob = json.dumps(recorded_turn.tools, default=str)
        assert SECRET_JWT not in tools_blob


# ---------------------------------------------------------------------------
# Trail persistence
# ---------------------------------------------------------------------------


async def test_dispatched_tool_call_persists_trail_entry_and_full_result() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="runQuery",
                        arguments={"sql": "SELECT EmployeeCode FROM employee"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Here are the employee codes."),
        ]
    )
    raw_result = {
        "columns": ["EmployeeCode"],
        "rows": [["E1"], ["E2"]],
        "row_count": 2,
        "truncated": False,
    }
    mcp = FakeMCPClient(scripted={"runQuery": [dict(raw_result)]})
    loop, store = _build_loop(model_client=model, mcp_client=mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="List employee codes."
    )
    assert outcome.status == "done"
    assert outcome.tool_calls_made == 1

    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 1
    entry = trail[0]
    assert entry.tool_call_id == "call_1"
    assert entry.tool_name == "runQuery"
    assert entry.status == "ok"
    assert entry.provenance == frozenset({(_E, "EmployeeCode")})
    assert entry.result_preview is not None
    assert entry.result_preview.row_count == 2
    assert entry.result_full_ref is not None
    assert entry.result_full_ref.startswith("result::")

    # The second model call must have seen a synthesized assistant+tool exchange
    # reflecting this trail entry (not the raw full result, only the preview).
    second_call_messages = model.calls[1].messages
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any(m["tool_call_id"] == "call_1" for m in tool_messages)


# ---------------------------------------------------------------------------
# S3 — tool-calls-per-model-response are capped, never unbounded
# ---------------------------------------------------------------------------


async def test_tool_calls_are_capped_per_iteration_never_unbounded() -> None:
    """One model response requesting far more tool calls than
    `max_tool_calls_per_iteration` must only have that many dispatched — the
    rest are simply not dispatched this round (best-partial), never an
    unbounded burst. Proven adversarially: `FakeMCPClient` is scripted with
    exactly `max_tool_calls_per_iteration` responses, so dispatching even ONE
    more would raise `AssertionError` from the fake itself."""
    many_calls = [
        ToolCallRequest(id=f"c{i}", name="listDatabases", arguments={}) for i in range(10)
    ]
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=many_calls),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"listDatabases": [[{"name": "db"}] for _ in range(3)]})
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp, CATALOG)
    assembler = ContextAssembler(store, history_token_budget=100_000)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        max_tool_calls_per_iteration=3,
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")

    assert outcome.status == "done"
    assert outcome.tool_calls_made == 3  # capped, not 10
    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 3
    assert len(mcp.calls) == 3


# ---------------------------------------------------------------------------
# S4 — the static denial user_message is included in the rendered tool
# content (unit-level: `_tool_trail_entry_to_canonical`'s output shape).
#
# UPDATE (2026-07-01, turn-scoped continuity fix): a denied/errored
# `TrailEntry` always has `provenance=None` (`dispatch/tool_dispatcher.py`),
# which the PRE-EXISTING, QA-locked D44 fail-closed filter
# (`context/scope_filter.py::is_entry_in_scope`, proven by
# `tests/runtime/provenance/test_fail_closed_replay_adversarial.py
# ::test_denied_tool_call_never_persists_fabricated_provenance`) still drops
# from every `ContextAssembler.assemble()` REPLAY of a PRIOR turn, under
# every scope including allow-all. However, `AgentLoop` now threads the
# in-progress turn's own `turn_index` into `ContextAssembler.assemble(...,
# current_turn_index=turn_index)`, which exempts THAT turn's own entries
# from the drop (`scope_filter.filter_trail`'s `current_turn_index` param) —
# a denial/error still carries no result rows, so nothing is leaked. This
# makes the rendering below reachable end-to-end WITHIN the same external
# turn (design §3.4 self-correction); see
# `test_current_turn_denial_is_visible_to_model_within_same_turn` and
# `test_prior_turn_denial_is_dropped_from_next_turns_context` below for the
# end-to-end proof, in both directions.
# ---------------------------------------------------------------------------


def test_tool_trail_entry_to_canonical_includes_static_denial_user_message() -> None:
    from data_agent.runtime.loop.agent_loop import _tool_trail_entry_to_canonical

    rendered_entry = {
        "tool_call_id": "call_1",
        "tool_name": "runQuery",
        "args": {"sql": "SELECT bad"},
        "status": "denied",
        "error_code": "CLICKHOUSE_QUERY_ERROR",
        "user_message": "That query didn't run correctly. Let me fix it and try again.",
        "result_preview": None,
    }

    _assistant_message, tool_message = _tool_trail_entry_to_canonical(rendered_entry)

    content = json.loads(tool_message["content"])
    assert content["error_code"] == "CLICKHOUSE_QUERY_ERROR"
    assert content["user_message"] == "That query didn't run correctly. Let me fix it and try again."


# ---------------------------------------------------------------------------
# Turn-scoped continuity (2026-07-01): current-turn denials/errors reach the
# model within the SAME turn; a PRIOR turn's denial/error stays dropped
# (cross-turn D44 unchanged).
# ---------------------------------------------------------------------------


async def test_current_turn_denial_is_visible_to_model_within_same_turn() -> None:
    """A `runQuery` the (fake) MCP denies as `TABLE_NOT_FOUND` in iteration 1
    of a turn must be visible — status/error_code/S4 user_message, no result
    rows — to the model's NEXT `send_turn` call in that SAME turn, so it can
    self-correct (design §3.4)."""
    from data_agent.runtime.mcp.client import MCPToolError

    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call_1", name="runQuery", arguments={"sql": "SELECT * FROM ghost"}
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Let me try a different table."),
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [MCPToolError("TABLE_NOT_FOUND", "no such table")]})
    loop, store = _build_loop(model_client=model, mcp_client=mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Query the ghost table."
    )
    assert outcome.status == "done"

    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 1
    assert trail[0].status == "denied"
    assert trail[0].provenance is None  # confirms the drop would otherwise fire

    # The SECOND send_turn call (same external turn, iteration 2) must have
    # seen this turn's denial.
    second_call_messages = model.calls[1].messages
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    content = json.loads(tool_messages[0]["content"])
    assert content["status"] == "denied"
    assert content["error_code"] == "TABLE_NOT_FOUND"
    assert content["user_message"] == "I couldn't find that table. Let me verify the table name."
    assert content["result_preview"] is None  # no rows ever leak from a denial


async def test_prior_turn_denial_is_dropped_from_next_turns_context() -> None:
    """Cross-turn D44 is unchanged by the exemption: once the turn a denial
    happened in has ENDED, a LATER external turn's context assembly must not
    resurrect it."""
    from data_agent.runtime.mcp.client import MCPToolError

    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call_1", name="runQuery", arguments={"sql": "SELECT * FROM ghost"}
                    )
                ]
            ),
            ModelTurnResult(assistant_text="I could not find that table."),
            ModelTurnResult(assistant_text="Here is something else."),
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [MCPToolError("TABLE_NOT_FOUND", "no such table")]})
    loop, store = _build_loop(model_client=model, mcp_client=mcp)

    first = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Query ghost."
    )
    assert first.status == "done"

    second = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Try something else."
    )
    assert second.status == "done"

    # model.calls[0]/[1] belong to the FIRST external turn; model.calls[2] is
    # the SECOND external turn's opening round-trip — it must not see turn 0's
    # denial at all.
    assert len(model.calls) == 3
    reopened_turn_messages = model.calls[2].messages
    blob = json.dumps(reopened_turn_messages, default=str)
    assert "call_1" not in blob
    assert "TABLE_NOT_FOUND" not in blob


# ---------------------------------------------------------------------------
# D77 — resolveValues is intercepted in the loop (never dispatched under its
# own name), returns an INLINE result, counts as exactly one tool call, and
# persists a TrailEntry carrying the inner runQuery's provenance.
# ---------------------------------------------------------------------------


def _resolve_loop(
    *, model_client: ScriptedModelClient, mcp_client: FakeMCPClient
) -> tuple[AgentLoop, InMemorySessionStore]:
    from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
    from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

    catalog = CatalogHandle(
        {_E: {"EarnCode": "Nullable(String)", "EarnDescription": "Nullable(String)"}}
    )
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp_client, catalog)
    assembler = ContextAssembler(store, history_token_budget=100_000)
    composite = ResolveValuesComposite(
        tool_dispatcher=dispatcher,
        catalog=catalog,
        embedding_client=FakeEmbeddingClient(dim=2),
    )
    loop = AgentLoop(
        model_client=model_client,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={"resolveValues": composite},
    )
    return loop, store


def _resolve_result() -> dict:
    return {
        "columns": ["EarnCode", "EarnDescription", "freq"],
        "rows": [["PTO", "paid time off", 10], ["OT", "overtime", 3]],
        "row_count": 2,
        "truncated": False,
    }


async def test_resolve_values_inline_result_continues_loop_no_pause() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rv_1",
                        name="resolveValues",
                        arguments={"table": _E, "column": "EarnCode", "concept": "leave"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="PTO is the paid-time-off code."),
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_resolve_result()]})
    loop, store = _resolve_loop(model_client=model, mcp_client=mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Which code is PTO?"
    )

    assert outcome.status == "done"  # inline result, never a pause
    # Exactly one tool call counted — the inner runQuery is not double-counted.
    assert outcome.tool_calls_made == 1
    # Exactly one MCP call fired: the inner runQuery (resolveValues never
    # reaches the MCP under its own name).
    assert [c.tool_name for c in mcp.calls] == ["runQuery"]


async def test_resolve_values_trail_entry_has_inner_provenance() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rv_1",
                        name="resolveValues",
                        arguments={"table": _E, "column": "EarnCode", "concept": "leave"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_resolve_result()]})
    loop, store = _resolve_loop(model_client=model, mcp_client=mcp)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 1
    entry = trail[0]
    assert entry.tool_name == "resolveValues"
    assert entry.status == "ok"
    # Provenance is the inner runQuery's — the columns the built SQL references.
    assert entry.provenance == frozenset(
        {(_E, "EarnCode"), (_E, "EarnDescription")}
    )
    assert entry.result_full_ref is not None


async def test_resolve_values_respects_per_iteration_cap() -> None:
    # Two resolveValues calls in one model response, cap=1 -> only one dispatched.
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rv_1",
                        name="resolveValues",
                        arguments={"table": _E, "column": "EarnCode", "concept": "leave"},
                    ),
                    ToolCallRequest(
                        id="rv_2",
                        name="resolveValues",
                        arguments={"table": _E, "column": "EarnCode", "concept": "overtime"},
                    ),
                ]
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_resolve_result()]})  # only ONE response
    from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
    from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

    catalog = CatalogHandle(
        {_E: {"EarnCode": "Nullable(String)", "EarnDescription": "Nullable(String)"}}
    )
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp, catalog)
    assembler = ContextAssembler(store, history_token_budget=100_000)
    composite = ResolveValuesComposite(
        tool_dispatcher=dispatcher, catalog=catalog, embedding_client=FakeEmbeddingClient(dim=2)
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        max_tool_calls_per_iteration=1,
        runtime_tools={"resolveValues": composite},
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")
    assert outcome.tool_calls_made == 1  # capped, not 2
    assert len(mcp.calls) == 1


async def test_resolve_values_credentials_never_in_model_payload() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rv_1",
                        name="resolveValues",
                        arguments={"table": _E, "column": "EarnCode", "concept": "leave"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_resolve_result()]})
    loop, store = _resolve_loop(model_client=model, mcp_client=mcp)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    # The inner runQuery DID receive credentials at the transport boundary.
    assert mcp.calls[0].jwt == SECRET_JWT
    assert mcp.calls[0].session_id == SESSION_ID
    # No model payload ever carries them.
    for recorded_turn in model.calls:
        blob = json.dumps(recorded_turn.messages, default=str)
        assert SECRET_JWT not in blob


async def test_resolve_values_unwired_returns_local_error_never_dispatched() -> None:
    # L2: when the composite is NOT wired, a resolveValues call must NOT be
    # dispatched to the MCP under its own name — it returns a clean local error.
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rv_1",
                        name="resolveValues",
                        arguments={"table": _E, "column": "EarnCode", "concept": "leave"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="ok"),
        ]
    )
    mcp = FakeMCPClient()  # no scripted responses — must never be called
    loop, store = _build_loop(model_client=model, mcp_client=mcp)  # resolve_values defaults None

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert outcome.status == "done"
    assert outcome.tool_calls_made == 1
    assert mcp.calls == []  # resolveValues never dispatched to the MCP

    trail = await store.load_trail(SESSION_ID)
    assert trail[0].tool_name == "resolveValues"
    assert trail[0].status == "error"
    assert trail[0].error_code == "RESOLVE_VALUES_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Runtime-tool registry (read-tools-design §2): a registered RuntimeTool is
# intercepted (inline ToolResult, one tool call, never dispatched to the MCP);
# an advertised-but-unwired one returns a clean local unavailable error; a
# genuinely unknown tool falls through to MCP dispatch UNCHANGED; askUser stays
# terminal even when other runtime tools are registered.
# ---------------------------------------------------------------------------


class _StubRuntimeTool:
    """A minimal RuntimeTool double — records its call and returns an inline
    `ToolResult` with the read-tools `provenance=frozenset()` contract."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict, RuntimeCredentials]] = []

    async def run(self, model_args: dict, credentials: RuntimeCredentials) -> ToolResult:
        self.calls.append((model_args, credentials))
        return ToolResult(
            status="ok",
            tool_name="searchBlueprints",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=None,
            result_full={"count": 0, "degraded": False, "blueprints": []},
        )


def _registry_loop(
    *,
    model_client: ScriptedModelClient,
    mcp_client: FakeMCPClient,
    runtime_tools: dict,
) -> tuple[AgentLoop, InMemorySessionStore]:
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    assembler = ContextAssembler(store, history_token_budget=100_000)
    loop = AgentLoop(
        model_client=model_client,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools=runtime_tools,
    )
    return loop, store


async def test_registered_runtime_tool_intercepted_one_call_never_dispatched() -> None:
    handler = _StubRuntimeTool()
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="sb_1", name="searchBlueprints", arguments={"query": "overtime"})
                ]
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient()  # must never be called for searchBlueprints
    loop, store = _registry_loop(
        model_client=model, mcp_client=mcp, runtime_tools={"searchBlueprints": handler}
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert outcome.status == "done"
    assert outcome.tool_calls_made == 1  # inline, exactly one
    assert len(handler.calls) == 1  # the registry routed to the handler
    assert mcp.calls == []  # never dispatched to the MCP under its own name

    trail = await store.load_trail(SESSION_ID)
    assert trail[0].tool_name == "searchBlueprints"
    assert trail[0].status == "ok"
    assert trail[0].provenance == frozenset()  # safe-empty, kept in D44 replay


async def test_unwired_retrieval_tool_returns_unavailable_never_dispatched() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="gb_1", name="getBlueprint", arguments={"id": "bp-x"})
                ]
            ),
            ModelTurnResult(assistant_text="ok"),
        ]
    )
    mcp = FakeMCPClient()
    loop, store = _registry_loop(model_client=model, mcp_client=mcp, runtime_tools={})

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert outcome.status == "done"
    assert outcome.tool_calls_made == 1
    assert mcp.calls == []  # a runtime tool is NEVER dispatched to the MCP

    trail = await store.load_trail(SESSION_ID)
    assert trail[0].tool_name == "getBlueprint"
    assert trail[0].status == "error"
    assert trail[0].error_code == "RETRIEVAL_TOOL_UNAVAILABLE"


async def test_unknown_tool_falls_through_to_mcp_dispatch_unchanged() -> None:
    # A tool that is neither a registered runtime tool NOR an advertised runtime
    # name still routes to the MCP dispatcher exactly as before the registry.
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="ld_1", name="listDatabases", arguments={})]
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"listDatabases": [["db1", "db2"]]})
    loop, _ = _registry_loop(
        model_client=model, mcp_client=mcp, runtime_tools={"searchBlueprints": _StubRuntimeTool()}
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert outcome.status == "done"
    assert [c.tool_name for c in mcp.calls] == ["listDatabases"]  # dispatched to MCP


async def test_ask_user_stays_terminal_even_with_runtime_tools_registered() -> None:
    # askUser is NOT a RuntimeTool — the registry must not swallow it; it still
    # pauses with a checkpoint.
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="au_1", name="askUser", arguments={"question": "Which dept?"})
                ]
            )
        ]
    )
    mcp = FakeMCPClient()
    loop, store = _registry_loop(
        model_client=model, mcp_client=mcp, runtime_tools={"searchBlueprints": _StubRuntimeTool()}
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert outcome.status == "paused_ask_user"
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.pause_checkpoint is not None
    assert doc.pause_checkpoint.reason == "askUser"
