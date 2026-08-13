"""`ToolDispatcher.dispatch(emit_progress=False)` — the UI-progress gate.

WHY IT EXISTS: `blueprint/executor.py` runs every internal node of a blueprint
through this same `dispatch`, and `tool_dispatch_start`/`ok`/`denied`/`error` are
UI progress labels (`observability/progress.py::_STEP_LABELS`). So one
`runBlueprint` painted a "running runQuery…" line per internal node, telling the
user their single analysis is really N SQL queries over internal tables.

The gate is deliberately narrow, and each half is asserted here:
  - the four OBSERVER events are silenced for that call, and ONLY for that call;
  - the `tool.<name>` OTel span is still emitted (an operator debugging a
    blueprint needs the inner nodes);
  - the returned `ToolResult` is byte-identical either way, on ok/denied/error —
    this changes what the UI is told, never what happens.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.progress import to_progress_event
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

_ROWS = {"columns": ["EmployeeCode"], "rows": [["E1"]], "row_count": 1, "truncated": False}
_SQL = "SELECT EmployeeCode FROM employee"


class _RecordingObserver:
    """Records every `(event, payload)` the dispatcher emits."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, dict(payload)))

    @property
    def names(self) -> list[str]:
        return [name for name, _payload in self.events]


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="sess-progress-gate", jwt="jwt", column_scope=frozenset())


def _tracer_with_memory_exporter() -> tuple[tracing.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracing.get_tracer(provider), exporter


def _comparable(result: Any) -> dict[str, Any]:
    payload = dataclasses.asdict(result)
    if payload.get("provenance") is not None:
        payload["provenance"] = sorted(payload["provenance"])
    return payload


async def test_default_dispatch_still_emits_the_progress_events() -> None:
    """The outer `runBlueprint`/`runQuery` the model asked for must stay visible —
    a gate that defaulted the other way would leave the turn silent."""
    observer = _RecordingObserver()
    dispatcher = ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [dict(_ROWS)]}), CATALOG, observer=observer
    )

    result = await dispatcher.dispatch("runQuery", {"sql": _SQL}, _credentials())

    assert result.status == "ok"
    assert observer.names == ["tool_dispatch_start", "tool_dispatch_ok"]
    # ...and they really do render as UI progress lines (the thing being gated).
    assert [to_progress_event(name, payload) is not None for name, payload in observer.events] == [
        True,
        True,
    ]


async def test_gated_dispatch_emits_no_tool_dispatch_events() -> None:
    observer = _RecordingObserver()
    dispatcher = ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [dict(_ROWS)]}), CATALOG, observer=observer
    )

    result = await dispatcher.dispatch(
        "runQuery", {"sql": _SQL}, _credentials(), emit_progress=False
    )

    assert result.status == "ok"
    assert observer.events == []


async def test_gated_and_ungated_results_are_identical_on_success() -> None:
    ungated_observer = _RecordingObserver()
    gated_observer = _RecordingObserver()
    ungated = await ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [dict(_ROWS)]}), CATALOG, observer=ungated_observer
    ).dispatch("runQuery", {"sql": _SQL}, _credentials())
    gated = await ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [dict(_ROWS)]}), CATALOG, observer=gated_observer
    ).dispatch("runQuery", {"sql": _SQL}, _credentials(), emit_progress=False)

    assert _comparable(gated) == _comparable(ungated)
    assert gated_observer.events == []
    assert ungated_observer.names


async def test_a_denial_inside_a_gated_dispatch_still_returns_the_same_result() -> None:
    """Control flow is untouched: only the UI line is silenced, never the denial."""
    ungated_observer = _RecordingObserver()
    gated_observer = _RecordingObserver()
    ungated = await ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "denied")]}),
        CATALOG,
        observer=ungated_observer,
    ).dispatch("runQuery", {"sql": _SQL}, _credentials())
    gated = await ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "denied")]}),
        CATALOG,
        observer=gated_observer,
    ).dispatch("runQuery", {"sql": _SQL}, _credentials(), emit_progress=False)

    assert gated.status == "denied"
    assert gated.error_code == "COLUMN_SCOPE_VIOLATION"
    assert _comparable(gated) == _comparable(ungated)
    assert "tool_dispatch_denied" in ungated_observer.names
    assert gated_observer.events == []


