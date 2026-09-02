"""ADVERSARIAL: the fail-to-review ROUTE, taken as a matrix rather than as a list of
examples, plus the three seams the builder's suite drives around instead of through.

`_persist_declined_for_review` has exactly two conditions — a `proceeded` judgement and a
reason a human can act on — and the whole value of the slice depends on both staying
narrow. The builder's `test_consumer_fail_to_review.py` pins one representative of each
side. What is added here:

  * **the full product** of {judge outcome} × {decline reason}, so a widened condition
    (an extra reason, a `skipped_*` treated as merit) fails a named cell rather than
    slipping through the gaps between representatives;
  * **the REAL consume path.** Every existing test calls `_run_extractor` directly with a
    hand-built `JudgeOutcomeResult`. Nothing proved the consumer actually threads its own
    judgement into that argument — a `_run_extractor(summary, verdict)` left behind at the
    call site would keep all thirteen of them green while the route never fired in
    production;
  * **the seams around it**: a session that produces a kept candidate AND a parked one,
    the span the pair emits, the supersede that clears a stale form when a later run
    succeeds, and the leakage gate standing in front of the persist.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.audit.judgement import CoverageAssessment
from data_agent.learning.candidate import (
    InMemoryCandidateStore,
    mint_candidate_id,
    mint_review_candidate_id,
)
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.config import LearningSettings
from data_agent.learning.consumer import (
    REVIEW_ROUTED_DECLINE_REASONS,
    LearningConsumer,
)
from data_agent.learning.extractor.validation import (
    REASON_RULE_MISMATCH,
    REASON_TOTALITY,
)
from data_agent.learning.inbox.models import InboxItem
from data_agent.learning.judge import (
    OUTCOME_DROPPED,
    OUTCOME_FAILED,
    OUTCOME_PROCEEDED,
    OUTCOME_RECORD_WRITE_FAILED,
    OUTCOME_SKIPPED_ABOVE_BAND,
    OUTCOME_SKIPPED_BELOW_FLOOR,
    OUTCOME_SKIPPED_NO_PRIOR_ART,
    OUTCOME_SKIPPED_UNAVAILABLE,
    JudgeOutcomeResult,
)
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.memory_queue import InMemoryLearningQueue
from data_agent.learning.models import LearningStatus
from data_agent.learning.triage import TriageVerdict
from tests._catalog_fixture import fixture_catalog

from .extractor.helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    make_extractor,
    make_summary,
    make_tool_call,
    make_turn,
    scripted_turn,
)
from .extractor.test_totality_hint import _INDEX, _KNOWN, _RATIO_SQL, _UNCOVERED

assert fixture_catalog()  # the grounding both the extractor and the assertions use

_PAYROLL = "dbpcm_warehouse.payroll"


# --- the candidates that produce each decline reason -------------------------------------


def _mismatched_raw() -> dict:
    """`rule_predicate_mismatch`: the entries cite a REAL catalog rule the catalog proves
    is a different filter (`gross_earnings` is 'EARN', not 'DDUCT')."""
    return blueprint_raw(
        intent="deductions per employee",
        source_refs=("tc1",),
        parameterization=[
            {
                "locator": {"table": _PAYROLL, "column": "register_type", "value": "DDUCT,EARN"},
                "role": "inline",
                "why": "the ratio is defined over these two register types",
            },
            {
                "locator": {"table": _PAYROLL, "column": "register_type", "value": "DDUCT"},
                "role": "rule",
                "rule_id": "gross_earnings",
            },
            {
                "locator": {"table": _PAYROLL, "column": "register_type", "value": "EARN"},
                "role": "rule",
                "rule_id": "gross_earnings",
            },
        ],
    )


def _missing_rule_raw() -> dict:
    """`missing_rule` / `missing_rule_hinted`: the plan cites a rule id the catalog does
    not have. DELIBERATELY out of the route (decision doc, still-open question) — its
    value is as the §7 signal that a human must ADD a rule, which is a different action
    from completing a form."""
    return blueprint_raw(
        intent="deductions per employee",
        source_refs=("tc1",),
        parameterization=[
            {
                "locator": {"table": _PAYROLL, "column": "register_type", "value": "DDUCT,EARN"},
                "role": "rule",
                "rule_id": "no_such_rule_in_the_catalog",
            }
        ],
    )


def _no_evidence_raw() -> dict:
    """`no_evidence`: D31, and it is SUPPOSED to die — there is nothing a human could
    complete."""
    return blueprint_raw(evidence=[], source_refs=("tc1",))


def _bad_role_raw() -> dict:
    """`role_inconsistent`: an `inline` entry with no `why`."""
    return blueprint_raw(
        source_refs=("tc1",),
        parameterization=[
            {
                "locator": {"table": _PAYROLL, "column": "register_type", "value": "DDUCT,EARN"},
                "role": "inline",
            }
        ],
    )


_RAW_BY_INTENDED_REASON = {
    REASON_TOTALITY: _UNCOVERED,
    REASON_RULE_MISMATCH: _mismatched_raw(),
    "missing_rule": _missing_rule_raw(),
    "no_evidence": _no_evidence_raw(),
    "role_inconsistent": _bad_role_raw(),
}

_JUDGE_OUTCOMES = [
    OUTCOME_PROCEEDED,
    OUTCOME_DROPPED,
    OUTCOME_FAILED,
    OUTCOME_RECORD_WRITE_FAILED,
    OUTCOME_SKIPPED_ABOVE_BAND,
    OUTCOME_SKIPPED_BELOW_FLOOR,
    OUTCOME_SKIPPED_NO_PRIOR_ART,
    OUTCOME_SKIPPED_UNAVAILABLE,
    "not_judged",
]


def _summary(**kwargs):
    return make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql=_RATIO_SQL),),
        turns=(make_turn(0),),
        **kwargs,
    )


def _judgement(outcome: str) -> JudgeOutcomeResult:
    return JudgeOutcomeResult(
        drop=False,
        outcome=outcome,
        assessment=CoverageAssessment(
            verdict="existing-plus-delta",
            covered_by="bp-total-earnings-by-department",
            covered_by_tier="mcp",
            reason="the closest artifact covers total earnings only",
            confidence=0.86,
        ),
    )


def _consumer(candidates, *, turns, stages=(), judge=None, tracer=None):
    return LearningConsumer(
        store=object(),  # type: ignore[arg-type]
        queue=InMemoryLearningQueue(),
        settings=LearningSettings(_env_file=None),
        audit=InMemoryAuditStore(),
        candidates=candidates,
        extractor=make_extractor(turns, known_rules=_KNOWN, rule_index=_INDEX),
        stages=stages,
        judge=judge,
        tracer=tracer,
    )


def _repeated(raw: dict, n: int = 3):
    """The live shape: the model emits, is corrected twice, and repeats itself."""
    return [scripted_turn([raw]) for _ in range(n)]


# --- the matrix ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", _JUDGE_OUTCOMES)
@pytest.mark.parametrize("reason", sorted(_RAW_BY_INTENDED_REASON))
async def test_the_gating_matrix(outcome: str, reason: str) -> None:
    """{9 judge outcomes} × {5 decline reasons} = 45 cells, and exactly two of them
    persist.

    The expectation is COMPUTED from the two conditions rather than tabulated, so the
    test states the rule instead of a list a future reader would have to diff against the
    code. `dropped` appears among the outcomes even though a real drop never reaches the
    extractor — if it ever did (a refactor that stopped honouring `drop` at the call
    site), it must not be mistaken for merit here as well."""
    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=_repeated(_RAW_BY_INTENDED_REASON[reason]))

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _judgement(outcome))

    stored = candidates.all_candidates()
    should_persist = outcome != OUTCOME_DROPPED and reason in REVIEW_ROUTED_DECLINE_REASONS
    assert bool(stored) is should_persist, (
        f"judge={outcome!r} reason={reason!r} persisted={[e.status for e in stored]}"
    )
    if should_persist:
        expected_status = (
            CandidateStatus.NEEDS_PARAMETERIZATION
            if outcome == OUTCOME_PROCEEDED
            else CandidateStatus.AWAITING_JUDGE
        )
        assert stored[0].status == expected_status
        assert stored[0].decline is not None
        assert stored[0].decline.reason == reason


@pytest.mark.parametrize("reason", sorted(_RAW_BY_INTENDED_REASON))
async def test_each_matrix_fixture_really_declines_for_the_reason_it_is_named_for(
    reason: str,
) -> None:
    """The matrix above is only as good as its inputs. A fixture that quietly started
    declining for a DIFFERENT reason would turn a "this reason is excluded" cell into a
    tautology — the row would not persist, and not for the reason the cell claims. This
    test is what keeps the 45 cells honest.

    `missing_rule` is matched by PREFIX because the hint machinery renames it
    `missing_rule_hinted` when the catalog can suggest a pairing; both are the same
    exclusion and neither is in the route."""
    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=_repeated(_RAW_BY_INTENDED_REASON[reason]))

    result = await consumer._extractor.extract(_summary(), KEEP_VERDICT)

    assert result.candidates == ()
    assert result.declines, f"{reason!r} fixture produced no decline at all"
    assert result.declines[-1].reason.startswith(reason)


def test_the_route_takes_exactly_two_reasons() -> None:
    """The set itself, pinned. `missing_rule` is the one a future reader will be tempted
    to add: the decision doc leaves it open and explicitly leaves it OUT, because routing
    it would answer a "add a rule to the catalog" signal with a "complete this form"
    action and blur the count the pairing work is prioritized from."""
    assert REVIEW_ROUTED_DECLINE_REASONS == {REASON_TOTALITY, REASON_RULE_MISMATCH}


async def test_a_decline_with_no_raw_payload_is_not_persistable() -> None:
    """The third, unwritten condition: there must be something to review. A decline
    raised before the payload was readable carries no `raw_payload`, and persisting one
    would put an empty form in front of a person."""
    from dataclasses import replace as dc_replace

    from data_agent.learning.extractor.models import Decline, ExtractionResult

    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=_repeated(_UNCOVERED))
    bare = Decline(
        type="blueprint",
        reason=REASON_TOTALITY,
        detail="no entry for 3 predicates",
        correctable=False,
        raw_payload=None,
    )

    async def _extract(summary, verdict):
        return ExtractionResult(candidates=(), declines=(bare,), corrections=2)

    consumer._extractor = type("_E", (), {"extract": staticmethod(_extract)})()  # type: ignore[assignment]
    await consumer._run_extractor(_summary(), KEEP_VERDICT, _judgement(OUTCOME_PROCEEDED))

    assert candidates.put_calls == 0
    assert dc_replace(bare, raw_payload={}) is not None  # the shape exists, it is empty


# --- the REAL consume path ------------------------------------------------------------------


class _StubJudge:
    """A judge stand-in that returns a fixed result (or explodes). The scripted
    `CoverageJudge` is exercised in `tests/learning/judge/`; what this suite needs is
    control of the OUTCOME LABEL at the consumer's own seam."""

    def __init__(self, result: JudgeOutcomeResult | None = None, *, boom: bool = False) -> None:
        self._result = result
        self._boom = boom
        self.calls = 0

    async def screen_session(self, summary):
        self.calls += 1
        if self._boom:
            raise RuntimeError("judge exploded")
        return self._result


