"""`GeneralizeStage` seam behavior: it fills the additive `payload["generalization"]`
key from the envelope + the session tool trail, mutates no S3 field, always returns
`control="continue"` (fail_to_review flows on, D102 §7.1), and passes non-blueprint
envelopes straight through."""

from __future__ import annotations

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.extractor.models import ExtractedCandidate
from data_agent.learning.extractor.sql_predicates import literal_predicates
from data_agent.learning.extractor.validation import to_candidate
from data_agent.learning.generalize import GeneralizeStage
from data_agent.learning.generalize.stage import _collapse_designations
from data_agent.learning.generalize.validate import REASON_UNREWRITABLE
from data_agent.learning.stage import StageContext

from ..extractor.helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    make_answer_sql,
    make_summary,
    make_tool_call,
    param_inline,
    payroll_parameterization,
)
from .helpers import CATALOG, SINGLE_SQL, load_plan


def _ctx() -> StageContext:
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=SINGLE_SQL),))
    return StageContext(summary=summary, verdict=KEEP_VERDICT)


def _blueprint_env(payload: dict) -> CandidateEnvelope:
    return CandidateEnvelope(
        candidate_id="candidate::h::0", type="blueprint", status="extracted",
        payload=payload, source_session="sess-1", source_trace="trace-1",
        evidence_refs=(), extractor_rationale="r",
        entity_scan={"result": "pending"}, confidence=0.9, proposed_action="new",
        depends_on=(), content_hash="h",
    )


async def test_stage_fills_generalization_and_continues():
    plan = load_plan()["single"]
    env = _blueprint_env(dict(plan))
    stage = GeneralizeStage(catalog_schema=CATALOG)

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    gen = result.envelope.payload["generalization"]
    assert gen["static_validation"]["outcome"] == "ok"
    assert gen["sql_template"].endswith("region = {region}")
    # Additive only: every S3 field is untouched.
    for key in plan:
        assert result.envelope.payload[key] == plan[key]


async def test_the_accepted_sql_can_come_from_the_answer_designation():
    """Release 1: the plan's source ref is the `answerWithTable` call, and that query
    was never dispatched as a runQuery — every `tc.sql` in the session is None. The
    stage resolves refs through `summary/refs.py`, so S4 still has SQL to rewrite
    instead of failing to review with nothing named."""
    summary = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql=None, tool_name="answerWithTable"),),
        answer_sqls=(make_answer_sql(SINGLE_SQL, ref="tc1"),),
    )
    result = await GeneralizeStage(catalog_schema=CATALOG).process(
        _blueprint_env(dict(load_plan()["single"])),
        StageContext(summary=summary, verdict=KEEP_VERDICT),
    )
    gen = result.envelope.payload["generalization"]
    assert gen["static_validation"]["outcome"] == "ok"
    assert gen["sql_template"].endswith("region = {region}")


async def test_a_multi_table_ref_generalizes_from_its_last_designation():
    """One ref, two designated queries, one template: the LAST designation wins.

    THE FIXTURE'S PRECONDITION IS LOAD-BEARING and is asserted, not assumed: the
    earlier designation has NO literal predicates at all, so it is trivially a subset
    of the last one and the collapse is allowed. Change it to a query with a `WHERE`
    of its own and this test SHOULD start failing — see the sibling below."""
    unfiltered = "SELECT count(*) FROM payroll.payroll_fact"
    assert literal_predicates(unfiltered) == []  # the precondition, stated
    summary = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql=None, tool_name="answerWithTable"),),
        answer_sqls=(
            make_answer_sql(unfiltered, ref="tc1"),
            make_answer_sql(SINGLE_SQL, ref="tc1"),
        ),
    )
    result = await GeneralizeStage(catalog_schema=CATALOG).process(
        _blueprint_env(dict(load_plan()["single"])),
        StageContext(summary=summary, verdict=KEEP_VERDICT),
    )
    assert result.envelope.payload["generalization"]["static_validation"]["outcome"] == "ok"


async def test_a_designation_the_chosen_sql_does_not_constrain_refuses_to_collapse():
    """THE REGRESSION (found in review, reproduced live). Two designations under one
    ref: the first filters on `country`, the last does not. A plan whose only entry
    for `country` is `role=inline` passes S3 totality — `_validate_totality` counts
    ANY entry as coverage — while the rewrite is handed the LAST designation alone. So
    before the subset check S4 shipped `outcome: ok` with a template that has no country
    filter and a plan that claims one: a silently dropped filter, the D56 class.

    Both halves are asserted here, because the bug lives in the DISAGREEMENT between
    the two layers and either half alone reads as correct.

    WHICH LAYER REFUSES IS PART OF THE CLAIM. It is the COLLAPSE — asserted directly
    below — and it runs first. Since the H3 fix the strict rewrite would also refuse
    this plan (an inline literal absent from the SQL being rewritten now raises), so a
    test that only checked the outcome would keep passing with this check deleted. The
    collapse is still the layer that must hold it: it is the only one that can see the
    DISCARDED designation at all."""
    country_sql = "SELECT count(*) FROM payroll.payroll_fact WHERE country = 'IE'"
    summary = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql=None, tool_name="answerWithTable"),),
        answer_sqls=(make_answer_sql(country_sql, ref="tc1"),
                     make_answer_sql(SINGLE_SQL, ref="tc1")),
    )
    plan = [*payroll_parameterization(),
            param_inline("country", why="the report is Ireland-only", value="IE")]
    raw = blueprint_raw(source_refs=("tc1",), parameterization=plan)

    # S3 says yes: every predicate of both designations has SOME entry.
    accepted = to_candidate(raw, summary, known_rules=frozenset())
    assert isinstance(accepted, ExtractedCandidate)

    # The COLLAPSE is what refuses: it will not pick a query that drops `country`, so
    # the builder is handed no accepted SQL and never reaches the rewrite.
    assert _collapse_designations((country_sql, SINGLE_SQL)) is None

    # S4 refuses anyway — it will not rewrite from a query that drops `country`.
    result = await GeneralizeStage(catalog_schema=CATALOG).process(
        _blueprint_env(accepted.payload.to_doc()),
        StageContext(summary=summary, verdict=KEEP_VERDICT),
    )
    validation = result.envelope.payload["generalization"]["static_validation"]
    assert validation["outcome"] == "fail_to_review"
    assert validation["reason"] == REASON_UNREWRITABLE
    # No guessed template is shipped alongside the refusal.
    assert result.envelope.payload["generalization"]["sql_template"] is None
    assert result.control == "continue"  # S4 never drops; the writer routes it


async def test_stage_is_pure_original_envelope_unchanged():
    plan = load_plan()["single"]
    env = _blueprint_env(dict(plan))
    stage = GeneralizeStage(catalog_schema=CATALOG)

    await stage.process(env, _ctx())
    # The stage returns a NEW envelope; the input payload gains no key.
    assert "generalization" not in env.payload


async def test_non_blueprint_passes_through_untouched():
    env = CandidateEnvelope(
        candidate_id="candidate::h::1", type="global_knowledge", status="extracted",
        payload={"statement": "policy X"}, source_session="s", source_trace="t",
        evidence_refs=(), extractor_rationale="r", entity_scan={"result": "pending"},
        confidence=0.9, proposed_action="new", depends_on=(), content_hash="h",
    )
    stage = GeneralizeStage(catalog_schema=CATALOG)

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    assert result.envelope is env  # untouched, same object
    assert "generalization" not in result.envelope.payload
