"""Phase-3 PART A — the `verified` flag threads through the single landing sink so a
human-approve lands `verified=True` and the auto path lands `verified=False`.

Proven at BOTH levels:
  * the mapping seed builders honour the `verified` param (defaulting False, `source`
    staying `learning` either way);
  * the scheduler's ONE land-then-status edge stamps the landed node AND the store
    envelope to match — True on the human-approve path, False on the auto path.
"""

from __future__ import annotations

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.generalize.mapping import (
    blueprint_seed_from_candidate,
    knowledge_seed_from_candidate,
)
from data_agent.learning.promotion import PromotionScheduler

from .helpers import (
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
    with_type,
)

KEY = "sha256:single-bp"


def _knowledge_candidate(status: str):
    env = with_type(
        make_blueprint_candidate(status=status, canonical_key=KEY), "global_knowledge"
    )
    payload = dict(env.payload)
    payload["statement"] = "the fiscal year starts in April"
    payload["scope"] = "fiscal calendar"
    from dataclasses import replace

    return replace(env, payload=payload)


def _scheduler(store, *, writer):
    return PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 5}),
        policy=promotion_policy(),
        landing_writer=writer,
        require_landing=True,
        clock=lambda: "2026-08-01T00:00:00+00:00",
    )


# --- the mapping seed builders honour the param --------------------------------


def test_blueprint_seed_verified_param_defaults_false_and_keeps_source_learning() -> None:
    env = make_blueprint_candidate(canonical_key=KEY)
    default = blueprint_seed_from_candidate(env, id="bp::x")
    assert default.verified is False and default.source == "learning"
    verified = blueprint_seed_from_candidate(env, id="bp::x", verified=True)
    assert verified.verified is True and verified.source == "learning"


def test_knowledge_seed_verified_param_defaults_false_and_keeps_source_learning() -> None:
    env = _knowledge_candidate(CandidateStatus.IN_REVIEW)
    default = knowledge_seed_from_candidate(env, id="kn::x")
    assert default.verified is False and default.source == "learning"
    verified = knowledge_seed_from_candidate(env, id="kn::x", verified=True)
    assert verified.verified is True and verified.source == "learning"


# --- the scheduler stamps node + envelope to match -----------------------------


async def test_human_approve_lands_verified_true() -> None:
    """A human-approve of a blueprint lands `verified=True` on the node AND stamps the
    store envelope True (the two never diverge)."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, writer=writer)

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.action == "approve"
    assert writer.verified_flags == [True]  # the landed node got verified=True
    assert (await store.get(env.candidate_id)).verified is True


async def test_human_approve_knowledge_lands_verified_true() -> None:
    store = InMemoryCandidateStore()
    env = _knowledge_candidate(CandidateStatus.IN_REVIEW)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, writer=writer)

    await sched.apply_human_decision(env, "approve")

    assert writer.verified_flags == [True]
    assert (await store.get(env.candidate_id)).verified is True


async def test_the_cron_never_lands_anything_so_nothing_is_auto_verified() -> None:
    """WAS `test_auto_promote_lands_verified_false`, and the property it guarded is now
    unreachable rather than merely enforced.

    The concern was that the cron's auto-landing edge might stamp `verified=True` and
    silently claim a human vouched for a node nobody looked at. Plan §4 removed the edge:
    the cron routes to `in_review` and never calls `land`, so the only landing path is the
    human approve — which passes `verified=True` legitimately, because a human is
    standing right there. The assertion is re-pointed at the absence, since a
    re-introduced auto-land would restore the original hazard."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, writer=writer)

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert writer.verified_flags == []  # `land` was never called at all
    assert (await store.get(env.candidate_id)).verified is False
