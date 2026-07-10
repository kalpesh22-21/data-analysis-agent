"""CouchbaseCandidateStore.put retention branch (ui-inbox-type-archive §Retention).

No infra: a fake async cluster/collection is injected (mirroring
`test_couchbase_corpus_mapping.py`) so the KV upsert's `expiry` is captured
hermetically. The one behaviour under test is the highest-risk line — terminal
statuses (`rejected`/`validated`/`retired`) persist with NO TTL (`expiry=0`) so an
archived row is never evicted, while every transient status keeps the configured
candidate TTL.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from data_agent.learning.candidate import (
    CandidateStatus,
    build_envelope,
    mint_candidate_id,
)
from data_agent.learning.candidate.couchbase_candidate_store import (
    COUCHBASE_AVAILABLE,
    CouchbaseCandidateStore,
)
from data_agent.learning.config import LearningSettings
from data_agent.learning.extractor.validation import to_candidate

from ..extractor.helpers import blueprint_raw, make_summary

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE, reason="Requires the 'couchbase' package for UpsertOptions."
)


class _FakeCollection:
    """Records upserts with the exact `UpsertOptions` (→ expiry) each carried."""

    def __init__(self) -> None:
        self.upsert_calls: list[tuple[str, dict, object]] = []

    async def upsert(self, doc_id, doc, options):
        self.upsert_calls.append((doc_id, doc, options))


class _FakeBucket:
    def __init__(self, collection) -> None:
        self._collection = collection

    def default_collection(self):
        return self._collection


class _FakeCluster:
    def __init__(self, collection) -> None:
        self._bucket = _FakeBucket(collection)

    def bucket(self, name):
        return self._bucket


def _store() -> tuple[CouchbaseCandidateStore, _FakeCollection, LearningSettings]:
    collection = _FakeCollection()
    settings = LearningSettings(_env_file=None)
    store = CouchbaseCandidateStore(settings, cluster=_FakeCluster(collection))
    return store, collection, settings


def _envelope(status: str):
    raw = blueprint_raw(evidence=[{"turn_ref": 0, "tool_call_ref": "tc1", "quote": "q"}])
    cand = to_candidate(raw, make_summary(), known_rules=frozenset())
    summary = make_summary()
    cid = mint_candidate_id(summary.content_hash, 0)
    env = build_envelope(cand, summary, candidate_id=cid,
                         evidence_refs=("evidence::sess-1::a",))
    return replace(env, status=status)


@pytest.mark.parametrize(
    "status",
    [CandidateStatus.REJECTED, CandidateStatus.VALIDATED, CandidateStatus.RETIRED],
)
async def test_put_terminal_status_writes_with_no_ttl(status):
    """A terminal row (archived reject / settled validate / retire) persists with
    expiry=0 — it must never be TTL-evicted (D29 archive + settled records)."""
    store, collection, _ = _store()
    await store.put(_envelope(status))
    assert len(collection.upsert_calls) == 1
    _, _, options = collection.upsert_calls[0]
    assert options.get("expiry") == timedelta(0)


@pytest.mark.parametrize(
    "status",
    [
        CandidateStatus.EXTRACTED,
        CandidateStatus.CANDIDATE,
        CandidateStatus.IN_REVIEW,
        CandidateStatus.QUARANTINED,
    ],
)
async def test_put_transient_status_keeps_candidate_ttl(status):
    """A transient row keeps the configured candidate TTL (set fresh per write)."""
    store, collection, settings = _store()
    await store.put(_envelope(status))
    assert len(collection.upsert_calls) == 1
    _, _, options = collection.upsert_calls[0]
    # Assert against the independent source of truth (config), not the store's
    # private `_ttl`, so this pins the retention contract rather than the impl.
    assert options.get("expiry") == timedelta(
        seconds=settings.learning_candidates_ttl_seconds
    )
