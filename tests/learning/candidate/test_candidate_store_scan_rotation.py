"""The S9 scan cursor (`last_scanned_at`) + the rotating `list_by_status` read.

The bug these pin: `list_by_status` was `ORDER BY created_at ASC LIMIT $limit` with
no cursor, so a candidate stuck in a HOLD stayed at the front of the bounded window
for ever. Past `scan_limit` held candidates the scheduler could never see a newly
extracted one again — silent head-of-line starvation, no error, no metric.

Two things are asserted here rather than in the scheduler tests, because both are
store contracts the scheduler can only inherit:

  * ORDERING, including where a never-scanned (None/MISSING) cursor lands. It must
    sort FIRST — that is what makes new work jump the queue — and it must not raise,
    which a bare `sorted()` over `str | None` would.
  * PARITY between `InMemoryCandidateStore` and `CouchbaseCandidateStore`. The whole
    unit suite drives the scheduler through the fake, so any divergence here is a
    place where a green suite proves nothing about production.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from data_agent.learning.candidate import (
    CandidateStatus,
    InMemoryCandidateStore,
    build_envelope,
)
from data_agent.learning.candidate.couchbase_candidate_store import (
    CouchbaseCandidateStore,
)
from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.candidate.verdicts import DriftStamp
from data_agent.learning.config import LearningSettings
from data_agent.learning.extractor.validation import to_candidate
from data_agent.runtime.couchbase_connect import COUCHBASE_AVAILABLE

from ..extractor.helpers import blueprint_raw, make_summary


def _envelope(cid: str, *, status: str = CandidateStatus.CANDIDATE, **kwargs):
    raw = blueprint_raw(evidence=[{"turn_ref": 0, "tool_call_ref": "tc1", "quote": "q"}])
    summary = make_summary()
    cand = to_candidate(raw, summary, known_rules=frozenset())
    env = build_envelope(
        cand, summary, candidate_id=cid, evidence_refs=("evidence::sess-1::a",)
    )
    return replace(env, status=status, **kwargs)


# --- the envelope field itself ------------------------------------------------


def test_cursor_is_omitted_until_set_so_old_docs_round_trip_byte_identically():
    """A pre-S9 candidate doc has no `last_scanned_at` key at all. That is both a
    compatibility property (an existing doc round-trips unchanged) and the mechanism
    the rotation relies on: MISSING sorts before every string in N1QL, so a
    never-scanned candidate is picked up on the very next cycle."""
    env = _envelope("candidate::h::0")
    assert env.last_scanned_at is None
    assert "last_scanned_at" not in env.to_doc()
    assert CandidateEnvelope.from_doc(env.to_doc()) == env

    stamped = replace(env, last_scanned_at="2026-08-10T00:00:00+00:00")
    assert stamped.to_doc()["last_scanned_at"] == "2026-08-10T00:00:00+00:00"
    assert CandidateEnvelope.from_doc(stamped.to_doc()) == stamped


@pytest.mark.parametrize("bad", [123, 12.5, True, {"at": "now"}, ["now"], None])
def test_non_string_cursor_rehydrates_as_never_scanned_not_as_a_crash(bad):
    """This store holds rehydrated JSON that other processes (and humans, via cbq)
    can write. Every consumer of the cursor treats it as a string — `fromisoformat`
    raises TypeError on an int/dict, and a `str`-vs-`None` sort key is a TypeError
    too — so a non-string must be normalized at the boundary, not carried inward.

    Normalizing to None (rather than raising) is the SELF-HEALING direction: the
    candidate reads as never-scanned, sorts first, gets examined immediately, and the
    next stamp overwrites the malformed value."""
    doc = _envelope("candidate::h::0").to_doc()
    doc["last_scanned_at"] = bad
    assert CandidateEnvelope.from_doc(doc).last_scanned_at is None


# --- in-memory ordering + touch ------------------------------------------------


async def test_never_scanned_sorts_first_ahead_of_every_stamped_candidate():
    store = InMemoryCandidateStore()
    await store.put(_envelope("c::old", last_scanned_at="2020-01-01T00:00:00+00:00"))
    await store.put(_envelope("c::new", last_scanned_at="2026-01-01T00:00:00+00:00"))
    await store.put(_envelope("c::never"))  # no cursor

    got = await store.list_by_status(
        CandidateStatus.CANDIDATE, order_by="last_scanned_at"
    )

    assert [c.candidate_id for c in got] == ["c::never", "c::old", "c::new"]


async def test_mixed_none_and_string_cursors_do_not_raise_on_sort():
    """A bare `sorted()` over `str | None` raises `TypeError: '<' not supported
    between instances of 'str' and 'NoneType'` — which would be a crash in the cron's
    very first read, not a mis-order. The rank-tuple sort key makes it total."""
    store = InMemoryCandidateStore()
    for i in range(5):
        cursor = f"2026-01-0{i + 1}T00:00:00+00:00" if i % 2 else None
        await store.put(_envelope(f"c::{i}", last_scanned_at=cursor))

    got = await store.list_by_status(
        CandidateStatus.CANDIDATE, order_by="last_scanned_at"
    )

    assert [c.candidate_id for c in got][:3] == ["c::0", "c::2", "c::4"]  # unscanned


async def test_tied_cursors_break_on_candidate_id_not_on_insertion_order():
    """Without a secondary key, rows sharing a cursor value fell back to whatever each
    store happened to do — this fake to dict insertion order, the Couchbase GSI to its
    implicit trailing doc key — so "which rows the bounded window contains" was
    impl-defined and the two stores could disagree. Insertion order below is
    deliberately NOT id order, so a regression changes the answer.

    MEASURED equal against live Couchbase 7.6.5 with the shipped
    `idx_candidates_scan_rotation(status, last_scanned_at, candidate_id)` index."""
    store = InMemoryCandidateStore()
    tied = "2026-08-10T12:00:00+00:00"
    for i in (3, 0, 4, 1, 2):
        await store.put(_envelope(f"c::{i}", last_scanned_at=tied))
    await store.put(_envelope("c::never"))  # MISSING still outranks every tie

    got = await store.list_by_status(
        CandidateStatus.CANDIDATE, limit=4, order_by="last_scanned_at"
    )

    assert [c.candidate_id for c in got] == ["c::never", "c::0", "c::1", "c::2"]


async def test_desc_stays_the_exact_reverse_of_asc_including_the_tiebreak():
    """Every ORDER BY key takes the SAME direction in the N1QL statement, which is what
    lets this fake implement DESC as `sort(); reverse()` and still match. If the tiebreak
    were ever emitted with a fixed direction the two would silently diverge on ties."""
    store = InMemoryCandidateStore()
    tied = "2026-08-10T12:00:00+00:00"
    for i in (2, 0, 1):
        await store.put(_envelope(f"c::{i}", last_scanned_at=tied))

    asc = await store.list_by_status(
        CandidateStatus.CANDIDATE, order_by="last_scanned_at"
    )
    desc = await store.list_by_status(
        CandidateStatus.CANDIDATE, order_by="last_scanned_at", order="desc"
    )

    assert [c.candidate_id for c in desc] == [c.candidate_id for c in reversed(asc)]


async def test_created_at_ordering_is_unchanged_by_default():
    """The inbox/archive read is untouched: `order_by` defaults to `created_at`, so
    every existing caller keeps the byte-identical arrival-order view."""
    store = InMemoryCandidateStore()
    # Cursors are the REVERSE of created_at, so a default read that accidentally
    # sorted on the cursor would come back reversed.
    await store.put(
        _envelope("c::a", created_at="2026-01-01T00:00:00+00:00",
                  last_scanned_at="2026-09-09T00:00:00+00:00")
    )
    await store.put(
        _envelope("c::b", created_at="2026-02-01T00:00:00+00:00",
                  last_scanned_at="2026-01-01T00:00:00+00:00")
    )

    got = await store.list_by_status(CandidateStatus.CANDIDATE)

    assert [c.candidate_id for c in got] == ["c::a", "c::b"]


async def test_touch_scanned_writes_only_the_cursor_on_the_current_document():
    """The anti-clobber contract. The cron reads a snapshot at the top of the cycle
    and stamps the cursor at the end; if that stamp wrote the SCANNED copy back, it
    would revert anything a concurrent inbox transition changed in between. The fake
    must mirror the Couchbase sub-document write: re-read, change one field."""
    store = InMemoryCandidateStore()
    scanned = _envelope("c::x", status=CandidateStatus.CANDIDATE)
    await store.put(scanned)
    # A concurrent writer moves it on while the scheduler holds the stale `scanned`.
    await store.put(replace(scanned, status=CandidateStatus.VALIDATED, verified=True))

    await store.touch_scanned("c::x", "2026-08-10T12:00:00+00:00")

    current = await store.get("c::x")
    assert current.status == CandidateStatus.VALIDATED  # NOT reverted to candidate
    assert current.verified is True
    assert current.last_scanned_at == "2026-08-10T12:00:00+00:00"


async def test_touch_scanned_is_not_counted_as_a_put():
    """A cursor stamp is bookkeeping, not a lifecycle write: it neither rewrites the
    envelope nor (in Couchbase) renews the TTL, so it must not inflate `put_calls` —
    existing suites assert exact put totals."""
    store = InMemoryCandidateStore()
    await store.put(_envelope("c::x"))
    before = store.put_calls

    await store.touch_scanned("c::x", "2026-08-10T12:00:00+00:00")

    assert store.put_calls == before
    assert store.touch_calls == 1


async def test_touch_scanned_unknown_id_is_a_silent_no_op():
    """The document expired (90-day TTL) or was superseded between the scan read and
    the stamp. There is nothing left to rotate — never an error that would poison the
    cycle's decision for a candidate that no longer exists."""
    store = InMemoryCandidateStore()
    await store.touch_scanned("candidate::gone::0", "2026-08-10T12:00:00+00:00")
    assert store.all_candidates() == []


