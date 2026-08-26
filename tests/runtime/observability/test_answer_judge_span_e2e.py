"""The ANSWER JUDGE's telemetry, through the REAL OTel SDK (doc 09 §K).

TWO SURFACES, AND THEY ARE GUARDED BY DIFFERENT MECHANISMS — which is the whole reason
this file exists rather than a unit test over payload dicts.

THE EVENTS go through `tracing.guardrail_observer`, which keeps only allowlisted keys
carrying `str|int|float|bool`. An event whose name lacks the `loop_` prefix is dropped
SILENTLY, and an attribute missing from `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` vanishes
without a word — so a misnamed event or an unlisted key fires perfectly in every
raw-recorder test and reaches production telemetry never. That near-miss has shipped
here before (`loop_analysis_state_auto_bound`), which is why the doc requires this pass.

THE JUDGE SPAN bypasses that allowlist ENTIRELY: `answer_judge_span` sets attributes
directly on the span. Nothing filters it, so the discipline has to hold by hand at the
call site, and the only way to know it does is to read the exported span.

WHAT MUST NEVER APPEAR, on either surface: the judge's `feedback` (model-composed prose
written after reading warehouse rows), the user's question, the draft answer, and any
figure out of a result. 05 §L's own span test states the same rule for the answer rules.
"""

from __future__ import annotations

import json
from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.loop.answer_judge import (
    ANSWER_JUDGE_CALLED_EVENT,
    ANSWER_JUDGE_REFUSED_EVENT,
    ANSWER_JUDGE_SKIPPED_EVENT,
    ASK_USER_JUDGE_REFUSED_EVENT,
    JUDGE_TOOL_NAME,
    AnswerJudge,
    JudgeBrief,
)
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing

# Text that must never reach telemetry, distinctive enough to grep a whole span dump for.
SECRET_FEEDBACK = "Say which year the ACME-Q3-SECRET figure covers."
SECRET_QUESTION = "How many people did we hire at ACME-Q3-SECRET this year?"
SECRET_DRAFT = "We hired 9,184 people at ACME-Q3-SECRET this year."
SECRET_FIGURE = "9,184"


def _tracer_with_memory_exporter() -> tuple[Any, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracing.get_tracer(provider), exporter


def _all_attribute_text(exporter: InMemorySpanExporter) -> str:
    """Every attribute of every exported span, flattened — so an assertion can ask
    "does this string appear ANYWHERE in telemetry" rather than naming keys it would
    have to keep in step with the code."""
    return json.dumps(
        [dict(span.attributes or {}) for span in exporter.get_finished_spans()],
        default=str,
    )


def _brief(**overrides: Any) -> JudgeBrief:
    base: dict[str, Any] = {
        "site": "exit_prose",
        "question": SECRET_QUESTION,
        "draft": SECRET_DRAFT,
        "date_anchor": "2026-08-26",
    }
    base.update(overrides)
    return JudgeBrief(**base)


def _verdict_turn(approved: bool, violation: str = "", feedback: str = "") -> ModelTurnResult:
    return ModelTurnResult(
        assistant_text=None,
        tool_calls=[
            ToolCallRequest(
                id="c1",
                name=JUDGE_TOOL_NAME,
                arguments={
                    "approved": approved,
                    "violation": violation,
                    "feedback": feedback,
                },
            )
        ],
        usage={"total_tokens": 1234},
    )


# --- the events, through the real guardrail observer -------------------------


def test_the_judge_events_survive_the_real_observer_with_their_slugs() -> None:
    """`violation`, `site` and `reason` must be on the allowlist or these spans arrive
    carrying nothing — correctly named, perfectly emitted, and useless."""
    tracer, exporter = _tracer_with_memory_exporter()
    observe = tracing.guardrail_observer(tracer)

    observe(ANSWER_JUDGE_REFUSED_EVENT, {"violation": "unrecorded_assumption", "site": "exit_prose"})
    observe(ASK_USER_JUDGE_REFUSED_EVENT, {"violation": "non_contextual_question"})
    observe(ANSWER_JUDGE_SKIPPED_EVENT, {"reason": "wall_clock"})
    observe(ANSWER_JUDGE_CALLED_EVENT, {"site": "exit_table", "tokens": 1234})

    spans = {span.name: dict(span.attributes or {}) for span in exporter.get_finished_spans()}
    assert spans[ANSWER_JUDGE_REFUSED_EVENT]["violation"] == "unrecorded_assumption"
    assert spans[ANSWER_JUDGE_REFUSED_EVENT]["site"] == "exit_prose"
    assert spans[ASK_USER_JUDGE_REFUSED_EVENT]["violation"] == "non_contextual_question"
    assert spans[ANSWER_JUDGE_SKIPPED_EVENT]["reason"] == "wall_clock"
    assert spans[ANSWER_JUDGE_CALLED_EVENT]["site"] == "exit_table"
    assert spans[ANSWER_JUDGE_CALLED_EVENT]["tokens"] == 1234
    for attributes in spans.values():
        assert (
            attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND]
            == OpenInferenceSpanKindValues.GUARDRAIL.value
        )


