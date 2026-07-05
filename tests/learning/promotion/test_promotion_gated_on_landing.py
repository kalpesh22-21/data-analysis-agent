"""Layer-1 — `S9-promotion-gated-on-landing` (S9-activation Slice 1, §4).

This slice makes the golden-replay gate REAL but keeps auto-promotion-INTO-RETRIEVAL
dormant: there is no corpus-landing writer yet (Slice 2). So a blueprint that passes
EVERY guard — including the now-real replay gate — HOLDS `landing_unavailable` rather
than validating a never-recallable artifact. Proven for BOTH the auto edge and the
human-approve edge, with the default (`require_landing=False`) preserving today's
behavior.
"""

from __future__ import annotations

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.promotion import PromotionPolicy, PromotionScheduler

from .helpers import (
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
)

KEY = "sha256:single-bp"


def _scheduler(store, *, probe, require_landing, landing_writer=None):
    return PromotionScheduler(
        store,
        probe=probe,
        hit_counts=FakeHitCountReader({KEY: 5}),  # well above T
        policy=PromotionPolicy(blueprint_hit_threshold=3),
        landing_writer=landing_writer,
        require_landing=require_landing,
        clock=lambda: "2026-07-05T00:00:00+00:00",
    )


async def test_blueprint_holds_landing_unavailable_when_gated() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store, probe=FakeWarehouseProbe(), require_landing=True)

    sweep = await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE
    assert sweep.decisions[0].action == "hold"
    assert sweep.decisions[0].reason == "landing_unavailable"


async def test_replay_gate_still_runs_under_the_landing_gate() -> None:
    """The gate is placed AFTER the replay guard — the replay gate RAN (the probe was
    called) even though promotion holds. This is what makes the real replay gate
    Layer-2-provable while auto-promotion stays dormant."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, probe=probe, require_landing=True)

    await sched.run_once()

    assert probe.calls, "the replay gate must run before the landing gate holds"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE


async def test_promotes_when_landing_not_required() -> None:
    """Default (`require_landing=False`) preserves today's behavior — a blueprint that
    passes every guard PROMOTES (the regression guard for the 422 baseline)."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store, probe=FakeWarehouseProbe(), require_landing=False)

    sweep = await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    assert sweep.decisions[0].action == "promote"


async def test_real_writer_lands_then_promotes_clearing_the_gate() -> None:
    """Slice 2: with a real (fake) landing writer present, the `landing_unavailable`
    hold CLEARS — the blueprint LANDS then promotes to `validated` (the gate is a
    presence check ONLY when no writer is wired)."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, probe=FakeWarehouseProbe(), require_landing=True, landing_writer=writer)

    sweep = await sched.run_once()

    assert writer.calls == 1
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    assert sweep.decisions[0].action == "promote"


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
