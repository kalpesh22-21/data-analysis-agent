"""Wire lifecycle closes dispatch rows independently of free-form progress copy."""

import json
from types import SimpleNamespace

import pytest

from data_agent.runtime.app import _stream_turn
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.loop.agent_loop import AgentLoop, TurnContext, TurnOutcome
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.observability.progress import ProgressEmitter, to_progress_event
from tests.runtime.dispatch.test_tool_dispatcher import CATALOG, _credentials


@pytest.mark.parametrize("path", ["mcp", "runtime"])
@pytest.mark.parametrize("status", ["ok", "denied", "error"])
async def test_dispatch_lifecycle_survives_sse_for_remote_and_runtime_tools(path, status):
    emitter = ProgressEmitter()
    name = "runQuery" if path == "mcp" else "updateAnalysisState"
    payload = {"tool_name": name, "tool_call_id": "step-1"}

    async def execute():
        emitter.observe(
            "tool_progress_summary", {**payload, "summary": "Checking the requested information"}
        )
        if path == "mcp":
            response = {
                "ok": {"columns": ["EmployeeCode"], "rows": [["E1"]], "row_count": 1},
                "denied": MCPToolError("COLUMN_SCOPE_VIOLATION", "private denial detail"),
                "error": RuntimeError("private transport detail"),
            }[status]
            dispatcher = ToolDispatcher(
                FakeMCPClient({name: [response]}), CATALOG, observer=emitter.observe
            )
            result = await dispatcher.dispatch(
                name,
                {"sql": "SELECT EmployeeCode FROM employee"},
                _credentials(),
                tool_call_id="step-1",
            )
        else:

            class Handler:
                async def run(self, *args, **kwargs):
                    if status == "error":
                        raise RuntimeError("private runtime failure")
                    return ToolResult(
                        status,
                        name,
                        "SCOPE_DENIED" if status == "denied" else None,
                        False,
                        None,
                        frozenset(),
                        None,
                        None,
                    )

            result = await AgentLoop._run_runtime_tool(
                SimpleNamespace(_observer=emitter.observe),
                Handler(),
                name,
                {},
                _credentials(),
                TurnContext(turn_index=0, question="Check the information"),
                "step-1",
            )
        assert result.status == status
        # A late text update must not reopen or mark a denied/error row successful.
        emitter.observe(
            "tool_progress_summary",
            {**payload, "summary": "Continuing with the answer", "lifecycle": "ok"},
        )
        return TurnOutcome("done", "Finished.", None, 1)

    frames = [frame async for frame in _stream_turn(execute, emitter)]
    progress = [
        json.loads(frame.split("data: ", 1)[1]) for frame in frames if "event: progress" in frame
    ]
    assert [p["shape"].get("lifecycle") for p in progress] == [None, "start", status]
    assert all(p["step"] == "Checking the requested information" for p in progress)
    assert all(p["shape"]["tool_call_id"] == "step-1" for p in progress)
    assert all(p["shape"]["tool_name"] == name for p in progress)
    assert "event: result" in frames[-1]
    assert "private" not in "".join(frames)
    assert "secret-jwt" not in "".join(frames)
    assert "SELECT EmployeeCode" not in "".join(frames)


@pytest.mark.parametrize(
    "event", ["tool_progress_summary", "loop_model_call_start", "loop_turn_done"]
)
def test_non_dispatch_events_cannot_inject_lifecycle(event):
    progress = to_progress_event(event, {"summary": "Checking progress", "lifecycle": "denied"})
    assert progress is not None
    assert "lifecycle" not in progress.shape


@pytest.mark.parametrize("status", ["start", "ok", "denied", "error"])
def test_dispatch_event_is_authoritative_over_payload_lifecycle(status):
    progress = to_progress_event(
        "tool_dispatch_" + status, {"tool_name": "runQuery", "lifecycle": "spoofed"}
    )
    assert progress.shape["lifecycle"] == status


@pytest.mark.usefixtures("answer_tools", "blueprint_consulted")
async def test_blueprint_resume_preserves_denied_result_lifecycle(monkeypatch):
    from data_agent.runtime.model.scripted_client import ScriptedModelClient
    from data_agent.runtime.session.memory_store import InMemorySessionStore
    from tests.runtime.observability.test_resume_tool_span import (
        SESSION_ID,
        _creds,
        _make_loop,
        _prose_resume_script,
        _resume_mcp,
        _run_to_approval_pause,
    )

    class Executor:
        async def resume(self, **kwargs):
            return object()

    store = InMemorySessionStore()
    await _run_to_approval_pause(store)
    emitter = ProgressEmitter()
    loop = _make_loop(
        store,
        ScriptedModelClient(_prose_resume_script("This step could not proceed.")),
        _resume_mcp(),
        executor=Executor(),
    )
    loop._observer = emitter.observe
    # The dispatch boundary must preserve any ToolResult's actual status, rather
    # than collapsing every non-ok result to error during resume.
    monkeypatch.setattr(
        loop,
        "_blueprint_outcome_to_tool_result",
        lambda _: ToolResult(
            "denied",
            "runBlueprint",
            "COLUMN_SCOPE_VIOLATION",
            False,
            "Not accessible.",
            frozenset(),
            None,
            None,
        ),
    )
    frames = [
        frame
        async for frame in _stream_turn(
            lambda: loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="approve"),
            emitter,
        )
    ]
    progress = [json.loads(f.split("data: ", 1)[1]) for f in frames if "event: progress" in f]
    dispatch = [
        p["shape"]
        for p in progress
        if p["shape"].get("tool_name") == "runBlueprint" and "lifecycle" in p["shape"]
    ]
    assert [p["lifecycle"] for p in dispatch] == ["start", "denied"]
    assert dispatch[0]["tool_call_id"] == dispatch[1]["tool_call_id"]


@pytest.mark.parametrize("status", ["ok", "denied", "error"])
async def test_without_summary_completion_keeps_start_label(status):
    emitter = ProgressEmitter()
    payload = {"tool_name": "runQuery", "tool_call_id": "fallback"}
    emitter.observe("tool_dispatch_start", payload)
    emitter.observe("tool_dispatch_" + status, payload)
    emitter.close()
    events = [event async for event in emitter.stream()]
    assert [event.shape["lifecycle"] for event in events] == ["start", status]
    assert events[0].step == events[1].step == "finding the requested information…"
