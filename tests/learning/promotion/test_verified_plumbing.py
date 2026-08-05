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
from data_agent.learning.promotion import PromotionPolicy, PromotionScheduler

from .helpers import (
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
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
        policy=PromotionPolicy(blueprint_hit_threshold=3),
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


async def test_auto_promote_lands_verified_false() -> None:
    """The cron auto-promotion edge lands `verified=False` on the node AND leaves the
    store envelope False — an auto-landed node is never silently verified."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, writer=writer)

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "promote"
    assert writer.verified_flags == [False]  # the landed node got verified=False
    assert (await store.get(env.candidate_id)).verified is False
