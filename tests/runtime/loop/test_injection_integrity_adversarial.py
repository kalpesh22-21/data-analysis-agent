"""Adversarial D5 injection-integrity coverage at the full `AgentLoop` level (QA hardening pass).

Extends `tests/runtime/loop/test_agent_loop.py`'s
`test_credentials_never_appear_in_any_model_payload_across_multi_tool_call_turn`
with a harsher, multi-window, multi-turn scenario per the QA brief:

    (a) the JWT, raw `column_scope` members, and `session_id` NEVER appear in
        ANY `messages`/`tools` payload passed to `ScriptedModelClient.send_turn`
        across every iteration of every window of every turn — including a
        *second*, later external turn that replays the first turn's trail
        through `ContextAssembler`.
    (b) they DID reach `FakeMCPClient` at the transport boundary.
    (c) they don't appear in any persisted `TrailEntry`/`SessionDoc` that
        later gets replayed.

Distinctive sentinel values are used throughout (per the brief) so a plain
substring scan is unambiguous — no legitimate tool argument or SQL literal in
this test happens to collide with any sentinel.
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

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

# Distinctive, collision-proof sentinels (brief's explicit instruction).
JWT_SENTINEL = "SENTINEL-JWT-9f3ac2e1-do-not-leak.header.sig"
SESSION_SENTINEL = "SENTINEL-SESSION-7b21ffd0-do-not-leak"
SCOPE_COLUMN_SENTINEL = "SentinelScopeColumnZZZ"
SCOPE = frozenset({f"{_E}.EmployeeCode", f"{_E}.{SCOPE_COLUMN_SENTINEL}"})

TOOLS_SCHEMA = [
    {"type": "function", "name": "listDatabases", "description": "", "parameters": {}},
    {"type": "function", "name": "runQuery", "description": "", "parameters": {}},
    {"type": "function", "name": "sampleRows", "description": "", "parameters": {}},
    {
        "type": "function",
        "name": "askUser",
        "description": "",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
    },
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_SENTINEL, jwt=JWT_SENTINEL, column_scope=SCOPE)


def _build_loop(
    *, model_client: ScriptedModelClient, mcp_client: FakeMCPClient, store: InMemorySessionStore
) -> AgentLoop:
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    assembler = ContextAssembler(store)
    return AgentLoop(
        model_client=model_client,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=10,
        max_wall_clock_seconds=999,
        max_budget_windows=3,
    )


def _assert_no_sentinels(blob: str, *, context: str) -> None:
    assert JWT_SENTINEL not in blob, f"JWT sentinel leaked into {context}"
    assert SESSION_SENTINEL not in blob, f"session_id sentinel leaked into {context}"
    assert SCOPE_COLUMN_SENTINEL not in blob, f"raw column_scope sentinel leaked into {context}"


async def test_credentials_never_leak_across_multi_window_multi_turn_replay() -> None:
    session_id = "sess-injection-adversarial"
    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {
                    "columns": ["EmployeeCode"],
                    "rows": [["E1"]],
                    "row_count": 1,
                    "truncated": False,
                },
                {
                    "columns": ["EmployeeCode"],
                    "rows": [["E2"]],
                    "row_count": 1,
                    "truncated": False,
                },
            ],
            "listDatabases": [[{"name": "dbpcm_warehouse"}]],
            "sampleRows": [
                {"columns": ["EmployeeCode"], "rows": [["E1"]], "row_count": 1, "truncated": False}
            ],
        }
    )
    model = ScriptedModelClient(
        [
            # --- Turn 1 / window 1: 3 model round-trips, ending in askUser pause.
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="runQuery",
                        arguments={"sql": "SELECT EmployeeCode FROM employee"},
                    )
                ]
            ),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="call_2", name="listDatabases", arguments={}),
                    ToolCallRequest(
                        id="call_3",
                        name="sampleRows",
                        arguments={"database": "dbpcm_warehouse", "table": "employee"},
                    ),
                ]
            ),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="call_4", name="askUser", arguments={"question": "Which dept?"})
                ]
            ),
            # --- Turn 1 resume (same window, askUser is not a budget grant).
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call_5",
                        name="runQuery",
                        arguments={"sql": "SELECT EmployeeCode FROM employee WHERE 1=1"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Turn 1 complete."),
            # --- Turn 2: a brand-new external turn on the SAME session — its
            # first model call replays turn 1's trail through ContextAssembler.
            ModelTurnResult(assistant_text="Turn 2 complete, using replayed history."),
        ]
    )
    loop = _build_loop(model_client=model, mcp_client=mcp, store=store)

    paused = await loop.run(session_id=session_id, credentials=_credentials(), user_message="Show payroll.")
    assert paused.status == "paused_ask_user"

    resumed = await loop.resume(session_id=session_id, credentials=_credentials(), answer="Sales")
    assert resumed.status == "done"

    turn2 = await loop.run(
        session_id=session_id, credentials=_credentials(), user_message="Now show me more."
    )
    assert turn2.status == "done"

    # (a) Every payload ever handed to the model — across every iteration of
    # every window of BOTH turns, including the replay of turn 1 into turn 2 —
    # must never carry any sentinel.
    assert len(model.calls) == 6
    for i, recorded_turn in enumerate(model.calls):
        messages_blob = json.dumps(recorded_turn.messages, default=str)
        tools_blob = json.dumps(recorded_turn.tools, default=str)
        _assert_no_sentinels(messages_blob, context=f"model.calls[{i}].messages")
        _assert_no_sentinels(tools_blob, context=f"model.calls[{i}].tools")

    # Sanity: the replayed trail actually reached the model in turn 2 (proves
    # the assertion above is meaningful, not vacuous — turn 2's first call
    # must contain the turn-1 tool-call ids in its rendered history).
    turn2_first_call_blob = json.dumps(model.calls[5].messages, default=str)
    assert "call_1" in turn2_first_call_blob
    assert "call_5" in turn2_first_call_blob

    # (b) The transport boundary DID receive the real credentials on every
    # dispatched call (askUser is intercepted and never reaches the MCP).
    assert len(mcp.calls) == 4  # call_1, call_2, call_3, call_5
    assert all(c.jwt == JWT_SENTINEL for c in mcp.calls)
    assert all(c.session_id == SESSION_SENTINEL for c in mcp.calls)

    # (c) Nothing persisted to the SessionDoc/TrailEntry (which IS later
    # replayed into turn 2's messages, proven above) carries the JWT or the
    # raw column_scope. `session_id` legitimately appears once as the
    # SessionDoc's own identifier field (its primary key) — that is expected
    # and is NOT a credential leak — so it is scanned only within the
    # `tool_trail` sub-structure, never the whole document.
    doc = await store.get_or_create_session(session_id)
    full_doc_blob = json.dumps(doc.to_doc(), default=str)
    assert JWT_SENTINEL not in full_doc_blob
    assert SCOPE_COLUMN_SENTINEL not in full_doc_blob

    trail_blob = json.dumps([e.to_doc() for e in doc.tool_trail], default=str)
    assert SESSION_SENTINEL not in trail_blob
    assert JWT_SENTINEL not in trail_blob
    assert SCOPE_COLUMN_SENTINEL not in trail_blob
