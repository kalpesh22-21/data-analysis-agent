"""S9 cron cost + fairness: the rate-limited golden replay and the rotating scan.

Two bugs, one fix, because they are the same bug seen from either end of the query.

**Cost.** `_advance_candidate` ran Guard 3 (`golden_replay`) BEFORE Guard 4 (the
hit-count check), so a candidate that could never clear Guard 4 was fully replayed
every single cycle, for ever. One replay is a JWT mint plus two live ClickHouse
queries; at a 300s cadence that is 576 warehouse queries a day, per parked candidate,
indefinitely — every one of them re-deriving a verdict that had not changed.

**Fairness.** `list_by_status` was `ORDER BY created_at ASC LIMIT $limit`. A holding
candidate stays `candidate`, so it sat at the front of that window for its entire
life. Past `scan_limit` held candidates, a newly extracted candidate was NEVER
examined — no exception, no metric, the loop just silently stopped making progress on
anything new.

What is asserted, and what is deliberately NOT:
  * ONLY the expensive guard is rate-limited. The cheap guards (`entity_scan`, static
    validation, `depends_on`) are field reads and keep running every examination —
    `test_cheap_guards_still_run_while_the_replay_is_cached` is the pin for that, and
    it is the reason the freshness check sits at Guard 3 rather than at the top of the
    handler.
  * A verdict is only reused when it IS a replay verdict. A `suspect` stamp written by
    a user correction (`probes=()`) says nothing about whether the template still
    executes and must not suppress the real probe.
"""

from __future__ import annotations

from dataclasses import replace

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import DriftStamp
from data_agent.learning.promotion import (
    GRAIN_INTEGRITY,
    PromotionScheduler,
)

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
DAY = 86_400.0

# A 12h re-check window inside a 24h trust window (the shipped defaults). Built from the
# shared production-derived builder rather than from literals, so a change to the shipped
# routing threshold shows up here instead of being masked by a hardcoded 3.
POLICY = promotion_policy(
    replay_recheck_interval_seconds=43_200.0,
    drift_freshness_seconds=DAY,
)

# The same policy with the corroboration gate deliberately RAISED above the shipped 1.
#
# Several tests here are about what happens to a candidate that is PARKED — the drift
# verdict is cached, the cheap guards keep running, the hold write is a narrow stamp
# rather than a full put. At the shipped threshold of 1 nothing parks: every candidate
# clears Guard 4 on its first examination and leaves the candidate scan, so those tests
# would silently stop exercising the behaviour they are named for. Raising the threshold
# is the honest way to reach that state, and it says out loud that the state is now
# reachable only under a raised threshold.
PARKED_POLICY = promotion_policy(
    replay_recheck_interval_seconds=43_200.0,
    drift_freshness_seconds=DAY,
    blueprint_hit_threshold=3,
)


class Clock:
    """A movable injected clock. `PromotionScheduler` takes `Callable[[], str]`, so
    the tests drive real elapsed time without sleeping or monkeypatching datetime."""

    def __init__(self, iso: str = "2026-08-10T12:00:00+00:00") -> None:
        self.iso = iso

    def __call__(self) -> str:
        return self.iso


def _clean_stamp(at: str) -> DriftStamp:
    """A stored GREEN golden-replay verdict — the thing the rate limit reuses."""
    return DriftStamp(
        status="clean", last_drift_check_at=at, probes=(GRAIN_INTEGRITY,),
        failed_probe=None,
    )


def _suspect_stamp(at: str) -> DriftStamp:
    return DriftStamp(
        status="suspect", last_drift_check_at=at, probes=(GRAIN_INTEGRITY,),
        failed_probe=GRAIN_INTEGRITY,
    )


