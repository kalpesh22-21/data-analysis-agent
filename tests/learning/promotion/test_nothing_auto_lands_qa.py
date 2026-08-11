"""QA (plan §4, item 1) — independent verification that NOTHING auto-lands, and that
the human-approve edge still carries every guard the auto edge used to carry.

The slice's central safety claim is that the graph is written on a human approve and on
nothing else. That claim is easy to satisfy for one candidate on one cycle and easy to
break in the ways that only show up later: a candidate that cycles back round, a
`validated` row the re-check touches, a knowledge chunk, a demoted-then-re-routed
artifact. So the first half of this file drives a MIXED population through many cycles
with a real landing writer wired and asserts on the WRITER, not on the decision list — a
decision that says `route` while the writer recorded a `land` would be exactly the bug.

The second half re-walks the approve edge's guard list one by one.

HISTORY, kept because it is the point of the file. On first writing, four of the five
guards held and the fifth — the LEAKAGE scan — was absent from the approve edge entirely,
recorded here as three strict-xfails. That was filed as an accepted limitation and
REJECTED as one on review: a strict-xfail says "we know, we accept this, tell us when it
changes", which is right for the scratch-join gap and wrong for a live path on which
unscanned entity-bearing text reaches a corpus the agent recalls from — especially once
plan §4 made approve the only edge that lands. The guard was added
(`_entity_scan_is_actionable`) and all three flipped to the plain passing assertions
below, keeping their original names so the history stays greppable.

The fix went through two shapes, and the second is the one worth remembering: a guard
demanding a SETTLED verdict closes the `pending` symptom and leaves the
settled-finding-with-no-spans one open, because that verdict is settled. Only a predicate
derived from what the strip CONSUMES — an empty span set is safe iff a clean `pass`
explains the emptiness — covers both.

Slugs:
  * S9-qa-nothing-lands-without-approve
  * S9-qa-approve-guard-matrix
  * S9-qa-approve-leakage-guard          (was a strict-xfail; now enforced)
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import DriftStamp
from data_agent.learning.promotion import PromotionScheduler
from data_agent.learning.writer.routing import route_candidate

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
CLOCK = "2026-08-10T12:00:00+00:00"
FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"
ENTITY = "0420"


class _MovableClock:
    """An injected clock a test can step forward.

    Needed by any test that has to cross the slice-1.5 replay re-check window: a stored
    `suspect` verdict naming `grain_integrity` is REUSED for
    `replay_recheck_interval_seconds`, so cycling `run_once` against a frozen instant
    re-reads the same cached "no" for ever. That looks exactly like a stalled loop and is
    in fact the rate limit working."""

    def __init__(self, start: str = CLOCK) -> None:
        self._at = datetime.fromisoformat(start)

    def __call__(self) -> str:
        return self._at.isoformat()

    def advance(self, *, hours: float) -> None:
        self._at += timedelta(hours=hours)


def _sched(store, *, writer=None, hits=5, probe=None, resolved=None, clock=None, **overrides):
    return PromotionScheduler(
        store,
        probe=probe or FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: hits}),
        dependency_resolver=FakeDependencyResolver(resolved if resolved else set()),
        policy=promotion_policy(**overrides),
        landing_writer=writer,
        require_landing=writer is not None,
        clock=clock if clock is not None else (lambda: CLOCK),
    )


# =============================================================================
# 1. Nothing lands without an approve — a mixed population, many cycles
# =============================================================================


async def test_a_mixed_population_over_many_cycles_lands_nothing() -> None:
    """Every shape the scan can meet, driven 50 times with a real landing writer.

    Fifty cycles rather than one because the failure this guards against is not "the
    route edge lands" — that is a one-liner to check — but "some path OTHER than approve
    eventually lands": a re-examined candidate, a validated row the drift re-check
    refreshes, a knowledge chunk that falls through the blueprint branch. The assertion
    is on `writer.land`, which is the only call that creates a recallable node;
    `update_status` is a STAMP on an existing node and is expected to fire."""
    store = InMemoryCandidateStore()
    population = {
        "plain": make_blueprint_candidate(
            status=CandidateStatus.CANDIDATE, canonical_key=KEY
        ),
        "already_validated": replace(
            make_blueprint_candidate(
                status=CandidateStatus.VALIDATED, canonical_key=KEY
            ),
            candidate_id="candidate::validated::0",
        ),
        "knowledge": replace(
            with_type(
                make_blueprint_candidate(
                    status=CandidateStatus.CANDIDATE, canonical_key=KEY
                ),
                "global_knowledge",
            ),
            candidate_id="candidate::knowledge::0",
        ),
        "no_key": replace(
            make_blueprint_candidate(
                status=CandidateStatus.CANDIDATE, canonical_key=None
            ),
            candidate_id="candidate::nokey::0",
        ),
        "suspect_drift": replace(
            make_blueprint_candidate(
                status=CandidateStatus.VALIDATED,
                canonical_key=KEY,
                drift=DriftStamp(status="suspect", last_drift_check_at=CLOCK),
            ),
            candidate_id="candidate::suspect::0",
        ),
    }
    for env in population.values():
        await store.put(env)
    writer = FakeLandingWriter()
    sched = _sched(store, writer=writer)

    for _ in range(50):
        await sched.run_once()

    assert writer.landed == [], "the cron LANDED something; only an approve may land"
    assert writer.calls == 0
    assert writer.verified == []
    final = {name: (await store.get(env.candidate_id)).status
             for name, env in population.items()}
    assert final == {
        "plain": CandidateStatus.IN_REVIEW,
        # A validated row stays validated (the drift re-check is not a landing edge) —
        # it was already validated before the cron ever saw it.
        "already_validated": CandidateStatus.VALIDATED,
        "knowledge": CandidateStatus.CANDIDATE,   # human-gated, held
        "no_key": CandidateStatus.IN_REVIEW,      # the corroboration floor, item 2
        "suspect_drift": CandidateStatus.VALIDATED,
    }


async def test_a_demoted_artifact_re_routes_but_never_re_lands() -> None:
    """The cycle that a one-shot test cannot see: validated → demote → candidate → route.

    A blueprint that has ALREADY landed once is the most tempting thing for a scheduler to
    quietly re-land — the node exists, the id is deterministic, the write is idempotent.
    It must still take a human to put it back.

    **THE CLOCK HAS TO MOVE, and the first version of this test did not move it.** It ran
    ten cycles against the pinned module `CLOCK` and asserted the demoted row had
    re-routed; it had not, and the reason is the slice-1.5 replay rate limit, not a
    routing defect. The failing probe stamps a `suspect` verdict NAMING `grain_integrity`,
    which is a reusable replay verdict, so Guard 3 holds `replay_failed:cached_suspect`
    for the whole `replay_recheck_interval_seconds` window rather than re-probing — by
    design, so a broken blueprint does not cost two warehouse queries every five minutes.
    Ten cycles at a frozen instant are ten cycles inside that window. Under the real
    `_now_iso()` the window elapses and the candidate converges, which is what the second
    half below drives.

    Both halves are asserted, because they fail for opposite reasons: a re-route that
    never happens is a stalled loop, and a re-land is a corpus write nobody approved."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    clock = _MovableClock()
    # A probe that FAILS the replay demotes it; the demoted row then re-enters the
    # candidate scan, where a fresh (passing) probe would previously have re-promoted.
    failing = FakeWarehouseProbe(columns=("wrong_column",))
    sched = _sched(store, writer=writer, probe=failing, clock=clock)
    await sched.run_once()
    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE

    # The warehouse recovers, but the cached `suspect` verdict is still fresh: the
    # scheduler deliberately does NOT re-probe, and the row stays put. Pinned explicitly
    # so the rate limit is visible here rather than mistaken for a stall.
    healthy = _sched(store, writer=writer, probe=FakeWarehouseProbe(), clock=clock)
    for _ in range(10):
        await healthy.run_once()
    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE

    # A day later the verdict has expired, the real probe runs, it passes — and the
    # candidate goes to a HUMAN, not back into the corpus.
    clock.advance(hours=25)
    await healthy.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    assert writer.landed == []


