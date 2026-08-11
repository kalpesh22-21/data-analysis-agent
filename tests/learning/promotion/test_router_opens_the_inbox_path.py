"""THE point of plan §4: a candidate can now actually reach a human.

Everything the learning loop built — prior-art search, the coverage judge, blueprint
references — fed a review inbox no candidate could reach. The auto gate required three
sessions to converge on a byte-identical normalized query (`sha256(resolves, uses_rules,
result_grain, canonical_ast_norm)`), which has never happened and realistically will not.
So the loop extracted, deduped, generalized and parked everything, forever.

These tests drive the WHOLE opened path with no infra: a first-sighting candidate goes
cron → `in_review` → visible in the projection → human approve → landed → `validated`.

Slugs:
  * S9-first-sighting-reaches-a-human   — the gate that never fired now fires at T=1.
  * S9-router-never-validates           — and the destination is a queue, not the corpus.
  * S9-approve-is-the-only-landing-edge
"""

from __future__ import annotations

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.promotion import PromotionScheduler

from .helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
    with_type,
)

KEY = "sha256:single-bp"
CLOCK = "2026-08-10T12:00:00+00:00"


def _plane(store, *, writer=None, hits=1):
    """A scheduler + an inbox over ONE store, at the SHIPPED policy."""
    scheduler = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: hits}),
        dependency_resolver=FakeDependencyResolver(),
        policy=promotion_policy(),
        landing_writer=writer,
        require_landing=writer is not None,
        clock=lambda: CLOCK,
    )
    return scheduler, ReviewInbox(store, scheduler=scheduler, policy=scheduler.policy)


async def test_a_single_sighting_candidate_reaches_the_review_queue():
    """THE regression this whole slice exists to prevent recurring. `hit_count == 1` is
    what `_seed_on_insert` writes for a first sighting; under the old threshold of 3 that
    was a permanent hold, and nothing in the system ever said so."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    scheduler, inbox = _plane(store)

    sweep = await scheduler.run_once()

    assert sweep.routed == 1
    assert sweep.promoted == 0  # the retired auto-land edge stays at zero, permanently
    items = await inbox.list()
    assert [i.candidate_id for i in items] == [env.candidate_id]


async def test_the_router_never_produces_validated_however_corroborated():
    """Landing into the graph happens only on a human approve. The drift re-check runs a
    golden replay — a token mint plus two live warehouse queries — against EVERY validated
    artifact, so auto-landing at a threshold of 1 would put hundreds of never-recalled
    nodes into that loop within weeks. And recall ignores the learning tier anyway, so
    auto-landing buys the agent nothing at all."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    scheduler, _ = _plane(store, writer=writer, hits=10_000)

    for _ in range(5):
        await scheduler.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    assert writer.landed == []


async def test_the_human_approve_completes_the_path_into_the_corpus():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    scheduler, inbox = _plane(store, writer=writer)

    await scheduler.run_once()
    approved = await inbox.approve(env.candidate_id)

    assert approved.status == CandidateStatus.VALIDATED
    assert len(writer.landed) == 1
    assert writer.verified_flags == [True]  # a human vouched for it


async def test_a_routed_candidate_leaves_the_candidate_scan():
    """A routed candidate must not be re-examined by the cron for ever: the scan reads
    `candidate` and `validated`, and `in_review` is neither. Without this the loop would
    pay the cheap guards (and, past the replay cache, a warehouse probe) on every parked
    review item for as long as a human took to look at it."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    scheduler, _ = _plane(store)

    first = await scheduler.run_once()
    second = await scheduler.run_once()

    assert [d.action for d in first.decisions] == ["route"]
    assert second.decisions == ()


async def test_a_human_gated_target_is_still_never_routed_by_the_cron():
    """`global_knowledge`/`schema_edit` are T=∞ (D58a/D18) and reach the inbox through
    the WRITER, not through this scheduler. Lowering the blueprint threshold must not
    have opened a second door into review for them — they would arrive without the
    knowledge pre-gate's reason and without S8's handling."""
    store = InMemoryCandidateStore()
    env = with_type(
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
        "global_knowledge",
    )
    await store.put(env)
    scheduler, _ = _plane(store)

    sweep = await scheduler.run_once()

    assert sweep.decisions[0].action == "hold"
    assert sweep.decisions[0].reason == "human_gated_target"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE


async def test_every_correctness_guard_still_blocks_the_route():
    """The threshold changed; the guards did not. Each of these must still HOLD at
    `candidate` — routing a leaky or un-replayable candidate to a human would be handing
    a reviewer something the machine already knows is broken, and (for the entity scan)
    would put unscanned payload in front of them."""
    async def _reason(**kwargs) -> str | None:
        store = InMemoryCandidateStore()
        env = make_blueprint_candidate(
            status=CandidateStatus.CANDIDATE, canonical_key=KEY, **kwargs
        )
        await store.put(env)
        scheduler, _ = _plane(store)
        return (await scheduler.run_once()).decisions[0].reason

    assert await _reason(static_ok=False) == "static_not_ok"
    assert await _reason(depends_on=("schema_edit::x",)) == "depends_on_unresolved"
