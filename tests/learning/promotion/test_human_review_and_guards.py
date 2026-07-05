"""S9 human-review transitions + remaining promotion guards (Contract D/E, D52/D58c).

`in_review` human approve → validated / reject → rejected (the ONE caller-driven
path); a blueprint approval still passes the static + replay safety guards (human
approval substitutes for the hit-count threshold, not for structural integrity).
Plus: static-not-ok holds; the kill-switch halts the whole cycle.
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
    with_type,
)

KEY = "sha256:single-bp"


def _scheduler(store, *, probe=None, hits=None):
    return PromotionScheduler(
        store,
        probe=probe or FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(hits or {}),
        dependency_resolver=FakeDependencyResolver(),
        policy=PromotionPolicy(blueprint_hit_threshold=3),
        clock=lambda: "2026-07-03T12:00:00+00:00",
    )


async def test_human_approve_blueprint_in_review_promotes_after_replay():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store)  # hit_count 0 — human approval is the promotion path

    decision = await sched.apply_human_decision(env, "approve")

    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    assert decision.action == "approve"


async def test_human_reject_archives_as_rejected():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store)

    decision = await sched.apply_human_decision(env, "reject")

    assert (await store.get(env.candidate_id)).status == CandidateStatus.REJECTED
    assert decision.action == "reject"


async def test_human_approve_knowledge_promotes_without_replay():
    store = InMemoryCandidateStore()
    env = with_type(
        make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY),
        "global_knowledge",
    )
    await store.put(env)
    probe = FakeWarehouseProbe()
    sched = _scheduler(store, probe=probe)

    decision = await sched.apply_human_decision(env, "approve")

    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    assert decision.action == "approve"
    assert probe.calls == []  # no template to replay for a knowledge target


async def test_human_approve_blueprint_blocked_when_replay_fails():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.IN_REVIEW, canonical_key=KEY, grain_verifiable=True
    )
    await store.put(env)
    probe = FakeWarehouseProbe(row_count=10, distinct_grain_count=5)  # grain fails
    sched = _scheduler(store, probe=probe)

    decision = await sched.apply_human_decision(env, "approve")

    # Human approval does NOT override a failed structural replay (D98).
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    assert decision.action == "hold"


class _RaisingProbe:
    """A warehouse probe that RAISES (mirrors the deferred stub in
    `run_learning_scheduler.py`, or a warehouse/query service that is down)."""

    async def run(self, sql: str, *, grain_columns, column_scope=()):
        raise NotImplementedError("no warehouse probe wired")


async def test_human_approve_blueprint_with_raising_probe_holds_not_raises():
    """A probe that RAISES must degrade to a clean HOLD on the human approve path —
    never an uncaught exception out of `apply_human_decision` (which would surface a
    500 on a future inbox UI). `golden_replay` catches the probe failure →
    `probe_unavailable`, so the blueprint holds fail-closed at in_review."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.IN_REVIEW, canonical_key=KEY, grain_verifiable=True
    )
    await store.put(env)
    sched = _scheduler(store, probe=_RaisingProbe())

    decision = await sched.apply_human_decision(env, "approve")  # must NOT raise

    assert decision.action == "hold"
    assert decision.reason == "approve_blocked_replay:probe_unavailable"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


async def test_human_approve_blueprint_without_generalization_fails_closed():
    """S3: a blueprint whose `generalization` is absent/malformed (`from_doc`→None)
    must NOT approve directly as 'non-replayable' — that would bypass BOTH static and
    replay (D98: human approval never substitutes for replay). It HOLDS fail-closed."""
    from dataclasses import replace

    store = InMemoryCandidateStore()
    base = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    # Strip the generalization → a blueprint with no replayable template.
    env = replace(
        base,
        payload={k: v for k, v in base.payload.items() if k != "generalization"},
    )
    await store.put(env)
    sched = _scheduler(store)

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.action == "hold"
    assert decision.reason == "approve_blocked_no_generalization"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


async def test_static_not_ok_holds_candidate():
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.CANDIDATE, canonical_key=KEY, static_ok=False
    )
    await store.put(env)
    sched = _scheduler(store, hits={KEY: 5})  # above T, but static failed

    sweep = await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE
    assert sweep.decisions[0].reason == "static_not_ok"


async def test_kill_switch_disables_the_cycle(monkeypatch):
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store, hits={KEY: 5})
    monkeypatch.setattr(
        "data_agent.learning.promotion.scheduler.learning_enabled", lambda: False
    )

    sweep = await sched.run_once()

    assert sweep.disabled is True
    assert sweep.decisions == ()
    # No transition happened while disabled.
    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE
