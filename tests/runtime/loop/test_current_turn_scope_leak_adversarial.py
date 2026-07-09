"""BLOCKER regression, end-to-end (2026-07-01 second pass): the turn-scoped
continuity exemption (`scope_filter.filter_trail`'s `current_turn_index`
param) must be STATUS-GATED — it may only exempt a denied/errored (no-row)
CURRENT-turn `TrailEntry` from the D44 replay drop, never a SUCCESSFUL one.

The adopted MCP does NOT column-scope `sampleRows` (its result is real cell
values from all columns; only `runQuery` — and `getTableSchema`'s metadata —
are column-scoped server-side, D80(b)); `provenance/capture.py` computes
`sampleRows` provenance declaratively as "all columns of the table", which is
frequently NOT a subset of a narrow `column_scope`. Before the fix, the
unconditional current-turn exemption let a successful, PII-bearing
`sampleRows` result reach the model within the SAME external turn it ran in
— exploitable end-to-end, not just in `context/scope_filter.py`'s pure-function
unit tests (`tests/runtime/context/test_scope_filter.py` covers those).

This file proves, driven through the real `AgentLoop` (mirroring
`tests/runtime/loop/test_agent_loop.py`'s continuity tests and
`tests/runtime/loop/test_message_provenance_adversarial.py`'s pattern):
    1. a successful, out-of-scope `sampleRows` result is NEVER surfaced to
       the model, even within the same turn it ran in (the leak this file
       closes);
    2. a denied/errored current-turn entry IS still surfaced within the same
       turn (confirming the self-correction continuity fix — design §3.4 —
       still works for its intended, no-row case).
"""

from __future__ import annotations

import json

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "Name": "String", "Salary": "Decimal(18,2)"}}
)

SESSION_ID = "sess-current-turn-leak-test"
JWT = "jwt-secret-should-never-leak"

# PII sentinels — must never appear in any model-facing blob under the
# narrow scope used below (only employee.EmployeeCode is granted).
_PII_NAME = "Jane Doe"
_PII_SALARY = "999999"

TOOLS_SCHEMA = [
    {"type": "function", "name": "sampleRows", "description": "", "parameters": {}},
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


def _credentials(scope: frozenset[str]) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=scope)


def _build_loop(model: ScriptedModelClient, mcp: FakeMCPClient) -> tuple[AgentLoop, InMemorySessionStore]:
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
    )
    return loop, store


def _messages_blob(recorded_turn) -> str:
    return json.dumps(recorded_turn.messages, default=str, ensure_ascii=False)


async def test_successful_out_of_scope_sample_rows_never_reaches_model_same_turn() -> None:
    """A `sampleRows(employee)` call succeeds (the MCP itself does not
    column-scope `sampleRows`) and returns PII rows, but the caller's
    `column_scope` only grants `employee.EmployeeCode`. The declarative
    all-columns provenance (`EmployeeCode`, `Name`, `Salary`) is therefore
    NOT a subset of scope — this entry must be dropped even though it
    belongs to the turn CURRENTLY in progress, because it carries real
    result rows."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="sampleRows",
                        arguments={"database": "dbpcm_warehouse", "table": "employee"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Here is what I found."),
        ]
    )
    mcp = FakeMCPClient(
        scripted={
            "sampleRows": [
                {
                    "columns": ["EmployeeCode", "Name", "Salary"],
                    "rows": [["E1", _PII_NAME, _PII_SALARY]],
                    "row_count": 1,
                    "truncated": False,
                }
            ]
        }
    )
    loop, store = _build_loop(model, mcp)

    narrow_scope = frozenset({f"{_E}.EmployeeCode"})
    outcome = await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(narrow_scope),
        user_message="Show me some employee rows.",
    )
    assert outcome.status == "done"

    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 1
    assert trail[0].status == "ok"
    assert trail[0].provenance == frozenset(
        {(_E, "EmployeeCode"), (_E, "Name"), (_E, "Salary")}
    )  # confirms the drop would otherwise NOT fire (declarative provenance exceeds scope)

    # The SECOND send_turn call (same external turn, iteration 2) must NOT
    # have seen this turn's own successful-but-out-of-scope tool result.
    assert len(model.calls) == 2
    second_call_blob = _messages_blob(model.calls[1])
    assert _PII_NAME not in second_call_blob
    assert _PII_SALARY not in second_call_blob
    assert "call_1" not in second_call_blob

    # And the final outcome/assistant text obviously carries no PII either.
    assert outcome.assistant_text is not None
    assert _PII_NAME not in outcome.assistant_text
    assert _PII_SALARY not in outcome.assistant_text


async def test_denied_current_turn_entry_is_still_surfaced_same_turn() -> None:
    """Control case (self-correction continuity, design §3.4, still intact):
    a DENIED current-turn `runQuery` call — which carries no result rows —
    IS surfaced to the model within the same turn, so the model can retry
    with a corrected query."""
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
    loop, store = _build_loop(model, mcp)

    narrow_scope = frozenset({f"{_E}.EmployeeCode"})
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(narrow_scope), user_message="Query the ghost table."
    )
    assert outcome.status == "done"

    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 1
    assert trail[0].status == "denied"
    assert trail[0].provenance is None

    second_call_blob = _messages_blob(model.calls[1])
    assert "TABLE_NOT_FOUND" in second_call_blob
    assert "call_1" in second_call_blob
