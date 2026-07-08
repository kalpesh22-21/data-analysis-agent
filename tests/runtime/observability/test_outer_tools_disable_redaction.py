"""`otlp_disable_redaction` extended to the OUTER RuntimeTool span sites —
`resolveValues` (concept + period), `searchBlueprints` (query), and
`runBlueprint` (slot_bindings). Companion to the dispatcher-scoped
`tests/runtime/dispatch/test_tool_dispatcher_disable_redaction.py`.

For each tool: default OFF keeps the D25-redacted span byte-identical; ON reveals
the real free-text/nested-dict values on the span; and the flag is TELEMETRY-ONLY
— the actual tool work (the dispatched/backing calls, the returned result) is
identical whether the flag is on or off, because each site feeds the redacted
args ONLY to its span and always passes the RAW model_args to the real work.

Layer-1: real OTel SDK + InMemorySpanExporter, Layer-1 fakes — no OpenAI spend.

Tags:
  - otlp-redaction-on-by-default
  - otlp-disable-redaction-shows-real-tool-calls
  - otlp-disable-redaction-is-telemetry-only
"""

from __future__ import annotations

import json

import pytest
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import BlueprintExecutor
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail, Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import (
    GetBlueprintTool,
    SearchBlueprintsTool,
    SearchKnowledgeTool,
)
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

JWT = "jwt-should-never-leak"
SESSION_ID = "sess-outer-disable-redaction"


def _tracer_with_exporter() -> tuple[tracing.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracing.get_tracer(provider), exporter


def _tool_span(exporter: InMemorySpanExporter, name: str):  # noqa: ANN201
    tool_spans = [
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
        and s.attributes.get("tool.name") == name
    ]
    assert len(tool_spans) == 1, f"expected exactly one {name} TOOL span, got {len(tool_spans)}"
    return tool_spans[0]


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=scope)


# ===========================================================================
# resolveValues — concept (free text) + period (nested dict)
# ===========================================================================

_RV_TABLE = "dbpcm_warehouse.accrual_events"
_RV_CATALOG = CatalogHandle(
    {_RV_TABLE: {"EarnCode": "Nullable(String)", "EarnDescription": "Nullable(String)", "d": "Date"}}
)
PII_CONCEPT = "earnings for Jane Doe"
PERIOD = {"column": "d", "start": "2023-01-01", "end": "2023-12-31"}
_RV_RESULT = {
    "columns": ["EarnCode", "EarnDescription", "freq"],
    "rows": [["MAT", "maternity leave", 12]],
    "row_count": 1,
    "truncated": False,
}


async def _run_resolve_values(disable_redaction: bool):  # noqa: ANN202
    tracer, exporter = _tracer_with_exporter()
    mcp = FakeMCPClient(scripted={"runQuery": [_RV_RESULT]})
    dispatcher = ToolDispatcher(mcp, _RV_CATALOG, tracer=tracer)
    composite = ResolveValuesComposite(
        tool_dispatcher=dispatcher,
        catalog=_RV_CATALOG,
        embedding_client=FakeEmbeddingClient(dim=2),
        tracer=tracer,
        disable_redaction=disable_redaction,
    )
    result = await composite.run(
        {"table": _RV_TABLE, "column": "EarnCode", "concept": PII_CONCEPT, "period": PERIOD},
        _creds(),
    )
    return result, _tool_span(exporter, "resolveValues"), mcp


async def test_resolve_values_off_redacts_concept_and_omits_period() -> None:
    """otlp-redaction-on-by-default: default resolveValues span redacts the
    concept and (period being a nested dict) never puts period on the span."""
    _result, span, _mcp = await _run_resolve_values(False)
    assert span.attributes.get("tool.args.concept") == "<redacted>"
    assert "tool.args.period" not in span.attributes  # dict skipped in default posture
    assert PII_CONCEPT not in str(dict(span.attributes))
    assert "2023-01-01" not in str(dict(span.attributes))


async def test_resolve_values_on_reveals_concept_and_period() -> None:
    """otlp-disable-redaction-shows-real-tool-calls: with the flag on, the
    resolveValues span carries the REAL concept AND the real period bounds."""
    _result, span, _mcp = await _run_resolve_values(True)
    assert span.attributes["tool.args.concept"] == PII_CONCEPT
    period = json.loads(span.attributes["tool.args.period"])
    assert period == PERIOD


