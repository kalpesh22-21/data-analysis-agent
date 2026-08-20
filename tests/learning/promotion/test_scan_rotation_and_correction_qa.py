"""QA on the S9 cron's cost + fairness fix: rotation under overload, the timestamp
handling around it, the retention clock, and one pre-existing defect it exposes.

Five things are pinned here that the slice's own tests do not cover:

  1. ROTATION UNDER OVERLOAD. The slice proves a newcomer is reached behind a full
     window; it does not prove the steady state is a genuine round-robin. With 500
     permanently-held candidates and `scan_limit=200`, EVERY candidate must be examined
     within `ceil(500/200)` cycles, for ever — not just once during the first pass.
  2. STORE PARITY UNDER OVERLOAD. The same assertion is re-run against the REAL
     `CouchbaseCandidateStore` driven by a fake cluster whose `query` sorts by the
     MEASURED N1QL collation (see `test_candidate_store_ordering_parity_qa`), so the
     fairness claim is not proved only against the fake.
  3. TIMESTAMP HANDLING. `_parse_clock` / `_is_fresh` read values out of a rehydrated
     document and an injected clock. Every neighbouring type is fuzzed for a crash site,
     and the ISO-8601 offset-format hazard in the sort key is pinned as behaviour.
  4. THE RETENTION CLOCK. The Guard-3 verdict write must NOT be a `put`, or a parked
     candidate's 90-day TTL is renewed on a schedule and it becomes immortal — a new
     unbounded-growth vector in exchange for the one the slice removed.
  5. A PRE-EXISTING DEFECT the rate limit did not fix: a user correction was undone by
     the very next cron cycle. FIXED by plan §4's routing change, and re-pointed here to
     assert the fix (plus the residue the fix does NOT cover).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.couchbase_candidate_store import (
    CouchbaseCandidateStore,
)
from data_agent.learning.candidate.memory_candidate_store import _sort_key
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import DriftStamp
from data_agent.learning.config import LearningSettings
from data_agent.learning.promotion import (
    GRAIN_INTEGRITY,
    PromotionScheduler,
)
from data_agent.learning.promotion.drift import _is_fresh
from data_agent.learning.promotion.scheduler import _parse_clock
from data_agent.runtime.couchbase_connect import COUCHBASE_AVAILABLE

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
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class Clock:
    """A pinned injected clock."""

    def __init__(self, iso: str = "2026-08-10T12:00:00+00:00") -> None:
        self.iso = iso

    def __call__(self) -> str:
        return self.iso


class TickingClock:
    """A clock that advances a microsecond per read — what `_now_iso()`
    (`datetime.now(UTC).isoformat()`) actually behaves like in production. Pinning the
    clock to a literal (as the slice's own rotation test does) makes every stamp in a
    cycle IDENTICAL, which hides the tie behaviour; see the tie known-limitation."""

    def __init__(self, start: datetime = NOW) -> None:
        self._t = start

    def __call__(self) -> str:
        self._t += timedelta(microseconds=1)
        return self._t.isoformat()


def _cheap_hold(ordinal: int) -> CandidateEnvelope:
    """A candidate that holds cheaply and for ever (a human-gated target): exactly the
    population that used to pin the scan window."""
    env = with_type(
        make_blueprint_candidate(status=CandidateStatus.CANDIDATE), "global_knowledge"
    )
    return replace(env, candidate_id=f"held::{ordinal:03d}")


def _scheduler(store, *, clock, policy, probe=None):
    return PromotionScheduler(
        store,
        probe=probe or FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 0}),
        dependency_resolver=FakeDependencyResolver(),
        policy=policy,
        clock=clock,
    )


# --- 1. rotation under overload: a real round-robin, not a differently-shaped stall ---


async def test_every_held_candidate_is_examined_within_one_rotation_period():
    """THE property the fix has to deliver, stated for the steady state rather than the
    first pass. 500 permanently-held candidates, a window of 200: the rotation period is
    `ceil(500/200) = 3` cycles, and EVERY candidate must appear in EVERY 3-cycle window
    — not merely somewhere in the first pass, which a rotation that stalls after the
    backlog is drained would also satisfy."""
    n, limit = 500, 200
    store = InMemoryCandidateStore()
    for i in range(n):
        await store.put(_cheap_hold(i))
    sched = _scheduler(
        store, clock=TickingClock(), policy=promotion_policy(scan_limit=limit)
    )

    period = math.ceil(n / limit)
    per_cycle: list[set[str]] = []
    for _ in range(period * 4):  # four full rotation periods
        per_cycle.append({d.candidate_id for d in (await sched.run_once()).decisions})

    everyone = {f"held::{i:03d}" for i in range(n)}
    # Every SLIDING window of `period` cycles covers the whole population — including
    # the windows entirely after the initial drain.
    for start in range(len(per_cycle) - period + 1):
        window = set().union(*per_cycle[start : start + period])
        assert window == everyone, f"cycles {start}..{start + period - 1} starved rows"


async def test_a_newcomer_never_waits_longer_than_one_cycle_however_deep_the_backlog():
    """A candidate extracted while 500 others are parked must be examined on the NEXT
    cycle, not after the backlog drains — never-scanned sorts first, so backlog depth
    does not delay new work at all. This is the property the old `created_at ASC` read
    destroyed."""
    store = InMemoryCandidateStore()
    for i in range(500):
        await store.put(_cheap_hold(i))
    clock = TickingClock()
    sched = _scheduler(store, clock=clock, policy=promotion_policy(scan_limit=200))
    await sched.run_once()
    await sched.run_once()

    await store.put(replace(_cheap_hold(999), candidate_id="newcomer"))

    seen = {d.candidate_id for d in (await sched.run_once()).decisions}
    assert "newcomer" in seen


async def test_rotation_stalls_when_every_cursor_is_identical_is_a_known_limitation():
    """KNOWN LIMITATION (fairness, LOW severity in production — see the store-side pin
    in tests/learning/candidate/test_candidate_store_ordering_parity_qa.py).

    Once MORE than `scan_limit` rows share one cursor value, the same prefix comes back
    every cycle and the remainder starve — the original bug, re-shaped.

    Production is safe by ACCIDENT, not by construction: the real clock is
    `datetime.now(UTC).isoformat()` at microsecond resolution, so no two rows in a cycle
    collide (`test_every_held_candidate_is_examined_within_one_rotation_period` drives a
    ticking clock and passes). It is reachable by a bulk back-fill that writes a constant
    cursor, or by any harness that pins the clock.

    HISTORY — the proposed fix in the original version of this docstring ("a `created_at`
    / `META().id` tiebreak would close it") was TRIED and DISPROVED. Both stores now do
    carry a `candidate_id` tiebreak, and it closed the store-PARITY half of the problem
    (see the renamed store-side pin,
    `test_tied_cursors_order_by_candidate_id_but_still_do_not_rotate`) — but this test is
    unchanged and still passes, because a tiebreak makes the order TOTAL, not ROTATING.
    What rotates the window is the CURSOR advancing, which confines ties to rows stamped
    inside one clock tick; any clock that moves between cycles rotates fine, and a clock
    frozen across cycles cannot rotate whatever the secondary key is. Closing this for
    real needs the scan to advance a position, not just to sort deterministically — e.g.
    keyset pagination over `(last_scanned_at, candidate_id)` carried across cycles.

    The slice's own rotation test has since been switched to a ticking clock, so it no
    longer passes for the wrong reason."""
    n, limit = 500, 200
    store = InMemoryCandidateStore()
    for i in range(n):
        await store.put(_cheap_hold(i))
    sched = _scheduler(store, clock=Clock(), policy=promotion_policy(scan_limit=limit))

    per_cycle = [
        {d.candidate_id for d in (await sched.run_once()).decisions} for _ in range(8)
    ]

    # The first pass still drains the backlog (rank 0 beats rank 1 regardless of ties).
    assert set().union(*per_cycle[:3]) == {f"held::{i:03d}" for i in range(n)}
    # But once every row carries the SAME stamp the rotation freezes on one window.
    steady = per_cycle[3:]
    assert all(s == steady[0] for s in steady)
    assert len(steady[0]) == limit  # 300 of the 500 are now never examined again


async def test_the_validated_scan_rotates_too_not_only_the_candidate_scan():
    """COVERAGE GAP the slice's own tests leave open: `run_once` passes
    `order_by="last_scanned_at"` to BOTH status reads, but only the CANDIDATE read is
    pinned. Deleting it from the VALIDATED read is invisible to every other test, and
    the consequence is the same starvation in the other direction — past `scan_limit`
    validated blueprints, the newest ones would never be drift-re-checked, so a
    blueprint whose warehouse table changed would stay recallable for ever."""
    n, limit = 12, 5
    store = InMemoryCandidateStore()
    for i in range(n):
        env = make_blueprint_candidate(
            status=CandidateStatus.VALIDATED, canonical_key=KEY
        )
        await store.put(
            replace(
                env,
                candidate_id=f"val::{i:02d}",
                # OLDEST-first by created_at is the reverse of the id order, so a read
                # that fell back to `created_at ASC` would return a fixed prefix.
                created_at=f"2020-01-01T00:00:{n - i:02d}+00:00",
            )
        )
    sched = _scheduler(store, clock=TickingClock(), policy=promotion_policy(scan_limit=limit))

    period = math.ceil(n / limit)
    per_cycle = [
        {d.candidate_id for d in (await sched.run_once()).decisions}
        for _ in range(period * 3)
    ]

    everyone = {f"val::{i:02d}" for i in range(n)}
    for start in range(len(per_cycle) - period + 1):
        assert set().union(*per_cycle[start : start + period]) == everyone


async def test_a_far_future_cursor_starves_that_candidate_is_a_known_limitation():
    """KNOWN LIMITATION (fairness, LOW severity — needs a bad clock or a hand-edit).

    `list_by_status` deliberately has NO freshness predicate in the WHERE (a string
    comparison of ISO timestamps would be format-sensitive and could DROP a row for
    ever). The trade is explicit and correct — but ordering alone still means a cursor
    stamped far in the future sorts behind everything for as long as that future lasts,
    so past `scan_limit` rows that candidate is never examined again.

    Pinned rather than fixed: nothing in the codebase writes a future cursor, and the
    alternative (a WHERE cutoff) was rejected for a stronger reason. Closing it would
    mean clamping the stamp to `now` on write, or ordering on
    `LEAST(last_scanned_at, now)`."""
    store = InMemoryCandidateStore()
    for i in range(4):
        await store.put(_cheap_hold(i))
    await store.put(
        replace(
            _cheap_hold(9),
            candidate_id="held::future",
            last_scanned_at="2099-01-01T00:00:00+00:00",
        )
    )
    sched = _scheduler(store, clock=TickingClock(), policy=promotion_policy(scan_limit=2))

    seen: set[str] = set()
    for _ in range(20):
        seen |= {d.candidate_id for d in (await sched.run_once()).decisions}

    assert {f"held::{i:03d}" for i in range(4)} <= seen
    assert "held::future" not in seen  # starved for the next 73 years


# --- 2. the same fairness claim against the REAL Couchbase store ----------------

pytestmark_cb = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE, reason="Requires the 'couchbase' package for options."
)

# The MEASURED N1QL total collation order (live Couchbase 7.6.5 — the derivation is in
# tests/learning/candidate/test_candidate_store_ordering_parity_qa.py). Used to make the
# fake cluster sort the way the real query service does, so this exercise proves
# something about production rather than about a convenient in-Python sort.
_TYPE_RANK = {type(None): 1, bool: 2, int: 3, float: 3, str: 4, list: 5, dict: 6}


def _n1ql_sort_key(doc: dict, field: str):
    if field not in doc:
        return (0, "")
    raw = doc[field]
    return (_TYPE_RANK.get(type(raw), 6), raw if isinstance(raw, str) else "")


class _N1qlCollection:
    """A KV collection over a dict of raw documents, with `mutate_in` REPLACE semantics
    (a missing id raises, exactly like the real sub-document write)."""

    def __init__(self, docs: dict[str, dict]) -> None:
        self.docs = docs
        self.upserts: list[tuple[str, object]] = []
        self.mutates: list[tuple[str, str, object, object]] = []

    async def upsert(self, doc_id, doc, options):
        self.upserts.append((doc_id, options))
        self.docs[doc_id] = doc

    async def mutate_in(self, doc_id, specs, options):
        from couchbase.exceptions import DocumentNotFoundException

        if doc_id not in self.docs:
            raise DocumentNotFoundException(f"{doc_id} is gone")
        for spec in specs:
            path, value = _spec_path_value(spec)
            self.mutates.append((doc_id, path, value, options))
            self.docs[doc_id][path] = value


def _spec_path_value(spec):
    """Read (path, value) off a `couchbase.subdocument.upsert` spec.

    `couchbase.subdocument.Spec` is a bare `tuple` subclass with no field names:
    `(SubDocOp.DICT_UPSERT, path, expand_macros, create_parents, xattr, value)`. Read it
    positionally from both ends so an SDK that grows a middle flag does not break this."""
    assert isinstance(spec, tuple) and len(spec) >= 3, f"unexpected spec {spec!r}"
    return spec[1], spec[-1]


class _N1qlCluster:
    def __init__(self, collection: _N1qlCollection) -> None:
        self._collection = collection
        self.statements: list[str] = []

    def bucket(self, name):
        return _N1qlBucket(self._collection)

    def query(self, statement, options):
        self.statements.append(statement)
        params = options["named_parameters"]
        field = "last_scanned_at" if "c.last_scanned_at" in statement else "created_at"
        rows = [d for d in self._collection.docs.values() if d["status"] == params["status"]]
        rows.sort(key=lambda d: _n1ql_sort_key(d, field))
        if " DESC " in statement:
            rows.reverse()
        return _N1qlResult(rows[: params["limit"]])


class _N1qlBucket:
    """One collection, reachable through EITHER binding path.

    The learning stores now bind `bucket.scope(...).collection(...)` (defaults
    `_default`/`_default`, the same handle `default_collection()` returns), so this
    double answers both and ignores the names — what these tests assert is the store's
    behaviour against a collection, not which one it picked.
    """

    def __init__(self, collection):
        self._collection = collection

    def scope(self, _name):
        return self

    def collection(self, _name):
        return self._collection

    def default_collection(self):
        return self._collection


class _N1qlResult:
    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]

    def __aiter__(self):
        self._it = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


def _cb_store(docs: dict[str, dict]):
    collection = _N1qlCollection(docs)
    cluster = _N1qlCluster(collection)
    store = CouchbaseCandidateStore(LearningSettings(_env_file=None), cluster=cluster)
    return store, collection


@pytestmark_cb
async def test_couchbase_store_rotates_under_overload_too():
    """PARITY. The unit suite proves fairness against `InMemoryCandidateStore`; this
    re-proves the identical property against the REAL `CouchbaseCandidateStore` code
    path — its N1QL statement, its `mutate_in` cursor write, its `from_doc` rehydrate —
    with a cluster double that sorts by the MEASURED N1QL collation."""
    n, limit = 500, 200
    docs = {}
    for i in range(n):
        env = _cheap_hold(i)
        docs[env.candidate_id] = env.to_doc()
    store, collection = _cb_store(docs)
    sched = _scheduler(
        store, clock=TickingClock(), policy=promotion_policy(scan_limit=limit)
    )

    period = math.ceil(n / limit)
    per_cycle = [
        {d.candidate_id for d in (await sched.run_once()).decisions}
        for _ in range(period * 3)
    ]

    everyone = {f"held::{i:03d}" for i in range(n)}
    for start in range(len(per_cycle) - period + 1):
        assert set().union(*per_cycle[start : start + period]) == everyone
    # ...and none of it went through a whole-document write.
    assert collection.upserts == []


@pytestmark_cb
async def test_couchbase_store_reaches_a_newcomer_behind_a_full_window():
    docs = {}
    for i in range(300):
        env = _cheap_hold(i)
        docs[env.candidate_id] = env.to_doc()
    store, collection = _cb_store(docs)
    sched = _scheduler(store, clock=TickingClock(), policy=promotion_policy(scan_limit=200))
    await sched.run_once()

    newcomer = replace(_cheap_hold(999), candidate_id="newcomer")
    collection.docs["newcomer"] = newcomer.to_doc()
    assert "last_scanned_at" not in collection.docs["newcomer"]  # MISSING, not null

    seen = {d.candidate_id for d in (await sched.run_once()).decisions}
    assert "newcomer" in seen


# --- 3. timestamp handling ------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        12.5,
        True,
        b"2026-08-10T12:00:00+00:00",
        {"at": "2026-08-10T12:00:00+00:00"},
        ["2026-08-10T12:00:00+00:00"],
        "",
        "   ",
        "not-a-timestamp",
        "2026-13-45T99:99:99+00:00",
        "2026-08-10",
        "2026-08-10T12:00:00",  # naive
        "2026-08-10T12:00:00.123456789012+00:00",
        "0001-01-01T00:00:00+00:00",
        "9999-12-31T23:59:59.999999+00:00",
    ],
)
def test_parse_clock_never_raises_and_only_ever_yields_an_aware_datetime(value):
    """`clock` is injected, so its output is whatever the caller supplies.
    `datetime.fromisoformat` raises TypeError (not the ValueError that is caught) on a
    non-string, so the isinstance guard is the actual boundary, not padding. Anything
    unusable must degrade to `None` — which every caller reads as "cannot judge
    freshness, run the real probe", i.e. the pre-rate-limit behaviour."""
    parsed = _parse_clock(value)
    assert parsed is None or parsed.tzinfo is not None


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        12.5,
        True,
        b"2026-08-10T06:00:00+00:00",
        {"at": "x"},
        ["x"],
        "",
        "junk",
        "2026-08-10T06:00:00",  # naive: unusable, must not be compared to an aware now
        "0001-01-01T00:00:00+00:00",  # far past
        "9999-12-31T23:59:59+00:00",  # far future
        "2027-08-10T12:00:00+00:00",  # a year in the future (clock skew)
    ],
)
def test_is_fresh_never_raises_and_fails_closed_on_anything_unusable(value):
    """Same boundary on the read side: `last_drift_check_at` comes straight out of
    `DriftStamp.from_doc`, a bare `doc.get` over rehydrated JSON a human can hand-edit
    via cbq. Every one of these must read as "no usable stamp" — never a crash inside
    the cron scan, and never a fabricated `fresh`."""
    assert _is_fresh(value, NOW, 43_200.0) is False


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-10T12:00:01+00:00",  # one second AHEAD of now
        "2026-08-10T13:00:00+00:00",  # an hour ahead — inside the 12h window
        "2026-08-10T23:59:00+00:00",  # still inside the window, still the future
    ],
)
def test_a_future_dated_stamp_is_never_fresh_even_inside_the_window(value):
    """The `0.0 <=` lower bound, pinned. `_is_fresh` is an AGE test, so a stamp dated
    ahead of `now` must fail closed however small the skew — the alternative (an
    `abs()`-style window) would let a hand-edited or clock-skewed future timestamp
    suppress the real golden replay for a full window, and would hand the D43 silent
    fast path a trust window nothing earned. Nothing else in the suite covers it: a
    mutation replacing the bound with `abs(...)` passes every other test."""
    assert _is_fresh(value, NOW, 43_200.0) is False


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-10T06:00:00+00:00",
        "2026-08-10T06:00:00Z",  # Z suffix
        "2026-08-10T11:30:00+05:30",  # 06:00 UTC, a non-zero offset
        "2026-08-10T06:00:00.123456+00:00",
    ],
)
def test_is_fresh_accepts_every_aware_iso_offset_form_for_the_same_instant(value):
    """The freshness COMPARISON is done on parsed datetimes, so all four spellings of
    06:00 UTC are equally fresh. Contrast with the SORT key below, which compares the
    same four strings lexicographically."""
    assert _is_fresh(value, NOW, 43_200.0) is True


def test_the_scan_cursor_sorts_lexicographically_not_chronologically():
    """PINNED BEHAVIOUR, not a bug — the trade-off `list_by_status` documents.

    Four spellings of the SAME instant sort into four different positions because
    `ORDER BY` compares ISO-8601 as text, and a naive stamp (no offset at all) is a
    prefix of the others and sorts before all of them.

    Why it is tolerable: the mis-ordering is bounded by the width of the format
    variation, so a row written by a foreign `Z`-emitting writer loses at most one tick
    of the leading digits to the scheduler's own `+00:00` stamps — it is delayed, never
    dropped. That is exactly the property a WHERE-clause freshness cutoff would NOT
    have, which is why there is no cutoff. A far-future stamp is the one case where
    "delayed" becomes "never"; see the known-limitation test above."""
    same_instant = [
        "2026-08-10T06:00:00+00:00",
        "2026-08-10T06:00:00Z",
        "2026-08-10T11:30:00+05:30",
        "2026-08-10T06:00:00",
    ]
    envs = [replace(_cheap_hold(0), last_scanned_at=v) for v in same_instant]
    ordered = [e.last_scanned_at for e in sorted(envs, key=lambda e: _sort_key(e, "x"))]

    assert ordered == [
        "2026-08-10T06:00:00",  # naive — shortest, sorts first
        "2026-08-10T06:00:00+00:00",
        "2026-08-10T06:00:00Z",  # 'Z' > '+' in ASCII
        "2026-08-10T11:30:00+05:30",  # same instant, sorts a whole "5 hours" later
    ]


