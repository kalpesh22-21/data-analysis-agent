"""Adversarial QA on the S9 promotion scheduler (invariant #3 — no value oracle,
single-session never auto-promotes, depends_on gating).

The existing suite covers the AUTO (cron) path guards. This file attacks the
CALLER-DRIVEN human path (`apply_human_decision`) and pins the no-value-oracle
posture. strict-xfail = real hole; passing = pinned guarantee.
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
)

KEY = "sha256:single-bp"
DEP = "schema_edit::add_rule::earning_record_type"


def _sched(store, *, resolved, counts=None, probe=None, **policy_overrides):
    return PromotionScheduler(
        store,
        probe=probe or FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(counts or {KEY: 5}),
        dependency_resolver=FakeDependencyResolver(resolved),
        policy=promotion_policy(**policy_overrides),
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


async def test_replay_alone_can_never_reach_validated():
    """D98 layer iii, restated for the plan-§4 shape.

    It used to be enforced by a THRESHOLD: a green replay with a single-session count
    (1 < T=3) held at `candidate`. That protection was arithmetic, and lowering the
    threshold to 1 removes it entirely.

    What replaced it is STRUCTURAL and strictly stronger: the auto path cannot produce
    `validated` at ALL, at any threshold, however green the replay. A passing replay buys
    a place in a human's queue and nothing else."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    sched = _sched(store, resolved=set(), counts={KEY: 999})  # arbitrarily corroborated

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


async def test_below_the_corroboration_threshold_stays_candidate():
    """The gate MECHANISM, pinned at an explicit threshold rather than at the shipped
    one. At the shipped threshold of 1 every candidate clears the gate on its own first
    sighting, so the shipped configuration cannot exercise the below-threshold branch at
    all — a test that relied on it would silently stop testing anything."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    sched = _sched(store, resolved=set(), counts={KEY: 1}, blueprint_hit_threshold=3)

    sweep = await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE
    assert sweep.decisions[0].action == "hold"
    assert sweep.decisions[0].reason == "below_hit_threshold"


async def test_a_candidate_with_no_dedup_key_still_reaches_a_human():
    """DELIBERATE BEHAVIOUR CHANGE (plan §4), recorded rather than quietly made.

    This test used to assert that a candidate which never got a `canonical_key` (S6 could
    not mint one — no `canonical_ast_norm`, or malformed hard-key inputs) reads
    `hit_count == 0` and therefore stays `candidate`. At T=3 that was indistinguishable
    from "not corroborated yet". At T=1 it would have become the one class of candidate
    that could NEVER reach a human, forever — the precise bug this slice removes, in
    miniature.

    So `_corroboration` floors the count at 1: the candidate in hand IS one sighting, and
    a keyed first sighting only reads 1 because `_seed_on_insert` wrote that 1 on its
    behalf. It changes nothing at any threshold above 1 (see the test below), and it
    routes to a HUMAN, not to the corpus — every correctness guard still ran."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=None)
    await store.put(env)
    sched = _sched(store, resolved=set())

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


async def test_the_unkeyed_floor_does_not_defeat_a_raised_threshold():
    """The other half of the change above: the floor of 1 is a floor, not a bypass. Raise
    the threshold and an unkeyable candidate holds exactly as it always did."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=None)
    await store.put(env)
    sched = _sched(store, resolved=set(), blueprint_hit_threshold=2)

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "hold"
    assert sweep.decisions[0].reason == "below_hit_threshold"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE


async def test_wrong_number_replay_does_not_gate_on_value():
    """No value oracle (D98/D17): a structurally-valid replay whose row_count is an
    absurd number still passes the gate — the replay verifies structure, never the value.
    This pins that S9 deliberately does NOT add a value oracle. (The destination is now
    `in_review` rather than `validated`; the point is that the wrong NUMBER did not stop
    it, which is unchanged.)"""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    # grain unverifiable (fixture default) → teeth skipped; columns still match the
    # declared signature, so structure passes regardless of the (wrong) row_count.
    probe = FakeWarehouseProbe(row_count=999_999, distinct_grain_count=None)
    sched = _sched(store, resolved=set(), probe=probe)

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    # and the replay bound a SYNTHETIC slot value, never a stored entity input (D17)
    bound_sql = probe.calls[0][0]
    assert "__replay_sample_" in bound_sql