def _scheduler(store, *, probe, clock, hits=None, policy=POLICY, deps=None, writer=None):
    return PromotionScheduler(
        store,
        probe=probe,
        hit_counts=FakeHitCountReader(hits if hits is not None else {KEY: 1}),
        dependency_resolver=deps if deps is not None else FakeDependencyResolver(),
        policy=policy,
        landing_writer=writer,
        clock=clock,
    )


async def _seed(store, env: CandidateEnvelope, **kwargs) -> CandidateEnvelope:
    env = replace(env, **kwargs) if kwargs else env
    await store.put(env)
    return env


# --- the freshness gate on Guard 3 --------------------------------------------


async def test_fresh_verdict_reuses_the_stamp_and_never_touches_the_warehouse():
    """The headline saving. A candidate parked below the hit threshold carries a green
    stamp from its last replay; inside the re-check window the scheduler holds on that
    stamp instead of minting a token and running two queries."""
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    env = await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
        drift=_clean_stamp("2026-08-10T06:00:00+00:00"),  # 6h old, window is 12h
    )
    probe = FakeWarehouseProbe()
    sched = _scheduler(
        store, probe=probe, clock=clock, hits={KEY: 1}, policy=PARKED_POLICY
    )

    sweep = await sched.run_once()

    assert probe.calls == []  # NO replay: no JWT mint, no warehouse query
    assert sweep.decisions[0].reason == "below_hit_threshold"  # same verdict as before
    # The stamp is left EXACTLY as it was — reusing a verdict must never re-date it,
    # or the stamp would look freshly probed and could be reused for ever.
    assert (await store.get(env.candidate_id)).drift.last_drift_check_at == (
        "2026-08-10T06:00:00+00:00"
    )


async def test_stale_verdict_pays_for_a_real_replay_again():
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    env = await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
        drift=_clean_stamp("2026-08-09T12:00:00+00:00"),  # 24h old, window is 12h
    )
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, probe=probe, clock=clock, hits={KEY: 1})

    await sched.run_once()

    assert len(probe.calls) == 1
    # ...and the refreshed verdict is PERSISTED, which is what makes the rate limit
    # real: without a stored stamp the next cycle would have nothing to reuse.
    assert (await store.get(env.candidate_id)).drift.last_drift_check_at == clock.iso


async def test_never_checked_candidate_is_replayed_immediately():
    """A brand-new candidate's drift is the `unchecked` default (no timestamp, no
    probes). There is no verdict to reuse, so it must be probed on its first
    examination — the rate limit must never delay a candidate's FIRST gate."""
    store = InMemoryCandidateStore()
    env = await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
    )
    assert env.drift.status == "unchecked"
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, probe=probe, clock=Clock(), hits={KEY: 1})

    await sched.run_once()

    assert len(probe.calls) == 1


async def test_a_user_correction_stamp_is_not_mistaken_for_a_replay_verdict():
    """`user_correction_stamp` writes `suspect` with `probes=()` — a human said the
    ANSWER was wrong, which says nothing about whether the template still executes.
    Reusing it as "the replay failed" would suppress the real structural gate for a
    whole window. Only a stamp naming `grain_integrity` is a replay verdict."""
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
        drift=DriftStamp(status="suspect", last_drift_check_at=clock.iso, probes=()),
    )
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, probe=probe, clock=clock, hits={KEY: 1})

    await sched.run_once()

    assert len(probe.calls) == 1  # probed for real despite the fresh suspect stamp


async def test_cached_failure_holds_with_a_reason_that_says_it_was_cached():
    """A failing candidate is not re-probed every 5 minutes either — but an operator
    must be able to tell "the warehouse just said no" from "we are still holding
    yesterday's no", because only the second one has a maximum age."""
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    env = await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
        drift=_suspect_stamp("2026-08-10T06:00:00+00:00"),
    )
    probe = FakeWarehouseProbe()
    # hits ≥ T, so ONLY the cached failure is standing between it and promotion.
    sched = _scheduler(store, probe=probe, clock=clock, hits={KEY: 5})

    sweep = await sched.run_once()

    assert probe.calls == []
    assert sweep.decisions[0].action == "hold"
    assert sweep.decisions[0].reason == "replay_failed:cached_suspect"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE


async def test_live_replay_failure_keeps_its_diagnostic_reason():
    """The live path's hold reason is unchanged (`bind_failed` / `probe_unavailable` /
    the D56 verdict) — far more useful than the probe id a stamp can carry."""
    store = InMemoryCandidateStore()
    await _seed(
        store,
        make_blueprint_candidate(
            status=CandidateStatus.CANDIDATE, canonical_key=KEY, grain_verifiable=True
        ),
    )
    probe = FakeWarehouseProbe(row_count=10, distinct_grain_count=5)  # grain fan-out
    sched = _scheduler(store, probe=probe, clock=Clock(), hits={KEY: 5})

    sweep = await sched.run_once()

    assert sweep.decisions[0].reason.startswith("replay_failed:")
    assert sweep.decisions[0].reason != "replay_failed:cached_suspect"


async def test_reuse_window_is_clamped_to_the_trust_window():
    """The policy documents `replay_recheck_interval_seconds <= drift_freshness_seconds`
    (a verdict must not be reused for longer than the trust window says it may be
    believed). It is ENFORCED at the point of use, so a misconfiguration can only make
    the scheduler probe MORE often — never trust a stamp its own policy calls stale."""
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
        drift=_clean_stamp("2026-08-09T18:00:00+00:00"),  # 18h old
    )
    probe = FakeWarehouseProbe()
    # Misconfigured: a 30-day re-check interval inside a 24h trust window.
    bad = promotion_policy(
        replay_recheck_interval_seconds=30 * DAY,
        drift_freshness_seconds=6 * 3600.0,  # 6h trust window
    )
    sched = _scheduler(store, probe=probe, clock=clock, hits={KEY: 1}, policy=bad)

    await sched.run_once()

    assert len(probe.calls) == 1  # clamped to 6h ⇒ the 18h-old stamp is NOT reused


async def test_unparseable_clock_degrades_to_always_replaying():
    """`clock` is injected, so its output is not guaranteed to be an ISO string. With
    no usable `now` there is no way to judge freshness — degrade to the pre-rate-limit
    behaviour (always probe: correct but expensive), never to "everything looks fresh",
    which would silently disable the structural gate."""
    store = InMemoryCandidateStore()
    await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
        drift=_clean_stamp("2026-08-10T06:00:00+00:00"),
    )
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, probe=probe, clock=lambda: "not-a-timestamp", hits={KEY: 1})

    await sched.run_once()

    assert len(probe.calls) == 1


async def test_promotion_on_a_reused_verdict_keeps_the_original_check_timestamp():
    """A blueprint that crosses the corroboration threshold while its verdict is cached
    still advances — but the stamp it carries forward keeps the timestamp of the probe
    that actually ran. Re-dating it to now would claim a probe that did not happen and
    hand the silent fast path a full trust window it did not earn.

    (Plan §4 moved the destination to `in_review`; the stamp rule is unchanged, and it
    still matters, because the human-approve edge reuses that verdict.)"""
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    env = await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
        drift=_clean_stamp("2026-08-10T06:00:00+00:00"),
    )
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, probe=probe, clock=clock, hits={KEY: 5})

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    promoted = await store.get(env.candidate_id)
    assert promoted.status == CandidateStatus.IN_REVIEW
    assert promoted.drift.last_drift_check_at == "2026-08-10T06:00:00+00:00"
    assert probe.calls == []


# --- only the EXPENSIVE guard is gated ----------------------------------------