async def _drive(consumer: LearningConsumer, summary) -> None:
    """Run the consumer's real KEEP path (`_do_work`) — triage → judge → extractor —
    rather than reaching past it into `_run_extractor`."""

    class _Doc:
        learning_status = LearningStatus.PROCESSING

    class _Delivered:
        job = None

    async def _loader(doc, store, *, job):
        return summary

    consumer._summary_loader = _loader  # type: ignore[assignment]
    consumer._triage = lambda s: TriageVerdict(  # type: ignore[assignment]
        decision="keep", reason="K1", target_hints=("blueprint",)
    )
    await consumer._do_work(_Doc(), _Delivered())  # type: ignore[arg-type]


async def test_the_consume_path_actually_threads_its_own_judgement_through() -> None:
    """THE WIRING TEST. Every other fail-to-review test hands `_run_extractor` a
    judgement it built itself; this one makes the consumer produce its own and proves the
    route fires end to end from `_do_work`. Without it, a call site that dropped the
    argument would leave the whole slice dead in production and the suite green."""
    candidates = InMemoryCandidateStore()
    judge = _StubJudge(_judgement(OUTCOME_PROCEEDED))
    consumer = _consumer(
        candidates,
        turns=_repeated(_UNCOVERED),
        stages=(LeakageGateStage(candidate_store=candidates),),
        judge=judge,
    )

    await _drive(consumer, _summary(content_hash="hash-live"))

    assert judge.calls == 1
    stored = candidates.all_candidates()
    assert len(stored) == 1
    assert stored[0].status == CandidateStatus.NEEDS_PARAMETERIZATION
    assert stored[0].candidate_id == mint_review_candidate_id("hash-live", 0)
    # The judge's own assessment travelled onto the envelope — the reason this row is in
    # front of a person rather than in the bin.
    assert stored[0].judge is not None
    assert stored[0].judge.covered_by == "bp-total-earnings-by-department"


