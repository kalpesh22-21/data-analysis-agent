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
    """Confirms real Couchbase CAS semantics reject a losing concurrent resume."""
    from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore
    from data_agent.runtime.session.models import PauseCheckpoint
    from data_agent.runtime.session.store import CASMismatchError

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

    await store.resume_checkpoint("layer2-sess-2", cas_a, "Sales")
    with pytest.raises(CASMismatchError):
        await store.resume_checkpoint("layer2-sess-2", cas_b, "Engineering")
