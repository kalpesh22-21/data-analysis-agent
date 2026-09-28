"""API validation errors reach the model immediately and break blind retry loops."""

from __future__ import annotations

import json
from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from tests.runtime.final_answer import final_answer

pytestmark = pytest.mark.usefixtures("answer_tools")

# The catalog knows `employee` but NOT `ghost_table` — a sampleRows against the
# latter is rejected by the API before any rows can reach the agent.
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String"}})

SESSION_ID = "sess-d94-loop-break"
JWT = "jwt-not-under-test"

_ERROR_FRAGMENT = "PARSE_FAILED_CLOSED"
_PII = "PII_ROW_VALUE_do_not_surface"

TOOLS_SCHEMA = [
    {"type": "function", "name": "sampleRows", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset())


class _RetryUntilErrorModel:
    """Retry until the API validation error arrives, then stop."""

    _CALL_ID = "call_ghost"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        # Did the model receive the API error for its call?
        saw_error = any(
            m.get("role") == "tool"
            and m.get("tool_call_id") == self._CALL_ID
            and isinstance(m.get("content"), str)
            and _ERROR_FRAGMENT in m["content"]
            for m in messages
        )
        if saw_error:
            return final_answer(
                assistant_text="I don't have any information to answer your question.",
                usage={"total_tokens": 1},
            )
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id=self._CALL_ID,
                    name="sampleRows",
                    arguments={"database": "dbpcm_warehouse", "table": "ghost_table"},
                )
            ],
            usage={"total_tokens": 1},
        )

    def begin_turn(self) -> _RetryUntilErrorModel:
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
        max_loop_iterations=10,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
    )
    return loop, store


def _ghost_mcp() -> FakeMCPClient:
    # Enough scripted responses that, absent the fix, the loop could spin for a
    # long time; the sentinel must stop it well before these run out.
    return FakeMCPClient(
        scripted={
            "sampleRows": [
                {
                    "columns": ["EmployeeCode", "Name"],
                    "rows": [["E1", _PII]],
                    "row_count": 1,
                    "truncated": False,
                }
                for _ in range(20)
            ]
        }
    )


async def test_api_error_breaks_retry_loop_and_terminates_normally() -> None:
    model = _RetryUntilErrorModel()
    loop, store = _build_loop(model, _ghost_mcp())

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Sample the ghost table."
    )

    # Terminated via the NORMAL done path — NOT the budget cap / hard ceiling.
    assert outcome.status == "done"
    assert outcome.status not in {"paused_budget_cap", "stopped_hard_ceiling"}

    # The failed read is recorded as a denial, with no data provenance.
    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 2
    assert trail[0].status == "denied"
    assert trail[0].provenance is None

    # It broke fast: exactly two model round-trips (emit call, then see error
    # and stop) — nowhere near max_budget_windows * max_loop_iterations.
    assert len(model.calls) == 2


async def test_second_round_trip_contains_original_api_error() -> None:
    """A failed call has a paired result carrying its actual API error."""
    model = _RetryUntilErrorModel()
    loop, _ = _build_loop(model, _ghost_mcp())
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")

    assert len(model.calls) >= 2
    second_ctx = model.calls[1]["messages"]
    sentinel_tool_msgs = [
        m
        for m in second_ctx
        if m.get("role") == "tool"
        and m.get("tool_call_id") == "call_ghost"
        and isinstance(m.get("content"), str)
        and _ERROR_FRAGMENT in m["content"]
    ]
    assert len(sentinel_tool_msgs) == 1

    # No rows from the invalid response leaked.
    blob = json.dumps(second_ctx, default=str, ensure_ascii=False)
    assert _PII not in blob


async def test_every_assistant_tool_call_has_a_matching_tool_result() -> None:
    """The OpenAI message-pairing invariant the sentinel exists to preserve:
    across every model round-trip, each assistant `tool_call` id has exactly one
    matching `tool` result and there are no orphan tool results."""
    model = _RetryUntilErrorModel()
    loop, _ = _build_loop(model, _ghost_mcp())
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")

    for recorded in model.calls:
        messages = recorded["messages"]
        assistant_ids: list[str] = []
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                assistant_ids.extend(tc["id"] for tc in m["tool_calls"])
        tool_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
        assert sorted(assistant_ids) == sorted(tool_ids), messages
        assert len(tool_ids) == len(set(tool_ids))
