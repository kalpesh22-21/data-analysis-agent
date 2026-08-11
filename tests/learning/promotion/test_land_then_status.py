"""Layer-1 — the S9 land-then-status invariant (S9-activation Slice 2, §3.1).

"Not landed ⇒ not validated": on the `→ validated` edge the scheduler LANDS into the
neo4j retrieval corpus FIRST, then CAS-writes `status = validated`. Proven with a fake
landing writer (no real neo4j):

  * `S9-landing-failure-holds` — a writer that RAISES ⇒ status is NOT advanced; the
    candidate HOLDS `landing_failed` and stays `in_review`;
  * `S9-land-then-status-idempotent` — a crash AFTER land, BEFORE the status write
    (the store `put` fails once) leaves the candidate un-promoted; the retry re-lands
    IDEMPOTENTLY (same deterministic id) then writes `validated`;
  * the full `→ validated` happy path (real replay-pass + a fake landing writer) ⇒
    landed BEFORE the status flip, then `validated`.

**MIGRATED BY PLAN §4, from the cron edge to the human-approve edge.** Every assertion
below used to be driven through `run_once` on a `candidate`, because the auto path landed.
It no longer does: `_advance_candidate` ends at `in_review` and `apply_human_decision`'s
approve is the ONLY caller of `_land_and_promote`. The invariants are unchanged and are
still the invariants of the one edge that can produce a recallable artifact — so the
tests were re-pointed rather than deleted, and
`test_the_auto_path_never_lands_anything` pins the new absence directly.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import data_agent.learning.promotion.scheduler as scheduler_mod
from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.promotion import PromotionScheduler
from data_agent.learning.promotion.landing import CorpusLandingWriter, landing_id
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

from .helpers import (
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
)

KEY = "sha256:single-bp"


class _NoSessionDriver:
    """A neo4j driver double that RAISES if `session()` is opened — proves the entity
    last gate fires BEFORE any neo4j write."""

    def session(self, **_: object) -> object:
        raise AssertionError("neo4j must not be opened when the entity defense raises")


def _entity_bearing_candidate() -> CandidateEnvelope:
    """A candidate whose intent still carries an entity ("0420") AND whose settled
    (passed) scan names it — a strip-regressed envelope for the last-gate test."""
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    payload = dict(env.payload)
    payload["intent"] = "total earnings for department 0420 in a given year"
    return replace(
        env,
        payload=payload,
        entity_scan={
            "result": "pass",
            "hits": [{"field": "intent", "kind": "dept_code", "span": "0420"}],
            "scanned_fields": ["intent"],
            "scanner": "regex+ner+llm",
        },
    )


def _scheduler(store, *, writer, probe=None):
    return PromotionScheduler(
        store,
        probe=probe or FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 5}),
        policy=promotion_policy(),
        landing_writer=writer,
        require_landing=True,
        clock=lambda: "2026-07-05T00:00:00+00:00",
    )


# --- S9-landing-failure-holds ---------------------------------------------------


async def test_landing_failure_holds_and_never_validates() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter(fail=RuntimeError("neo4j down"), fail_times=99)
    sched = _scheduler(store, writer=writer)

    decision = await sched.apply_human_decision(env, "approve")

    assert writer.calls == 1  # the land was ATTEMPTED
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    assert decision.action == "hold"
    assert decision.reason == "landing_failed"


async def test_the_auto_path_never_lands_anything() -> None:
    """PLAN §4, stated as its own assertion rather than left implicit in the migration
    above: the cron routes to `in_review` and touches the landing writer's `land` NOT AT
    ALL. Deleting the `in_review` destination and restoring the old promote would make
    every other test in this file pass again (they drive the approve edge), so the
    absence needs its own pin."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, writer=writer)

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    assert writer.landed == []
    assert writer.calls == 0


async def test_human_approve_landing_failure_holds_at_in_review() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter(fail=RuntimeError("neo4j down"), fail_times=99)
    sched = _scheduler(store, writer=writer)

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.action == "hold"
    assert decision.reason == "landing_failed"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


# --- S9-land-then-status-idempotent (crash after land, before status) -----------


