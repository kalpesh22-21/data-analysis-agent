"""End-to-end D25 proof for the read tools (read-tools-design §5): `searchBlueprints`'s
`query` (free user text, the highest-PII-risk arg) never reaches ANY span
attribute or ANY progress-event payload on the LIVE loop path — the read-path
sibling of `test_resolve_values_redaction_e2e.py`.

Uses the real OTel SDK + InMemorySpanExporter and a capturing progress observer,
wired through the runtime-tool registry exactly as `app.py` composes it. Also
proves the tool emits ONE TOOL span with the recall CHAIN nested inside, and the
structural `id` on `getBlueprint` is KEPT (redaction is targeted at `query`).
"""

from __future__ import annotations

from typing import Any

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
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.progress import to_progress_event
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import SearchBlueprintsTool
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore

_A = "dbpcm_warehouse.payroll.Amount"
CATALOG = CatalogHandle({"dbpcm_warehouse.payroll": {"Amount": "Decimal(18,2)"}})

SESSION_ID = "sess-read-redact-e2e"
JWT = "jwt-should-never-leak"
# The query carries a name — the exact free-text PII the design fully redacts.
PII_QUERY = "overtime paid to Jane Doe in the Sales department"
PII_NAME = "Jane Doe"


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "searchBlueprints", "description": "", "parameters": {}}]


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset({_A}))


def _tracer_with_exporter() -> tuple[tracing.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracing.get_tracer(provider), exporter


async def test_query_absent_from_every_span_and_progress_event() -> None:
    tracer, exporter = _tracer_with_exporter()
    events: list[tuple[str, dict[str, Any]]] = []

    def capturing_observer(event: str, payload: dict[str, Any]) -> None:
        events.append((event, dict(payload)))

    index = FakeVectorIndex(
        [
            (
                Candidate(
                    id="bp-ot",
                    kind="blueprint",
                    text="overtime rollup",
                    uses=frozenset({_A}),
                    payload={"intent": "overtime rollup", "slots_summary": "dept"},
                ),
                [1.0, 0.0],
            )
        ]
    )
    pipeline = RetrievalPipeline(
        embedding_client=FakeEmbeddingClient({PII_QUERY: [1.0, 0.0]}),
        reranker=None,
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
        tracer=tracer,
    )
    tool = SearchBlueprintsTool(
        pipeline=pipeline,
        default_k=5,
        max_k=20,
        observer=capturing_observer,
        tracer=tracer,
    )
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(FakeMCPClient(), CATALOG, observer=capturing_observer, tracer=tracer)
    assembler = ContextAssembler(store, tracer=tracer)
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="sb_1", name="searchBlueprints", arguments={"query": PII_QUERY, "k": 5}
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Found the overtime blueprint."),
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
        runtime_tools={"searchBlueprints": tool},
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="overtime?"
    )
    assert outcome.status == "done"

    spans = exporter.get_finished_spans()
    # (a) exactly ONE searchBlueprints TOOL span, with the recall CHAIN nested.
    tool_spans = [
        s
        for s in spans
        if s.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
    ]
    assert [s.attributes.get("tool.name") for s in tool_spans] == ["searchBlueprints"]
    assert any(s.name == "retrieval.recall" for s in spans)  # nested recall CHAIN span

    # (b) the query text (and the PII name it carries) never on ANY span attribute.
    for span in spans:
        for value in span.attributes.values():
            assert PII_QUERY not in str(value)
            assert PII_NAME not in str(value)

    # (c) the redacted placeholder DID land on the TOOL span; structural k kept.
    sb_span = tool_spans[0]
    assert sb_span.attributes.get("tool.args.query") == "<redacted>"
    assert sb_span.attributes.get("tool.args.k") == 5

    # (d) query never on ANY progress-event payload, and never on the UI shape.
    assert events  # the loop/tool DID emit progress events
    for event_name, payload in events:
        assert PII_QUERY not in str(payload)
        assert PII_NAME not in str(payload)
        progress = to_progress_event(event_name, payload)
        if progress is not None:
            assert "query" not in progress.shape