async def test_a_mixture_of_stamped_and_never_scanned_never_raises_on_the_first_read():
    """A bare `sorted()` over `str | None` raises `TypeError: '<' not supported between
    instances of 'str' and 'NoneType'` — a crash in the cron's very FIRST read, not a
    mis-order. Driven here through `run_once` (rather than the store directly) so the
    whole cycle is covered, including the `finally` cursor stamp."""
    store = InMemoryCandidateStore()
    for i in range(6):
        env = _cheap_hold(i)
        await store.put(
            env if i % 2 else replace(env, last_scanned_at=f"2026-01-0{i + 1}T00:00:00+00:00")
        )

    sweep = await _scheduler(
        store, clock=TickingClock(), policy=promotion_policy(scan_limit=10)
    ).run_once()

    assert len(sweep.decisions) == 6


async def test_a_clock_that_raises_does_not_abort_the_cycle():
    """The clock is injected and read on the cursor-stamp path inside `_guard`'s
    `finally`. An exception escaping a `finally` would replace the real decision (or the
    real exception) with the clock's — so it must be swallowed like any other
    bookkeeping failure."""
    store = InMemoryCandidateStore()
    env = await _store_put(store, _cheap_hold(0))

    def _explode() -> str:
        raise RuntimeError("clock unavailable")

    sched = _scheduler(store, clock=_explode, policy=promotion_policy())

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "hold"
    assert sweep.decisions[0].reason == "human_gated_target"  # the REAL reason survives
    assert (await store.get(env.candidate_id)).last_scanned_at is None