async def test_stamp_drift_writes_only_the_verdict_on_the_current_document():
    """`stamp_drift` is `touch_scanned`'s sibling and obeys the same contract: it
    re-reads the stored document and changes one field. S9 stamps a golden-replay
    verdict on candidates it is NOT transitioning, so it must not carry the rest of a
    cycle-start snapshot along with it."""
    store = InMemoryCandidateStore()
    scanned = _envelope("c::x")
    await store.put(scanned)
    await store.put(replace(scanned, status=CandidateStatus.VALIDATED, verified=True))

    await store.stamp_drift("c::x", DriftStamp(status="clean", probes=("grain_integrity",)))

    current = await store.get("c::x")
    assert current.status == CandidateStatus.VALIDATED  # NOT reverted
    assert current.verified is True
    assert current.drift.status == "clean"
    assert store.drift_stamps == 1


async def test_stamp_drift_on_a_deleted_candidate_does_not_resurrect_it():
    """The behaviour that matters most to reproduce in the fake. `supersede` deletes a
    session's candidates when a redelivered session re-extracts; if that lands between
    the cron's scan read and the verdict write, an upsert-shaped write would RECREATE
    the row the pipeline deliberately dropped. The real `mutate_in` uses REPLACE
    semantics and raises `DocumentNotFoundException`, which the store swallows — so the
    fake must be a no-op too, or the divergence only ever shows up in production."""
    store = InMemoryCandidateStore()
    await store.put(_envelope("c::x"))
    await store.supersede(_envelope("c::x").content_hash)
    assert store.all_candidates() == []

    await store.stamp_drift("c::x", DriftStamp(status="clean", probes=("grain_integrity",)))

    assert store.all_candidates() == []
    assert store.drift_stamps == 0


