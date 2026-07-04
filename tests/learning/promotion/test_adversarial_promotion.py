"""Adversarial QA on the S9 promotion scheduler (invariant #3 — no value oracle,
single-session never auto-promotes, depends_on gating).

The existing suite covers the AUTO (cron) path guards. This file attacks the
CALLER-DRIVEN human path (`apply_human_decision`) and pins the no-value-oracle
posture. strict-xfail = real hole; passing = pinned guarantee.
"""

from __future__ import annotations

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.promotion import PromotionPolicy, PromotionScheduler

from .helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeWarehouseProbe,
    make_blueprint_candidate,
)

KEY = "sha256:single-bp"
DEP = "schema_edit::add_rule::earning_record_type"


def _sched(store, *, resolved, counts=None, probe=None):
    return PromotionScheduler(
        store,
        probe=probe or FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(counts or {KEY: 5}),
        dependency_resolver=FakeDependencyResolver(resolved),
        policy=PromotionPolicy(blueprint_hit_threshold=3),
        clock=lambda: "2026-07-03T12:00:00+00:00",
    )


# =============================================================================
# STRICT-XFAIL — the human path bypasses the depends_on guard
# =============================================================================


async def test_human_approve_must_not_bypass_depends_on_guard():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.IN_REVIEW, canonical_key=KEY, depends_on=(DEP,)
    )
    await store.put(env)
    sched = _sched(store, resolved=set())  # the dependency is NOT landed

    decision = await sched.apply_human_decision(env, "approve")

    # SECURE: an unresolved dependency blocks even a human approval (hold, not
    # validated). Currently FAILS (action == "approve") -> strict xfail.
    assert decision.to_status != CandidateStatus.VALIDATED
    assert (await store.get(env.candidate_id)).status != CandidateStatus.VALIDATED


# =============================================================================
# PASSING HARDENING — pinned guarantees
# =============================================================================


async def test_single_session_green_replay_stays_candidate():
    """Replay alone never promotes (D98 layer iii): a green replay with a
    single-session hit_count (1 < T=3) and no human approval STAYS candidate."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    sched = _sched(store, resolved=set(), counts={KEY: 1})  # single session

    sweep = await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE
    assert sweep.decisions[0].action == "hold"
    assert sweep.decisions[0].reason == "below_hit_threshold"


async def test_no_dedup_verdict_means_zero_count_stays_candidate():
    """A candidate that never ran S6 (dedup is None) reads hit_count 0 → cannot
    promote by count. The auto path must not treat 'no count available' as promotable."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=None)
    await store.put(env)
    sched = _sched(store, resolved=set())

    await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE


async def test_wrong_number_replay_does_not_gate_on_value():
    """No value oracle (D98/D17): a structurally-valid replay whose row_count is an
    absurd number still PROMOTES (with hit_count ≥ T) — the gate verifies structure,
    never the value. This pins that S9 deliberately does NOT add a value oracle."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    # grain unverifiable (fixture default) → teeth skipped; columns still match the
    # declared signature, so structure passes regardless of the (wrong) row_count.
    probe = FakeWarehouseProbe(row_count=999_999, distinct_grain_count=None)
    sched = _sched(store, resolved=set(), probe=probe)

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "promote"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    # and the replay bound a SYNTHETIC slot value, never a stored entity input (D17)
    bound_sql = probe.calls[0][0]
    assert "__replay_sample_" in bound_sql