async def _store_put(store, env):
    await store.put(env)
    return env


# --- 4. the retention clock -----------------------------------------------------


@pytestmark_cb
async def test_a_parked_candidate_never_has_its_90_day_ttl_renewed():
    """REGRESSION PIN for the retention clock.

    Persisting the Guard-3 verdict is what makes the rate limit real, but `put` sets the
    candidate TTL FRESH on every write ("Set fresh per write" —
    `learning_candidates_ttl_seconds`, 90 days). A verdict write issued through `put`
    every 12h would renew that clock for ever, so a candidate that can never clear Guard
    4 would never expire either — trading the warehouse-query cost for an unbounded
    storage cost.

    Driven end-to-end through the REAL `CouchbaseCandidateStore`: across three days of
    cycles on a candidate parked below the hit threshold, with the verdict expiring and
    being re-probed, there must be NO whole-document upsert at all — only TTL-preserving
    sub-document stamps."""
    stale = DriftStamp(
        status="clean",
        last_drift_check_at="2026-08-09T12:00:00+00:00",
        probes=(GRAIN_INTEGRITY,),
    )
    held = replace(
        make_blueprint_candidate(
            status=CandidateStatus.CANDIDATE, canonical_key=KEY, drift=stale
        ),
        candidate_id="c::parked",
    )
    store, collection = _cb_store({"c::parked": held.to_doc()})
    clock = Clock()
    sched = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 1}),
        dependency_resolver=FakeDependencyResolver(),
        # Threshold RAISED above the shipped 1 on purpose: this test is about a candidate
        # that stays PARKED across days of cycles, and at the shipped threshold nothing
        # parks (see PARKED_POLICY in test_replay_freshness_and_rotation.py).
        policy=promotion_policy(blueprint_hit_threshold=3),
        clock=clock,
    )

    for day in (10, 11, 12):
        clock.iso = f"2026-08-{day}T12:00:00+00:00"
        sweep = await sched.run_once()
        assert sweep.decisions[0].reason == "below_hit_threshold"

    assert collection.upserts == []  # NO `put` ⇒ the retention clock never restarts
    paths = {path for _, path, _, _ in collection.mutates}
    assert paths == {"drift", "last_scanned_at"}
    assert all(opts.get("preserve_expiry") is True for *_, opts in collection.mutates)
    # ...and a real lifecycle write still sets the TTL fresh (the fix stayed local).
    await store.put(replace(held, status=CandidateStatus.CANDIDATE))
    _, options = collection.upserts[0]
    assert options.get("expiry") == timedelta(
        seconds=LearningSettings(_env_file=None).learning_candidates_ttl_seconds
    )