async def test_stamp_drift_is_not_counted_as_a_put():
    store = InMemoryCandidateStore()
    await store.put(_envelope("c::x"))
    before = store.put_calls

    await store.stamp_drift("c::x", DriftStamp(status="clean"))

    assert store.put_calls == before


# --- Couchbase parity ----------------------------------------------------------

pytestmark_cb = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE, reason="Requires the 'couchbase' package for options."
)


class _FakeCollection:
    def __init__(self) -> None:
        self.upsert_calls: list[tuple[str, dict, object]] = []
        self.mutate_calls: list[tuple[str, list, object]] = []

    async def upsert(self, doc_id, doc, options):
        self.upsert_calls.append((doc_id, doc, options))

    async def mutate_in(self, doc_id, specs, options):
        self.mutate_calls.append((doc_id, specs, options))


class _FakeQueryResult:
    def __init__(self) -> None:
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeCluster:
    def __init__(self, collection) -> None:
        self._collection = collection
        self.queries: list[tuple[str, object]] = []

    def bucket(self, name):
        return _FakeBucket(self._collection)

    def query(self, statement, options):
        self.queries.append((statement, options))
        return _FakeQueryResult()


class _FakeBucket:
    """One collection, reachable through EITHER binding path.

    The learning stores now bind `bucket.scope(...).collection(...)` (defaults
    `_default`/`_default`, the same handle `default_collection()` returns), so this
    double answers both and ignores the names — what these tests assert is the store's
    behaviour against a collection, not which one it picked.
    """

    def __init__(self, collection) -> None:
        self._collection = collection

    def scope(self, _name):
        return self

    def collection(self, _name):
        return self._collection

    def default_collection(self):
        return self._collection


