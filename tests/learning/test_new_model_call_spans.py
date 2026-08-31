"""Every model call on the learning plane emits a span — including the two added last.

The `learning.param_judge` and `learning.revise` calls shipped untraced while every other
stage on the plane had a span. That is worse than an ordinary observability gap for two
specific reasons, one per call:

  * the judge's span is the DENOMINATOR of the phase-D-1 flag rate. The audit store holds only
    verdicts a judge actually GAVE, so the reasons a candidate was never judged exist on the
    span and nowhere else; computing "how often does it flag" from the rows alone divides by
    the wrong number.
  * the reviser WRITES NOTHING. Without a span there is no record anywhere that a human asked
    the assistant, so "it keeps suggesting nothing" — the complaint that would drive the next
    prompt change — cannot be substantiated at all.

The D25 posture is inherited rather than re-decided: shape-only by default, entity-bearing
detail only under `learning_trace_verbose`, and `record_exception=False` via `_learning_span`
so a provider error cannot attach a response body to a span.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.observability import param_judge_span, revise_span


@pytest.fixture
def recorded() -> tuple:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_the_param_judge_span_carries_the_measurement_shape(recorded: tuple) -> None:
    """`class_a_findings` is separate from `findings` and always present, because it is the
    count the phase-D-2 decision turns on: Class A means the blueprint is WRONG rather than
    merely narrow, and a `revise` carrying only naming nits is a different event."""
    tracer, exporter = recorded
    with param_judge_span(
        tracer,
        candidate_id="candidate::h::0",
        session_id="s1",
        outcome="judged",
        verdict="revise",
        confidence=0.91,
        findings=3,
        class_a_findings=1,
        would_discard=True,
    ):
        pass
    span = exporter.get_finished_spans()[0]
    assert span.name == "learning.param_judge"
    assert span.attributes["learning.param_judge.verdict"] == "revise"
    assert span.attributes["learning.param_judge.class_a_findings"] == 1
    assert span.attributes["learning.param_judge.would_discard"] is True
    assert span.attributes["learning.candidate_id"] == "candidate::h::0"


def test_the_param_judge_span_withholds_prose_unless_verbose(recorded: tuple) -> None:
    """⚠ `template` is entity-bearing — an inline predicate keeps its literal value in it — and
    `feedback` is model prose about the unredacted payload. With the D25 gate off the keys are
    ABSENT, not None-valued: a Phoenix filter must not be able to tell "withheld" from "empty"."""
    tracer, exporter = recorded
    for verbose in (False, True):
        with param_judge_span(
            tracer,
            candidate_id="c",
            session_id="s",
            outcome="judged",
            verbose=verbose,
            feedback="record_type = 'EARNING' defines the metric",
            template="SELECT 1 WHERE employee = 'E12345'",
        ):
            pass
    off, on = exporter.get_finished_spans()
    assert "learning.param_judge.feedback" not in off.attributes
    assert "learning.param_judge.template" not in off.attributes
    assert "E12345" not in str(dict(off.attributes))
    assert on.attributes["learning.param_judge.template"].endswith("'E12345'")


@pytest.mark.parametrize(
    "outcome",
    ["proposed", "no_snapshot", "withheld_scan", "unusable", "timeout", "failed",
     "refused_template_edit"],
)
def test_every_revise_outcome_is_a_distinct_span_value(
    recorded: tuple, outcome: str
) -> None:
    """They are kept apart because they need different fixes: `no_snapshot` is a MIGRATION
    signal (the candidate predates the stamp), `withheld_scan` is the leakage gate working,
    and only the last three are the model. Collapsing them to "no proposal" would make the
    most common early failure look like a model problem."""
    tracer, exporter = recorded
    with revise_span(tracer, candidate_id="c", outcome=outcome):
        pass
    span = exporter.get_finished_spans()[0]
    assert span.name == "learning.revise"
    assert span.attributes["learning.revise.outcome"] == outcome


def test_the_revise_span_withholds_the_human_text_unless_verbose(recorded: tuple) -> None:
    """The sharpest verbose payload on the plane: `feedback` is free text a human typed into a
    browser, `rationale` is model prose about the unredacted accepted SQL. Neither is
    leakage-scanned."""
    tracer, exporter = recorded
    for verbose in (False, True):
        with revise_span(
            tracer,
            candidate_id="c",
            outcome="proposed",
            entries=2,
            conflicts=1,
            verbose=verbose,
            feedback="employee E12345 is the subject",
            rationale="inline it",
        ):
            pass
    off, on = exporter.get_finished_spans()
    assert "learning.revise.feedback" not in off.attributes
    assert "E12345" not in str(dict(off.attributes))
    # Shape is present in BOTH: counts are what an operator watches, and they are entity-free.
    assert off.attributes["learning.revise.entries"] == 2
    assert off.attributes["learning.revise.conflicts"] == 1
    assert on.attributes["learning.revise.feedback"] == "employee E12345 is the subject"


def test_neither_span_records_exceptions(recorded: tuple) -> None:
    """`_learning_span` forces `record_exception=False` (D25): an OpenAI SDK error embeds
    response bodies, so recording it would leak content EVEN WITH VERBOSE OFF. The status is
    still set, so a failure stays visible in the trace."""
    tracer, exporter = recorded
    for factory in (
        lambda: param_judge_span(tracer, candidate_id="c", session_id="s", outcome="failed"),
        lambda: revise_span(tracer, candidate_id="c", outcome="failed"),
    ):
        with pytest.raises(RuntimeError), factory():
            raise RuntimeError("provider returned a body full of literals")
    for span in exporter.get_finished_spans():
        assert not span.events, f"{span.name} recorded an exception event"
