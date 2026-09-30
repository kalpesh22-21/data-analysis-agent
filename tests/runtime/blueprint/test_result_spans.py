"""Blueprint span output makes completion versus slot pause unambiguous."""

from dataclasses import replace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.blueprint.executor import ExecCompleted, ExecPaused
from data_agent.runtime.blueprint.tool import RunBlueprintTool
from data_agent.runtime.dispatch.tool_dispatcher import _build_preview
from data_agent.runtime.retrieval.tools import GetBlueprintTool
from tests.runtime.blueprint.test_executor import _creds, _detail, _index


@pytest.mark.parametrize("reveal", [False, True])
async def test_blueprint_result_spans_are_bounded_and_honor_redaction(reveal):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    detail = replace(_detail(), intent="PRIVATE BLUEPRINT DESCRIPTION " + "x" * 10000)
    get = GetBlueprintTool(vector_index=_index(detail), tracer=tracer, disable_redaction=reveal)
    await get.run({"id": detail.id}, _creds())

    class Executor:
        async def execute(self, **kwargs):
            raw = {
                "columns": ["Employee"],
                "rows": [["PRIVATE ROW " + "x" * 10000]],
                "row_count": 1,
            }
            return ExecCompleted(
                result_full=raw, preview=_build_preview(raw, 20), provenance=frozenset()
            )

    run = RunBlueprintTool(executor=Executor(), tracer=tracer, disable_redaction=reveal)
    await run.run({"id": detail.id, "slot_bindings": {}}, _creds())
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["tool.getBlueprint", "tool.runBlueprint"]
    for span in spans:
        assert span.attributes["tool.result.kind"] == "result"
        assert span.attributes["tool.result.row_count"] >= 1
        if reveal:
            assert len(span.attributes["tool.result.preview_rows"]) <= 8000
        else:
            assert "PRIVATE" not in str(span.attributes)
            assert "tool.result.preview_rows" not in span.attributes


async def test_blueprint_pause_span_cannot_look_like_successful_query_result():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Executor:
        async def execute(self, **kwargs):
            return ExecPaused(
                reason="blueprint_slot",
                pending_question={"question": "PRIVATE QUESTION"},
                blueprint_id="bp",
                slot_bindings_json="{}",
            )

    tool = RunBlueprintTool(executor=Executor(), tracer=provider.get_tracer("test"))
    await tool.run({"id": "bp", "slot_bindings": {}}, _creds())
    attributes = exporter.get_finished_spans()[0].attributes
    assert attributes["tool.status"] == "paused"
    assert attributes["tool.result.kind"] == "pause"
    assert attributes["tool.pause.reason"] == "blueprint_slot"
    assert "PRIVATE" not in str(attributes)
