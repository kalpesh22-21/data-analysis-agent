"""Adversarial B1/D44 coverage: the replayed-trail scope filter extends to
conversational ASSISTANT messages (2026-07-01 clarification), exercised
end-to-end through the real `AgentLoop` (not just `scope_filter.py`'s pure
functions — `tests/runtime/context/test_scope_filter.py` covers those).

Scenario: an assistant answer derived from a wide-scope turn's tool results
("Jane Doe's salary is $85,000...") must NEVER be replayed into the model's
context once the user's `column_scope` narrows past what that answer was
derived from — the exact leak surface D44's trail filter alone did not cover.
"""

from __future__ import annotations

import json

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_PAYROLL = "dbpcm_warehouse.payroll_fact"
_EMPLOYEE = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {
        _PAYROLL: {"GrossPay": "Decimal(18,2)"},
        _EMPLOYEE: {"Department": "Nullable(String)"},
        # NOTE: "dbpcm_warehouse.scratch_upload" deliberately NOT catalogued —
        # used to force an undetermined-provenance sampleRows below.
    }
)

SESSION_ID = "sess-msg-provenance-test"
JWT = "jwt-secret-should-never-leak"
PII_ANSWER = "Jane Doe's salary is $85,000."

TOOLS_SCHEMA = [
    {"type": "function", "name": "runQuery", "description": "", "parameters": {}},
    {"type": "function", "name": "sampleRows", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=scope)


def _build_loop(model: ScriptedModelClient, mcp: FakeMCPClient, store: InMemorySessionStore) -> AgentLoop:
    dispatcher = ToolDispatcher(mcp, CATALOG)
    assembler = ContextAssembler(store, history_token_budget=100_000)
    return AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
    )


def _messages_blob(recorded_turn) -> str:
    return json.dumps(recorded_turn.messages, default=str, ensure_ascii=False)


async def test_assistant_message_from_payroll_turn_is_dropped_once_payroll_scope_removed() -> None:
    store = InMemorySessionStore()
    model = ScriptedModelClient(
        [
            # Turn 0 (wide/allow-all scope): dispatch a payroll query, then
            # answer with PII derived from it.
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call_1", name="runQuery", arguments={"sql": "SELECT GrossPay FROM payroll_fact"}
                    )
                ]
            ),
            ModelTurnResult(assistant_text=PII_ANSWER),
            # Turn 1 (narrowed scope): the model is asked something else; we
            # only care about what canonical history IT was shown.
            ModelTurnResult(assistant_text="Here is the department breakdown."),
        ]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {
                    "columns": ["GrossPay"],
                    "rows": [[85000.0]],
                    "row_count": 1,
                    "truncated": False,
                }
            ]
        }
    )
    loop = _build_loop(model, mcp, store)

    turn0 = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(frozenset()), user_message="Show me payroll."
    )
    assert turn0.status == "done"
    assert turn0.assistant_text == PII_ANSWER

    # Narrowed scope: payroll.GrossPay is no longer granted, only employee.Department is.
    narrowed_scope = frozenset({f"{_EMPLOYEE}.Department"})
    turn1 = await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(narrowed_scope),
        user_message="What about departments?",
    )
    assert turn1.status == "done"

    # The model's THIRD send_turn call (turn 1's only round-trip) must NOT
    # have seen the prior PII-laden assistant answer anywhere in its history.
    last_call_blob = _messages_blob(model.calls[-1])
    assert PII_ANSWER not in last_call_blob
    assert "85000" not in last_call_blob


async def test_user_messages_always_survive_narrowing() -> None:
    store = InMemorySessionStore()
    model = ScriptedModelClient(
        [
            ModelTurnResult(assistant_text="Sure, checking now."),
            ModelTurnResult(assistant_text="Here you go."),
        ]
    )
    mcp = FakeMCPClient()
    loop = _build_loop(model, mcp, store)

    await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(frozenset()),
        user_message="Show me Jane Doe's payroll please.",
    )
    await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(frozenset({f"{_EMPLOYEE}.Department"})),
        user_message="Anything else?",
    )

    # The user's OWN prior input is never dropped by the replay filter, no
    # matter how much the scope narrows — only assistant/tool content is gated.
    last_call_blob = _messages_blob(model.calls[-1])
    assert "Show me Jane Doe's payroll please." in last_call_blob


async def test_clarification_only_assistant_turn_survives_any_narrowing() -> None:
    """A turn with NO tool calls at all (a pure chat/clarification response)
    is determined-empty provenance (`frozenset()`) — always kept, never
    treated as undetermined just because no tool ran."""
    store = InMemorySessionStore()
    model = ScriptedModelClient(
        [
            ModelTurnResult(assistant_text="Sure — what timeframe are you interested in?"),
            ModelTurnResult(assistant_text="Got it, one moment."),
        ]
    )
    mcp = FakeMCPClient()
    loop = _build_loop(model, mcp, store)

    await loop.run(
        session_id=SESSION_ID, credentials=_credentials(frozenset()), user_message="Show me payroll."
    )
    await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(frozenset({f"{_EMPLOYEE}.Department"})),
        user_message="Last quarter.",
    )

    last_call_blob = _messages_blob(model.calls[-1])
    assert "Sure — what timeframe are you interested in?" in last_call_blob


async def test_undetermined_provenance_turns_assistant_message_always_dropped() -> None:
    """A turn whose only tool call had undetermined provenance (here: an
    uncatalogued `sampleRows` table) makes that turn's assistant message
    undetermined too — dropped even under the widest possible (allow-all)
    re-assembly scope (D44 fail-closed, read literally)."""
    store = InMemorySessionStore()
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="sampleRows",
                        arguments={"database": "dbpcm_warehouse", "table": "scratch_upload"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="UNDETERMINED_PROVENANCE_ANSWER"),
            ModelTurnResult(assistant_text="Something else."),
        ]
    )
    mcp = FakeMCPClient(
        scripted={
            "sampleRows": [
                {"columns": ["x"], "rows": [["v1"]], "row_count": 1, "truncated": False}
            ]
        }
    )
    loop = _build_loop(model, mcp, store)

    turn0 = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(frozenset()), user_message="Sample the upload."
    )
    assert turn0.status == "done"
    assert turn0.assistant_text == "UNDETERMINED_PROVENANCE_ANSWER"

    # Re-assemble under the WIDEST possible scope (allow-all) — still dropped.
    turn1 = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(frozenset()), user_message="Anything else?"
    )
    assert turn1.status == "done"

    last_call_blob = _messages_blob(model.calls[-1])
    assert "UNDETERMINED_PROVENANCE_ANSWER" not in last_call_blob