async def test_run_forever_lands_nothing_either() -> None:
    """The daemon entrypoint, not just `run_once`. `run_forever` is what production
    actually calls, and it is a different code path (the try/except wrapper)."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _sched(store, writer=writer)

    ticks = 0

    async def _sleep(_seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= 12:
            raise _StopError

    class _StopError(Exception):
        """Breaks out of the daemon loop after a fixed number of ticks."""

    with pytest.raises(_StopError):
        await sched.run_forever(sleep=_sleep)

    assert writer.landed == []
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


# =============================================================================
# 2. The approve edge's guard matrix — four guards that DO still fire
# =============================================================================


async def test_approve_still_blocks_on_static_validation() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.IN_REVIEW, canonical_key=KEY, static_ok=False
    )
    await store.put(env)
    writer = FakeLandingWriter()

    decision = await _sched(store, writer=writer).apply_human_decision(env, "approve")

    assert decision.action == "hold"
    assert decision.reason == "approve_blocked_static_not_ok"
    assert writer.landed == []


async def test_approve_still_blocks_on_an_unresolved_dependency() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.IN_REVIEW, canonical_key=KEY, depends_on=("schema_edit::x",)
    )
    await store.put(env)
    writer = FakeLandingWriter()

    decision = await _sched(store, writer=writer).apply_human_decision(env, "approve")

    assert decision.action == "hold"
    assert decision.reason == "approve_blocked_depends_on_unresolved"
    assert writer.landed == []


async def test_approve_still_runs_and_blocks_on_the_golden_replay() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    probe = FakeWarehouseProbe(columns=("a_column_the_signature_does_not_declare",))

    decision = await _sched(store, writer=writer, probe=probe).apply_human_decision(
        env, "approve"
    )

    assert probe.calls, "the replay must actually run on the approve edge"
    assert decision.action == "hold"
    assert decision.reason.startswith("approve_blocked_replay:")
    assert writer.landed == []


async def test_approve_still_blocks_on_the_landing_gate() -> None:
    """`require_landing` with no writer: approve refuses rather than minting a
    `validated` artifact nothing can recall."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    sched = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 5}),
        dependency_resolver=FakeDependencyResolver(),
        policy=promotion_policy(),
        landing_writer=None,
        require_landing=True,
        clock=lambda: CLOCK,
    )

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.action == "hold"
    assert decision.reason == "approve_blocked_landing_unavailable"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