def test_feedback_and_question_cannot_ride_a_judge_event() -> None:
    """The allowlist is DEFAULT-DENY, and this is the property that matters most: a
    future call site that helpfully adds the judge's sentence to the payload must not be
    able to publish it."""
    tracer, exporter = _tracer_with_memory_exporter()
    observe = tracing.guardrail_observer(tracer)
    observe(
        ANSWER_JUDGE_REFUSED_EVENT,
        {
            "violation": "unrecorded_assumption",
            "site": "exit_prose",
            "feedback": SECRET_FEEDBACK,
            "question": SECRET_QUESTION,
            "draft": SECRET_DRAFT,
        },
    )
    dumped = _all_attribute_text(exporter)
    assert "ACME-Q3-SECRET" not in dumped
    assert SECRET_FIGURE not in dumped
    assert "unrecorded_assumption" in dumped, "the slug DOES survive"


# --- the judge's own span ----------------------------------------------------


async def test_the_judge_opens_a_chain_span_carrying_shape_only() -> None:
    """`answer_judge_span` sets attributes DIRECTLY, with no allowlist between it and the
    exporter — so this is the only guard the span has."""
    tracer, exporter = _tracer_with_memory_exporter()
    judge = AnswerJudge(
        model_client=ScriptedModelClient(
            [_verdict_turn(False, "unrecorded_assumption", SECRET_FEEDBACK)]
        ),
        token_budget=100_000,
        tracer=tracer,
    )
    verdict = await judge.review(_brief())
    assert verdict.approved is False

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "answer_judge"]
    attributes = dict(span.attributes or {})
    assert (
        attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND]
        == OpenInferenceSpanKindValues.CHAIN.value
    )
    assert attributes["site"] == "exit_prose"
    assert attributes["approved"] is False
    assert attributes["violation"] == "unrecorded_assumption"
    assert attributes["tokens"] == 1234

    dumped = _all_attribute_text(exporter)
    assert SECRET_FEEDBACK not in dumped, "the judge's own prose must never be exported"
    assert "ACME-Q3-SECRET" not in dumped, "nor the question, nor the draft"
    assert SECRET_FIGURE not in dumped, "nor any figure the answer reported"


async def test_the_span_covers_the_failure_paths_too() -> None:
    """A timeout or a provider error is where a latency question is actually answered, so
    the span has to close over those exits rather than only the happy one."""

    class _Boom:
        async def send_turn(self, messages: Any, tools: Any) -> ModelTurnResult:
            raise RuntimeError("502 from the provider")

    tracer, exporter = _tracer_with_memory_exporter()
    judge = AnswerJudge(
        model_client=_Boom(),  # type: ignore[arg-type]
        token_budget=100_000,
        tracer=tracer,
    )
    assert (await judge.review(_brief())).approved is True

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "answer_judge"]
    assert dict(span.attributes or {})["outcome"] == "provider_error"


async def test_no_tracer_means_no_span_and_no_crash() -> None:
    """`tracer=None` is every Layer-1 test and any unconfigured deploy: the judge must
    work exactly as before this was wired."""
    _tracer, exporter = _tracer_with_memory_exporter()
    judge = AnswerJudge(
        model_client=ScriptedModelClient([_verdict_turn(True)]),
        token_budget=100_000,
        tracer=None,
    )
    assert (await judge.review(_brief())).approved is True
    assert [s for s in exporter.get_finished_spans() if s.name == "answer_judge"] == []
