"""Every tool call has a time limit.

The dispatcher cancels each MCP call after its per-call limit — the loop's own calls,
blueprint nodes and probes alike — or sooner when the turn window ends first. A runtime
tool, which may make several MCP calls, is cancelled at the end of the turn window. A
cancelled call returns `TOOL_CALL_TIMEOUT`: its result is unknown, never empty.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.denial_mapping import classify_denial
from data_agent.runtime.dispatch.tool_dispatcher import (
    TOOL_CALL_TIMEOUT_CODE,
    ToolDispatcher,
    ToolResult,
)
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from tests.runtime.final_answer import final_answer, work_trail

CATALOG = CatalogHandle({"dbpcm_warehouse.employee": {"EmployeeCode": "String"}})
SESSION_ID = "sess-timeout"


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="SECRET-JWT", column_scope=frozenset())


class _HangingClient(FakeMCPClient):
    async def call_tool(self, tool_name, args, *, jwt, session_id):
        await asyncio.Event().wait()


class _SocketTimeoutClient(FakeMCPClient):
    async def call_tool(self, tool_name, args, *, jwt, session_id):
        raise TimeoutError("connect timed out")


async def _dispatch(dispatcher: ToolDispatcher, **kwargs: Any) -> tuple[ToolResult, float]:
    started = time.monotonic()
    result = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials(), **kwargs)
    return result, time.monotonic() - started


async def test_a_call_that_never_returns_is_cancelled_at_the_per_call_limit() -> None:
    events: list[tuple[str, dict]] = []
    dispatcher = ToolDispatcher(
        _HangingClient(), CATALOG, tool_call_timeout_seconds=0.05,
        observer=lambda event, payload: events.append((event, payload)),
    )
    result, elapsed = await _dispatch(dispatcher, tool_call_id="c1")
    assert elapsed < 2
    assert (result.status, result.error_code, result.retryable) == ("error", TOOL_CALL_TIMEOUT_CODE, True)
    assert result.result_full is None and result.provenance is None
    assert events[-1] == (
        "tool_dispatch_error",
        {"tool_name": "runQuery", "error_code": TOOL_CALL_TIMEOUT_CODE, "tool_call_id": "c1"},
    )
    # What the model reads on this and every later replay.
    assert classify_denial(result.error_code).user_message == result.user_message
    assert "unknown, not empty" in result.user_message


async def test_the_turn_window_shortens_the_limit_but_never_lengthens_it() -> None:
    dispatcher = ToolDispatcher(_HangingClient(), CATALOG, tool_call_timeout_seconds=60)
    result, elapsed = await _dispatch(dispatcher, timeout_seconds=0.05)
    assert result.error_code == TOOL_CALL_TIMEOUT_CODE and elapsed < 2

    dispatcher = ToolDispatcher(_HangingClient(), CATALOG, tool_call_timeout_seconds=0.05)
    result, elapsed = await _dispatch(dispatcher, timeout_seconds=60)
    assert result.error_code == TOOL_CALL_TIMEOUT_CODE and elapsed < 2


async def test_a_spent_window_still_cancels_rather_than_waiting() -> None:
    dispatcher = ToolDispatcher(_HangingClient(), CATALOG)
    result, elapsed = await _dispatch(dispatcher, timeout_seconds=-5)
    assert result.error_code == TOOL_CALL_TIMEOUT_CODE and elapsed < 2


async def test_a_transport_timeout_is_not_reported_as_the_time_limit() -> None:
    dispatcher = ToolDispatcher(_SocketTimeoutClient(), CATALOG, tool_call_timeout_seconds=60)
    result, _ = await _dispatch(dispatcher)
    assert result.error_code == "INTERNAL_TRANSPORT_ERROR"


def test_the_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ToolDispatcher(FakeMCPClient(), CATALOG, tool_call_timeout_seconds=0)


async def test_the_real_client_is_cancelled_when_the_server_never_answers() -> None:
    """Cancellation has to get through the MCP SDK's task groups and httpx, whose own
    read timeout (300s) would otherwise hold the call."""
    connected = asyncio.Event()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connected.set()
        try:
            await reader.read()  # read the request, never answer it
        finally:
            writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        dispatcher = ToolDispatcher(
            RealMCPClient(f"http://127.0.0.1:{port}/mcp"), CATALOG, tool_call_timeout_seconds=0.5
        )
        result, elapsed = await _dispatch(dispatcher)
    finally:
        server.close()
        await server.wait_closed()
    assert connected.is_set()
    assert result.error_code == TOOL_CALL_TIMEOUT_CODE
    assert elapsed < 5


# --- the loop: a runtime tool is bounded by the turn window -----------------


class _HangingRuntimeTool:
    async def run(self, model_args, credentials, turn=None, tool_call_id=None) -> ToolResult:
        await asyncio.Event().wait()


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "listDatabases", "description": "", "parameters": {}}]


@pytest.mark.usefixtures("answer_tools", "blueprint_consulted")
async def test_a_runtime_tool_is_cancelled_at_the_end_of_the_turn_window() -> None:
    store = InMemorySessionStore()
    loop = AgentLoop(
        model_client=ScriptedModelClient(
            [
                ModelTurnResult(
                    tool_calls=[ToolCallRequest(id="c1", name="searchBlueprints", arguments={"intent": "x"})]
                ),
                final_answer(assistant_text="I could not finish that search."),
            ]
        ),
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=1,
        max_budget_windows=1,
        runtime_tools={"searchBlueprints": _HangingRuntimeTool()},
    )
    started = time.monotonic()
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="hi")
    assert time.monotonic() - started < 5
    trail = await work_trail(store, SESSION_ID)
    assert [(e.tool_name, e.status, e.error_code) for e in trail][:1] == [
        ("searchBlueprints", "error", TOOL_CALL_TIMEOUT_CODE)
    ]


# --- the ledger: a timeout lets the model close the intent honestly ---------


@pytest.mark.parametrize("error_code", [TOOL_CALL_TIMEOUT_CODE, "INTERNAL_TRANSPORT_ERROR"])
@pytest.mark.parametrize("tool_name", ["runQuery", "runBlueprint"])
def test_a_timed_out_data_call_supports_an_execution_failed_block(tool_name, error_code) -> None:
    """The timeout message offers "deliver verified results with this limitation", which
    needs the ledger to accept the timeout as the reason the intent is blocked."""
    from data_agent.runtime.composite.analysis_state import classify_block_evidence
    from data_agent.runtime.session.models import TrailEntry

    entry = TrailEntry(
        turn_index=0, tool_call_id="timed-out", tool_name=tool_name, args={},
        status="error", error_code=error_code, provenance=None,
        result_preview=None, result_full_ref=None, ts="now",
    )
    assert classify_block_evidence("timed-out", [entry], 0) == "EXECUTION_FAILED"



@pytest.mark.parametrize("error_code", ["HELP_CENTER_UNAVAILABLE", "HELP_CENTER_CIRCUIT_OPEN"])
@pytest.mark.parametrize("tool_name", ["searchHelpCenter", "getHelpCenterDocument"])
def test_a_help_center_outage_supports_an_execution_failed_block(tool_name, error_code) -> None:
    """A mixed question's how-to part could not be closed while Help Center was down: the
    model retried the ledger five times and the turn fell to salvage. After repeated
    outages the runtime's breaker refuses the call itself, which is the same outage."""
    from data_agent.runtime.composite.analysis_state import classify_block_evidence
    from data_agent.runtime.session.models import TrailEntry

    entry = TrailEntry(
        turn_index=0, tool_call_id="help-down", tool_name=tool_name, args={},
        status="error", error_code=error_code, provenance=None,
        result_preview=None, result_full_ref=None, ts="now",
    )
    assert classify_block_evidence("help-down", [entry], 0) == "EXECUTION_FAILED"
