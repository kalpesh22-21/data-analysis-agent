"""S7 writer — the write-router stage routing on verdicts + type (Slice 7).

Slug:
  * S7-knowledge-schema-human-pregate — ALL global_knowledge + schema_edit route to
    the inbox (status=in_review), never auto-retrievable (D58a/D18).

Also exercises the blueprint auto-land / sample / fail_to_review / dedup-conflict /
leakage-near-miss routing against `envelopes_each_reason.json`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.learning.writer import WriterStage, route_candidate

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _reasons() -> dict[str, CandidateEnvelope]:
    return {k: CandidateEnvelope.from_doc(v) for k, v in _load("envelopes_each_reason.json").items()}


def _ctx() -> StageContext:
    summary = SessionSummary(
        session_id="s", user_id="u", scope_ref="sc", trace_id="t", content_hash="h",
        turns=(), tool_calls=(), blueprint_usages=(), askuser_exchanges=(),
        failed_fixed_sql=(), accepted_signal="no_correction",
    )
    return StageContext(summary=summary, verdict=TriageVerdict(decision="keep", reason="K1"))


def _extracted(env: CandidateEnvelope) -> CandidateEnvelope:
    """Reset a fixture (which is stored at in_review) back to the extracted status
    the writer actually receives, so the writer's own transition is what we test."""
    return replace(env, status=CandidateStatus.EXTRACTED)


# --- S7-knowledge-schema-human-pregate ----------------------------------------


async def test_all_global_knowledge_and_schema_edit_route_to_in_review():
    """S7-knowledge-schema-human-pregate: EVERY global_knowledge and schema_edit
    candidate goes to the inbox at status=in_review — never candidate, never
    auto-retrievable — regardless of a clean entity_scan / absent dedup."""
    reasons = _reasons()
    # A sampler that would auto-land blueprints (never sample) — proving the
    # knowledge/schema routing is unconditional, not a sampling artifact.
    writer = WriterStage(sampler=lambda _env: False)

    for key, expected_reason in (("knowledge_pre_gate", "knowledge_pre_gate"),
                                 ("schema_edit", "schema_edit")):
        env = _extracted(reasons[key])
        result = await writer.process(env, _ctx())
        assert result.control == "route_inbox"
        assert result.envelope.status == CandidateStatus.IN_REVIEW
        # never candidate / validated ⇒ not auto-retrievable
        assert result.envelope.status != CandidateStatus.CANDIDATE
        decision = route_candidate(env, sampled_for_inbox=False)
        assert decision.reason == expected_reason


async def test_clean_blueprint_unsampled_auto_lands_as_candidate():
    env = _extracted(_reasons()["blueprint_sampled"])  # a clean pass blueprint
    writer = WriterStage(sampler=lambda _env: False)  # never sampled
    result = await writer.process(env, _ctx())
    assert result.control == "continue"
    assert result.envelope.status == CandidateStatus.CANDIDATE


async def test_clean_blueprint_sampled_routes_to_inbox_blueprint_sampled():
    env = _extracted(_reasons()["blueprint_sampled"])
    writer = WriterStage(sampler=lambda _env: True)  # forced sample
    result = await writer.process(env, _ctx())
    assert result.control == "route_inbox"
    assert result.envelope.status == CandidateStatus.IN_REVIEW
    assert route_candidate(env, sampled_for_inbox=True).reason == "blueprint_sampled"


async def test_leakage_near_miss_always_inbox_even_when_unsampled():
    """100% of leakage near-misses go to the inbox, never sampled out (D58b)."""
    env = _extracted(_reasons()["leakage_near_miss"])
    writer = WriterStage(sampler=lambda _env: False)
    result = await writer.process(env, _ctx())
    assert result.control == "route_inbox"
    assert result.envelope.status == CandidateStatus.IN_REVIEW
    assert route_candidate(env, sampled_for_inbox=False).reason == "leakage_near_miss"


async def test_dedup_conflict_routes_to_inbox():
    env = _extracted(_reasons()["dedup_conflict"])
    writer = WriterStage(sampler=lambda _env: False)
    result = await writer.process(env, _ctx())
    assert result.control == "route_inbox"
    assert result.envelope.status == CandidateStatus.IN_REVIEW
    assert route_candidate(env, sampled_for_inbox=False).reason == "dedup_conflict"


async def test_fail_to_review_routes_to_inbox():
    env = _extracted(_reasons()["fail_to_review"])
    writer = WriterStage(sampler=lambda _env: False)
    result = await writer.process(env, _ctx())
    assert result.control == "route_inbox"
    assert result.envelope.status == CandidateStatus.IN_REVIEW
    assert route_candidate(env, sampled_for_inbox=False).reason == "fail_to_review"


async def test_fail_to_review_precedence_over_sampling():
    """A fail_to_review blueprint routes to inbox for the fail_to_review reason even
    when the sampler is False (precedence: static-validation before the sample)."""
    env = _extracted(_reasons()["fail_to_review"])
    assert route_candidate(env, sampled_for_inbox=False).reason == "fail_to_review"


async def test_soft_merge_action_routes_to_inbox_as_dedup_conflict():
    """A soft-layer `merge` verdict (a mergeable variant) is never auto-appended:
    it routes to the inbox under the `dedup_conflict` reason ('soft conflict/variant')."""
    from data_agent.learning.candidate.verdicts import DedupVerdict

    env = _extracted(_reasons()["blueprint_sampled"])
    env = replace(env, dedup=DedupVerdict(
        canonical_key="sha256:k", matched_id="blueprint::x",
        similarity=0.97, action="merge", layer="soft"))
    decision = route_candidate(env, sampled_for_inbox=False)
    assert decision.status == CandidateStatus.IN_REVIEW
    assert decision.control == "route_inbox"
    assert decision.reason == "dedup_conflict"