async def test_approve_still_refuses_from_the_wrong_status() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()

    decision = await _sched(store, writer=writer).apply_human_decision(env, "approve")

    assert decision.reason == "approve_not_in_review"
    assert writer.landed == []


async def test_approve_strips_a_settled_leakage_hit_before_landing() -> None:
    """The half of D17 that DOES work: a SETTLED verdict with enumerated hits is stripped
    out of the payload and handed to the writer's tripwire as forbidden spans."""
    store = InMemoryCandidateStore()
    env = _entity_bearing(
        {
            "result": "reroute",
            "scanner": "regex",
            "scanned_fields": ["intent"],
            "hits": [{"field": "intent", "kind": "employee_id", "span": ENTITY}],
        }
    )
    await store.put(env)
    writer = FakeLandingWriter()

    await _sched(store, writer=writer).apply_human_decision(env, "approve")

    assert writer.forbidden_spans == [(ENTITY,)]
    assert ENTITY not in json.dumps(writer.landed[0].payload)


# =============================================================================
# 3. THE HOLE — the approve edge has no leakage guard (strict-xfail)
# =============================================================================


def _entity_bearing(scan: dict) -> CandidateEnvelope:
    """An `in_review` blueprint whose intent carries a raw entity, with *scan* as its
    S5 verdict. Built from the frozen S4 fixture so the rest of the shape is real."""
    doc = copy.deepcopy(
        json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())["single"][
            "envelope"
        ]
    )
    doc["status"] = CandidateStatus.IN_REVIEW
    doc["payload"]["intent"] = f"total earnings for employee {ENTITY}"
    doc["entity_scan"] = scan
    return CandidateEnvelope.from_doc(doc)


_UNSETTLED = {"result": "pending", "scanner": "self_check", "hits": []}
_SETTLED_NO_HITS = {
    "result": "quarantine",
    "scanner": "regex",
    "scanned_fields": ["intent"],
    "hits": [],
}


async def test_the_writer_really_does_route_an_unsettled_scan_to_a_human() -> None:
    """Reachability, established before the xfails below claim anything.

    Both shapes are routed to `in_review` by the REAL writer rule — deliberately, so a
    human decides. That is what makes the missing guard on the approve edge a live path
    rather than a hypothetical one, and it is why it matters more now: before this slice
    the queue was empty, and a hole on the approve edge was a hole in a door nobody
    opened."""
    assert route_candidate(
        _entity_bearing(_UNSETTLED), sampled_for_inbox=False
    ).status == CandidateStatus.IN_REVIEW
    assert route_candidate(
        _entity_bearing(_SETTLED_NO_HITS), sampled_for_inbox=False
    ).status == CandidateStatus.IN_REVIEW


async def test_the_cron_edge_still_refuses_an_unsettled_scan() -> None:
    """The guard EXISTS — on the edge that can no longer produce a recallable artifact.
    `_entity_scan_is_clean` is Guard 0 of `_advance_candidate`, and since plan §4 the
    most that guard can now prevent is a row appearing in a queue."""
    store = InMemoryCandidateStore()
    env = replace(_entity_bearing(_UNSETTLED), status=CandidateStatus.CANDIDATE)
    await store.put(env)

    sweep = await _sched(store).run_once()

    assert sweep.decisions[0].reason == "entity_scan_not_pass"