async def test_resolve_values_flag_is_telemetry_only() -> None:
    """otlp-disable-redaction-is-telemetry-only: the backing runQuery the MCP
    actually executed is byte-identical on/off, as is the returned result — the
    flag changed ONLY the span."""
    off_result, _off_span, off_mcp = await _run_resolve_values(False)
    on_result, _on_span, on_mcp = await _run_resolve_values(True)
    assert off_result.status == on_result.status == "ok"
    assert off_result.result_full == on_result.result_full
    # The real dispatched runQuery (SQL + injected creds) is identical either way.
    assert [c.args for c in off_mcp.calls] == [c.args for c in on_mcp.calls]
    assert off_mcp.calls[0].jwt == on_mcp.calls[0].jwt == JWT


# ===========================================================================
# searchBlueprints — query (free text)
# ===========================================================================

_SB_A = "dbpcm_warehouse.payroll.Amount"
PII_QUERY = "overtime paid to Jane Doe"


async def _run_search_blueprints(disable_redaction: bool):  # noqa: ANN202
    tracer, exporter = _tracer_with_exporter()
    index = FakeVectorIndex(
        [
            (
                Candidate(
                    id="bp-ot",
                    kind="blueprint",
                    text="overtime rollup",
                    uses=frozenset({_SB_A}),
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
        tracer=tracer,
        disable_redaction=disable_redaction,
    )
    result = await tool.run({"query": PII_QUERY, "k": 5}, _creds(frozenset({_SB_A})))
    return result, _tool_span(exporter, "searchBlueprints")


async def test_search_blueprints_off_redacts_query() -> None:
    """otlp-redaction-on-by-default: default searchBlueprints span redacts query."""
    _result, span = await _run_search_blueprints(False)
    assert span.attributes.get("tool.args.query") == "<redacted>"
    assert span.attributes.get("tool.args.k") == 5  # structural k kept
    assert PII_QUERY not in str(dict(span.attributes))


async def test_search_blueprints_on_reveals_query() -> None:
    """otlp-disable-redaction-shows-real-tool-calls: with the flag on, the span
    carries the REAL query free text."""
    _result, span = await _run_search_blueprints(True)
    assert span.attributes["tool.args.query"] == PII_QUERY
    assert span.attributes.get("tool.args.k") == 5


async def test_search_blueprints_flag_is_telemetry_only() -> None:
    """otlp-disable-redaction-is-telemetry-only: the returned result (what the
    pipeline actually recalled from the raw query) is identical on/off."""
    off_result, _off = await _run_search_blueprints(False)
    on_result, _on = await _run_search_blueprints(True)
    assert off_result.status == on_result.status == "ok"
    assert off_result.result_full == on_result.result_full


# ---------------------------------------------------------------------------
# The shared `_ReadTool` seam: prove the per-subclass `super().__init__(...,
# disable_redaction=...)` passthrough for ALL THREE read tools — a dropped kwarg
# on any subclass would silently still-redact when ON (the gap this change
# closed). SearchBlueprintsTool is functionally covered above; here every
# subclass' construction is asserted, plus searchKnowledge's `query` reveal.
# ---------------------------------------------------------------------------


def _empty_pipeline() -> RetrievalPipeline:
    return RetrievalPipeline(
        embedding_client=FakeEmbeddingClient(dim=2),
        reranker=None,
        vector_index=FakeVectorIndex(),
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
    )


def _read_tool(name: str, disable_redaction: bool):  # noqa: ANN202
    if name == "searchBlueprints":
        return SearchBlueprintsTool(
            pipeline=_empty_pipeline(), default_k=5, max_k=20, disable_redaction=disable_redaction
        )
    if name == "searchKnowledge":
        return SearchKnowledgeTool(
            pipeline=_empty_pipeline(), knowledge_k=5, disable_redaction=disable_redaction
        )
    return GetBlueprintTool(
        vector_index=FakeVectorIndex(), disable_redaction=disable_redaction
    )


@pytest.mark.parametrize("name", ["searchBlueprints", "searchKnowledge", "getBlueprint"])
@pytest.mark.parametrize("disable_redaction", [True, False])
def test_read_tool_subclasses_thread_disable_redaction(name: str, disable_redaction: bool) -> None:
    """Every read-tool subclass forwards `disable_redaction` through
    `super().__init__` to the shared `_ReadTool` seam — not just SearchBlueprints."""
    tool = _read_tool(name, disable_redaction)
    assert tool._disable_redaction is disable_redaction


async def test_search_knowledge_off_redacts_query_on_reveals_it() -> None:
    """The searchKnowledge subclass (not only searchBlueprints) redacts `query`
    by default and reveals it when ON — the reveal actually reaches its span."""
    query = "knowledge about Jane Doe's overtime policy"
    for disable_redaction, expected in ((False, "<redacted>"), (True, query)):
        tracer, exporter = _tracer_with_exporter()
        pipeline = RetrievalPipeline(
            embedding_client=FakeEmbeddingClient({query: [1.0, 0.0]}),
            reranker=None,
            vector_index=FakeVectorIndex(),
            user_memory=NullUserMemoryProvider(),
            recall_k=30,
            top_k_blueprints=3,
            top_k_knowledge=3,
            tracer=tracer,
        )
        tool = SearchKnowledgeTool(
            pipeline=pipeline, knowledge_k=5, tracer=tracer, disable_redaction=disable_redaction
        )
        result = await tool.run({"query": query}, _creds())
        assert result.status == "ok"
        span = _tool_span(exporter, "searchKnowledge")
        assert span.attributes.get("tool.args.query") == expected


# ===========================================================================
# runBlueprint — slot_bindings (nested dict)
# ===========================================================================

_BP_E = "dbpcm_warehouse.employee"
_BP_CATALOG = CatalogHandle(
    {
        _BP_E: {
            "EmployeeCode": "String",
            "Department": "Nullable(String)",
            "AnnualSalary": "Nullable(Float64)",
        }
    }
)
_BID = "bp-average-salary-by-department"
PII_SLOT_VALUE = "Warehouse-JaneDoe"
_AVG_SQL = (
    "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary, "
    "COUNT(DISTINCT EmployeeCode) AS headcount "
    "FROM dbpcm_warehouse.employee WHERE Department = {department} GROUP BY Department"
)


def _bp_detail() -> BlueprintDetail:
    return BlueprintDetail(
        id=_BID,
        intent="Average annual salary by department",
        slots_summary="department",
        uses=frozenset({f"{_BP_E}.Department", f"{_BP_E}.AnnualSalary", f"{_BP_E}.EmployeeCode"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=[
            {"name": "department", "type": "string", "required": True, "binds_to": f"{_BP_E}.Department"}
        ],
        sql_template=_AVG_SQL,
        result_grain=["Department"],
    )


def _bp_mcp() -> FakeMCPClient:
    return FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["Department"], "rows": [[PII_SLOT_VALUE]], "row_count": 1, "truncated": False},
                {
                    "columns": ["department", "avg_salary", "headcount"],
                    "rows": [[PII_SLOT_VALUE, 50000.0, 3]],
                    "row_count": 1,
                    "truncated": False,
                },
                {"columns": ["__bp_n", "__bp_d"], "rows": [[1, 1]], "row_count": 1, "truncated": False},
            ]
        }
    )


async def _run_run_blueprint(disable_redaction: bool):  # noqa: ANN202
    tracer, exporter = _tracer_with_exporter()
    mcp = _bp_mcp()
    index = FakeVectorIndex()
    index.add_detail(_bp_detail())
    executor = BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, _BP_CATALOG, tracer=tracer),
        vector_index=index,
    )
    tool = RunBlueprintTool(executor=executor, tracer=tracer, disable_redaction=disable_redaction)
    result = await tool.run(
        {"id": _BID, "slot_bindings": {"department": PII_SLOT_VALUE}}, _creds()
    )
    return result, _tool_span(exporter, "runBlueprint"), mcp