async def test_a_judge_that_raises_on_the_real_path_preserves_retry_work() -> None:
    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=_repeated(_UNCOVERED), judge=_StubJudge(boom=True))

    await _drive(consumer, _summary())

    assert candidates.all_candidates()[0].status == CandidateStatus.AWAITING_JUDGE


async def test_no_judge_wired_on_the_real_path_preserves_retry_work() -> None:
    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=_repeated(_UNCOVERED), judge=None)

    await _drive(consumer, _summary())

    assert candidates.all_candidates()[0].status == CandidateStatus.AWAITING_JUDGE


async def test_a_judge_drop_never_reaches_the_review_route() -> None:
    """A dropped session does not extract at all, so there is no decline to route. Pinned
    because the drop branch and the review branch now read the same result object."""
    candidates = InMemoryCandidateStore()
    judge = _StubJudge(JudgeOutcomeResult(drop=True, outcome=OUTCOME_DROPPED))
    consumer = _consumer(candidates, turns=_repeated(_UNCOVERED), judge=judge)

    await _drive(consumer, _summary())

    assert candidates.put_calls == 0


# --- a session that produces BOTH ---------------------------------------------------------------


def _kept_and_declined():
    """One well-formed candidate the pipeline keeps, and one that dies on the form. The
    kept one is built on the payroll SQL of `tc2`, the declined one on the ratio SQL of
    `tc1`, so both are grounded in the same session."""
    from .extractor.helpers import PAYROLL_SQL, payroll_parameterization

    kept = blueprint_raw(
        intent="total earnings for a department in a given year",
        source_refs=("tc2",),
        parameterization=payroll_parameterization(),
        evidence=[{"turn_ref": 0, "tool_call_ref": "tc2", "quote": "total earnings"}],
    )
    return kept, PAYROLL_SQL


