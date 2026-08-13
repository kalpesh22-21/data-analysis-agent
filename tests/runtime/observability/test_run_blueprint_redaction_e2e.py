"""End-to-end D25 proof for `runBlueprint` (runblueprint-design §5.5): the
model-authored `slot_bindings` VALUES never reach ANY span attribute or ANY
progress-event payload on the LIVE loop path — the fast-path sibling of
`test_read_tools_redaction_e2e.py` / `test_resolve_values_redaction_e2e.py`.

Uses the real OTel SDK + InMemorySpanExporter and a capturing progress observer,
wired through the runtime-tool registry exactly as `app.py` composes it. The slot
value is also a SQL literal in the bound node query — the inner runQuery TOOL
span masks it via `mask_sql`, so it never appears there either.
"""

from __future__ import annotations

from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import BlueprintExecutor
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore
from tests._blueprint_gate import expand_blueprint

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "AnnualSalary": "Nullable(Float64)"}}
)
SESSION_ID = "sess-bp-redact-e2e"
JWT = "jwt-should-never-leak"
_BID = "bp-average-salary-by-department"
# The slot value carries a person name — the exact free-text PII to redact.
PII_VALUE = "Warehouse-JaneDoe"

_AVG_SQL = (
    "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary, "
    "COUNT(DISTINCT EmployeeCode) AS headcount "
    "FROM dbpcm_warehouse.employee WHERE Department = {department} GROUP BY Department"
)


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "runBlueprint", "description": "", "parameters": {}}]


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset())


def _tracer_with_exporter() -> tuple[tracing.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracing.get_tracer(provider), exporter


def _detail() -> BlueprintDetail:
    return BlueprintDetail(
        id=_BID,
        intent="Average annual salary by department",
        slots_summary="department",
        uses=frozenset({f"{_E}.Department", f"{_E}.AnnualSalary", f"{_E}.EmployeeCode"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": f"{_E}.Department"}],
        sql_template=_AVG_SQL,
        result_grain=["Department"],
    )


async def test_slot_value_absent_from_every_span_and_progress_event() -> None:
    tracer, exporter = _tracer_with_exporter()
    events: list[tuple[str, dict[str, Any]]] = []

    def capturing_observer(event: str, payload: dict[str, Any]) -> None:
        events.append((event, dict(payload)))

    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["Department"], "rows": [[PII_VALUE]], "row_count": 1, "truncated": False},
                {"columns": ["department", "avg_salary", "headcount"], "rows": [[PII_VALUE, 50000.0, 3]], "row_count": 1, "truncated": False},
                {"columns": ["__bp_n", "__bp_d"], "rows": [[1, 1]], "row_count": 1, "truncated": False},
            ]
        }
    )
    index = FakeVectorIndex()
    index.add_detail(_detail())
    executor = BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG, observer=capturing_observer, tracer=tracer),
        vector_index=index,
        observer=capturing_observer,
    )
    tool = RunBlueprintTool(executor=executor, observer=capturing_observer, tracer=tracer)

    store = InMemorySessionStore()
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rb_1",
                        name="runBlueprint",
                        arguments={"id": _BID, "slot_bindings": {"department": PII_VALUE}},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="The average salary is $50,000."),
        ]
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG, observer=capturing_observer, tracer=tracer),
        context_assembler=ContextAssembler(store, history_token_budget=100_000, tracer=tracer),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=capturing_observer,
        runtime_tools={"runBlueprint": tool},
    )
    # The getBlueprint-before-runBlueprint gate (tests/_blueprint_gate.py).
    await expand_blueprint(store, SESSION_ID, _BID)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="avg salary?")
    assert outcome.status == "done"

    spans = exporter.get_finished_spans()
    tool_spans = [
        s
        for s in spans
        if s.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
    ]
    # exactly ONE runBlueprint TOOL span, with the inner runQuery spans nested.
    assert "runBlueprint" in [s.attributes.get("tool.name") for s in tool_spans]

    # (a) the slot value never appears on ANY span attribute (incl. the masked
    # inner runQuery SQL literal + the JWT never leaks either).
    for span in spans:
        for value in span.attributes.values():
            assert PII_VALUE not in str(value)
            assert JWT not in str(value)

    # (b) the slot value never appears on ANY progress-event payload.
    assert events
    for _event_name, payload in events:
        assert PII_VALUE not in str(payload)
        assert JWT not in str(payload)

    # (c) the structural blueprint id IS kept on the runBlueprint span (debuggable).
    rb_span = next(s for s in tool_spans if s.attributes.get("tool.name") == "runBlueprint")
    assert rb_span.attributes.get("tool.args.id") == _BID
