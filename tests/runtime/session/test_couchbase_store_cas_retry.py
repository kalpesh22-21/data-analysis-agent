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
from data_agent.runtime.session.couchbase_store import COUCHBASE_AVAILABLE

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
