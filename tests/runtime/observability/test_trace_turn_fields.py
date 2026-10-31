"""Readable root spans and measured tool calls through the runtime composition."""

import asyncio
import json

import jwt
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime import app as app_module
from data_agent.runtime.app import create_app
from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from tests.runtime.final_answer import final_answer


@pytest.mark.usefixtures("answer_tools", "blueprint_consulted")
@pytest.mark.parametrize("redact", [True, False])
def test_root_input_output_and_jti_metadata(monkeypatch, redact):
    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **kw: frozenset())
    token = jwt.encode(
        {
            "jti": "request-12345",
            "clientcode": "0O003",
            "proc_center": "pc",
            "sub": "alice",
            "user_name": "alice.user",
            "session_id": "wrong-claim-session",
            "service_key": "DO_NOT_EXPORT",
            "column_scope": ["private.column"],
        },
        "test-only-key-with-at-least-32-bytes",
        algorithm="HS256",
    )
    sink = InMemorySpanExporter()
    answer = "I don't have any information to answer your question."
    app = create_app(
        settings=RuntimeSettings(
            _env_file=None,
            otlp_endpoint="",
            answer_judge_enabled=False,
            otlp_disable_redaction=not redact,
        ),
        session_store=InMemorySessionStore(),
        mcp_client=FakeMCPClient(),
        model_client=ScriptedModelClient([final_answer(assistant_text=answer)]),
        catalog=CatalogHandle({}),
        span_exporter=sink,
    )
    with TestClient(app) as client:
        response = client.post(
            "/turn",
            json={"message": "Show payroll above 500"},
            headers={"Authorization": "Bearer " + token, "X-Session-Id": "trace-root-fields"},
        )
        assert response.status_code == 200
        root = next(s for s in sink.get_finished_spans() if s.name == "agent.turn")
        assert root.attributes["input.value"] == (
            "Show payroll above [number]" if redact else "Show payroll above 500"
        )
        assert root.attributes["output.value"] == answer
        emitted = next(s for s in sink.get_finished_spans() if s.name == "loop_response_emitted")
        assert emitted.context.trace_id == root.context.trace_id
        assert emitted.parent.span_id == root.context.span_id
        assert "delivery_traceparent" not in response.text
        metadata = json.loads(root.attributes["metadata"])
        assert metadata["jti"] == "request-12345"
        assert metadata["clientcode"] == "0O003"
        assert metadata["proc_center"] == "pc"
        assert metadata["sub"] == "alice"
        assert metadata["user_name"] == "alice.user"
        assert metadata["session_id"] == "trace-root-fields"
        assert metadata["turn_index"] == 0
        assert "scope_hash" in metadata
        assert "service_key" not in metadata and "column_scope" not in metadata
        assert "DO_NOT_EXPORT" not in json.dumps(dict(root.attributes))
        assert token not in json.dumps(dict(root.attributes))


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_tool_duration_includes_transport_and_records_sql_without_rows(fail):
    class SlowMCP(FakeMCPClient):
        async def call_tool(self, *args, **kwargs):
            await asyncio.sleep(0.02)
            return await super().call_tool(*args, **kwargs)

    sink = InMemorySpanExporter()
    provider = tracing.configure_tracing(
        otlp_endpoint="",
        service_name="test",
        span_exporter=sink,
        phoenix_export=True,
        selective_redaction=True,
    )
    response = (
        MCPToolError("ACCESS_DENIED", "Denied")
        if fail
        else {
            "columns": ["employee_code"],
            "rows": [["RESULT_CELL"]],
            "row_count": 1,
            "truncated": False,
        }
    )
    mcp = SlowMCP(scripted={"runQuery": [response]})
    dispatcher = ToolDispatcher(
        mcp,
        CatalogHandle({"db.employee": {"employee_code": "String"}}),
        tracer=tracing.get_tracer(provider),
        disable_redaction=True,
    )
    sql = "SELECT employee_code FROM db.employee LIMIT 500"
    await dispatcher.dispatch(
        "runQuery",
        {"sql": sql},
        RuntimeCredentials(session_id="s", jwt="opaque", column_scope=frozenset()),
    )
    tool = next(s for s in sink.get_finished_spans() if s.name == "tool.runQuery")
    assert tool.attributes["tool.duration_ms"] >= 15
    assert (tool.end_time - tool.start_time) / 1_000_000 >= 15
    assert tool.attributes["input.value"] == "SELECT employee_code FROM db.employee LIMIT [number]"
    assert tool.attributes["db.statement"] == tool.attributes["input.value"]
    assert "output.value" not in tool.attributes
    assert "RESULT_CELL" not in json.dumps(dict(tool.attributes))
    assert mcp.calls[0].args["sql"] == sql