def _cb_store():
    collection = _FakeCollection()
    cluster = _FakeCluster(collection)
    store = CouchbaseCandidateStore(LearningSettings(_env_file=None), cluster=cluster)
    return store, cluster, collection


@pytestmark_cb
async def test_default_n1ql_pins_the_whole_inbox_archive_statement():
    """The inbox/archive query is pinned WHOLE — a sort key and the keyspace are both
    interpolated, so this is the guard that neither drifts unnoticed.

    The keyspace is now THREE parts. Against the default settings that is
    ``learning_candidates`.`_default`.`_default``, which is what a bare
    ``learning_candidates`` already meant: N1QL resolves a one-part keyspace to the
    bucket's default scope + collection, and `CREATE PRIMARY INDEX ON `learning_candidates``
    provisions the index on that same collection. So the TEXT changed and the plan did
    not — an existing bucket-per-store deployment is unaffected. Naming all three parts is
    what makes the statement correct in a SHARED bucket, where the one-part form would
    scan the sessions/audit/corpus scopes too (see `CouchbaseCandidateStore._keyspace`).
    """
    store, cluster, _ = _cb_store()

    await store.list_by_status(CandidateStatus.IN_REVIEW, limit=50)

    statement, _ = cluster.queries[0]
    assert statement == (
        "SELECT c.* FROM `learning_candidates`.`_default`.`_default` c "
        "WHERE c.status = $status "
        "ORDER BY c.created_at ASC LIMIT $limit"
    )


@pytestmark_cb
async def test_n1ql_keyspace_follows_the_configured_scope_and_collection():
    """A shared-bucket deployment reaches its own scope by CONFIG alone — and the
    statement must name all three parts, or `list_by_status` would return other stores'
    documents from the same bucket."""
    settings = LearningSettings(
        _env_file=None,
        learning_candidates_bucket="pcm_iwant",
        learning_candidates_scope="learning",
        learning_candidates_collection="candidates",
    )
    collection = _FakeCollection()
    cluster = _FakeCluster(collection)
    store = CouchbaseCandidateStore(settings, cluster=cluster)

    await store.list_by_status(CandidateStatus.IN_REVIEW, limit=50)
    await store.supersede("hash-1")

    for statement, *_ in cluster.queries:
        assert "`pcm_iwant`.`learning`.`candidates`" in statement