async def test_cheap_guards_still_run_while_the_replay_is_cached():
    """The reason the freshness check lives at Guard 3 and not at the top of the
    handler. `depends_on` resolution is a field read plus one resolver call; gating it
    behind the replay window would park a candidate whose dependency landed minutes
    ago until the window expired, for no saving at all."""
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    env = await _seed(
        store,
        make_blueprint_candidate(
            status=CandidateStatus.CANDIDATE, canonical_key=KEY, depends_on=("rule::x",)
        ),
        drift=_clean_stamp("2026-08-10T06:00:00+00:00"),
    )
    probe = FakeWarehouseProbe()
    unresolved = FakeDependencyResolver()
    sched = _scheduler(store, probe=probe, clock=clock, hits={KEY: 5}, deps=unresolved)

    # Cycle 1 — the dependency has not landed: held at the CHEAP guard, no probe.
    sweep = await sched.run_once()
    assert sweep.decisions[0].reason == "depends_on_unresolved"
    assert probe.calls == []

    # The dependency lands. The very NEXT cycle must notice — not the next window.
    resolved = FakeDependencyResolver({"rule::x"})
    sched = _scheduler(store, probe=probe, clock=clock, hits={KEY: 5}, deps=resolved)
    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    assert probe.calls == []  # advanced on the CACHED verdict; the guard was live


# --- the validated re-check obeys the same rate limit -------------------------


async def test_validated_recheck_reuses_a_fresh_verdict():
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY),
        drift=_clean_stamp("2026-08-10T06:00:00+00:00"),
    )
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, probe=probe, clock=clock)

    sweep = await sched.run_once()

    assert probe.calls == []
    assert sweep.decisions[0].action == "drift_clean"
    assert sweep.decisions[0].reason == "replay_fresh"


async def test_cached_recheck_still_re_asserts_the_landed_node_every_cycle():
    """The self-heal re-assert is deliberately OUTSIDE the rate limit. It repairs a
    landed node whose demote/land write-back transiently failed, and it is one
    idempotent Cypher — slowing a convergence guarantee to daily in order to save a
    cheap write would be the wrong trade. Only the PROBE is expensive."""
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY),
        drift=_clean_stamp("2026-08-10T06:00:00+00:00"),
    )
    probe = FakeWarehouseProbe()
    writer = FakeLandingWriter()
    sched = _scheduler(store, probe=probe, clock=clock, writer=writer)

    await sched.run_once()

    assert probe.calls == []  # no replay...
    # ...but the node stamp was re-asserted anyway.
    assert [(u[1], u[2]) for u in writer.status_updates] == [("validated", "clean")]


async def test_a_passing_replay_must_not_switch_off_the_demote_convergence_re_assert():
    """REGRESSION PIN (review BLOCKER). Persisting the Guard-3 verdict interacts with
    the demote-direction re-assert, and the dangerous case is the replay PASSING.

    The re-assert used to trigger on `drift.status == "suspect"`. Sequence that
    defeated it:

      1. A user correction demotes a validated blueprint (`suspect`, `probes=()`) while
         neo4j is down. The write-back fails OPEN, so the landed node is left
         `validated`/`clean` — still RECALLABLE — while the store says `candidate`.
      2. Next examination: the re-assert fires and fails again. Guard 3 correctly
         refuses to reuse a correction stamp as a replay verdict and probes for real —
         and the replay PASSES, because a correction is about values, not structure
         (D98). The verdict is persisted, overwriting `suspect` with `clean`.
      3. From then on `drift.status != "suspect"`, so the re-assert never fires again.
         Neo4j recovers and nothing notices. Below the hit threshold the blueprint never
         re-promotes and never re-lands, so nothing else repairs the node either: it
         stays recallable FOREVER.

    The fix keys the re-assert on STATUS instead. Every envelope in this scan is
    `candidate` by construction, and a candidate-status landed node must be
    non-recallable whatever drift says — a trigger a later write can erase is not a
    convergence guarantee."""
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    env = await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY),
    )
    # The write-back fails for the correction AND for the first re-assert; neo4j
    # recovers only afterwards — i.e. exactly when the stamp has already turned clean.
    writer = FakeLandingWriter(update_fail=RuntimeError("neo4j down"), update_fail_times=2)
    # Below T, so the blueprint can NEVER re-promote and re-land: the re-assert is the
    # only thing left that could ever repair the node.
    sched = _scheduler(
        store, probe=FakeWarehouseProbe(), clock=clock, hits={KEY: 1}, writer=writer,
        policy=PARKED_POLICY,
    )

    await sched.apply_user_correction(env)
    assert (await store.get(env.candidate_id)).drift.status == "suspect"
    assert writer.status_updates == []  # write-back 1 failed → node still recallable

    # Examination 1: re-assert attempted (fails), then a REAL replay passes and the
    # persisted verdict flips the stamp from `suspect` to `clean`.
    sweep = await sched.run_once()
    assert sweep.decisions[0].reason == "below_hit_threshold"
    assert (await store.get(env.candidate_id)).drift.status == "clean"
    assert writer.status_updates == []  # write-back 2 failed → still recallable

    # Examination 2: neo4j is healthy but the stamp is no longer `suspect`. The
    # re-assert MUST still fire, and must stamp the node non-recallable.
    await sched.run_once()

    assert [(u[1], u[2]) for u in writer.status_updates] == [("candidate", "clean")]
    # `status="candidate"` IS the non-recallable stamp (the recall filter drops any
    # non-`validated` status) — which is precisely why status, not drift, is the trigger.
    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE


async def test_validated_recheck_probes_once_the_verdict_expires():
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY),
        drift=_clean_stamp("2026-08-08T12:00:00+00:00"),  # 48h old
    )
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, probe=probe, clock=clock)

    sweep = await sched.run_once()

    assert len(probe.calls) == 1
    assert sweep.decisions[0].reason is None  # the LIVE clean path, not the cached one


async def test_a_suspect_validated_artifact_is_re_probed_not_demoted_on_a_cached_no():
    """A demotion retracts a live, recallable artifact. That must rest on a probe run
    NOW, not on a cached no — so `suspect` is never reused on the validated side even
    when it is fresh."""
    store = InMemoryCandidateStore()
    clock = Clock("2026-08-10T12:00:00+00:00")
    await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY),
        drift=_suspect_stamp("2026-08-10T06:00:00+00:00"),
    )
    probe = FakeWarehouseProbe()  # a GREEN probe — the cached no was out of date
    sched = _scheduler(store, probe=probe, clock=clock)

    sweep = await sched.run_once()

    assert len(probe.calls) == 1
    assert sweep.decisions[0].action == "drift_clean"  # not demoted on stale evidence


# --- scan rotation -------------------------------------------------------------


def _held(ordinal: int) -> CandidateEnvelope:
    """A candidate that holds cheaply and for ever (human-gated target), i.e. exactly
    the population that used to pin the scan window."""
    env = with_type(
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE), "global_knowledge"
    )
    return replace(
        env,
        candidate_id=f"candidate::held::{ordinal}",
        created_at=f"2020-01-{ordinal + 1:02d}T00:00:00+00:00",
    )


