"""Layer-2 tests for CouchbaseSessionStore — require a real Couchbase cluster.

Skipped automatically unless RUN_COUCHBASE_TESTS is set (and a cluster is
reachable at the configured connection string), so `uv run pytest` stays
green with zero infrastructure (design §8).
"""

from __future__ import annotations

import os

import pytest

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.session.couchbase_store import COUCHBASE_AVAILABLE

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE or not os.environ.get("RUN_COUCHBASE_TESTS"),
    reason="Requires the 'couchbase' package AND a live Couchbase cluster "
    "(set RUN_COUCHBASE_TESTS=1 with a reachable COUCHBASE_CONNECTION_STRING).",
)


@pytest.fixture
def settings() -> RuntimeSettings:
    return RuntimeSettings(_env_file=None)


async def test_create_and_load_session_round_trips(settings: RuntimeSettings) -> None:
    from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

    store = CouchbaseSessionStore(settings)
    doc = await store.create_session("layer2-sess-1")
    assert doc.session_id == "layer2-sess-1"


async def test_real_cas_race(settings: RuntimeSettings) -> None:
    """D45 exactly-once against real Couchbase: two concurrent resumes over one
    checkpoint — EXACTLY ONE wins, the loser is rejected. The loser may raise
    either `CASMismatchError` (they truly interleaved and the stale CAS was
    caught) or `AlreadyConsumedError` (the winner committed first, so the loser
    re-read an already-consumed checkpoint). Both outcomes preserve exactly-once;
    the invariant under test is "one winner, one rejection", not the exact type.
    """
    import asyncio

    from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore
    from data_agent.runtime.session.models import PauseCheckpoint
    from data_agent.runtime.session.store import AlreadyConsumedError, CASMismatchError

    store = CouchbaseSessionStore(settings)
    checkpoint = PauseCheckpoint(
        reason="askUser",
        pending_question={"question": "Which dept?", "options": None},
        awaiting="user_answer",
        consumed=False,
    )
    await store.write_pause_checkpoint("layer2-sess-2", checkpoint)

    _, cas_a = await store.get_session_with_cas("layer2-sess-2")
    _, cas_b = await store.get_session_with_cas("layer2-sess-2")

    results = await asyncio.gather(
        store.resume_checkpoint("layer2-sess-2", cas_a, "Sales"),
        store.resume_checkpoint("layer2-sess-2", cas_b, "Engineering"),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1, f"exactly one resume must win; got {results!r}"
    assert len(failures) == 1, f"exactly one resume must be rejected; got {results!r}"
    assert isinstance(failures[0], (AlreadyConsumedError, CASMismatchError)), failures[0]


async def test_analysis_state_round_trips_and_merges_against_real_couchbase(
    settings: RuntimeSettings,
) -> None:
    """`apply_analysis_state` against the real store (Release 1, 03 §B).

    The Layer-1 sibling in `test_couchbase_store_cas_retry.py` proves the merge
    callback re-runs against the winner's document with a mocked collection; this
    proves the field itself survives a real write/read cycle, that the A.1 gate
    holds across turns, and that a second call merges rather than replaces.
    """
    from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore
    from data_agent.runtime.session.models import (
        AnalysisState,
        TrackedIntent,
        live_analysis_state,
    )

    store = CouchbaseSessionStore(settings)
    session_id = "layer2-sess-analysis-state"
    await store.create_session(session_id)

    declared = await store.apply_analysis_state(
        session_id,
        7,
        lambda _live: AnalysisState(
            turn_index=7,
            intents=(
                TrackedIntent(intent_id="i1", description="headcount", status="pending"),
                TrackedIntent(intent_id="i2", description="attrition", status="pending"),
            ),
        ),
    )
    assert len(declared.intents) == 2

    def _complete_i1(live: AnalysisState | None) -> AnalysisState:
        assert live is not None, "the second call must see the first call's state"
        by_id = {i.intent_id: i for i in live.intents}
        by_id["i1"] = TrackedIntent(
            intent_id="i1", description="headcount", status="completed",
            evidence_tool_call_id="call_1",
        )
        return AnalysisState(
            turn_index=7, intents=tuple(by_id[i.intent_id] for i in live.intents)
        )

    merged = await store.apply_analysis_state(session_id, 7, _complete_i1)
    assert [i.status for i in merged.intents] == ["completed", "pending"]

    doc = await store.get_or_create_session(session_id)
    assert live_analysis_state(doc, 7) == merged
    # A.1: history for every other turn.
    assert live_analysis_state(doc, 8) is None
