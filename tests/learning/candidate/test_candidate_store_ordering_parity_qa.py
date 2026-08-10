"""QA: does `InMemoryCandidateStore` really order like `CouchbaseCandidateStore`?

The whole unit suite drives the S9 promotion scheduler through the in-memory fake, so
the rotation's fairness property is only ever *proved* against `_sort_key`. If that
diverges from what N1QL actually does, a green suite says nothing about production.

The N1QL side of the comparison is not assumed here — it was MEASURED against a live
Couchbase 7.6.5 (`docker-compose.integration.yml` service `couchbase`, the same version
the integration suite targets) with the shipped rotation GSI in place. The GSI has since
been WIDENED to `idx_candidates_scan_rotation(status, last_scanned_at, candidate_id)` to
carry the tiebreak; the collation measurements below are unaffected (they are properties
of the N1QL type order, not of the index), and the EXPLAIN line is re-measured at the
bottom of this header. Original measurement, against
`idx_candidates_status_scanned(status, last_scanned_at)`:

    SELECT RAW d.a
      FROM [ {"a":"2020-01-02T00:00:00+00:00"}, {}, {"a":null}, {"a":123},
             {"a":["x"]}, {"a":{"k":1}}, {"a":true}, {"a":false} ] AS d
     ORDER BY d.a ASC
    -> MISSING, NULL, false, true, 123, "2020-...", ["x"], {"k":1}

    -- and against the real bucket + real index, docs in one status:
    ORDER BY c.last_scanned_at ASC
    -> miss::1, miss::2, miss::3, null::1, num::1, stamp::1, obj::1

    -- EXPLAIN: IndexScan3 on idx_candidates_status_scanned,
    --          index_order keypos 1, limit pushed down, NO Order (sort) stage.

    -- RE-MEASURED after the tiebreak landed, same cluster:
    --   ORDER BY c.last_scanned_at ASC, c.candidate_id ASC
    --   -> IndexScan3 on idx_candidates_scan_rotation,
    --      index_order [keypos 1, keypos 2], limit=200 pushed down, NO Order stage.
    -- The three-key index is REQUIRED for that: with the tiebreak against the old
    -- two-key index the planner falls back to idx_candidates_status + a full Order.

`_N1QL_RANK` below encodes exactly that measured total order. Two conclusions follow,
and both are pinned as tests:

  * the MISSING-sorts-FIRST claim the whole rotation rests on is TRUE — a never-scanned
    candidate is reached on the next cycle, in BOTH stores;
  * the two stores DISAGREE for an array/object-valued cursor, and the disagreement is
    in the dangerous direction (see the `_is_a_known_limitation` test).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from data_agent.learning.candidate import CandidateStatus, InMemoryCandidateStore
from data_agent.learning.candidate.memory_candidate_store import _sort_key
from data_agent.learning.candidate.models import CandidateEnvelope

from .test_candidate_store_scan_rotation import _envelope

# The MEASURED N1QL total collation order (see the module docstring). Lower rank sorts
# earlier under `ORDER BY ... ASC`.
_N1QL_RANK: dict[str, int] = {
    "missing": 0,
    "null": 1,
    "bool": 2,
    "number": 3,
    "string": 4,
    "array": 5,
    "object": 6,
}


def _n1ql_rank(raw: object, *, present: bool) -> int:
    if not present:
        return _N1QL_RANK["missing"]
    if raw is None:
        return _N1QL_RANK["null"]
    if isinstance(raw, bool):
        return _N1QL_RANK["bool"]
    if isinstance(raw, int | float):
        return _N1QL_RANK["number"]
    if isinstance(raw, str):
        return _N1QL_RANK["string"]
    if isinstance(raw, list):
        return _N1QL_RANK["array"]
    return _N1QL_RANK["object"]


def _memory_rank(raw: object) -> int:
    """Where the in-memory fake puts the same raw stored value.

    NOTE the two-step: a rehydrated non-string is normalized to `None` by
    `CandidateEnvelope.from_doc` FIRST, and `_sort_key` then ranks that `None` at 0.
    In Couchbase there is no such normalization before the sort — N1QL orders the RAW
    stored value server-side, and `from_doc` only runs on the rows that came back."""
    env = CandidateEnvelope.from_doc(
        {**_envelope("c::probe").to_doc(), "last_scanned_at": raw}
    )
    return _sort_key(env, "last_scanned_at")[0]


# --- the claim the whole rotation rests on ------------------------------------


def test_missing_sorts_first_in_both_stores():
    """The load-bearing claim, stated as a parity assertion rather than a comment.

    A never-scanned candidate omits the key entirely (`to_doc` emits it only when set),
    and MISSING is the LOWEST value in the measured N1QL collation — so brand-new work
    jumps the queue in production exactly as it does in the fake. If this were the other
    way round the fix would invert into a WORSE starvation bug than the one it replaced:
    newly extracted candidates would queue behind every already-examined row."""
    never = _envelope("c::never")
    assert "last_scanned_at" not in never.to_doc()

    assert _n1ql_rank(None, present=False) == 0  # MISSING is rank 0 in N1QL
    assert _sort_key(never, "last_scanned_at")[0] == 0  # ...and in the fake
    # And strictly below every stamped row in both.
    assert _n1ql_rank("2020-01-01T00:00:00+00:00", present=True) > 0
    assert _sort_key(
        replace(never, last_scanned_at="2020-01-01T00:00:00+00:00"), "last_scanned_at"
    )[0] > 0


@pytest.mark.parametrize(
    ("raw", "kind"),
    [(None, "null"), (True, "bool"), (123, "number"), (12.5, "number")],
)
def test_scalar_junk_cursors_still_sort_ahead_of_every_real_stamp(raw, kind):
    """A NULL / boolean / numeric cursor (a hand-edit via cbq, a foreign writer) sorts
    before every ISO string in N1QL, and rank-0 in the fake. The two are not identical
    — N1QL keeps null < bool < number < string, the fake collapses all of them to one
    bucket — but they AGREE on the only thing the rotation needs: a junk cursor is
    scanned SOON, and the next stamp repairs it."""
    assert _n1ql_rank(raw, present=True) < _N1QL_RANK["string"]
    assert _memory_rank(raw) < _sort_key(
        replace(_envelope("c::x"), last_scanned_at="2020-01-01T00:00:00+00:00"),
        "last_scanned_at",
    )[0]


# --- the divergence -----------------------------------------------------------


@pytest.mark.parametrize("raw", [["2020-01-01T00:00:00+00:00"], {"at": "2020-01-01"}])
def test_array_or_object_cursor_sorts_opposite_ways_in_the_two_stores_is_a_known_limitation(
    raw,
):
    """KNOWN LIMITATION (store parity, LOW severity — needs a corrupt document).

    `CandidateEnvelope.from_doc` normalizes a non-string `last_scanned_at` to `None`,
    and `models.py` documents that as "the SELF-HEALING direction: the candidate sorts
    first, gets scanned immediately, and the next stamp overwrites the malformed value."

    That is TRUE of the in-memory fake and FALSE of Couchbase. `ORDER BY` runs
    SERVER-SIDE on the RAW stored value; `from_doc` only ever sees rows that already came
    back. In the measured N1QL collation an array or object sorts AFTER every string —
    i.e. LAST — so a candidate whose cursor was corrupted to an array/object is pushed to
    the very back of the rotation and, past `scan_limit` rows, is never examined again.
    The fake reports the exact opposite (scanned first), so no unit test can see it.

    Not a blocker: it requires a malformed document, which nothing in the codebase
    writes (`touch_scanned` only ever stamps the clock's string). Pinned so the claim in
    `models.py` is not read as holding for production. Closing it means either ordering
    on a COALESCEd expression or normalizing on read in N1QL."""
    # Couchbase: LAST — behind every well-formed stamp.
    assert _n1ql_rank(raw, present=True) > _N1QL_RANK["string"]
    # In-memory fake: FIRST — ahead of every well-formed stamp.
    assert _memory_rank(raw) == 0


async def test_tied_cursors_order_by_candidate_id_but_still_do_not_rotate():
    """HISTORY — this pin FLIPPED, and only halfway. Read the whole docstring before
    changing it; the assertion below is unchanged but it now proves something different.

    ORIGINALLY (`test_neither_store_has_a_secondary_tiebreak_is_a_known_limitation`,
    QA round 1): `ORDER BY last_scanned_at ASC` had no secondary key in either impl, so
    rows sharing a cursor value had an impl-defined order — this fake fell back to dict
    insertion order, the Couchbase GSI to its implicit trailing doc key. The two stores
    could disagree about which rows the bounded window contained, and nothing pinned it.

    RESOLVED, in part: both impls now carry `candidate_id` as a secondary sort key
    (`ORDER BY last_scanned_at ASC, candidate_id ASC`, backed by the widened
    `idx_candidates_scan_rotation(status, last_scanned_at, candidate_id)`). Tie order is
    now TOTAL and MEASURED identical across the two stores against live Couchbase 7.6.5,
    with insertion order shuffled so it cannot be the thing being observed. The
    parity half of the limitation is closed.

    STILL OPEN, and now known NOT to be a tiebreak problem: the round-robin still stalls
    when every cursor is equal. The earlier docstring's closing claim — "a `created_at`
    or `META().id` tiebreak would make it safe by design" — was DISPROVED. A tiebreak
    makes the order total, not rotating; what rotates the window is the CURSOR advancing,
    which confines ties to rows stamped inside one clock tick. Any clock that moves
    between cycles is therefore fine, including a coarse one. A clock frozen across
    cycles leaves every row permanently tied and the same prefix returns for ever — by
    construction. `_now_iso()` is microsecond-resolution, so production only reaches this
    via a bulk back-fill writing one constant cursor; a harness with a pinned clock
    reaches it trivially, which is why a pinned-clock test cannot detect a stall.

    (Measured for the record: `ORDER BY last_scanned_at, candidate_id` against the OLD
    two-key index makes the planner abandon it for the plain status index plus a full
    Order stage; `META().id` keeps the index but still adds an Order. Only the widened
    three-key index preserves `index_order` + LIMIT pushdown.)

    See `test_rotation_stalls_when_every_cursor_is_identical_is_a_known_limitation` in
    tests/learning/promotion for the scheduler-level consequence."""
    store = InMemoryCandidateStore()
    # SHUFFLED insertion, so `candidate_id` order and insertion order are different
    # answers and the assertion below can only be satisfied by the former. The original
    # version of this test inserted in id order, where the two coincide.
    for i in (4, 1, 5, 0, 3, 2):
        await store.put(
            _envelope(f"c::{i}", last_scanned_at="2026-08-10T12:00:00+00:00")
        )

    got = await store.list_by_status(
        CandidateStatus.CANDIDATE, limit=3, order_by="last_scanned_at"
    )

    # `candidate_id` order (the tiebreak), NOT insertion order — the resolved half.
    assert [c.candidate_id for c in got] == ["c::0", "c::1", "c::2"]
    # ...and identically on every repeat call, which is precisely why nothing rotates
    # while the stamps stay equal — the half that a tiebreak was never going to fix.
    again = await store.list_by_status(
        CandidateStatus.CANDIDATE, limit=3, order_by="last_scanned_at"
    )
    assert [c.candidate_id for c in again] == ["c::0", "c::1", "c::2"]
