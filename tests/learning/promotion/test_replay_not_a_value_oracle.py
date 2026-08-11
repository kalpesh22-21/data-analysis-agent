"""S9-replay-not-a-value-oracle (contracts-design §9 row 10, D98/D29).

A golden replay alone NEVER promotes (D98 layer iii). Replay verifies STRUCTURE, not
values (D98); there is no value oracle (that would breach D17). These tests prove:

  * a structurally-valid (green) replay below the corroboration threshold does NOT
    advance — it holds at `candidate`;
  * it is the CORROBORATION COUNT (≥ T), not the replay, that advances a candidate;
  * a "value-changed" replay (the probe returns a different number) that is still
    STRUCTURALLY valid does not advance either — the number is never inspected;
  * the replay binds SYNTHETIC sampled values, never the stored entity inputs (D17).

**Every test here pins an EXPLICIT `threshold=3`, above the shipped 1.** That is
deliberate: the shipped configuration cannot reach the below-threshold branch at all, so
a file about that branch has to state the threshold it is testing rather than inherit it.
The destination on the pass side is `in_review` (plan §4), not `validated`.
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


def _scheduler(store, *, hits, probe=None, threshold=3):
    return PromotionScheduler(
        store,
        probe=probe or FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(hits),
        dependency_resolver=FakeDependencyResolver(),
        policy=promotion_policy(blueprint_hit_threshold=threshold),
        clock=lambda: "2026-07-03T12:00:00+00:00",
    )


async def test_single_session_candidate_stays_candidate():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    probe = FakeWarehouseProbe()  # a passing (green) replay
    sched = _scheduler(store, hits={KEY: 1}, probe=probe)  # single session

    sweep = await sched.run_once()

    still = await store.get(env.candidate_id)
    assert still.status == CandidateStatus.CANDIDATE  # replay green, but NOT promoted
    assert [d.action for d in sweep.decisions] == ["hold"]
    assert sweep.decisions[0].reason == "below_hit_threshold"
    # The replay DID run (structure was checked) — it just is not a promotion proof.
    assert probe.calls, "golden replay must run even though it never promotes alone"


async def test_replay_binds_synthetic_values_never_stored_entities():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, hits={KEY: 1}, probe=probe)

    await sched.run_once()

    replay_sql, _grain_cols = probe.calls[0]
    # Synthetic sampled tokens are bound (D17: no stored entity input is replayed).
    assert "__replay_sample_department__" in replay_sql
    assert "__replay_sample_year__" in replay_sql
    # The fixture's stored entity VALUES (0420 / 2025 / NA) must never appear.
    assert "0420" not in replay_sql
    assert "2025" not in replay_sql
    assert "'NA'" not in replay_sql


async def test_hit_count_threshold_is_what_advances_a_candidate():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store, hits={KEY: 3}, threshold=3)  # ≥ T

    sweep = await sched.run_once()

    routed = await store.get(env.candidate_id)
    assert routed.status == CandidateStatus.IN_REVIEW
    assert [d.action for d in sweep.decisions] == ["route"]
    # The routed candidate carries the clean, fresh drift the replay just produced, so a
    # human's approve can reuse the verdict inside the re-check window.
    assert routed.drift.status == "clean"
    assert routed.drift.probes == ("grain_integrity",)


async def test_value_changed_but_structurally_valid_does_not_advance():
    """The probe returns a DIFFERENT number (row_count=999) but a structurally
    valid result (columns match, grain skipped). With a single session, the
    candidate STILL stays `candidate` — the number is never inspected (no value
    oracle, D98), and structure alone never promotes."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    probe = FakeWarehouseProbe(row_count=999)  # a "changed value", still valid shape
    sched = _scheduler(store, hits={KEY: 1}, probe=probe)

    await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE
