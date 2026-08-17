"""Regression (2026-07-09): under a RESTRICTED `column_scope`, a successfully
fetched `getTableSchema` result must STAY in the model-facing context across a
round-trip.

The bug: `provenance/capture.py` recorded `getTableSchema` with declarative
all-columns (`SELECT *`) provenance — identical to `sampleRows`. Under a scope
that granted only a SUBSET of the table's columns, that all-columns provenance
was not a subset of the scope, so `context/scope_filter.filter_trail` DROPPED
the (successful, current-turn) getTableSchema entry from replay. The model's
context then held the getTableSchema call followed by the repeated-read guard's
"you already have this" nudge — with the actual schema NOWHERE in context.

The fix: `getTableSchema` returns MCP-scope-filtered column METADATA (no cell
values), so it is a `_NO_PROVENANCE_TOOLS` member — `frozenset()` provenance,
trivially a subset of ANY scope, always replayable. `sampleRows` (real cell
values from all columns) stays all-columns and is still correctly dropped.

This drives the REAL `AgentLoop`/`ContextAssembler`/`ToolDispatcher`/
`filter_trail` stack (only the model and MCP are doubles) and asserts the
getTableSchema tool result is present in the context the model sees on the
round-trip AFTER the fetch, under a scope that grants only ONE of the table's
two columns. On the pre-fix code the result is absent and this fails.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_E = "dbpcm_warehouse.employee"

# The table has TWO columns; the scope below grants only ONE of them, so the
# pre-fix all-columns provenance for getTableSchema ({EmployeeCode, AnnualSalary})
# is NOT a subset of the scope and gets dropped.
CATALOG = CatalogHandle(
    {"dbpcm_warehouse.employee": {"EmployeeCode": "String", "AnnualSalary": "Float64"}}
)

SESSION_ID = "sess-schema-restricted-scope"
JWT = "jwt-not-under-test"

# Restricted scope: only AnnualSalary is granted (EmployeeCode is NOT), so a
# declarative all-columns provenance would fail the D44 subset check.
RESTRICTED_SCOPE = frozenset({f"{_E}.AnnualSalary"})

TOOLS_SCHEMA = [
    {"type": "function", "name": "getTableSchema", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=RESTRICTED_SCOPE)


def _schema_response() -> dict[str, Any]:
    # Shape mirrors the MCP's scope-filtered getTableSchema result: only the
    # in-scope column's metadata (no cell values).
    return {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "catalogued": True,
        "columns": [{"name": "AnnualSalary", "type": "Float64", "comment": ""}],
    }


def _get_schema_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the `ok` getTableSchema tool-result message from a canonical
    (OpenAI-shape) message list, or None if it is absent.

    A getTableSchema call is a synthetic assistant `tool_calls` entry (function
    name = "getTableSchema") followed by a `tool` message keyed on the same
    `tool_call_id`; that tool message's JSON content carries `status`.
    """
    schema_call_ids = {
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
        if tc.get("function", {}).get("name") == "getTableSchema"
    }
    for m in messages:
        if m.get("role") != "tool" or m.get("tool_call_id") not in schema_call_ids:
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("status") == "ok":
            return m
    return None


class _FetchThenAnswerModel:
    """Round 1: fetch `getTableSchema(employee)`. Round 2 onward: answer once —
    no repeat is issued, so the repeated-read guard is never involved; this
    isolates the pure filter-survival behavior."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fetched = False

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        if self._fetched:
            return ModelTurnResult(
                assistant_text="Here is the employee schema.", usage={"total_tokens": 1}
            )
        self._fetched = True
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id="gts_1",
                    name="getTableSchema",
                    arguments={"database": "dbpcm_warehouse", "table": "employee"},
                )
            ],
            usage={"total_tokens": 1},
        )

    def begin_turn(self) -> _FetchThenAnswerModel:
        return self


def _build_loop(model: Any, mcp: FakeMCPClient) -> tuple[AgentLoop, InMemorySessionStore]:
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp, CATALOG)
    assembler = ContextAssembler(store)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=6,
        max_wall_clock_seconds=60,
        max_budget_windows=2,
    )
    return loop, store


async def test_get_table_schema_result_survives_restricted_scope_round_trip() -> None:
    model = _FetchThenAnswerModel()
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response()]})
    loop, store = _build_loop(model, mcp)

    outcome = await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(),
        user_message="Describe the employee table.",
    )

    assert outcome.status == "done"

    # The model made two round-trips: fetch, then answer. The SECOND round-trip's
    # context is what must still carry the fetched schema under the narrow scope.
    assert len(model.calls) == 2
    second_ctx = model.calls[1]["messages"]

    schema_result = _get_schema_tool_result(second_ctx)
    assert schema_result is not None, (
        "getTableSchema result was dropped from the replayed context under a "
        "restricted column_scope — the fetched schema must survive (safe-empty "
        "provenance), this is the bug's regression guard."
    )
    # And the surviving result carries the in-scope column metadata (no cell data).
    assert "AnnualSalary" in schema_result["content"]

    # The persisted entry itself was recorded with safe-empty provenance (the
    # capture-side half of the fix), which is why filter_trail kept it.
    trail = await store.load_trail(SESSION_ID)
    schema_entries = [e for e in trail if e.tool_name == "getTableSchema"]
    assert len(schema_entries) == 1
    assert schema_entries[0].status == "ok"
    assert schema_entries[0].provenance == frozenset()