@pytestmark_cb
async def test_rotation_read_orders_by_the_cursor_and_parameterizes_the_rest():
    store, cluster, _ = _cb_store()

    await store.list_by_status(
        CandidateStatus.CANDIDATE, limit=200, order_by="last_scanned_at"
    )

    statement, options = cluster.queries[0]
    # The tiebreak must be IN the statement, and both keys must take the same direction.
    # MEASURED on couchbase 7.6.5: with the shipped three-key
    # `idx_candidates_scan_rotation` this plans as `IndexScan3 index_order=[keypos 1,
    # keypos 2] limit=200` — no Order stage. Against a two-key index the planner drops
    # the index entirely and adds a full sort, so the ORDER BY and the index definition
    # in scripts/learning-candidates-init.sh must be changed together.
    assert "ORDER BY c.last_scanned_at ASC, c.candidate_id ASC" in statement
    # No freshness cutoff in the WHERE: a cutoff compares ISO timestamps as STRINGS,
    # so one row stamped with a different offset format could be excluded FOREVER —
    # re-creating the permanent starvation this ordering exists to fix. Ordering
    # mis-sorts such a row at worst; it never drops it.
    assert statement.count("WHERE") == 1
    assert "c.status = $status" in statement
    assert options["named_parameters"] == {"status": "candidate", "limit": 200}


@pytestmark_cb
def test_sort_key_is_allow_listed_never_interpolated_caller_text():
    """The sort key cannot be a named parameter, so it is interpolated. The only way
    that is safe is an allow-list lookup that RAISES on anything else — never a
    fallback that would splice an unexpected string into the statement."""
    from data_agent.learning.candidate.couchbase_candidate_store import _SORT_KEYS

    assert set(_SORT_KEYS) == {"created_at", "last_scanned_at"}
    with pytest.raises(KeyError):
        _SORT_KEYS["created_at ASC; DROP"]


@pytestmark_cb
async def test_touch_scanned_is_a_subdoc_write_that_preserves_expiry():
    """Two production-only properties the in-memory fake cannot express, so they are
    pinned directly on the real impl:

      * SUB-DOCUMENT, not `upsert` — it writes the single cursor path server-side, so
        it can never revert a concurrent inbox transition the way re-putting a stale
        scanned envelope would.
      * `preserve_expiry=True` — `put` sets the 90-day candidate TTL fresh on every
        write, so stamping the cursor through `put` every 5 minutes would make a
        permanently-held candidate immortal. The cursor is bookkeeping and must not
        restart the retention clock.
    """
    store, _, collection = _cb_store()

    await store.touch_scanned("candidate::h::0", "2026-08-10T12:00:00+00:00")

    assert collection.upsert_calls == []  # NOT a whole-document write
    doc_id, specs, options = collection.mutate_calls[0]
    assert doc_id == "candidate::h::0"
    assert len(specs) == 1  # exactly one path touched
    assert options.get("preserve_expiry") is True
    assert options.get("expiry") is None  # no TTL is asserted by a cursor stamp


@pytestmark_cb
async def test_stamp_drift_is_a_subdoc_write_that_preserves_expiry():
    """Same three production-only properties as the cursor stamp, plus the one that is
    specific to this field: because `mutate_in` REPLACES rather than upserts, a
    candidate `supersede` removed between the scan read and this write stays removed.
    A full-envelope `put` would have resurrected it."""
    store, _, collection = _cb_store()

    await store.stamp_drift(
        "candidate::h::0", DriftStamp(status="clean", probes=("grain_integrity",))
    )

    assert collection.upsert_calls == []  # NOT a whole-document write
    doc_id, specs, options = collection.mutate_calls[0]
    assert doc_id == "candidate::h::0"
    assert len(specs) == 1  # exactly one path touched
    assert options.get("preserve_expiry") is True
    assert options.get("expiry") is None  # a verdict stamp asserts no TTL


@pytestmark_cb
async def test_put_still_sets_the_ttl_fresh_so_the_cursor_change_did_not_leak():
    """Guard against the fix being applied in the wrong place: `put` must keep its
    documented "set fresh per write" retention behaviour for real lifecycle writes.
    Only the cursor stamp opts out."""
    store, _, collection = _cb_store()
    settings = LearningSettings(_env_file=None)

    await store.put(_envelope("candidate::h::0", status=CandidateStatus.CANDIDATE))

    _, _, options = collection.upsert_calls[0]
    assert options.get("expiry") == timedelta(
        seconds=settings.learning_candidates_ttl_seconds
    )
