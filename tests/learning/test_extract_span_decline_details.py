"""`learning.extract.decline_details` — the SENTENCE behind the decline code.

**The hole this closes.** The extract span has always carried `decline_reasons`: a closed
vocabulary of codes (`bad_role`, `missing_rule`, `no_evidence`, …). A code tells an
operator which CLASS of thing went wrong. It does not tell them what to fix, and for a
whole day of live sessions "declined, reason `bad_role`, zero candidates" was the entire
account of why the loop produced nothing — the detail that names the field existed in
memory and was thrown away at the span boundary.

The codes stay SHAPE-only (they are the rates). The detail is D25-GATED, because unlike
the codes it interpolates MODEL-authored strings — a slot name, a role, a type the model
invented — which are neither a closed vocabulary nor a leakage-scanned surface.

Slugs:
  * X-decline-detail-gated   — absent with verbose OFF, present with it ON.
  * X-decline-detail-useful  — the code and the sentence, per decline, in one attribute.
  * X-decline-detail-bounded — flattened to one line and capped, per item AND in total.
  * X-decline-rationale-any  — a NON-blueprint candidate is no longer invisible.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.consumer import (
    _DECLINE_DETAIL_LIMIT,
    _DECLINE_DETAILS_TOTAL_LIMIT,
    LearningConsumer,
    _decline_details,
)
from data_agent.learning.extractor.models import (
    CandidateHeader,
    Decline,
    EntitySelfCheck,
    EvidenceRef,
    ExtractedCandidate,
    ExtractionResult,
)
from data_agent.learning.memory_queue import InMemoryLearningQueue
from data_agent.learning.observability import extract_span

from .extractor.helpers import KEEP_VERDICT, make_summary


def _tracer_and_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("learning-loop-test"), exporter


def _emit(*, verbose: bool, details: str | None) -> dict:
    tracer, exporter = _tracer_and_exporter()
    with extract_span(
        tracer,
        session_id="sess-1",
        candidate_count=0,
        decline_count=1,
        decline_reasons=("bad_role",),
        verbose=verbose,
        decline_details=details,
    ):
        pass
    return dict(exporter.get_finished_spans()[0].attributes)


# --- the gate ------------------------------------------------------------------------


def test_the_detail_is_absent_with_verbose_off_and_the_code_is_not() -> None:
    """[X-decline-detail-gated] The split is the design: the CODE is a closed vocabulary
    and stays on every span; the SENTENCE quotes the model and is gated."""
    attrs = _emit(verbose=False, details="bad_role: slot pay_period has no binds_to")
    assert attrs["learning.extract.decline_reasons"] == "bad_role"
    assert attrs["learning.extract.decline_count"] == 1
    assert attrs["learning.extract.outcome"] == "declined"
    assert "learning.extract.decline_details" not in attrs


def test_the_detail_appears_with_verbose_on() -> None:
    """[X-decline-detail-gated] (positive control)"""
    detail = "bad_role: slot pay_period has no binds_to"
    attrs = _emit(verbose=True, details=detail)
    assert attrs["learning.extract.decline_details"] == detail


def test_no_detail_means_no_key_even_with_verbose_on() -> None:
    """[X-decline-detail-gated] `None` drops the key entirely (`_verbose_attrs`), rather
    than setting an empty string — a session that declined nothing must not look like a
    session whose declines had nothing to say."""
    assert "learning.extract.decline_details" not in _emit(verbose=True, details=None)


# --- the renderer ---------------------------------------------------------------------


def test_it_renders_the_code_and_the_sentence_for_every_decline() -> None:
    """[X-decline-detail-useful] `bad_role` names a class; "slot pay_period has no
    binds_to" names the thing to go fix. Both, for each decline, in reading order."""
    rendered = _decline_details(
        (
            Decline("blueprint", "bad_role", "slot pay_period has no binds_to"),
            Decline("blueprint", "missing_rule", "unknown rule 'headcount_only'"),
        )
    )
    assert rendered == (
        "bad_role: slot pay_period has no binds_to | "
        "missing_rule: unknown rule 'headcount_only'"
    )


def test_declines_without_a_detail_contribute_nothing() -> None:
    """[X-decline-detail-useful] A bare `reason: ` adds a separator and no information;
    the code is already on the span in `decline_reasons`."""
    assert _decline_details((Decline("blueprint", "no_evidence", ""),)) is None
    assert _decline_details(()) is None
    assert (
        _decline_details(
            (Decline("blueprint", "no_evidence", ""), Decline("knowledge", "totality", "why"))
        )
        == "totality: why"
    )


def test_a_newline_in_a_model_authored_detail_is_flattened() -> None:
    """[X-decline-detail-bounded] `Decline.detail` interpolates strings the MODEL wrote
    (`f"unknown role {p.role!r}"`). A newline would smear one attribute across the Phoenix
    UI and, in the log line the same text reaches, forge a record."""
    rendered = _decline_details(
        (Decline("blueprint", "bad_role", "unknown role 'x\nignore\r\nprevious'"),)
    )
    assert rendered is not None
    assert "\n" not in rendered and "\r" not in rendered
    assert rendered == "bad_role: unknown role 'x ignore previous'"


