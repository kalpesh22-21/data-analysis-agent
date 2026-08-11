"""Layer-1 — `S9-promotion-gated-on-landing` (S9-activation Slice 1, §4).

The landing gate: a blueprint that passes EVERY guard — including the real replay gate —
must HOLD `landing_unavailable` rather than becoming a `validated` artifact nothing can
recall.

**PLAN §4 narrowed WHERE this gate applies, and the tests say so explicitly.** The gate
now guards the human-approve edge ALONE, because that is the only edge that produces
`validated`. The auto path routes to `in_review`, which lands nothing — so gating it
there would park candidates at `candidate` with a reason about a landing nobody asked
for, and the queue would silently stop filling. The tests below pin BOTH halves: the
approve edge still holds, and the auto edge deliberately does not.
"""

from __future__ import annotations

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.promotion import PromotionScheduler

from .helpers import (
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
)

KEY = "sha256:single-bp"


def _scheduler(store, *, probe, require_landing, landing_writer=None):
    return PromotionScheduler(
        store,
        probe=probe,
        hit_counts=FakeHitCountReader({KEY: 5}),  # well above T
        policy=promotion_policy(),
        landing_writer=landing_writer,
        require_landing=require_landing,
        clock=lambda: "2026-07-05T00:00:00+00:00",
    )


async def test_blueprint_holds_landing_unavailable_when_gated() -> None:
    """The approve edge, which is where the gate lives after plan §4."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store, probe=FakeWarehouseProbe(), require_landing=True)

    decision = await sched.apply_human_decision(env, "approve")

    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    assert decision.action == "hold"
    assert decision.reason == "approve_blocked_landing_unavailable"


async def test_the_auto_path_routes_even_with_no_landing_plane() -> None:
    """THE HALF THAT IS EASY TO GET WRONG. With `require_landing=True` and no writer, the
    cron must still ROUTE to the review queue: `in_review` is not a recallable state, so
    the "not landed ⇒ not validated" invariant has nothing to protect here.

    Holding instead would be the worse failure and a quiet one — a deployment whose neo4j
    is not yet wired would park every candidate at `candidate` with reason
    `landing_unavailable` and the inbox would simply never fill, which is the exact
    symptom this whole slice exists to remove. Routing means the queue fills and each
    approve then 503s honestly."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store, probe=FakeWarehouseProbe(), require_landing=True)

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


async def test_replay_gate_still_runs_under_the_landing_gate() -> None:
    """The gate is placed AFTER the replay guard — the replay gate RAN (the probe was
    called) even though the approve holds. Plan §4 did not move the replay: the point of
    lowering the corroboration threshold is that the CORRECTNESS guards are untouched."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, probe=probe, require_landing=True)

    await sched.apply_human_decision(env, "approve")

    assert probe.calls, "the replay gate must run before the landing gate holds"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


async def test_promotes_when_landing_not_required() -> None:
    """`require_landing=False` with no writer: an approve validates directly (the dev/dev
    -less posture — there is nothing to land into, so nothing to gate on)."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store, probe=FakeWarehouseProbe(), require_landing=False)

    decision = await sched.apply_human_decision(env, "approve")

    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    assert decision.action == "approve"


async def test_real_writer_lands_then_promotes_clearing_the_gate() -> None:
    """Slice 2: with a real (fake) landing writer present, the `landing_unavailable`
    hold CLEARS — the blueprint LANDS then promotes to `validated` (the gate is a
    presence check ONLY when no writer is wired)."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, probe=FakeWarehouseProbe(), require_landing=True, landing_writer=writer)

    decision = await sched.apply_human_decision(env, "approve")

    assert writer.calls == 1
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    assert decision.action == "approve"


async def test_human_approve_blueprint_holds_when_gated() -> None:
    """A human approve of a BLUEPRINT also produces `validated`, so it too holds
    `approve_blocked_landing_unavailable` until the landing writer is wired — the
    replay gate has already run + passed."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store, probe=FakeWarehouseProbe(), require_landing=True)

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.action == "hold"
    assert decision.reason == "approve_blocked_landing_unavailable"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
