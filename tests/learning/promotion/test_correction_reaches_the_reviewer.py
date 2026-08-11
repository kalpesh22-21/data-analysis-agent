"""A user correction is CARRIED to the reviewer, not just survived (plan §4 follow-up).

Routing to review is what stops a corrected artifact silently re-landing — but it also
makes a human the ONLY thing between that artifact and the corpus. The approve path
re-runs static validation and the golden replay, and NEITHER can see a value error: the
replay is structure-only by design (D98) and there is no value oracle (D17). So without
this, the reviewer adjudicates the exact artifact a user flagged with strictly LESS
information than the machine had a moment earlier.

The evidence is destroyed in-flight, which is why the capture has to be where it is:
`apply_user_correction` stamps `suspect` with no probes, and Guard 3 of the very next
examination replays, PASSES, and overwrites that stamp with `clean`.

Slugs:
  * S9-correction-captured-before-guard-3
  * S9-correction-reaches-the-inbox-item
  * S9-correction-mark-is-sticky
"""

from __future__ import annotations

from dataclasses import replace

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import DriftStamp
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.promotion import GRAIN_INTEGRITY, PromotionScheduler
from data_agent.learning.promotion.scheduler import (
    ROUTE_REASON_USER_CORRECTED,
    _is_user_correction_stamp,
)

from .helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
)

KEY = "sha256:single-bp"


class _Clock:
    def __init__(self, iso: str = "2026-08-10T12:00:00+00:00") -> None:
        self.iso = iso

    def __call__(self) -> str:
        return self.iso


def _sched(store, *, clock=None):
    return PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 5}),
        dependency_resolver=FakeDependencyResolver(),
        policy=promotion_policy(),
        clock=clock or _Clock(),
    )


async def _corrected_then_routed(store, sched) -> CandidateEnvelope:
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    await sched.apply_human_decision(env, "approve")
    await sched.apply_user_correction(await store.get(env.candidate_id))
    await sched.run_once()
    return await store.get(env.candidate_id)


# --- the stamp is identified structurally -------------------------------------


def test_the_correction_stamp_is_recognised_by_its_absent_probe_list():
    """Identified STRUCTURALLY — `suspect` naming no probe — because that is literally
    what the stamp is: a suspect verdict for which no probe fired, the cause being an
    out-of-band human correction. `reusable_replay_verdict` keys off the same absence, so
    the two agree without either importing a flag from the other."""
    correction = DriftStamp(
        status="suspect", last_drift_check_at="2026-08-10T12:00:00+00:00",
        probes=(), failed_probe=None,
    )
    assert _is_user_correction_stamp(correction) is True


def test_a_real_failed_probe_is_not_mistaken_for_a_correction():
    """A drift-suspect DEMOTE names its failed probe. Confusing the two would tell a
    reviewer a user complained when in fact the warehouse schema moved — a different
    problem with a different fix."""
    probe_failure = DriftStamp(
        status="suspect", last_drift_check_at="2026-08-10T12:00:00+00:00",
        probes=(GRAIN_INTEGRITY,), failed_probe=GRAIN_INTEGRITY,
    )
    assert _is_user_correction_stamp(probe_failure) is False


def test_a_clean_or_unchecked_stamp_is_not_a_correction():
    assert _is_user_correction_stamp(DriftStamp()) is False
    assert (
        _is_user_correction_stamp(
            DriftStamp(status="clean", probes=(GRAIN_INTEGRITY,))
        )
        is False
    )


# --- the capture survives Guard 3 ---------------------------------------------


async def test_the_correction_is_captured_before_the_replay_erases_it():
    """The ordering that makes this work at all. Guard 3 replays, PASSES (the correction
    was about a value), and overwrites `drift` with `clean` — so the capture has to
    happen at the top of `_advance_candidate` or there is nothing left to capture."""
    store = InMemoryCandidateStore()
    routed = await _corrected_then_routed(store, _sched(store))

    assert routed.status == CandidateStatus.IN_REVIEW
    assert routed.drift.status == "clean"  # the evidence really is gone from `drift`
    assert routed.route_reason == ROUTE_REASON_USER_CORRECTED


async def test_the_route_decision_carries_the_reason_too():
    """The sweep's own copy, so the rate is countable in telemetry without joining back
    to the envelope. `reason` on a non-hold decision was `None` everywhere until now."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    sched = _sched(store)
    await sched.apply_human_decision(env, "approve")
    await sched.apply_user_correction(await store.get(env.candidate_id))

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert sweep.decisions[0].reason == ROUTE_REASON_USER_CORRECTED


async def test_an_ordinary_route_carries_no_reason():
    """The negative control. If every route were marked, the mark would mean nothing."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)

    sweep = await _sched(store).run_once()

    assert sweep.decisions[0].action == "route"
    assert sweep.decisions[0].reason is None
    assert (await store.get(env.candidate_id)).route_reason is None


# --- it reaches the human -----------------------------------------------------


async def test_the_reviewer_sees_the_correction_on_the_inbox_item():
    """The whole point: the reviewer is now the only gate, so they must be told. The
    approve they are about to click re-runs static validation and the replay, and neither
    can see the value error the user reported."""
    store = InMemoryCandidateStore()
    await _corrected_then_routed(store, _sched(store))
    inbox = ReviewInbox(store)

    items = await inbox.list()

    assert [i.route_reason for i in items] == [ROUTE_REASON_USER_CORRECTED]


async def test_the_mark_is_sticky_across_later_clean_cycles():
    """A negative signal a later success can erase is not a signal — it is the same shape
    as the erasure this whole routing change was written to fix.

    Here the candidate is corrected, routed, rejected back to `candidate` by a human's
    change of mind... actually simpler: it is demoted again by a clean-path re-check and
    re-routed. The mark must survive a route that saw no correction."""
    store = InMemoryCandidateStore()
    sched = _sched(store)
    routed = await _corrected_then_routed(store, sched)
    assert routed.route_reason == ROUTE_REASON_USER_CORRECTED

    # Put it back in the candidate scan with a CLEAN stamp — the second pass sees no
    # correction at all and must not overwrite the mark with `None`.
    await store.put(
        replace(
            routed,
            status=CandidateStatus.CANDIDATE,
            drift=DriftStamp(status="clean", probes=(GRAIN_INTEGRITY,)),
        )
    )
    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert (await store.get(routed.candidate_id)).route_reason == (
        ROUTE_REASON_USER_CORRECTED
    )


async def test_the_mark_round_trips_through_the_persisted_doc():
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    marked = replace(env, route_reason=ROUTE_REASON_USER_CORRECTED)
    assert CandidateEnvelope.from_doc(marked.to_doc()).route_reason == (
        ROUTE_REASON_USER_CORRECTED
    )
    # Additive + optional: an unmarked envelope emits no key at all.
    assert "route_reason" not in env.to_doc()


def test_a_non_string_route_reason_reads_as_absent():
    """It is RENDERED into a reviewer-facing wire field, so a dict or a list here would
    reach the review UI as a repr. A foreign writer must not be able to put arbitrary
    structure in front of a human through this path."""
    doc = {
        "candidate_id": "c::1", "type": "blueprint", "status": "in_review",
        "payload": {}, "entity_scan": {}, "content_hash": "h",
        "route_reason": {"evil": "<script>"},
    }
    assert CandidateEnvelope.from_doc(doc).route_reason is None