async def test_the_verdict_write_on_a_hold_is_a_stamp_not_an_envelope_put():
    """The in-memory mirror of the assertion above, so the property is covered in the
    suite that actually runs everywhere. A hold that re-probes records its verdict, but
    through the narrow `stamp_drift` path — never a whole-envelope `put`."""
    store = InMemoryCandidateStore()
    env = await _store_put(
        store,
        replace(
            make_blueprint_candidate(
                status=CandidateStatus.CANDIDATE, canonical_key=KEY
            ),
            candidate_id="c::parked",
        ),
    )
    puts_before = store.put_calls
    sched = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 1}),
        dependency_resolver=FakeDependencyResolver(),
        # Raised above the shipped 1 for the same reason as the Couchbase mirror above:
        # the hold path is what is being tested, and nothing holds at threshold 1.
        policy=promotion_policy(blueprint_hit_threshold=3),
        clock=Clock(),
    )

    await sched.run_once()

    assert store.put_calls == puts_before
    assert store.drift_stamps == 1
    assert store.touch_calls == 1
    assert (await store.get(env.candidate_id)).drift.status == "clean"


# --- 5. the pre-existing defect the rate limit did not fix (plan §4 closed it) ---


@pytest.mark.parametrize("with_landing_writer", [False, True])
async def test_a_user_correction_is_no_longer_erased_by_the_next_cron_cycle(
    with_landing_writer,
):
    """FIXED BY PLAN §4 — as a SIDE EFFECT of the routing change, which is exactly why it
    is verified here rather than assumed.

    HISTORY. This test was named `..._is_erased_by_the_next_cron_cycle_is_a_known_
    limitation` and asserted the OPPOSITE of what it asserts now.
    `apply_user_correction` demotes `validated → candidate` and stamps
    `user_correction_stamp` (`suspect`, `probes=()`). The very next `_advance_candidate`
    re-ran the guards, and every one of them passed:

      * Guard 0-2 are unchanged by a correction (the entity scan still reads `pass` —
        `strip_entity_bearing` only blanks spans when the S5 verdict HAS hits, so a
        clean candidate keeps its `pass` on the landing path too, which is why this
        reproduced with AND without a landing writer);
      * Guard 3 replays and PASSES — a correction is about a VALUE and the replay is
        structure-only by design (D98: there is no value oracle);
      * Guard 4 still read `hit_count >= T`, because a correction does not decrement it.

    So the artifact was re-promoted to `validated` about five minutes later and the
    human's negative signal was silently erased.

    **NOT ONE OF THOSE FACTS CHANGED.** The guards still all pass; the replay still
    returns green; the hit count is still untouched and, at a threshold of 1, is easier
    to clear than it has ever been. What changed is the DESTINATION: the re-examination
    routes to `in_review` instead of `validated`. The corrected artifact therefore stops
    being a recallable artifact and becomes a question for a human, and only a human
    approve can put it back — which is what a correction should produce.

    WHAT THIS STILL DOES NOT DO, asserted at the bottom so the residue is not mistaken
    for a complete fix: the correction leaves no durable mark on the ARTIFACT. There is
    still no `corrected_at`/`correction_count`, the passing replay overwrites the
    `suspect` drift stamp with `clean`, and the reviewer who opens the resulting inbox
    item is not told this blueprint was ever corrected. This closes the ERASURE, not the
    amnesia."""
    store = InMemoryCandidateStore()
    env = await _store_put(
        store,
        make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY),
    )
    clock = Clock("2026-08-10T12:00:00+00:00")
    writer = FakeLandingWriter() if with_landing_writer else None
    sched = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 5}),  # >= T, and a correction does not
        dependency_resolver=FakeDependencyResolver(),  # decrement it
        policy=promotion_policy(),
        landing_writer=writer,
        clock=clock,
    )

    # A human approves it into the corpus — the only edge that reaches `validated`.
    assert (await sched.apply_human_decision(env, "approve")).action == "approve"
    promoted = await store.get(env.candidate_id)
    assert promoted.status == CandidateStatus.VALIDATED

    # A human says the ANSWER was wrong.
    correction = await sched.apply_user_correction(promoted)
    assert correction.action == "demote"
    corrected = await store.get(env.candidate_id)
    assert corrected.status == CandidateStatus.CANDIDATE
    assert corrected.drift.status == "suspect"
    assert corrected.drift.probes == ()  # not a replay verdict

    # One cron interval later (the shipped cadence is 300s). The replay STILL passes and
    # the count is STILL above the threshold — and the artifact still does not come back.
    clock.iso = "2026-08-10T12:05:00+00:00"
    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "route"
    assert (
        await store.get(env.candidate_id)
    ).status == CandidateStatus.IN_REVIEW, "the correction must survive"
    if writer is not None:
        # ...and the LANDED node is left non-recallable: the demote stamped it
        # `candidate` and the route re-stamped it `in_review`. Neither is `validated`,
        # which is what the recall filter requires.
        assert [u[1] for u in writer.status_updates][-1] == CandidateStatus.IN_REVIEW

    # And it stays there — more cycles never re-promote it.
    for minute in (10, 15, 20, 25):
        clock.iso = f"2026-08-10T12:{minute}:00+00:00"
        await sched.run_once()
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW

    # The drift stamp IS overwritten by the passing replay — that part of the residue is
    # real and unavoidable, because the replay genuinely ran and genuinely passed.
    assert (await store.get(env.candidate_id)).drift.status == "clean"
    # ...but the CORRECTION is carried forward independently, so the reviewer is not
    # adjudicating blind. See the dedicated tests below.
    assert (await store.get(env.candidate_id)).route_reason == "user_corrected"