async def test_approve_must_not_land_an_unsettled_leakage_scan() -> None:
    """WAS A STRICT-XFAIL (QA finding, plan §4 review). Now a plain passing assertion.

    THE HOLE, as filed: `apply_human_decision`'s approve had no leakage guard at all. An
    UNSETTLED (`pending`) scan makes `entity_spans` return `()`, so the D17 strip is a
    no-op AND the landing writer's last-gate tripwire receives no forbidden spans — the
    raw entity landed in the globally-recallable corpus. The cron edge refuses this exact
    envelope (`_entity_scan_is_clean`, its Guard 0), but plan §4 made approve the ONLY
    edge that lands, so the single remaining door was the unguarded one.

    NOT converted to a plain assertion by weakening it: `_entity_scan_is_actionable` is
    now Guard 2 of the approve edge and this envelope is refused."""
    store = InMemoryCandidateStore()
    env = _entity_bearing(_UNSETTLED)
    await store.put(env)
    writer = FakeLandingWriter()

    decision = await _sched(store, writer=writer).apply_human_decision(env, "approve")

    assert decision.to_status != CandidateStatus.VALIDATED, (
        "an unsettled leakage scan must not be approvable into the corpus"
    )
    assert decision.reason == "approve_blocked_entity_scan_not_actionable"
    assert writer.landed == []


async def test_approve_must_not_land_a_settled_non_pass_verdict_with_no_spans() -> None:
    """WAS A STRICT-XFAIL (QA finding). Now a plain passing assertion.

    THE HOLE, as filed: the D17 strip is keyed on the verdict's HITS, not on its RESULT.
    A settled non-`pass` verdict naming no spans — a scanner saying "this leaked" without
    enumerating where — stripped nothing and landed the raw payload.

    This is the shape that defeated the first attempt at the fix. A guard that merely
    demanded a SETTLED verdict admitted this one, because it IS settled; the reasoning
    "a settled finding is the state in which the machinery works" is only true when the
    finding is LOCALIZED. And it is reachable with the shipped scanner rather than
    hypothetical: `gate._decide` returns `reroute` for a `user_fact` classification and
    `quarantine` as its fallback, neither of which consults whether `hits` is empty.

    The predicate that covers both symptoms is derived from the OPERATION rather than
    from the verdict vocabulary — an empty span set is only safe when a clean `pass`
    EXPLAINS the emptiness. See `_entity_scan_is_actionable`."""
    store = InMemoryCandidateStore()
    env = _entity_bearing(_SETTLED_NO_HITS)
    await store.put(env)
    writer = FakeLandingWriter()

    decision = await _sched(store, writer=writer).apply_human_decision(env, "approve")

    assert decision.reason == "approve_blocked_entity_scan_not_actionable"
    assert writer.landed == [] or ENTITY not in json.dumps(writer.landed[0].payload)


async def test_the_approve_edge_has_no_leakage_guard_at_all() -> None:
    """WAS A STRICT-XFAIL, kept under its original name so the history is greppable — it
    now asserts the OPPOSITE of what it was written to record.

    THE HOLE, as filed: `_entity_scan_is_clean` was referenced exactly once in the
    scheduler, inside `_advance_candidate`. The plan-§4 module docstring claimed "every
    correctness guard is unchanged and still runs on this edge: the leakage scan, static
    validation, `depends_on` resolution and the golden replay" — three of those four ran
    on the approve edge. The leakage scan did not.

    Asserted on the SOURCE rather than on behaviour on purpose, because the two
    behavioural tests above each pin one symptom and a narrow fix could satisfy either
    while leaving the edge structurally unguarded. What must hold is that the approve edge
    consults a leakage predicate AT ALL.

    The two predicates are deliberately different, and that asymmetry is the policy: the
    auto edge demands a clean `pass` because nobody is looking; the approve edge demands
    an ACTIONABLE verdict, admitting a localized finding so that D58b's
    100%-of-near-misses-to-a-human routing has a reachable approve."""
    import inspect

    import data_agent.learning.promotion.scheduler as sched_mod

    approve_src = inspect.getsource(sched_mod.PromotionScheduler.apply_human_decision)
    advance_src = inspect.getsource(sched_mod.PromotionScheduler._advance_candidate)
    assert "_entity_scan_is_clean" in advance_src  # the auto edge: settled PASS required
    assert "_entity_scan_is_actionable" in approve_src  # the landing edge: guarded now