async def test_a_session_that_extracts_and_parks_keeps_the_two_rows_apart() -> None:
    """The id collision the separate ordinal namespace exists to prevent, tested rather
    than argued: the kept candidate is minted from `enumerate(result.candidates)` and the
    declined one from its own namespace, so `candidate::<hash>::0` and
    `candidate::<hash>::review-0` are two different documents and neither overwrites the
    other."""
    from .extractor.helpers import make_tool_call as _tc

    kept, payroll_sql = _kept_and_declined()
    candidates = InMemoryCandidateStore()
    summary = make_summary(
        tool_calls=(_tc(ref="tc1", sql=_RATIO_SQL), _tc(ref="tc2", sql=payroll_sql)),
        turns=(make_turn(0, tool_call_refs=("tc1", "tc2")),),
        content_hash="hash-both",
    )
    consumer = _consumer(
        candidates,
        # The corrective rounds re-emit only the candidate that failed — the shape the
        # correction prompt asks for.
        turns=[scripted_turn([kept, _UNCOVERED]), *_repeated(_UNCOVERED, 2)],
        stages=(LeakageGateStage(candidate_store=candidates),),
    )

    await consumer._run_extractor(summary, KEEP_VERDICT, _judgement(OUTCOME_PROCEEDED))

    by_id = {env.candidate_id: env for env in candidates.all_candidates()}
    assert set(by_id) == {
        mint_candidate_id("hash-both", 0),
        mint_review_candidate_id("hash-both", 0),
    }
    kept_env = by_id[mint_candidate_id("hash-both", 0)]
    parked = by_id[mint_review_candidate_id("hash-both", 0)]
    assert kept_env.status == CandidateStatus.EXTRACTED
    # The DECLINE is what separates the two rows; the snapshot is on BOTH now, because
    # a kept candidate now carries its own `ValidationSnapshot` (`build_candidate_envelope`), and a completion no longer clears it — that is what makes the review queue editable at all (design §C.4).
    assert kept_env.decline is None
    assert kept_env.revalidation is not None
    assert parked.status == CandidateStatus.NEEDS_PARAMETERIZATION
    assert parked.decline is not None
    # Two rows about DIFFERENT work: the parked form is not a copy of the kept one.
    assert kept_env.payload["intent"] != parked.payload["intent"]


