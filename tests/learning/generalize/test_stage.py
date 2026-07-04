"""`GeneralizeStage` seam behavior: it fills the additive `payload["generalization"]`
key from the envelope + the session tool trail, mutates no S3 field, always returns
`control="continue"` (fail_to_review flows on, D102 §7.1), and passes non-blueprint
envelopes straight through."""

from __future__ import annotations

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.generalize import GeneralizeStage
from data_agent.learning.stage import StageContext

from ..extractor.helpers import KEEP_VERDICT, make_summary, make_tool_call
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