class _PutFailsOnceStore(InMemoryCandidateStore):
    """An in-memory store whose FIRST `put` of a VALIDATED envelope RAISES — models a
    crash AFTER the land succeeded but BEFORE the status write commits."""

    def __init__(self) -> None:
        super().__init__()
        self._validated_puts = 0

    async def put(self, envelope: CandidateEnvelope) -> None:
        if envelope.status == CandidateStatus.VALIDATED:
            self._validated_puts += 1
            if self._validated_puts == 1:
                raise RuntimeError("crash between land and status write")
        await super().put(envelope)


async def test_crash_between_land_and_status_re_lands_idempotently() -> None:
    store = _PutFailsOnceStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, writer=writer)

    # Approve 1: land succeeds, the status write CRASHES → the candidate is unchanged.
    # Not landed ⇒ not validated holds in the SAFE direction (a landed-but-not-validated
    # blueprint, healed by the retry). The crash escapes to the caller here, where the
    # cron used to swallow it in `_guard` — a human clicking approve gets an error rather
    # than a silent no-op, which is the correct difference between the two edges.
    with pytest.raises(RuntimeError):
        await sched.apply_human_decision(env, "approve")
    assert writer.calls == 1
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW

    # Approve 2 (the human retries): re-land (idempotent — SAME deterministic id) then
    # write validated.
    await sched.apply_human_decision(env, "approve")
    assert writer.calls == 2
    assert landing_id(writer.landed[0]) == landing_id(writer.landed[1])  # MERGE dedupes
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED


# --- the full happy path: real replay-pass + hit_count≥T + fake writer -----------


async def test_full_candidate_to_validated_lands_before_status() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, writer=writer)

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.action == "approve"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    # Land BEFORE status: the landed envelope was still `in_review` when it landed
    # (the writer ran before the `validated` CAS).
    assert len(writer.landed) == 1
    assert writer.landed[0].status == CandidateStatus.IN_REVIEW
    assert landing_id(writer.landed[0]) == f"bp::{KEY}"


async def test_full_human_approve_lands_then_validates() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, writer=writer)

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.action == "approve"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    assert len(writer.landed) == 1
    assert writer.landed[0].status == CandidateStatus.IN_REVIEW


# --- the landed seed carries the FRESH drift, not the stale pre-promotion stamp ----


async def test_landed_env_carries_fresh_drift_not_stale() -> None:
    """Review S1 (§8.1): the env handed to the writer carries the FRESH drift stamp
    (clean, from the passing replay), NOT the stale pre-promotion `unchecked`."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    assert env.drift.status == "unchecked"  # pre-promotion
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, writer=writer)

    await sched.apply_human_decision(env, "approve")

    assert writer.landed[0].drift.status == "clean"  # fresh, stamped before landing
    assert (await store.get(env.candidate_id)).drift.status == "clean"


# --- the D17 last gate FIRES through the real scheduler path (review BLOCKER) -------


async def test_entity_last_gate_fires_through_scheduler_if_strip_regresses(monkeypatch) -> None:
    """If the entity strip REGRESSES to a no-op, the last gate STILL blocks the write:
    the scheduler captures the forbidden spans BEFORE the strip, the (regressed) strip
    leaves the entity in the seed, the real writer RAISES → `landing_failed`, and the
    candidate never validates. Proven through the REAL `apply_human_decision` path — the
    only path that lands anything since plan §4."""
    # Simulate a strip regression: strip becomes identity, so "0420" survives into the
    # seed. The pre-strip forbidden-span capture is what makes the gate still fire.
    monkeypatch.setattr(scheduler_mod, "strip_entity_bearing", lambda env: env)

    store = InMemoryCandidateStore()
    env = replace(_entity_bearing_candidate(), status=CandidateStatus.IN_REVIEW)
    await store.put(env)
    embedder = FakeEmbeddingClient()
    writer = CorpusLandingWriter(_NoSessionDriver(), embedder, model_id="all-mpnet-base-v2")
    sched = _scheduler(store, writer=writer)

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.action == "hold"
    assert decision.reason == "landing_failed"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    assert embedder.calls == []  # the gate fired BEFORE any embed/neo4j write


async def test_scheduler_forwards_pre_strip_forbidden_spans() -> None:
    """The scheduler hands the writer the PRE-strip entity spans (D17 last gate) even
    though a validated candidate's own `entity_scan` gets blanked by the strip."""
    store = InMemoryCandidateStore()
    env = replace(_entity_bearing_candidate(), status=CandidateStatus.IN_REVIEW)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, writer=writer)

    await sched.apply_human_decision(env, "approve")

    assert writer.forbidden_spans == [("0420",)]
