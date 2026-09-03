"""Unit tests for observability/tracing.py (Layer 1 — SimpleSpanProcessor + InMemorySpanExporter,
no live OTLP/Phoenix collector required)."""

from __future__ import annotations

import asyncio

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from data_agent.runtime.dispatch.tool_envelope import in_tool_span
from data_agent.runtime.observability import tracing


def _provider_with_memory_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_configure_tracing_with_no_endpoint_is_a_no_op_provider() -> None:
    provider = tracing.configure_tracing(otlp_endpoint="", service_name="data-agent-runtime")
    tracer = tracing.get_tracer(provider)
    # Must not raise and must not attempt any network call.
    with tracing.agent_span(tracer, scope_hash="deadbeef", turn_index=0):
        pass


def test_agent_span_sets_openinference_kind_and_attributes() -> None:
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)

    with tracing.agent_span(tracer, scope_hash="deadbeef", turn_index=2):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs[SpanAttributes.OPENINFERENCE_SPAN_KIND] == OpenInferenceSpanKindValues.AGENT.value
    assert attrs["scope_hash"] == "deadbeef"
    assert attrs["turn.index"] == 2


async def test_every_assistant_round_is_a_child_of_one_turn_trace() -> None:
    """Multiple model responses, including task-spawned work, remain one trace."""
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)

    async def response(round_index: int) -> None:
        with tracer.start_as_current_span(
            "Response", attributes={"assistant.round": round_index}
        ):
            await asyncio.sleep(0)

    with tracing.agent_span(tracer, scope_hash="deadbeef", turn_index=7):
        await response(1)
        await asyncio.create_task(response(2))
        await response(3)

    spans = exporter.get_finished_spans()
    (turn,) = [span for span in spans if span.name == "agent.turn"]
    responses = [span for span in spans if span.name == "Response"]
    assert len(responses) == 3
    assert {span.context.trace_id for span in spans} == {turn.context.trace_id}
    assert {span.parent.span_id for span in responses} == {turn.context.span_id}


def test_configure_tracing_raises_span_capacity_above_sdk_defaults() -> None:
    exporter = InMemorySpanExporter()
    provider = tracing.configure_tracing(
        otlp_endpoint="", service_name="test", span_exporter=exporter
    )
    tracer = tracing.get_tracer(provider)
    attributes = {f"attribute.{index}": index for index in range(512)}

    with tracing.chain_span(tracer, "large.response", attributes=attributes):
        pass

    (span,) = exporter.get_finished_spans()
    assert len(span.attributes) == 513  # 512 payload attributes + OpenInference kind


def test_tool_span_masks_are_caller_responsibility_but_shape_is_recorded() -> None:
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)

    with tracing.tool_span(
        tracer,
        tool_name="runQuery",
        args={"sql": "SELECT * FROM t WHERE x = ''"},
        status="ok",
        error_code=None,
    ):
        pass

    spans = exporter.get_finished_spans()
    attrs = spans[0].attributes
    assert attrs[SpanAttributes.OPENINFERENCE_SPAN_KIND] == OpenInferenceSpanKindValues.TOOL.value
    assert attrs["tool.name"] == "runQuery"
    assert attrs["tool.status"] == "ok"
    assert "tool.error_code" not in attrs


def test_tool_span_records_error_code_when_denied() -> None:
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)

    with tracing.tool_span(
        tracer, tool_name="runQuery", args={}, status="denied", error_code="COLUMN_SCOPE_VIOLATION"
    ):
        pass

    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["tool.error_code"] == "COLUMN_SCOPE_VIOLATION"


def test_guardrail_and_chain_spans_use_correct_kinds() -> None:
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)

    with tracing.chain_span(tracer, "context.assembly", attributes={"dropped_by_scope_count": 3}):
        pass
    with tracing.guardrail_span(tracer, "budget.check", attributes={"window": 1}):
        pass

    spans = exporter.get_finished_spans()
    kinds = {s.name: s.attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND] for s in spans}
    assert kinds["context.assembly"] == OpenInferenceSpanKindValues.CHAIN.value
    assert kinds["budget.check"] == OpenInferenceSpanKindValues.GUARDRAIL.value


def _observed_attributes(event: str, payload: dict) -> dict:
    """Drive one observer event through the REAL `guardrail_observer` and return
    the attributes that actually reached the exported span."""
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)
    tracing.guardrail_observer(tracer)(event, payload)
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == [event]
    return dict(spans[0].attributes)


def test_analysis_state_events_reach_phoenix_with_their_attributes() -> None:
    """EMITTING AN EVENT DOES NOT PUBLISH ITS PAYLOAD (Release 1, doc 06).

    `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` is a strict ATTRIBUTE allowlist, so before
    it was extended every one of these events would have reached Phoenix as a
    correctly-named GUARDRAIL span carrying ZERO attributes — including
    `loop_intent_completed`, whose entire purpose is the one attribute it holds.
    """
    cases = [
        ("loop_analysis_state_initialized", {"intent_count": 3, "turn_index": 2}),
        (
            "loop_analysis_state_transition",
            {
                "intent_id": "i2",
                "from_status": "pending",
                "to_status": "blocked",
                "reason_code": "ENFORCEMENT_EXHAUSTED",
            },
        ),
        ("loop_intent_completed", {"intent_id": "i1", "evidence_tool_name": "runBlueprint"}),
        ("loop_metadata_evidence_completion", {"intent_id": "i1"}),
        ("loop_evidence_reused", {"intent_id": "i1", "tool_call_id": "call_q"}),
        ("loop_finalization_refused", {"exit": "answer_with_table", "pending_count": 2}),
        ("loop_finalization_block_spent", {"window": 2}),
        ("loop_zero_row_block", {"intent_id": "i2"}),
        ("loop_zero_row_completion", {"intent_id": "i1"}),
        (
            "loop_intent_force_blocked",
            {"intent_id": "i2", "reason_code": "BUDGET_EXHAUSTED"},
        ),
        # The cap-during-a-refused-round shape (05 §F). The reason code is the
        # honest cause; `budget_cap_reached` is the capacity fact kept alongside it,
        # and it is worthless to an operator if the allowlist eats it.
        (
            "loop_intent_force_blocked",
            {
                "intent_id": "i2",
                "reason_code": "ENFORCEMENT_EXHAUSTED",
                "budget_cap_reached": True,
            },
        ),
        ("loop_enforcement_exhausted", {"intent_count": 1}),
        (
            "loop_analysis_state_late_init_rejected",
            {"proposed_count": 3, "blocking_tool_name": "runQuery"},
        ),
        (
            "loop_analysis_state_rejected",
            {"reason": "unknown_intent_id", "intent_count": 2},
        ),
    ]
    for event, payload in cases:
        attributes = _observed_attributes(event, payload)
        for key, value in payload.items():
            assert attributes.get(key) == value, f"{event}.{key} did not survive the allowlist"


def test_a_description_is_dropped_even_if_an_emitter_ever_sends_one() -> None:
    """THE SECOND OF TWO INDEPENDENT GUARDS. The emitters never put `description`
    on a payload (asserted by the D25 scan in
    `tests/runtime/loop/test_analysis_state_telemetry.py`); this is what happens if
    one ever does. `description` must NEVER be added to the allowlist — it is
    model-authored text derived from the user's question.

    The allowlist is deliberately NOT a type filter: a bare `isinstance(v, str |
    int | float | bool)` check would let this straight through, which is exactly
    how `loop_paused_ask_user`'s `question` leaked once."""
    attributes = _observed_attributes(
        "loop_analysis_state_transition",
        {
            "intent_id": "i1",
            "to_status": "completed",
            "description": "employees earning above $100,000",
        },
    )
    assert attributes["intent_id"] == "i1"
    assert "description" not in attributes
    assert "employees earning above $100,000" not in str(attributes)
    assert "description" not in tracing._GUARDRAIL_OBSERVER_ATTR_ALLOWLIST


def test_the_paused_ask_user_question_is_still_dropped() -> None:
    """The regression the allowlist exists for, re-checked after extending it."""
    attributes = _observed_attributes(
        "loop_paused_ask_user", {"question": "Which department did you mean?"}
    )
    assert "question" not in attributes
    assert "Which department" not in str(attributes)


def test_instrument_openai_is_idempotent() -> None:
    provider = tracing.configure_tracing(otlp_endpoint="", service_name="data-agent-runtime")
    tracing.instrument_openai(provider)
    tracing.instrument_openai(provider)  # must not raise on a second call


# ---------------------------------------------------------------------------
# The envelope's `record_exception=False` belt (dispatch/tool_envelope.py)
# ---------------------------------------------------------------------------


async def test_the_tool_envelope_keeps_exception_text_off_the_span() -> None:
    """D25 on the ERROR path of a TOOL span, which the redaction tests do not reach.

    Every runtime tool guards its own work, so an exception normally never escapes into
    the span — but OTel's DEFAULT (`record_exception=True`) writes the exception MESSAGE
    and STACKTRACE as a span event if one ever does, and a tool's exception message is
    derived from a query, a slot value or a row. The span STATUS must still be ERROR:
    the belt withholds the detail, it does not hide the failure.

    Both halves are asserted, because "no exception event" is only meaningful next to
    proof that the default posture DOES record one — otherwise this test would pass just
    as happily against a tracer that records nothing at all."""
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)
    secret = "SELECT AnnualSalary FROM employee WHERE name = 'Jane Doe'"

    async def _boom() -> None:
        raise RuntimeError(secret)

    try:
        await in_tool_span(tracer, tool_name="runQuery", args={}, work=_boom)
    except RuntimeError:
        pass

    (span,) = exporter.get_finished_spans()
    assert [event.name for event in span.events] == []
    assert secret not in str(
        [dict(span.attributes or {}), [dict(e.attributes or {}) for e in span.events]]
    )
    # The failure itself stays visible — only the content-bearing detail is withheld.
    assert span.status.status_code is StatusCode.ERROR


def test_tool_span_still_records_exceptions_by_default() -> None:
    """The other half: the POST-HOC callers (`ToolDispatcher._emit_tool_span`) keep the
    default, so this parameter is a per-caller choice rather than a global change. It is
    also what makes the test above prove something."""
    provider, exporter = _provider_with_memory_exporter()
    tracer = tracing.get_tracer(provider)

    try:
        with tracing.tool_span(tracer, tool_name="runQuery", args={}, status="ok", error_code=None):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    (span,) = exporter.get_finished_spans()
    assert [event.name for event in span.events] == ["exception"]
