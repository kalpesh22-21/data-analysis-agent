"""End-to-end D25/D77 proof: the resolveValues `concept` (free user text, the
highest-PII-risk arg on the tool) never reaches ANY span attribute or ANY
progress-event payload on the live loop path — not merely the `redact_tool_args`
unit (which is covered separately in `test_redaction.py`).

Uses the real OTel SDK + InMemorySpanExporter and a capturing progress observer,
wired into the composite exactly as `app.py` composes it (tracer + observer).
"""

from __future__ import annotations

from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_T = "dbpcm_warehouse.accrual_events"
CATALOG = CatalogHandle(
    {_T: {"EarnCode": "Nullable(String)", "EarnDescription": "Nullable(String)"}}
)

SESSION_ID = "sess-rv-redact-e2e"
JWT = "jwt-should-never-leak"
# The concept carries a name — the exact free-text PII the design fully redacts.
PII_CONCEPT = "employees on maternity leave for Jane Doe"
PII_NAME = "Jane Doe"


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [
        {"type": "function", "name": "resolveValues", "description": "", "parameters": {}},
    ]


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset())


def _tracer_with_exporter() -> tuple[tracing.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracing.get_tracer(provider), exporter


async def test_concept_absent_from_every_span_and_progress_event() -> None:
    tracer, exporter = _tracer_with_exporter()
    events: list[tuple[str, dict[str, Any]]] = []

    def capturing_observer(event: str, payload: dict[str, Any]) -> None:
        events.append((event, dict(payload)))

    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {
                    "columns": ["EarnCode", "EarnDescription", "freq"],
                    "rows": [["MAT", "maternity leave", 12]],
                    "row_count": 1,
                    "truncated": False,
                }
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp, CATALOG, observer=capturing_observer, tracer=tracer)
    composite = ResolveValuesComposite(
        tool_dispatcher=dispatcher,
        catalog=CATALOG,
        embedding_client=FakeEmbeddingClient(dim=2),
        observer=capturing_observer,
        tracer=tracer,
    )
    assembler = ContextAssembler(store, history_token_budget=100_000, tracer=tracer)
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rv_1",
                        name="resolveValues",
                        arguments={"table": _T, "column": "EarnCode", "concept": PII_CONCEPT},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="MAT is the maternity code."),
        ]
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=capturing_observer,
        resolve_values=composite,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="which maternity code?"
    )
    assert outcome.status == "done"

    # (a) A resolveValues TOOL span was emitted, with the inner runQuery nested.
    spans = exporter.get_finished_spans()
    tool_spans = [
        s
        for s in spans
        if s.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
    ]
    tool_names = {s.attributes.get("tool.name") for s in tool_spans}
    assert "resolveValues" in tool_names
    assert "runQuery" in tool_names  # nested inner query span

    # (b) concept text (and the PII name it carries) never on ANY span attribute.
    for span in spans:
        for value in span.attributes.values():
            assert PII_CONCEPT not in str(value)
            assert PII_NAME not in str(value)

    # (c) concept never on ANY progress-event payload either.
    assert events  # the loop/composite DID emit progress events
    for _event_name, payload in events:
        blob = str(payload)
        assert PII_CONCEPT not in blob
        assert PII_NAME not in blob

    # (d) sanity: the resolveValues span DID keep the structural identifiers
    # (table/column) — redaction is targeted at concept, not everything.
    rv_span = next(s for s in tool_spans if s.attributes.get("tool.name") == "resolveValues")
    assert rv_span.attributes.get("tool.args.concept") == "<redacted>"
    assert rv_span.attributes.get("tool.args.column") == "EarnCode"
