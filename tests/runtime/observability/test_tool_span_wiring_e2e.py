"""B5 integration coverage: TOOL spans are actually emitted on the live path,
D25 SQL-literal masking lands on a real span, and the askUser question text
never reaches any span attribute (Layer 1 — real OTel SDK + InMemorySpanExporter,
no live Phoenix collector).

Before this fix, `app.py`'s `_tracing_observer` dropped every
`tool_dispatch_*` event (`if not event.startswith("loop_"): return`), so
`ToolDispatcher` never produced a TOOL span at all — `tracing.tool_span`,
`redaction.redact_tool_args`, and `redaction.mask_sql` were dead code on the
real request path despite being unit-tested in isolation. This file proves
the wiring itself, not just the pure functions.
"""

from __future__ import annotations

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.progress import combine_observers
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Salary": "Decimal(18,2)"}})

SESSION_ID = "sess-tool-span-e2e"
JWT = "jwt-secret-should-never-leak"
PII_NAME = "Jane Doe"
PII_SALARY = "128000"
PII_SQL = f"SELECT EmployeeCode FROM employee WHERE Name = '{PII_NAME}' AND Salary > {PII_SALARY}"
PII_QUESTION = f"Did you mean the record for {PII_NAME} (salary {PII_SALARY})?"


def _tracer_with_memory_exporter() -> tuple[tracing.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracing.get_tracer(provider), exporter


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset())


async def test_tool_span_emitted_per_dispatched_call_with_sql_literal_masked() -> None:
    """(a) a TOOL span is created per dispatched call; (b) its SQL attribute
    is literal-masked (D25), never the raw PII-laden SQL."""
    tracer, exporter = _tracer_with_memory_exporter()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["EmployeeCode"], "rows": [["E1"]], "row_count": 1, "truncated": False}
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp, CATALOG, tracer=tracer)

    result = await dispatcher.dispatch("runQuery", {"sql": PII_SQL}, _credentials())
    assert result.status == "ok"

    spans = exporter.get_finished_spans()
    tool_spans = [
        s
        for s in spans
        if s.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
    ]
    assert len(tool_spans) == 1
    attrs = tool_spans[0].attributes
    assert attrs["tool.name"] == "runQuery"
    assert attrs["tool.status"] == "ok"

    masked_sql = attrs["tool.args.sql"]
    assert PII_NAME not in masked_sql
    assert PII_SALARY not in masked_sql
    assert "SELECT EmployeeCode FROM employee" in masked_sql  # shape preserved


async def test_tool_span_emitted_for_denied_and_error_dispatches_too() -> None:
    """A TOOL span is created on EVERY dispatch outcome, not just success —
    denied (MCPToolError) and error (B4 raw transport exception) paths too."""
    from data_agent.runtime.mcp.client import MCPToolError

    tracer, exporter = _tracer_with_memory_exporter()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                MCPToolError("COLUMN_SCOPE_VIOLATION", "denied"),
                ConnectionError("connection reset by peer"),
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp, CATALOG, tracer=tracer)

    denied = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())
    assert denied.status == "denied"
    errored = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())
    assert errored.status == "error"

    spans = exporter.get_finished_spans()
    tool_spans = [
        s
        for s in spans
        if s.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
    ]
    assert len(tool_spans) == 2
    assert tool_spans[0].attributes["tool.status"] == "denied"
    assert tool_spans[0].attributes["tool.error_code"] == "COLUMN_SCOPE_VIOLATION"
    assert tool_spans[1].attributes["tool.status"] == "error"
    assert tool_spans[1].attributes["tool.error_code"] == "INTERNAL_TRANSPORT_ERROR"
    # Never the raw transport exception text, on a span any more than in the
    # user-facing message (B4).
    blob = str(dict(tool_spans[1].attributes))
    assert "connection reset by peer" not in blob


async def test_ask_user_question_never_appears_in_any_span_attribute() -> None:
    """(c) the askUser question text never reaches any span attribute — the
    loop's own GUARDRAIL-observer wiring (`tracing.guardrail_observer`, the
    exact function `app.py` wires in) must strip it, not merely the tool
    spans (askUser never reaches ToolDispatcher at all)."""
    tracer, exporter = _tracer_with_memory_exporter()
    guardrail_observer = tracing.guardrail_observer(tracer)

    store = InMemorySessionStore()
    mcp = FakeMCPClient()  # askUser must never reach the MCP transport
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="call_1", name="askUser", arguments={"question": PII_QUESTION})
                ]
            )
        ]
    )
    dispatcher = ToolDispatcher(mcp, CATALOG, observer=guardrail_observer, tracer=tracer)
    assembler = ContextAssembler(store, history_token_budget=100_000, tracer=tracer)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=lambda: _tools_provider(),
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=combine_observers(guardrail_observer),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Show me payroll."
    )
    assert outcome.status == "paused_ask_user"
    assert outcome.pending_question["question"] == PII_QUESTION  # UI DOES get it (not telemetry)

    spans = exporter.get_finished_spans()
    assert len(spans) >= 1  # at least the loop_paused_ask_user GUARDRAIL span fired
    for finished_span in spans:
        for value in finished_span.attributes.values():
            assert PII_NAME not in str(value)
            assert PII_SALARY not in str(value)
            assert PII_QUESTION not in str(value)


async def _tools_provider() -> list[dict]:
    return [
        {
            "type": "function",
            "name": "askUser",
            "description": "",
            "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
        }
    ]
