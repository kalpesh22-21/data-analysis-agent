"""depends_on-unresolved-stays-candidate (contracts-design §11.6, D35).

A candidate whose `depends_on` references an unresolved artifact (e.g. a blueprint
depending on a not-yet-landed `schema_edit(add_rule)`) STAYS `candidate` — S9
refuses to promote it, even with `hit_count ≥ T` and a green replay, until every
dependency resolves. Also covers the human-gated / auto-promotable-target routing.
"""

from __future__ import annotations

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.promotion import PromotionScheduler

from .helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
    with_type,
)

KEY = "sha256:single-bp"
DEP = "schema_edit::add_rule::earning_record_type"


def _scheduler(store, *, resolved):
    return PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 5}),  # well above T
        dependency_resolver=FakeDependencyResolver(resolved),
        policy=promotion_policy(),
        clock=lambda: "2026-07-03T12:00:00+00:00",
    )


async def test_unresolved_dependency_stays_candidate():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.CANDIDATE, canonical_key=KEY, depends_on=(DEP,)
    )
    await store.put(env)
    sched = _scheduler(store, resolved=set())  # dep NOT landed

    sweep = await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE
    assert sweep.decisions[0].action == "hold"
    assert sweep.decisions[0].reason == "depends_on_unresolved"


async def test_resolved_dependency_allows_routing_to_review():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.CANDIDATE, canonical_key=KEY, depends_on=(DEP,)
    )
    await store.put(env)
    sched = _scheduler(store, resolved={DEP})  # dep landed

    sweep = await sched.run_once()

    # Plan §4: the guard's job is unchanged (a resolved dependency stops blocking); only
    # the destination moved from `validated` to the human review queue.
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    assert sweep.decisions[0].action == "route"


async def test_missing_resolver_fails_closed_when_deps_present():
    """A candidate with deps but NO resolver injected cannot be verified ⇒
    fail-closed (stays candidate), never promoted on an unverifiable dependency."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.CANDIDATE, canonical_key=KEY, depends_on=(DEP,)
    )
    await store.put(env)
    sched = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 5}),
        dependency_resolver=None,
        policy=promotion_policy(),
    )

    await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE


async def test_human_gated_target_never_auto_promotes():
    """A global_knowledge candidate (T=∞, D58a) never auto-promotes by count."""
    store = InMemoryCandidateStore()
    env = with_type(
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
        "global_knowledge",
    )
    await store.put(env)
    sched = _scheduler(store, resolved=set())

    sweep = await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE
    assert sweep.decisions[0].reason == "human_gated_target"