async def test_a_human_may_still_approve_over_a_localized_finding() -> None:
    """The other half of the policy, and the reason the guard is not simply
    `_entity_scan_is_clean` copied onto the approve edge.

    D58b routes 100% of leakage near-misses to a human precisely so a person decides.
    Demanding a clean `pass` here would make `reason=leakage_near_miss` a permanently
    un-approvable dead end — an inbox row that no action but reject could ever clear —
    and it would do so while the D17 machinery is fully functional, because a LOCALIZED
    finding is exactly the state in which the strip has spans to remove and the tripwire
    has spans to verify.

    So the approve lands, AND the entity is gone from what landed."""
    store = InMemoryCandidateStore()
    located = {
        "result": "quarantine",
        "scanner": "regex",
        "scanned_fields": ["intent"],
        "hits": [{"field": "intent", "kind": "employee_id", "span": ENTITY}],
    }
    env = _entity_bearing(located)
    await store.put(env)
    writer = FakeLandingWriter()

    decision = await _sched(store, writer=writer).apply_human_decision(env, "approve")

    assert decision.to_status == CandidateStatus.VALIDATED
    assert writer.forbidden_spans == [(ENTITY,)]  # the tripwire got real spans
    assert ENTITY not in json.dumps(writer.landed[0].payload)  # and the strip worked


async def test_the_knowledge_landing_branch_is_guarded_too() -> None:
    """`global_knowledge` reaches the SAME `_land_and_promote` with the same
    `forbidden_spans`, down a different branch of `apply_human_decision`. QA's original
    tests only drove the blueprint branch, so a guard placed inside the
    `if env.type == BLUEPRINT_TYPE` arm would have satisfied them and left the knowledge
    path landing unscanned text.

    Guards 1-4 run BEFORE the type split, which is what makes one guard cover both."""
    store = InMemoryCandidateStore()
    doc = copy.deepcopy(
        json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())["single"][
            "envelope"
        ]
    )
    doc["status"] = CandidateStatus.IN_REVIEW
    doc["type"] = "global_knowledge"
    doc["payload"] = {
        "statement": f"employee {ENTITY} is paid monthly",
        "scope": "payroll",
    }
    doc["entity_scan"] = _UNSETTLED
    env = CandidateEnvelope.from_doc(doc)
    await store.put(env)
    writer = FakeLandingWriter()

    decision = await _sched(store, writer=writer).apply_human_decision(env, "approve")

    assert decision.reason == "approve_blocked_entity_scan_not_actionable"
    assert writer.landed == []


async def test_reject_stays_available_for_a_candidate_nobody_can_approve() -> None:
    """The consequence of a fail-closed guard, asserted so it is a decision rather than a
    discovery: a candidate whose scan never settled is now REJECT-ONLY.

    There is no re-scan action in the inbox, so if reject were gated by the same
    predicate such a row would have no terminal action at all and would sit in the queue
    for ever. Reject writes no content — its corpus write-backs set a status string on an
    existing node — so there is nothing for the guard to protect there."""
    store = InMemoryCandidateStore()
    env = _entity_bearing(_UNSETTLED)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _sched(store, writer=writer)

    assert (await sched.apply_human_decision(env, "approve")).action == "hold"
    decision = await sched.apply_human_decision(env, "reject")

    assert decision.action == "reject"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.REJECTED
    assert writer.landed == []


async def test_the_three_inherited_edges_all_require_validated() -> None:
    """THE INVARIANT THAT MAKES ONE GUARD ENOUGH, asserted rather than assumed.

    `apply_retract`, `apply_verify` and `apply_promote` write to the corpus too
    (`update_status`, `mark_verified`) and `promote` renders the payload into MCP YAML for
    a human PR. None of them re-checks leakage — they do not have to, because each
    requires `status == validated` and approve is the ONLY transition that produces it.
    They INHERIT the approve edge's guarantee.

    That inheritance is exactly the kind of cross-method dependency this codebase keeps
    getting bitten by (see `writer/routing.py::_AUTO_LAND_DEDUP_ACTIONS`), so it is pinned
    here: the day any other edge learns to write `validated`, all three silently inherit
    the hole instead, and this test is what should fail."""
    store = InMemoryCandidateStore()
    env = _entity_bearing(_UNSETTLED)  # in_review — never approved, so never validated
    await store.put(env)
    sched = _sched(store, writer=FakeLandingWriter())

    assert (await sched.apply_retract(env)).reason == "not_validated"
    assert (await sched.apply_verify(env))[0].reason == "not_validated"
    assert (await sched.apply_promote(env)).reason == "not_validated"