async def test_new_candidates_are_examined_even_behind_a_full_window_of_held_ones():
    """THE starvation bug. With `created_at ASC` the oldest held candidates owned the
    whole `scan_limit` window permanently, so a candidate extracted today would never
    be examined again — silently, for ever. Ordering by the scan cursor makes the
    window round-robin: examining a candidate pushes it to the back, and a candidate
    that has never been examined sorts FIRST."""
    store = InMemoryCandidateStore()
    for i in range(3):  # 3 held candidates, all OLDER than the newcomer
        await store.put(_held(i))
    clock = Clock("2026-08-10T12:00:00+00:00")
    policy = replace(POLICY, scan_limit=2)  # window smaller than the held population
    sched = _scheduler(store, probe=FakeWarehouseProbe(), clock=clock, policy=policy)

    # Cycle 1 fills the window with two of the held candidates and stamps their cursor.
    seen = {d.candidate_id for d in (await sched.run_once()).decisions}
    assert len(seen) == 2

    # The newcomer arrives AFTER the window is already full of older rows.
    await store.put(
        replace(_held(9), candidate_id="candidate::new::0",
                created_at="2026-08-10T11:59:00+00:00")
    )
    clock.iso = "2026-08-10T12:05:00+00:00"

    # Cycle 2: the never-scanned rows sort first, so the newcomer is examined
    # immediately — it does not queue behind the three older held candidates.
    seen2 = {d.candidate_id for d in (await sched.run_once()).decisions}
    assert "candidate::new::0" in seen2

    # And over enough cycles EVERY held candidate is examined — none is starved.
    #
    # The clock ADVANCES per cycle here, and that is load-bearing rather than tidiness.
    # Rotation is driven by the cursor moving; a pinned clock leaves every row tied on
    # one value, in which case the same window comes back for ever and this loop would
    # pass while proving nothing (an earlier revision of this test did exactly that —
    # QA caught it by demonstrating a real stall at 300/500 candidates under a literal
    # clock). The `candidate_id` tiebreak makes the tied order TOTAL and identical in
    # both stores, but it cannot rotate a set whose cursor never changes.
    for minute in range(10, 30, 5):
        clock.iso = f"2026-08-10T12:{minute}:00+00:00"
        seen2 |= {d.candidate_id for d in (await sched.run_once()).decisions}
    assert {f"candidate::held::{i}" for i in range(3)} <= (seen | seen2)


async def test_tied_cursors_return_the_same_window_in_both_stores():
    """The tiebreak's actual contract: with rows sharing a cursor value, WHICH rows the
    bounded window contains is decided by `candidate_id`, not by whichever accident each
    store happened to fall back on (the fake to dict insertion order, the GSI to its
    implicit trailing doc key). Insertion order here is deliberately NOT id order, so a
    regression to the old behaviour changes the answer.

    Verified equal against live Couchbase 7.6.5 with the shipped
    `idx_candidates_scan_rotation` index; this is the in-process pin of the same
    ordering. Note what it does NOT claim — see
    `test_rotation_stalls_when_every_cursor_is_identical_is_a_known_limitation`: a total
    order is not a rotating one, and a frozen cursor still stalls."""
    store = InMemoryCandidateStore()
    tied = "2026-08-10T12:00:00+00:00"
    for ordinal in (3, 0, 4, 1, 2):  # shuffled
        await store.put(replace(_held(ordinal), last_scanned_at=tied))

    got = await store.list_by_status(
        CandidateStatus.CANDIDATE, limit=3, order_by="last_scanned_at"
    )

    assert [c.candidate_id for c in got] == [
        "candidate::held::0", "candidate::held::1", "candidate::held::2",
    ]


async def test_a_hold_now_advances_the_cursor_without_rewriting_the_envelope():
    """A hold used to produce no store write at all, which is precisely why it could
    never lose its place in the window. It now stamps the cursor — via the narrow
    `touch_scanned` path, NOT a re-put of the scanned envelope, so it cannot revert a
    concurrent inbox transition and does not renew the retention TTL.

    SCOPE: this uses a human-gated hold, which returns before Guard 3. A hold that
    REACHES the replay also writes its drift verdict — but through the equally narrow
    `stamp_drift`, so the claim being pinned here ("a hold never rewrites the whole
    envelope") holds for both; only `put_calls` staying flat is specific to this path.
    `test_a_replay_reaching_hold_records_its_verdict_without_a_full_put` covers the
    other one."""
    store = InMemoryCandidateStore()
    env = await _seed(store, _held(0))
    puts_before = store.put_calls
    sched = _scheduler(store, probe=FakeWarehouseProbe(), clock=Clock())

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "hold"
    assert store.put_calls == puts_before  # no envelope rewrite
    assert store.touch_calls == 1
    assert (await store.get(env.candidate_id)).last_scanned_at == (
        "2026-08-10T12:00:00+00:00"
    )