def _tracer_and_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("learning-loop-test"), exporter


def _extract_attrs(exporter) -> dict:
    for span in exporter.get_finished_spans():
        if "learning.extract.outcome" in dict(span.attributes or {}):
            return dict(span.attributes)
    raise AssertionError("no extract span was exported")


async def test_the_span_reports_the_parked_item_the_consumer_actually_wrote() -> None:
    """The telemetry, wired rather than called. `test_extract_span_decline_details.py`
    proves the span function's three-way outcome; this proves the CONSUMER feeds it the
    count it really persisted — the number the decision doc §5 asks for is worthless if
    it is computed anywhere but at the write."""
    tracer, exporter = _tracer_and_exporter()
    candidates = InMemoryCandidateStore()
    consumer = _consumer(
        candidates,
        turns=_repeated(_UNCOVERED),
        stages=(LeakageGateStage(candidate_store=candidates),),
        tracer=tracer,
    )

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _judgement(OUTCOME_PROCEEDED))

    attrs = _extract_attrs(exporter)
    assert attrs["learning.extract.outcome"] == "declined_to_review"
    assert attrs["learning.extract.review_count"] == 1
    assert attrs["learning.extract.candidate_count"] == 0  # the §7 counts stay honest
    assert attrs["learning.extract.decline_reasons"] == REASON_TOTALITY


async def test_the_span_says_declined_to_review_when_judgement_is_pending() -> None:
    tracer, exporter = _tracer_and_exporter()
    candidates = InMemoryCandidateStore()
    consumer = _consumer(candidates, turns=_repeated(_UNCOVERED), tracer=tracer)

    await consumer._run_extractor(_summary(), KEEP_VERDICT, None)

    attrs = _extract_attrs(exporter)
    assert attrs["learning.extract.outcome"] == "declined_to_review"
    assert attrs["learning.extract.review_count"] == 1


# --- supersede: the stale form must not outlive the session that produced it -------------------


