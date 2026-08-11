"""The corroboration sum: `max(hit_count, 1) + recurrence_weight * recurrence_count`.

The soft recurrence counter is DORMANT at the shipped `recurrence_weight = 0.0`, and it
is wired end to end anyway. The plan's reasoning: retrofitting a counter with no history
behind it is worse than carrying a dormant one — switched on cold, its first month of
readings are all zeros and indistinguishable from "this never recurs".

That makes the read path unexercised by every OTHER test in the suite (they all run at
weight 0), which is exactly the shape of thing that is discovered to be broken on the day
someone finally turns it up. So it gets its own file, driven at a non-zero weight.

Slugs:
  * S9-recurrence-is-inert-at-weight-zero
  * S9-recurrence-can-cross-a-raised-threshold
  * S9-recurrence-read-fails-soft
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


def _sched(store, *, hits, recurrences, weight, threshold, counts=None):
    return PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=counts or FakeHitCountReader({KEY: hits}, {KEY: recurrences}),
        # The SAME object as `hit_counts`, mirroring production, where
        # `CouchbaseBlueprintCorpus` duck-types both ports so the two counts can never
        # address different artifact sets.
        recurrence_counts=counts or FakeHitCountReader({KEY: hits}, {KEY: recurrences}),
        dependency_resolver=FakeDependencyResolver(),
        policy=promotion_policy(
            blueprint_hit_threshold=threshold, recurrence_weight=weight
        ),
        clock=lambda: "2026-08-10T12:00:00+00:00",
    )


async def _run(sched, store):
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    return (await sched.run_once()).decisions[0]


async def test_at_the_shipped_weight_the_recurrence_count_changes_nothing():
    """Inert, proved by driving a LARGE recurrence count that must not move the gate."""
    store = InMemoryCandidateStore()
    sched = _sched(store, hits=1, recurrences=500, weight=0.0, threshold=3)
    assert (await _run(sched, store)).reason == "below_hit_threshold"


async def test_a_non_zero_weight_lets_paraphrases_cross_a_raised_threshold():
    """The state this exists FOR: at ~7000 sessions/day the threshold rises above 1, and
    the hard count cannot corroborate anything (it needs byte-identical normalized-AST
    equality across sessions, which is why nothing ever reached a human). The soft count
    is what makes corroboration reachable at all."""
    store = InMemoryCandidateStore()
    sched = _sched(store, hits=1, recurrences=4, weight=0.5, threshold=3)
    assert (await _run(sched, store)).action == "route"


async def test_a_non_zero_weight_still_respects_the_threshold():
    """The weight is a contribution, not a bypass: three paraphrases at 0.5 is 1 + 1.5,
    which is below 3."""
    store = InMemoryCandidateStore()
    sched = _sched(store, hits=1, recurrences=3, weight=0.5, threshold=3)
    assert (await _run(sched, store)).reason == "below_hit_threshold"


async def test_a_fractional_weight_is_not_floored_away():
    """`_corroboration` returns a float on purpose. An int return would silently discard
    exactly the tuning the fractional weight exists to express: 1 + 0.5*3 = 2.5, which
    clears a threshold of 2 and would not if it were floored to 2 by an int cast... and,
    more subtly, would clear a threshold of 3 if it were rounded up. Both directions are
    pinned by the pair of tests around this one."""
    store = InMemoryCandidateStore()
    sched = _sched(store, hits=1, recurrences=3, weight=0.5, threshold=2)
    assert (await _run(sched, store)).action == "route"


async def test_no_recurrence_reader_wired_reads_zero_rather_than_raising():
    """Optional port. Absent, the term contributes 0 — which at the shipped weight is
    arithmetically identical to having one, so a deployment that never wires it behaves
    exactly as it does today."""
    store = InMemoryCandidateStore()
    sched = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 1}),
        recurrence_counts=None,
        dependency_resolver=FakeDependencyResolver(),
        policy=promotion_policy(blueprint_hit_threshold=3, recurrence_weight=1.0),
        clock=lambda: "2026-08-10T12:00:00+00:00",
    )
    assert (await _run(sched, store)).reason == "below_hit_threshold"


async def test_a_failing_recurrence_read_falls_back_to_the_hard_count():
    """FAIL-SOFT, and deliberately asymmetric with the hard count. The recurrence term
    can only RAISE the sum, so swallowing its failure can only make the gate stricter —
    never let something through. The hard count is NOT treated this way, because
    swallowing ITS failure would be swallowing the gate."""

    class _Exploding(FakeHitCountReader):
        async def recurrence_count(self, canonical_key: str) -> int:
            raise RuntimeError("couchbase unreachable")

    store = InMemoryCandidateStore()
    counts = _Exploding({KEY: 9}, {KEY: 9})
    sched = _sched(store, hits=0, recurrences=0, weight=1.0, threshold=3, counts=counts)

    decision = await _run(sched, store)

    # The hard count (9) alone still clears the gate: the cycle was not aborted and the
    # candidate was not held on an infrastructure hiccup in a dormant signal.
    assert decision.action == "route"
