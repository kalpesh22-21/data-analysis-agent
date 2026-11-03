"""Denied tools and unavailable judges are ERROR spans, so status-only dashboards count them."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from opentelemetry.trace import StatusCode

from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.dispatch.tool_envelope import in_tool_span
from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief
from data_agent.runtime.loop.partial_answer import _table_counts
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from tests.runtime.dispatch.test_invalid_column_reference import MESSAGE, SQL
from tests.runtime.observability.test_tool_span_wiring_e2e import CATALOG as SPAN_CATALOG
from tests.runtime.observability.test_tool_span_wiring_e2e import (
    PII_SQL,
    _tracer_with_memory_exporter,
)
from tests.runtime.observability.test_tool_span_wiring_e2e import _credentials as span_credentials
from tests.runtime.test_harness_improvements import CATALOG, CREDS


def _span(exporter, name):
    return next(s for s in exporter.get_finished_spans() if s.name == name)


async def test_denied_dispatched_tool_span_is_an_error():
    tracer, exporter = _tracer_with_memory_exporter()
    mcp = FakeMCPClient(scripted={"runQuery": [MCPToolError("INVALID_COLUMN_REFERENCE", MESSAGE)]})
    await ToolDispatcher(mcp, CATALOG, tracer=tracer).dispatch(
        "runQuery", {"sql": SQL}, CREDS, tool_call_id="c1"
    )
    status = _span(exporter, "tool.runQuery").status
    assert status.status_code is StatusCode.ERROR
    assert status.description == "INVALID_COLUMN_REFERENCE"


async def test_successful_dispatched_tool_span_keeps_unset_status():
    tracer, exporter = _tracer_with_memory_exporter()
    mcp = FakeMCPClient(scripted={"runQuery": [
        {"columns": ["EmployeeCode"], "rows": [["E1"]], "row_count": 1, "truncated": False}
    ]})
    result = await ToolDispatcher(mcp, SPAN_CATALOG, tracer=tracer).dispatch(
        "runQuery", {"sql": PII_SQL}, span_credentials()
    )
    assert result.status == "ok"
    assert _span(exporter, "tool.runQuery").status.status_code is StatusCode.UNSET


@pytest.mark.parametrize(("outcome", "code"), [("denied", "BLUEPRINT_NOT_FOUND"), ("ok", None)])
async def test_runtime_tool_envelope_stamps_error_status(outcome, code):
    tracer, exporter = _tracer_with_memory_exporter()

    async def work():
        return SimpleNamespace(status=outcome, error_code=code)

    await in_tool_span(tracer, tool_name="getBlueprint", args={}, work=work)
    status = _span(exporter, "tool.getBlueprint").status
    expected = StatusCode.ERROR if outcome == "denied" else StatusCode.UNSET
    assert status.status_code is expected
    assert status.description == code


async def test_unavailable_judge_is_an_error_span_but_still_approves():
    tracer, exporter = _tracer_with_memory_exporter()

    class _TimesOut:
        async def send_turn(self, messages, tools):
            raise TimeoutError

    judge = AnswerJudge(model_client=_TimesOut(), token_budget=100_000, tracer=tracer,
                        timeout_retry_enabled=False)
    verdict = await judge.review(JudgeBrief("exit_prose", "How many?", draft="Ten."))
    assert verdict.approved and not verdict.reviewed
    status = _span(exporter, "answer_judge").status
    assert status.status_code is StatusCode.ERROR and status.description == "timeout"


def test_recovery_table_counts_are_shape_only():
    table = {"presentation": {"kind": "table"}}
    sections = [{**table, "table_reason": "Ranks."}, {**table, "table_reason": "  "},
                {"presentation": {"kind": "text"}}]
    components = ({"kind": "table"}, {"kind": "table"}, {"kind": "capability"})
    expected = {"visible_table_count": 2, "table_section_count": 2, "missing_table_reason_count": 1}
    assert _table_counts(sections, components) == expected
    assert _table_counts(json.dumps(sections), components) == expected
    assert _table_counts("not json", ()) == {
        "visible_table_count": 0, "table_section_count": 0, "missing_table_reason_count": 0,
    }


def test_new_telemetry_keys_survive_the_guardrail_allowlist():
    from data_agent.runtime.observability.tracing import guardrail_observer

    tracer, exporter = _tracer_with_memory_exporter()
    observe = guardrail_observer(tracer)
    payloads = {
        "loop_sql_correction_linked": {
            "tool_name": "runQuery", "tool_call_id": "b", "corrects_tool_call_id": "a",
            "corrects_error_code": "INVALID_COLUMN_REFERENCE", "attempt": 2,
            "prior_tool_call_ids": '["a"]',
        },
        "loop_recovery_validation_failed": {
            "reason": "invalid_presentation", "visible_table_count": 2,
            "table_section_count": 2, "missing_table_reason_count": 1,
        },
        "loop_answer_judge_repair_preempted": {
            "status": "stopped_hard_ceiling", "exit": "pause", "repair_type": "analysis", "calls": 1,
        },
    }
    for event, payload in payloads.items():
        observe(event, payload)
    for event, payload in payloads.items():
        attrs = dict(_span(exporter, event).attributes)
        assert {k: attrs.get(k) for k in payload} == payload, event