async def test_run_blueprint_off_redacts_slot_bindings() -> None:
    """otlp-redaction-on-by-default: default runBlueprint span keeps the
    structural id but (slot_bindings being a nested dict) puts no slot values on
    the span."""
    _result, span, _mcp = await _run_run_blueprint(False)
    assert span.attributes.get("tool.args.id") == _BID
    assert "tool.args.slot_bindings" not in span.attributes  # dict skipped in default
    assert PII_SLOT_VALUE not in str(dict(span.attributes))


async def test_run_blueprint_on_reveals_slot_bindings() -> None:
    """otlp-disable-redaction-shows-real-tool-calls: with the flag on, the span
    carries the REAL slot_bindings values."""
    _result, span, _mcp = await _run_run_blueprint(True)
    assert span.attributes.get("tool.args.id") == _BID
    slot_bindings = json.loads(span.attributes["tool.args.slot_bindings"])
    assert slot_bindings == {"department": PII_SLOT_VALUE}


async def test_run_blueprint_flag_is_telemetry_only() -> None:
    """otlp-disable-redaction-is-telemetry-only: the executor's dispatched
    runQuery calls (the real per-node SQL + injected creds) are identical on/off,
    as is the returned result — the flag changed ONLY the span."""
    off_result, _off_span, off_mcp = await _run_run_blueprint(False)
    on_result, _on_span, on_mcp = await _run_run_blueprint(True)
    assert off_result.status == on_result.status
    assert off_result.result_full == on_result.result_full
    assert [c.args for c in off_mcp.calls] == [c.args for c in on_mcp.calls]
    assert all(c.jwt == JWT for c in off_mcp.calls + on_mcp.calls)