async def test_a_replay_reaching_hold_records_its_verdict_without_a_full_put():
    """The other hold shape: it DOES write, because it has a fresh verdict worth
    keeping — but through `stamp_drift`, not `put`.

    Three things ride on that being a single-path write rather than an upsert of the
    cycle-start snapshot: it does not rewrite fields S9 does not own, it does not renew
    the 90-day retention TTL (a verdict re-stamped on a schedule would make a parked
    candidate immortal), and it cannot RESURRECT a document `supersede` deleted between
    the scan read and the write."""
    store = InMemoryCandidateStore()
    env = await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
    )
    puts_before = store.put_calls
    sched = _scheduler(
        store, probe=FakeWarehouseProbe(), clock=Clock(), hits={KEY: 1},
        policy=PARKED_POLICY,
    )

    sweep = await sched.run_once()

    assert sweep.decisions[0].reason == "below_hit_threshold"
    assert store.put_calls == puts_before  # no whole-envelope rewrite
    assert store.drift_stamps == 1
    assert (await store.get(env.candidate_id)).drift.status == "clean"


async def test_a_superseded_candidate_is_not_resurrected_by_its_own_verdict_stamp():
    """`supersede` deletes a session's candidates when a redelivered session
    re-extracts. If that lands between the cycle-start scan read and the verdict write,
    a full-envelope `put` would recreate the row the pipeline deliberately dropped —
    leaving a duplicate from an abandoned extraction attempt in the store (MEDIUM-3).
    A sub-document write on a missing document is a no-op instead."""
    store = InMemoryCandidateStore()
    env = await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
    )

    class _SupersedeMidCycle(InMemoryCandidateStore):
        """Deletes the candidate at the moment the scheduler derives its verdict —
        modelling a concurrent `supersede` racing the cycle."""

        async def stamp_drift(self, candidate_id, drift):
            self._by_id.pop(candidate_id, None)
            await super().stamp_drift(candidate_id, drift)

    racing = _SupersedeMidCycle()
    await racing.put(env)
    sched = _scheduler(
        racing, probe=FakeWarehouseProbe(), clock=Clock(), hits={KEY: 1},
        policy=PARKED_POLICY,
    )

    await sched.run_once()

    assert racing.all_candidates() == []  # stayed deleted


async def test_a_candidate_that_raises_still_loses_its_place_in_the_rotation():
    """The row least likely to ever succeed must not be the row that owns the window.
    If a crashing candidate kept its cursor it would sort first for ever and re-raise
    every cycle — the same head-of-line starvation, now caused by a poison row."""
    store = InMemoryCandidateStore()
    env = await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
    )

    class _Exploding:
        async def hit_count(self, canonical_key: str) -> int:
            raise RuntimeError("corpus unreachable")

    sched = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=_Exploding(),
        dependency_resolver=FakeDependencyResolver(),
        policy=POLICY,
        clock=Clock(),
    )

    sweep = await sched.run_once()

    assert sweep.decisions[0].reason == "error"
    assert (await store.get(env.candidate_id)).last_scanned_at is not None


async def test_a_cursor_write_failure_never_changes_the_cycles_decision():
    """The cursor is bookkeeping. A store hiccup while stamping it must not mask or
    alter the real decision — the candidate simply keeps its old rotation position and
    is re-examined next cycle."""

    class _TouchFails(InMemoryCandidateStore):
        async def touch_scanned(self, candidate_id: str, at: str) -> None:
            raise RuntimeError("couchbase unreachable")

    store = _TouchFails()
    env = await _seed(
        store,
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY),
    )
    sched = _scheduler(store, probe=FakeWarehouseProbe(), clock=Clock(), hits={KEY: 5})

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
