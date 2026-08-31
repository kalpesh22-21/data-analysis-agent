"""A candidate PERSISTED BEFORE the fifth static check existed must still promote.

`StaticValidation` gained `date_literal_ok` (the frozen-run-date check) as an ADDITIVE
field with a `True` default in `from_doc`. The unit that pins the default lives in
`tests/learning/candidate/test_contracts_wave0.py`; what it cannot show is that the
default is enough for the code that READS a rehydrated stamp.

S9 is that reader, and it is the one that matters: every candidate already sitting in the
store — `candidate` awaiting the sweep, `in_review` awaiting a human — carries the OLD
six-key `static_validation` dict, and `PromotionScheduler._generalization` swallows a
`KeyError` by returning `None`. A required field would therefore not crash; it would
turn every pre-existing candidate into `static_not_ok` / `approve_blocked_no_
generalization` — a silent, permanent promotion freeze on the whole backlog, indis-
tinguishable from an empty queue.

So this asserts the legacy doc's decisions are IDENTICAL to the modern one's, on both S9
paths: the cron sweep and the human approve.
"""

from __future__ import annotations

import copy
from dataclasses import replace

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.promotion import PromotionScheduler

from .helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
)

KEY = "sha256:single-bp"


def _sched(store: InMemoryCandidateStore) -> PromotionScheduler:
    return PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 5}),
        dependency_resolver=FakeDependencyResolver(set()),
        policy=promotion_policy(),
        clock=lambda: "2026-07-03T12:00:00+00:00",
    )


def _as_persisted_before_the_check(env: CandidateEnvelope) -> CandidateEnvelope:
    """The SAME envelope with the key deleted — i.e. what `to_doc` produced last week."""
    payload = copy.deepcopy(env.payload)
    del payload["generalization"]["static_validation"]["date_literal_ok"]
    return replace(env, payload=payload)


def test_the_legacy_stamp_rehydrates_as_passing_not_as_missing():
    """`_generalization` returns None on `KeyError`, and None is treated as `static_not_ok`
    — so "the field is absent" and "the check failed" would arrive at the guard as the same
    thing. Assert the object, not just the absence of an exception."""
    env = _as_persisted_before_the_check(make_blueprint_candidate(canonical_key=KEY))
    assert "date_literal_ok" not in env.payload["generalization"]["static_validation"]

    gen = _sched(InMemoryCandidateStore())._generalization(env)

    assert gen is not None
    assert gen.static_validation.date_literal_ok is True
    assert gen.static_validation.outcome == "ok"


async def test_the_cron_sweep_decides_a_legacy_candidate_exactly_as_a_modern_one():
    decisions = []
    for build in (_as_persisted_before_the_check, lambda env: env):
        store = InMemoryCandidateStore()
        await store.put(
            build(
                make_blueprint_candidate(
                    status=CandidateStatus.CANDIDATE, canonical_key=KEY
                )
            )
        )
        decisions.append((await _sched(store).run_once()).decisions)

    legacy, modern = decisions
    assert legacy == modern


async def test_a_human_can_still_approve_a_legacy_candidate():
    """Guard 5 of `apply_human_decision` re-reads the S4 stamp before a human approve can
    produce `validated` (D98: approval never substitutes for static + replay). A legacy
    stamp must clear it."""
    env = _as_persisted_before_the_check(
        make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    )
    store = InMemoryCandidateStore()
    await store.put(env)

    decision = await _sched(store).apply_human_decision(env, "approve")

    assert decision.action == "approve"
    assert decision.to_status == CandidateStatus.VALIDATED
    assert decision.reason is None