async def test_a_transport_error_inside_a_gated_dispatch_still_returns_the_same_result() -> None:
    ungated_observer = _RecordingObserver()
    gated_observer = _RecordingObserver()
    ungated = await ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [ConnectionError("connection reset by peer")]}),
        CATALOG,
        observer=ungated_observer,
    ).dispatch("runQuery", {"sql": _SQL}, _credentials())
    gated = await ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [ConnectionError("connection reset by peer")]}),
        CATALOG,
        observer=gated_observer,
    ).dispatch("runQuery", {"sql": _SQL}, _credentials(), emit_progress=False)

    assert gated.status == "error"
    assert _comparable(gated) == _comparable(ungated)
    assert "tool_dispatch_error" in ungated_observer.names
    assert gated_observer.events == []


async def test_the_gate_does_not_touch_the_tool_span() -> None:
    """Telemetry is a SEPARATE channel from UI progress: an operator debugging a
    blueprint still gets a `tool.runQuery` span per internal node."""
    tracer, exporter = _tracer_with_memory_exporter()
    observer = _RecordingObserver()
    dispatcher = ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [dict(_ROWS)]}),
        CATALOG,
        observer=observer,
        tracer=tracer,
    )

    await dispatcher.dispatch("runQuery", {"sql": _SQL}, _credentials(), emit_progress=False)

    tool_spans = [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
    ]
    assert len(tool_spans) == 1
    assert tool_spans[0].attributes["tool.name"] == "runQuery"
    assert tool_spans[0].attributes["tool.status"] == "ok"
    assert observer.events == []


async def test_the_gate_leaves_the_cards_dropped_operator_signal_alone() -> None:
    """The third clause of the gate's stated scope, asserted rather than assumed:
    `tool_dispatch_cards_dropped` is an OPERATOR degrade signal, not a UI label, so
    it is deliberately outside the gate — and it is only safe to leave outside
    because it renders to nothing (`to_progress_event` -> None). Both halves are
    pinned here, because a later `_STEP_LABELS` entry for it would silently turn
    this exemption into a leak."""
    observer = _RecordingObserver()
    cards = {
        "count": 12,
        "blueprints": [{"id": f"bp-{i}", "intent": "x" * 200} for i in range(12)],
    }
    dispatcher = ToolDispatcher(
        FakeMCPClient(scripted={"searchBlueprints": [cards]}),
        CATALOG,
        observer=observer,
        max_tool_result_tokens=200,
    )

    result = await dispatcher.dispatch(
        "searchBlueprints", {"query": "overtime"}, _credentials(), emit_progress=False
    )

    assert result.status == "ok"
    assert observer.names == ["tool_dispatch_cards_dropped"]
    assert to_progress_event(*observer.events[0]) is None


async def test_the_gate_is_per_call_not_sticky() -> None:
    """A gated dispatch must not disable progress for the dispatcher instance —
    the executor and the agent loop share one."""
    observer = _RecordingObserver()
    dispatcher = ToolDispatcher(
        FakeMCPClient(scripted={"runQuery": [dict(_ROWS), dict(_ROWS)]}),
        CATALOG,
        observer=observer,
    )

    await dispatcher.dispatch("runQuery", {"sql": _SQL}, _credentials(), emit_progress=False)
    assert observer.events == []
    await dispatcher.dispatch("runQuery", {"sql": _SQL}, _credentials())
    assert observer.names == ["tool_dispatch_start", "tool_dispatch_ok"]
