"""Layer-1 unit tests for `CouchbaseSessionStore._mutate_with_cas_retry` (B2).

Unlike `test_couchbase_store.py` (Layer 2, needs a real cluster), these tests
mock the Couchbase `collection` object directly — no live cluster required,
just the `couchbase` package installed (for its exception/options types).
Skipped automatically if `couchbase` is not importable, so `uv run pytest`
stays green with zero infrastructure either way.

Proves the actual defect this fix closes: `append_message`/`append_trail_entry`/
`bump_last_activity`/`write_pause_checkpoint` used to do a bare
read -> mutate -> `upsert()` with NO CAS guard, so a losing concurrent writer
silently clobbered another write. Now every one of them retries on
`CasMismatchException` (bounded, with backoff) against a freshly re-read
document, and raises `CASMismatchError` (never silently drops the write) if
contention persists past the retry budget.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.couchbase_connect import COUCHBASE_AVAILABLE

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE,
    reason="Requires the 'couchbase' package (for its exception/options types only "
    "— no live cluster is used here, the collection object is mocked).",
)


class _FakeContentAs:
    def __init__(self, content: dict) -> None:
        self._content = content

    def __getitem__(self, _type: type) -> dict:
        return self._content


class _FakeGetResult:
    def __init__(self, content: dict, cas: int) -> None:
        self.content_as = _FakeContentAs(content)
        self.cas = cas


def _settings() -> RuntimeSettings:
    return RuntimeSettings(_env_file=None)


def _build_store(*, sleep):
    from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

    fake_cluster = MagicMock()
    sessions_collection = MagicMock()
    results_collection = MagicMock()
    fake_cluster.bucket.return_value.scope.return_value.collection.side_effect = [
        sessions_collection,
        results_collection,
    ]
    store = CouchbaseSessionStore(_settings(), cluster=fake_cluster, sleep=sleep)
    return store, sessions_collection


async def test_append_message_retries_on_cas_mismatch_then_succeeds() -> None:
    from couchbase.exceptions import CasMismatchException

    from data_agent.runtime.session.models import SessionDoc, TurnMessage

    base_doc = SessionDoc(session_id="s1", created_at="t0", last_activity="t0")

    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    store, sessions_collection = _build_store(sleep=_record_sleep)

    # Each re-read reflects a HIGHER cas, as if a concurrent writer kept
    # winning — the retry loop must use the freshly re-read cas, not a stale one.
    sessions_collection.get = AsyncMock(
        side_effect=[
            _FakeGetResult(base_doc.to_doc(), cas=1),
            _FakeGetResult(base_doc.to_doc(), cas=2),
        ]
    )

    replace_calls: list[dict] = []

    async def _replace(key, doc, options):  # noqa: ANN001 - test double
        replace_calls.append({"key": key, "doc": doc, "options": dict(options)})
        if len(replace_calls) == 1:
            raise CasMismatchException("concurrent writer won")
        return None

    sessions_collection.replace = AsyncMock(side_effect=_replace)

    await store.append_message(
        "s1", TurnMessage(turn_index=0, role="user", content="hi", ts="t1")
    )

    assert len(replace_calls) == 2  # one failed attempt, one successful retry
    assert sleeps == [0.02]  # bounded backoff, exactly one sleep between attempts
    # The retry used the FRESHLY re-read cas (2), never the stale first one (1).
    assert replace_calls[0]["options"]["cas"] == 1
    assert replace_calls[1]["options"]["cas"] == 2
    # The message was actually appended in the doc that finally got written.
    assert len(replace_calls[1]["doc"]["messages"]) == 1
    assert replace_calls[1]["doc"]["messages"][0]["content"] == "hi"


async def test_append_trail_entry_never_silently_drops_a_write_under_sustained_contention() -> (
    None
):
    """The defect this closes: with NO CAS guard, a persistently-contended
    session used to just silently clobber writes. Now, after exhausting the
    retry budget, the write fails LOUDLY (`CASMismatchError`) instead of
    disappearing without a trace."""
    from couchbase.exceptions import CasMismatchException

    from data_agent.runtime.session.models import SessionDoc, TrailEntry
    from data_agent.runtime.session.store import CASMismatchError

    base_doc = SessionDoc(session_id="s1", created_at="t0", last_activity="t0")

    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    store, sessions_collection = _build_store(sleep=_record_sleep)

    sessions_collection.get = AsyncMock(
        side_effect=[_FakeGetResult(base_doc.to_doc(), cas=n) for n in range(1, 20)]
    )
    sessions_collection.replace = AsyncMock(
        side_effect=CasMismatchException("always contended")
    )

    entry = TrailEntry(
        turn_index=0,
        tool_call_id="c1",
        tool_name="listDatabases",
        args={},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref=None,
        ts="t1",
    )

    with pytest.raises(CASMismatchError):
        await store.append_trail_entry("s1", entry)

    # Bounded — never an unbounded retry loop.
    assert sessions_collection.replace.call_count == 5
    assert len(sleeps) == 4  # one fewer sleep than attempts (no sleep after the last)


async def test_bump_last_activity_and_write_pause_checkpoint_are_also_cas_guarded() -> None:
    """Every mutating write goes through the SAME CAS-guarded path — not just
    `append_message`/`append_trail_entry`."""
    from data_agent.runtime.session.models import PauseCheckpoint, SessionDoc

    base_doc = SessionDoc(session_id="s1", created_at="t0", last_activity="t0")

    async def _no_sleep(_seconds: float) -> None:
        return None

    store, sessions_collection = _build_store(sleep=_no_sleep)
    sessions_collection.get = AsyncMock(
        return_value=_FakeGetResult(base_doc.to_doc(), cas=7)
    )
    sessions_collection.replace = AsyncMock(return_value=None)

    await store.bump_last_activity("s1")
    checkpoint = PauseCheckpoint(
        reason="askUser",
        pending_question={"question": "Which dept?", "options": None},
        awaiting="user_answer",
        consumed=False,
    )
    await store.write_pause_checkpoint("s1", checkpoint)

    assert sessions_collection.replace.call_count == 2
    for call in sessions_collection.replace.await_args_list:
        options = dict(call.args[2])
        assert options["cas"] == 7


async def test_a_delete_racing_the_create_retries_instead_of_dereferencing_none() -> None:
    """A doc removed between the create and the CAS re-read must not crash the caller.

    `_mutate_with_cas_retry` creates the session when the first read misses, then
    re-reads purely to get the CAS that `_create_session` does not return. A concurrent
    `remove` (or a TTL expiry) inside that window makes the re-read miss too — and the
    loop used to walk straight into `mutate(doc)` with `doc=None`, surfacing a
    competing-writer race as an `AttributeError` raised from inside the caller's
    callback. It must retry instead.
    """
    from data_agent.runtime.session.models import SessionDoc, TurnMessage

    base_doc = SessionDoc(session_id="s1", created_at="t0", last_activity="t0")

    async def _no_sleep(_seconds: float) -> None:
        return None

    store, sessions_collection = _build_store(sleep=_no_sleep)

    def _missing():
        from couchbase.exceptions import DocumentNotFoundException

        return DocumentNotFoundException("gone")

    # attempt 1: miss -> create -> the deleter wins and the re-read misses too.
    # attempt 2: the doc is there, and the write goes through.
    sessions_collection.get = AsyncMock(
        side_effect=[
            _missing(),
            _missing(),
            _missing(),
            _FakeGetResult(base_doc.to_doc(), cas=9),
        ]
    )
    sessions_collection.upsert = AsyncMock(return_value=None)
    sessions_collection.replace = AsyncMock(return_value=None)

    await store.append_message(
        "s1", TurnMessage(turn_index=0, role="user", content="hi", ts="t1")
    )

    assert sessions_collection.replace.call_count == 1
    assert dict(sessions_collection.replace.await_args.args[2])["cas"] == 9


async def test_a_relentless_deleter_fails_loudly_rather_than_looping() -> None:
    """If the delete keeps winning, the write must fail in a family callers already
    handle — never an unbounded loop and never an AttributeError."""
    from data_agent.runtime.session.store import CASMismatchError

    async def _no_sleep(_seconds: float) -> None:
        return None

    store, sessions_collection = _build_store(sleep=_no_sleep)

    from couchbase.exceptions import DocumentNotFoundException

    sessions_collection.get = AsyncMock(side_effect=DocumentNotFoundException("gone"))
    sessions_collection.upsert = AsyncMock(return_value=None)
    sessions_collection.replace = AsyncMock(return_value=None)

    with pytest.raises(CASMismatchError):
        await store.bump_last_activity("s1")

    assert sessions_collection.replace.call_count == 0


async def test_apply_analysis_state_recomputes_the_merge_against_the_winners_doc() -> None:
    """THE 03 §B.1 TEST — why the store API takes the merge and not the result.

    `_mutate_with_cas_retry` documents its precondition: the callback "may be
    called more than once (once per retry) against a freshly re-read document, so
    it must not carry any state of its own across calls". A tool that loaded the
    state, computed a merged `AnalysisState`, and handed that OBJECT to a
    `setattr` callback would break exactly that — on a CAS conflict the callback
    re-runs against the fresh doc but writes a value derived from the STALE read,
    clobbering the winner. That is the lost-update class the helper exists to
    prevent.

    Here a peer completes `i2` between the two reads. The merge, re-run against
    the winner's doc, must preserve `i2` while applying its own change to `i1`.
    """
    from couchbase.exceptions import CasMismatchException

    from data_agent.runtime.session.models import (
        AnalysisState,
        SessionDoc,
        TrackedIntent,
    )

    def _doc(intents: tuple[TrackedIntent, ...]) -> dict:
        return SessionDoc(
            session_id="s1",
            created_at="t0",
            last_activity="t0",
            analysis_state=AnalysisState(turn_index=2, intents=intents),
        ).to_doc()

    stale = (
        TrackedIntent(intent_id="i1", description="a", status="pending"),
        TrackedIntent(intent_id="i2", description="b", status="pending"),
    )
    # What the PEER wrote and we then re-read: i2 is already completed.
    winner = (
        TrackedIntent(intent_id="i1", description="a", status="pending"),
        TrackedIntent(
            intent_id="i2", description="b", status="completed",
            evidence_tool_call_id="call_peer",
        ),
    )

    async def _no_sleep(_seconds: float) -> None:
        return None

    store, sessions_collection = _build_store(sleep=_no_sleep)
    sessions_collection.get = AsyncMock(
        side_effect=[
            _FakeGetResult(_doc(stale), cas=1),
            _FakeGetResult(_doc(winner), cas=2),
        ]
    )

    replace_calls: list[dict] = []

    async def _replace(key, doc, options):  # noqa: ANN001 - test double
        replace_calls.append({"doc": doc, "options": dict(options)})
        if len(replace_calls) == 1:
            raise CasMismatchException("a peer state write won")
        return None

    sessions_collection.replace = AsyncMock(side_effect=_replace)

    seen_live: list[AnalysisState | None] = []

    def _merge(live: AnalysisState | None) -> AnalysisState:
        """Exactly the shape the tool uses: derive the new state FROM the state
        handed in, never from one captured outside."""
        seen_live.append(live)
        assert live is not None
        by_id = {i.intent_id: i for i in live.intents}
        by_id["i1"] = TrackedIntent(
            intent_id="i1", description="a", status="completed",
            evidence_tool_call_id="call_mine",
        )
        return AnalysisState(
            turn_index=2, intents=tuple(by_id[i.intent_id] for i in live.intents)
        )

    returned = await store.apply_analysis_state("s1", 2, _merge)

    # The callback ran once per attempt, against each freshly re-read document.
    assert [tuple(i.status for i in (s.intents if s else ())) for s in seen_live] == [
        ("pending", "pending"),
        ("pending", "completed"),
    ]
    written = replace_calls[-1]["doc"]["analysis_state"]["intents"]
    assert [i["status"] for i in written] == ["completed", "completed"]
    # The peer's evidence survived — the write did NOT clobber the winner.
    assert written[1]["evidence_tool_call_id"] == "call_peer"
    assert written[0]["evidence_tool_call_id"] == "call_mine"
    # And the RETURNED state is the one actually written, not the first attempt's.
    assert returned.intents[1].evidence_tool_call_id == "call_peer"


async def test_apply_analysis_state_hands_the_merge_only_the_live_state() -> None:
    """The A.1 gate lives in the store adapter, so no caller can forget it: a
    state belonging to another turn arrives as `None`."""
    from data_agent.runtime.session.models import (
        AnalysisState,
        SessionDoc,
        TrackedIntent,
    )

    other_turn = SessionDoc(
        session_id="s1",
        created_at="t0",
        last_activity="t0",
        analysis_state=AnalysisState(
            turn_index=1,
            intents=(TrackedIntent(intent_id="i1", description="a", status="pending"),),
        ),
    ).to_doc()

    async def _no_sleep(_seconds: float) -> None:
        return None

    store, sessions_collection = _build_store(sleep=_no_sleep)
    sessions_collection.get = AsyncMock(
        side_effect=[_FakeGetResult(other_turn, cas=1)]
    )
    sessions_collection.replace = AsyncMock(return_value=None)

    seen: list[object] = []

    def _merge(live):  # noqa: ANN001, ANN202 - test double
        seen.append(live)
        return AnalysisState(
            turn_index=9,
            intents=(TrackedIntent(intent_id="i1", description="new", status="pending"),),
        )

    await store.apply_analysis_state("s1", 9, _merge)
    assert seen == [None]


async def test_claim_finalization_block_checks_and_increments_in_one_cas_step() -> None:
    """05 §C.1 in the real store: the limit check and the increment happen inside
    ONE `_mutate_with_cas_retry` callback, so a concurrent claimant cannot also see
    "unspent". Here a peer claims the window's block between the two reads — the
    callback re-runs against the winner's doc and correctly reports `False`, rather
    than writing a second claim derived from the stale read."""
    from couchbase.exceptions import CasMismatchException

    from data_agent.runtime.session.models import SessionDoc

    def _doc(blocks: dict[str, int] | None) -> dict:
        return SessionDoc(
            session_id="s1",
            created_at="t0",
            last_activity="t0",
            finalization_blocks=blocks,
        ).to_doc()

    async def _no_sleep(_seconds: float) -> None:
        return None

    store, sessions_collection = _build_store(sleep=_no_sleep)
    sessions_collection.get = AsyncMock(
        side_effect=[_FakeGetResult(_doc(None), cas=1), _FakeGetResult(_doc({"0:2:intents": 1}), cas=2)]
    )

    replace_calls: list[dict] = []

    async def _replace(key, doc, options):  # noqa: ANN001 - test double
        replace_calls.append(doc)
        if len(replace_calls) == 1:
            raise CasMismatchException("a peer claimed it first")
        return None

    sessions_collection.replace = AsyncMock(side_effect=_replace)

    claimed = await store.claim_finalization_block("s1", 0, 2, "intents")

    assert claimed is False, "two claimants both got the window's single re-round"
    # The peer's count was NOT overwritten by one derived from the stale read.
    assert replace_calls[-1]["finalization_blocks"] == {"0:2:intents": 1}


async def test_two_claims_of_different_kinds_are_both_granted_across_a_cas_retry() -> None:
    """05 §J.3's central property, and the one the mechanism gives away for free —
    which is exactly why it needs asserting.

    Two claimants of DIFFERENT kinds contend for the same `(turn, window)`. They are
    independent allowances, so BOTH must be granted; and because they live in ONE
    map on ONE document, the loser's CAS retry must re-run its callback against the
    WINNER'S doc and preserve the winner's key.

    Simulated exactly as the same-kind test does, with the interleave inverted: the
    `answer_shape` claimant reads the pre-race doc, loses the replace, re-reads a doc
    that now carries `intents`, and writes BOTH keys.

    The regression this guards is a lost update, and it would be silent: any future
    optimisation that cached the read document across retries (or built the new
    `finalization_blocks` outside the callback) would still return `True` for both
    claims while dropping one key — the counters would look right and one gate would
    quietly get a second re-round it was never granted.
    """
    from couchbase.exceptions import CasMismatchException

    from data_agent.runtime.session.models import SessionDoc

    def _doc(blocks: dict[str, int] | None, cas: int) -> _FakeGetResult:
        return _FakeGetResult(
            SessionDoc(
                session_id="s1",
                created_at="t0",
                last_activity="t0",
                finalization_blocks=blocks,
            ).to_doc(),
            cas=cas,
        )

    async def _no_sleep(_seconds: float) -> None:
        return None

    store, sessions_collection = _build_store(sleep=_no_sleep)
    # Read 1: the `answer_shape` claimant sees an empty map (the race is on).
    # Read 2: its retry sees the `intents` claimant's committed write.
    sessions_collection.get = AsyncMock(
        side_effect=[_doc(None, cas=1), _doc({"0:1:intents": 1}, cas=2)]
    )

    replace_calls: list[dict] = []

    async def _replace(key, doc, options):  # noqa: ANN001 - test double
        replace_calls.append(doc)
        if len(replace_calls) == 1:
            raise CasMismatchException("the intents claimant committed first")
        return None

    sessions_collection.replace = AsyncMock(side_effect=_replace)

    assert await store.claim_finalization_block("s1", 0, 1, "answer_shape") is True, (
        "a claim of one kind was denied by another kind's claim in the same window"
    )
    # BOTH keys survive: the retry rebuilt from the winner's doc rather than from
    # the stale read it started with.
    assert replace_calls[-1]["finalization_blocks"] == {
        "0:1:intents": 1,
        "0:1:answer_shape": 1,
    }


async def test_claim_finalization_block_grants_the_first_caller() -> None:
    from data_agent.runtime.session.models import SessionDoc

    async def _no_sleep(_seconds: float) -> None:
        return None

    store, sessions_collection = _build_store(sleep=_no_sleep)
    sessions_collection.get = AsyncMock(
        return_value=_FakeGetResult(
            SessionDoc(session_id="s1", created_at="t0", last_activity="t0").to_doc(), cas=1
        )
    )
    sessions_collection.replace = AsyncMock(return_value=None)

    assert await store.claim_finalization_block("s1", 0, 3, "intents") is True
    written = sessions_collection.replace.call_args.args[1]
    assert written["finalization_blocks"] == {"0:3:intents": 1}