async def test_a_later_successful_run_clears_the_stale_review_item() -> None:
    """The case the builder's re-processing test cannot see. Its three runs all decline,
    so the ONE row it counts is explained by the deterministic id overwriting itself —
    `supersede` could be a no-op and it would still pass. Here the second run SUCCEEDS and
    writes a different id, so only a real content-hash sweep removes the form. Otherwise
    a reviewer is asked, for a hundred and eighty days, to fill in a blank the model has already
    filled."""
    from .extractor.helpers import PAYROLL_SQL, payroll_parameterization
    from .extractor.helpers import make_tool_call as _tc

    candidates = InMemoryCandidateStore()
    summary = make_summary(
        tool_calls=(_tc(ref="tc1", sql=PAYROLL_SQL),),
        turns=(make_turn(0),),
        content_hash="hash-retry",
    )

    declining = _consumer(
        candidates,
        turns=[
            scripted_turn([blueprint_raw(parameterization=payroll_parameterization()[:3])])
            for _ in range(3)
        ],
    )
    await declining._run_extractor(summary, KEEP_VERDICT, _judgement(OUTCOME_PROCEEDED))
    assert [e.status for e in candidates.all_candidates()] == [
        CandidateStatus.NEEDS_PARAMETERIZATION
    ]

    succeeding = _consumer(
        candidates,
        turns=[scripted_turn([blueprint_raw(parameterization=payroll_parameterization())])],
    )
    await succeeding._run_extractor(summary, KEEP_VERDICT, _judgement(OUTCOME_PROCEEDED))

    stored = candidates.all_candidates()
    assert len(stored) == 1
    assert stored[0].candidate_id == mint_candidate_id("hash-retry", 0)
    assert stored[0].status == CandidateStatus.EXTRACTED
    assert stored[0].decline is None


# --- the leakage side door, at the persist ---------------------------------------------------------


async def test_an_entity_in_a_declined_payload_is_caught_before_the_row_is_written() -> None:
    """Spec §3. A decline must not become a side door around the entity scan: the SAME
    wired gate that runs for a kept candidate runs over the built envelope BEFORE the
    persist, so an entity in the payload the model could not parameterize is a settled
    `quarantine` on the stored row — and the wire then withholds the decline detail on the
    strength of that verdict."""
    candidates = InMemoryCandidateStore()
    leaky = blueprint_raw(
        intent="deductions to earnings ratio for Jane Doe in EMEA",
        source_refs=("tc1",),
        parameterization=[],
    )
    consumer = _consumer(
        candidates,
        turns=_repeated(leaky),
        stages=(LeakageGateStage(candidate_store=candidates),),
    )

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _judgement(OUTCOME_PROCEEDED))

    env = candidates.all_candidates()[0]
    assert env.status == CandidateStatus.NEEDS_PARAMETERIZATION
    assert env.entity_scan["result"] == "quarantine"
    assert env.entity_scan["scanner"] == "regex+ner"
    assert [h["kind"] for h in env.entity_scan["hits"]]

    # ...and the browser-facing projection fails closed on it.
    view = InboxItem.from_envelope(env).decline_view()
    assert view["detail_withheld"] is True
    assert view["detail"] == ""


async def test_the_gate_does_not_get_to_reroute_the_form_out_of_the_queue() -> None:
    """`_scan_declined` takes the gate's VERDICT and not its routing, deliberately: the
    stage's status decisions are written for a candidate flowing toward a landing and this
    one is flowing toward a form. A quarantine must therefore leave the row exactly where
    a reviewer will find it — `needs_parameterization`, not `rejected`, not `in_review`."""
    candidates = InMemoryCandidateStore()
    leaky = blueprint_raw(intent="ratio for Jane Doe", source_refs=("tc1",), parameterization=[])
    consumer = _consumer(
        candidates,
        turns=_repeated(leaky),
        stages=(LeakageGateStage(candidate_store=candidates),),
    )

    await consumer._run_extractor(_summary(), KEEP_VERDICT, _judgement(OUTCOME_PROCEEDED))

    env = candidates.all_candidates()[0]
    assert env.status == CandidateStatus.NEEDS_PARAMETERIZATION
    assert env.decline is not None and env.decline.reason == REASON_TOTALITY