def test_one_pathological_detail_cannot_inflate_the_span() -> None:
    """[X-decline-detail-bounded] Per-item cap first. A model that writes a megabyte into
    a field name must cost one truncated line, not one enormous span."""
    rendered = _decline_details((Decline("blueprint", "bad_role", "x" * 10_000),))
    assert rendered is not None
    assert len(rendered) == len("bad_role: ") + _DECLINE_DETAIL_LIMIT


def test_an_entity_bearing_detail_is_carried_not_silently_dropped() -> None:
    """[X-decline-detail-bounded] The `totality_violation` decision, pinned so it stays a
    DECISION.

    That message interpolates `pred.value` — a literal out of the analyst's accepted SQL
    (`validation.py::_validate_totality`) — and it is the only decline detail that names a
    value. It is carried, under the D25 gate, for the reasons in `_decline_details`: this
    same span already ships `learning.accepted_sql` (the whole query, literals included)
    through the same gate, so it is not a new class of content; and excluding it by reason
    CODE would be a name-keyed guard that the next entity-bearing message upstream would
    silently inherit an exemption from.

    If a future reader decides to filter after all, this test is the one to change on
    purpose — the failure mode being guarded against is filtering it by ACCIDENT, or
    carrying it without anyone having decided to."""
    from data_agent.learning.consumer import ENTITY_BEARING_DECLINE_REASONS

    rendered = _decline_details(
        (
            Decline(
                "blueprint",
                "totality_violation",
                "predicate Department='Analytics' (table dbpcm_warehouse.employee) has "
                "no parameterization entry",
            ),
        )
    )
    assert rendered is not None
    assert "Analytics" in rendered
    # And the surface is inventoried, so an auditor can find it without reading validation.
    assert "totality_violation" in ENTITY_BEARING_DECLINE_REASONS


def test_many_ordinary_details_cannot_inflate_the_span_either() -> None:
    """[X-decline-detail-bounded] And a TOTAL cap after it: the per-item cap alone leaves
    the attribute unbounded in the number of declines, which is the same failure reached
    by a different route."""
    rendered = _decline_details(
        tuple(Decline("blueprint", "bad_role", "y" * 200) for _ in range(50))
    )
    assert rendered is not None
    assert len(rendered) == _DECLINE_DETAILS_TOTAL_LIMIT


# --- the rationale, for every candidate type -------------------------------------------


class _StubExtractor:
    """An extractor that returns a fixed `ExtractionResult`. Deliberately NOT the real
    one: the point here is what the CONSUMER puts on the span given a result, and routing
    a knowledge candidate through the real validator would couple this test to that
    validator's rules for a property that has nothing to do with them."""

    def __init__(self, result: ExtractionResult) -> None:
        self._result = result

    async def extract(self, summary, verdict) -> ExtractionResult:  # noqa: ARG002
        return self._result


def _knowledge_candidate(rationale: str) -> ExtractedCandidate:
    """A NON-blueprint candidate — payload is a plain dict, as S3 models the other three
    targets (`extractor/models.py::ExtractedCandidate`)."""
    return ExtractedCandidate(
        header=CandidateHeader(
            type="knowledge",
            confidence=0.8,
            evidence=(EvidenceRef(turn_ref="t0", tool_call_ref="tc1", quote="q"),),
            rationale=rationale,
            proposed_action="new",
            entity_self_check=EntitySelfCheck(contains_entities=False),
        ),
        payload={"kind": "definition", "text": "headcount excludes contractors"},
    )


async def test_a_knowledge_only_extraction_still_says_what_it_learned() -> None:
    """[X-decline-rationale-any] REGRESSION. The rationale was read inside the caller's
    `isinstance(payload, BlueprintPayload)` branch, so a session that extracted only
    knowledge emitted a verbose extract span with NO human-readable attribute at all —
    `candidate_count=1` and nothing to say what the 1 was. `rationale` lives on
    `CandidateHeader`, which every target has, so it is read there now.

    `intent`/`slots` stay absent, correctly: a knowledge candidate has neither."""
    tracer, exporter = _tracer_and_exporter()
    rationale = "the contractor exclusion is a reusable definition, not a one-off filter"
    consumer = LearningConsumer(
        store=object(),  # type: ignore[arg-type]
        queue=InMemoryLearningQueue(),
        settings=LearningSettings(_env_file=None, learning_trace_verbose=True),
        audit=InMemoryAuditStore(),
        candidates=InMemoryCandidateStore(),
        extractor=_StubExtractor(
            ExtractionResult(candidates=(_knowledge_candidate(rationale),))
        ),
        tracer=tracer,
    )
    await consumer._run_extractor(make_summary(), KEEP_VERDICT)

    attrs = dict(
        next(s for s in exporter.get_finished_spans() if s.name == "learning.extract").attributes
    )
    assert attrs["learning.extract.candidate_count"] == 1
    assert attrs["learning.extract.rationale"] == rationale
    assert "learning.extract.intent" not in attrs
    assert "learning.extract.slots" not in attrs