async def test_the_cached_verdict_window_still_does_not_delay_the_re_examination():
    """The mechanism underneath the fix above, pinned separately so a future change to
    the replay cache cannot quietly become the thing that appears to hold a correction
    down.

    A `user_correction_stamp` carries `probes=()`, `reusable_replay_verdict` returns
    `None` for it, and the real probe runs on the very next cycle. So the correction does
    NOT survive because a cached `suspect` suppresses anything — the warehouse is
    re-queried five minutes later, it answers green, and the candidate is routed to a
    human anyway. The durability comes from the destination, not from a timer."""
    store = InMemoryCandidateStore()
    env = await _store_put(
        store,
        make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY),
    )
    clock = Clock("2026-08-10T12:00:00+00:00")
    probe = FakeWarehouseProbe()
    sched = PromotionScheduler(
        store,
        probe=probe,
        hit_counts=FakeHitCountReader({KEY: 5}),
        dependency_resolver=FakeDependencyResolver(),
        policy=promotion_policy(),
        clock=clock,
    )
    await sched.apply_human_decision(env, "approve")
    await sched.apply_user_correction(await store.get(env.candidate_id))
    probes_before = len(probe.calls)

    clock.iso = "2026-08-10T12:05:00+00:00"
    await sched.run_once()

    assert len(probe.calls) == probes_before + 1  # re-probed 5 minutes later, not 12h
