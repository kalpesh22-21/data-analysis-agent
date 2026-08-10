"""S7 routing — the dedup action ALLOWLIST (PriorArt Slice 2, reviewer suggestion).

**History.** This was a DENYLIST: `_INBOX_DEDUP_ACTIONS = {"conflict", "merge"}` forced
those two to review and let every other action auto-land. That was safe only because the
two DROP actions (`increment`, and, from this slice, `redundant_with_canon`) never reach
the writer — S6 stops the pipeline on both, in the same in-process pass.

But `DedupVerdict.from_doc` rehydrates `action` with NO validation, so the guarantee is
a fact about another module's control flow rather than a property of this one. A
persisted envelope, a redelivery, or an envelope produced by a future/foreign writer
carrying `redundant_with_canon` would have routed as CLEAN and auto-landed a blueprint
the loop had just decided was redundant with the MCP canon.

Inverted: `insert` (or no verdict) auto-lands; EVERYTHING else goes to a human. An
unknown action becoming review noise is the cheap failure; an unknown action auto-landing
is not.

Slug: S7-dedup-routing-is-an-allowlist.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import DedupVerdict
from data_agent.learning.writer.routing import derive_inbox_reason, route_candidate


def _blueprint(action: str | None) -> CandidateEnvelope:
    """A blueprint that is clean on EVERY other routing axis, so the dedup action is the
    only thing that can send it to review."""
    env = CandidateEnvelope(
        candidate_id="candidate::x",
        type="blueprint",
        status=CandidateStatus.EXTRACTED,
        payload={
            "intent": "total earnings by department",
            "generalization": {"static_validation": {"outcome": "ok"}},
        },
        source_session="s",
        source_trace="t",
        evidence_refs=(),
        extractor_rationale="",
        entity_scan={"result": "pass", "hits": []},
        confidence=0.9,
        proposed_action="new",
        depends_on=(),
        content_hash="h",
    )
    if action is None:
        return env
    return replace(
        env,
        dedup=DedupVerdict(
            canonical_key="sha256:k",
            matched_id="bp-x",
            similarity=1.0,
            action=action,  # type: ignore[arg-type]
            layer="soft",
        ),
    )


def test_only_insert_auto_lands():
    decision = route_candidate(_blueprint("insert"), sampled_for_inbox=False)
    assert decision.status == CandidateStatus.CANDIDATE
    assert decision.control == "continue"
    assert decision.reason is None


def test_no_dedup_verdict_at_all_auto_lands():
    """`dedup is None` is clean by construction — a candidate that never ran S6. The
    other routing rules (static validation, leakage, sampling) still cover it."""
    decision = route_candidate(_blueprint(None), sampled_for_inbox=False)
    assert decision.status == CandidateStatus.CANDIDATE


@pytest.mark.parametrize("action", ["conflict", "merge"])
def test_the_soft_near_miss_actions_still_route_to_review(action):
    """The behaviour the denylist encoded must be unchanged — the inversion is about
    what happens to everything ELSE."""
    decision = route_candidate(_blueprint(action), sampled_for_inbox=False)
    assert decision.status == CandidateStatus.IN_REVIEW
    assert decision.reason == "dedup_conflict"


@pytest.mark.parametrize("action", ["increment", "redundant_with_canon"])
def test_a_rehydrated_drop_action_routes_to_review_instead_of_auto_landing(action):
    """THE regression this inversion exists to prevent. S6 drops both of these in-process
    so the writer should never see them — but `DedupVerdict.from_doc` does not validate
    `action`, so "should never" is a property of a different module. Under the old
    denylist this auto-landed a blueprint the loop had decided was redundant with the
    canon; now it reaches a human."""
    decision = route_candidate(_blueprint(action), sampled_for_inbox=False)
    assert decision.status == CandidateStatus.IN_REVIEW
    assert decision.control == "route_inbox"
    assert decision.reason == "dedup_conflict"


@pytest.mark.parametrize(
    "action", ["", "INSERT", "insert ", "supersede", "unknown_future_verdict", "None"]
)
def test_an_unrecognized_action_fails_to_review_not_to_auto_land(action):
    """Fail-closed on the unknown. Includes near-misses of the allowlisted value
    (casing, whitespace) because a rehydrated string is not normalized anywhere."""
    decision = route_candidate(_blueprint(action), sampled_for_inbox=False)
    assert decision.status == CandidateStatus.IN_REVIEW


def test_the_inbox_reason_agrees_with_the_routing_decision():
    """The module's stated invariant: `route_candidate` and `derive_inbox_reason` are ONE
    source of truth, so the writer's decision and the inbox's label can never disagree.
    A change to one and not the other is the failure mode."""
    for action in ("conflict", "merge", "increment", "redundant_with_canon", "weird"):
        env = replace(_blueprint(action), status=CandidateStatus.IN_REVIEW)
        assert route_candidate(env, sampled_for_inbox=False).reason == derive_inbox_reason(env)


def test_a_clean_insert_in_review_is_labelled_sampled_not_dedup_conflict():
    """The other side of the same invariant: the allowlist must not relabel a clean
    blueprint that reached `in_review` via the audit sample."""
    env = replace(_blueprint("insert"), status=CandidateStatus.IN_REVIEW)
    assert derive_inbox_reason(env) == "blueprint_sampled"
