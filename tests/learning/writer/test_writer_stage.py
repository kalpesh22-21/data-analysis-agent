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


async def test_default_writer_routes_every_mined_blueprint_to_human_review():
    env = _extracted(_reasons()["blueprint_sampled"])
    result = await WriterStage().process(env, _ctx())
    assert result.control == "route_inbox"
    assert result.envelope.status == CandidateStatus.IN_REVIEW


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


async def test_unsettled_scan_blueprint_does_not_auto_land():
    """S4: a blueprint whose `entity_scan` is UNSETTLED (S5 skipped — still S3's
    `pending` self-check) must NOT auto-land as `candidate`. Guard 0 stops promotion,
    but auto-landing would strand it forever and contradicts the §5 doc amendment —
    so the writer fail-closes it to `in_review` (reason `fail_to_review`)."""
    from data_agent.learning.candidate.verdicts import LeakageVerdict

    env = _extracted(_reasons()["blueprint_sampled"])  # clean, would auto-land
    # Reset the settled `pass` verdict back to S3's unsettled `pending` self-check.
    unsettled = replace(env, entity_scan={"result": "pending", "hits": []})
    assert not LeakageVerdict.is_settled(unsettled.entity_scan)

    writer = WriterStage(sampler=lambda _env: False)  # never sampled ⇒ would auto-land
    result = await writer.process(unsettled, _ctx())
    assert result.control == "route_inbox"
    assert result.envelope.status == CandidateStatus.IN_REVIEW
    assert result.envelope.status != CandidateStatus.CANDIDATE
    assert route_candidate(unsettled, sampled_for_inbox=False).reason == "fail_to_review"


async def test_schema_edit_without_pr_marker_fails_closed_never_auto_lands():
    """R8 stage-order guard: a `schema_edit` that reaches the terminal writer WITHOUT
    the `schema_edit_pr` stage's `schema_edit_review` marker means the PR bot was
    bypassed. It must fail-closed to the inbox (reason `fail_to_review`) — NEVER
    auto-land as a `candidate`, regardless of the sampling coin."""
    marked = _reasons()["schema_edit"]
    assert "schema_edit_review" in marked.payload  # the PR-processed shape derives `schema_edit`
    assert route_candidate(_extracted(marked), sampled_for_inbox=False).reason == "schema_edit"

    # Strip the marker: the PR stage never ran.
    unmarked = replace(
        _extracted(marked),
        payload={k: v for k, v in marked.payload.items() if k != "schema_edit_review"},
    )
    writer = WriterStage(sampler=lambda _env: False)  # would auto-land a clean blueprint
    result = await writer.process(unmarked, _ctx())
    assert result.control == "route_inbox"
    assert result.envelope.status == CandidateStatus.IN_REVIEW
    assert result.envelope.status != CandidateStatus.CANDIDATE  # never auto-retrievable
    assert route_candidate(unmarked, sampled_for_inbox=False).reason == "fail_to_review"


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
