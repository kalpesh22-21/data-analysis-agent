"""S9-drift-suspect-demotes (contracts-design §9 row 12, D43).

A suspect drift probe on a `validated` artifact demotes it `validated → candidate`
+ a review flag. The review flag is carried by the demoted `status=candidate` plus a
`drift.status=suspect` naming the failed probe (S9 owns only `status` + `drift`,
D102). A CLEAN probe leaves it validated with a re-stamped fresh clean drift.
"""

from __future__ import annotations

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.promotion import (
    GRAIN_INTEGRITY,
    STUBBED_PROBES,
    PromotionScheduler,
)

from .helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeWarehouseProbe,
    make_blueprint_candidate,
)


def _scheduler(store, probe):
    return PromotionScheduler(
        store,
        probe=probe,
        hit_counts=FakeHitCountReader(),
        dependency_resolver=FakeDependencyResolver(),
        clock=lambda: "2026-07-03T12:00:00+00:00",
    )


async def test_suspect_grain_probe_demotes_validated_to_candidate():
    store = InMemoryCandidateStore()
    # A VERIFIABLE grain so the live grain_integrity probe actually runs; the probe
    # returns a fan-out (row_count 10 != distinct 5) → grain teeth FAIL → suspect.
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, grain_verifiable=True
    )
    await store.put(env)
    probe = FakeWarehouseProbe(row_count=10, distinct_grain_count=5)
    sched = _scheduler(store, probe)

    sweep = await sched.run_once()

    demoted = await store.get(env.candidate_id)
    assert demoted.status == CandidateStatus.CANDIDATE  # demoted
    assert demoted.drift.status == "suspect"  # the review flag
    assert demoted.drift.failed_probe == GRAIN_INTEGRITY
    assert demoted.drift.probes == (GRAIN_INTEGRITY,)  # only the live probe ran
    assert [d.action for d in sweep.decisions] == ["demote"]


async def test_clean_grain_probe_keeps_validated_and_restamps_fresh():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, grain_verifiable=True
    )
    await store.put(env)
    # row_count == distinct_grain_count ⇒ grain teeth pass ⇒ clean.
    probe = FakeWarehouseProbe(row_count=5, distinct_grain_count=5)
    sched = _scheduler(store, probe)

    sweep = await sched.run_once()

    kept = await store.get(env.candidate_id)
    assert kept.status == CandidateStatus.VALIDATED
    assert kept.drift.status == "clean"
    assert kept.drift.last_drift_check_at == "2026-07-03T12:00:00+00:00"
    assert [d.action for d in sweep.decisions] == ["drift_clean"]


def test_phase1_probe_coverage_is_documented():
    """Phase-1: only grain_integrity is LIVE; catalog_conformance + rule_currency
    are stubbed (absent from a stamp's `probes`)."""
    assert GRAIN_INTEGRITY not in STUBBED_PROBES
    assert set(STUBBED_PROBES) == {"catalog_conformance", "rule_currency"}


async def test_user_correction_demotes_validated_with_review_flag():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED)
    await store.put(env)
    sched = _scheduler(store, FakeWarehouseProbe())

    decision = await sched.apply_user_correction(env)

    demoted = await store.get(env.candidate_id)
    assert demoted.status == CandidateStatus.CANDIDATE
    assert demoted.drift.status == "suspect"
    # A user correction is not a probe — no failed_probe, but the suspect drift +
    # candidate status IS the review flag.
    assert demoted.drift.failed_probe is None
    assert decision.reason == "user_correction"