def test_context_reader_omits_missing_invalid_and_unlisted_claims():
    assert tracing.request_context("not-a-token") == {}
    token = jwt.encode(
        {
            "jti": ["invalid"],
            "clientcode": {"bad": "shape"},
            "aud": ["api"],
            "iss": "issuer",
            "exp": 123,
            "scope_token": "secret",
        },
        "test-only-key-with-at-least-32-bytes",
        algorithm="HS256",
    )
    assert tracing.request_context(token) == {"aud": ["api"], "iss": "issuer"}


def test_request_context_inherits_to_children_without_cross_session_leakage():
    sink = InMemorySpanExporter()
    provider = tracing.configure_tracing(
        otlp_endpoint="",
        service_name="test",
        span_exporter=sink,
        phoenix_export=True,
        selective_redaction=True,
    )
    tracer = tracing.get_tracer(provider)
    for session, client in [("session-one", "client-one"), ("session-two", "client-two")]:
        with tracing.agent_span(
            tracer,
            scope_hash="hash",
            turn_index=2,
            session_id=session,
            request_metadata={"jti": "request-123", "clientcode": client},
            catalog_sha="catalog-456",
        ):
            with tracer.start_as_current_span("llm"):
                with tracing.tool_span(
                    tracer, tool_name="listTables", args={}, status="ok", error_code=None
                ):
                    pass
    for span in sink.get_finished_spans():
        metadata = json.loads(span.attributes["metadata"])
        session = span.attributes["session.id"]
        assert metadata["session_id"] == session
        assert metadata["clientcode"] == (
            "client-one" if session == "session-one" else "client-two"
        )
        assert metadata["jti"] == "request-123"
        assert metadata["catalog_sha"] == "catalog-456"
        assert metadata["turn_index"] == 2


@pytest.mark.parametrize('status', ['done', 'paused_ask_user'])
@pytest.mark.parametrize('redact', [False, True])
async def test_delivery_span_contains_the_actual_wire_object_and_formats(status, redact):
    import hashlib
    from copy import deepcopy

    from data_agent.runtime.app import _stream_turn
    from data_agent.runtime.loop.agent_loop import TurnOutcome
    from data_agent.runtime.observability.progress import ProgressEmitter

    document = {
        'takeaway': 'Salary is $120,000.00.',
        'sections': [{'presentation': {'kind': 'table', 'result_id': 'result'}}],
        'sources': {'result': {'column_formats': {
            'ZIP Code': {'kind': 'identifier'},
            'Annual Salary': {'kind': 'currency', 'currency': 'USD', 'precision': 2},
        }}},
    }
    original = deepcopy(document)
    outcome = TurnOutcome(status, None, {'question': 'Which period?'} if status.startswith('paused') else None,
                          3, answer_document=document)
    sink = InMemorySpanExporter()
    provider = tracing.configure_tracing(otlp_endpoint='', service_name='delivery-test',
                                        span_exporter=sink, phoenix_export=True,
                                        selective_redaction=redact)
    observer = tracing.guardrail_observer(tracing.get_tracer(provider))

    async def execute():
        return outcome

    frames = [f async for f in _stream_turn(execute, ProgressEmitter(), delivery_observer=observer)]
    wire = json.loads(next(f for f in frames if f.startswith('event: result')).split('data: ', 1)[1])
    emitted = next(s for s in sink.get_finished_spans() if s.name == 'loop_response_emitted')
    attrs = emitted.attributes
    captured = json.loads(attrs['output.value'])
    assert attrs['output.mime_type'] == 'application/json'
    encoded = json.dumps(wire).encode('utf-8')
    assert attrs['ui.result.sha256'] == hashlib.sha256(encoded).hexdigest()
    assert attrs['ui.result.bytes'] == len(encoded)
    assert document == original  # tracing cannot change the delivered answer
    if not redact:
        assert captured == wire
    if status == 'done':
        assert attrs['ui.answer_document.present'] is True
        assert attrs['ui.column_format.count'] == 2
        assert attrs['ui.source.count'] == 1
        assert attrs['ui.presentation.kinds'] == 'table'
        formats = captured['answer_document']['sources']['result']['column_formats']
        assert formats['ZIP Code']['kind'] == 'identifier'
        assert formats['Annual Salary']['currency'] == 'USD'
        if redact:
            assert '120,000' not in attrs['output.value']
    else:
        assert 'answer_document' not in captured
        assert attrs['ui.answer_document.present'] is False
        assert attrs['ui.column_format.count'] == 0
