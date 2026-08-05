"""Phase-3 — the additive `verified` flag round-trips through the candidate envelope.

`verified` is stamped on the neo4j node but the inbox reads the candidate STORE, so the
envelope carries a mirror copy. It must round-trip through `to_doc`/`from_doc` in BOTH
store impls, and stay ADDITIVE — a pre-Phase-3 doc (no `verified` key) reads back as the
False default, and a False envelope emits NO `verified` key (byte-identical to before).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from data_agent.learning.candidate.couchbase_candidate_store import (
    COUCHBASE_AVAILABLE,
    CouchbaseCandidateStore,
)
from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.config import LearningSettings


def _env(**over) -> CandidateEnvelope:
    base = CandidateEnvelope(
        candidate_id="candidate::verified::0",
        type="blueprint",
        status=CandidateStatus.VALIDATED,
        payload={"intent": "x"},
        source_session="s1",
        source_trace="t1",
        evidence_refs=(),
        extractor_rationale="",
        entity_scan={"result": "pass"},
        confidence=0.9,
        proposed_action="new",
        depends_on=(),
        content_hash="h1",
        created_at="2026-08-01T00:00:00+00:00",
    )
    return replace(base, **over)


# --- to_doc / from_doc ---------------------------------------------------------


def test_verified_true_round_trips_through_to_doc_from_doc() -> None:
    env = _env(verified=True)
    doc = env.to_doc()
    assert doc["verified"] is True
    assert CandidateEnvelope.from_doc(doc).verified is True


def test_verified_false_is_omitted_from_doc_and_defaults_false() -> None:
    """Additive + optional (mirrors `traceparent`): a False envelope emits NO `verified`
    key, and a doc lacking the key reads back as False — a pre-Phase-3 doc is unchanged."""
    env = _env(verified=False)
    doc = env.to_doc()
    assert "verified" not in doc
    # A legacy doc with no `verified` key round-trips to the False default.
    assert CandidateEnvelope.from_doc(doc).verified is False


# --- InMemoryCandidateStore ----------------------------------------------------


async def test_in_memory_store_round_trips_verified() -> None:
    store = InMemoryCandidateStore()
    await store.put(_env(verified=True))
    assert (await store.get("candidate::verified::0")).verified is True


# --- CouchbaseCandidateStore ---------------------------------------------------


class _FakeContent:
    def __init__(self, doc: dict) -> None:
        self._doc = doc

    def __getitem__(self, _type) -> dict:
        return self._doc


class _FakeResult:
    def __init__(self, doc: dict) -> None:
        self.content_as = _FakeContent(doc)


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def upsert(self, doc_id, doc, options):
        self.docs[doc_id] = doc

    async def get(self, doc_id, options):
        return _FakeResult(self.docs[doc_id])


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


@pytest.mark.skipif(
    not COUCHBASE_AVAILABLE, reason="Requires the 'couchbase' package for options."
)
async def test_couchbase_store_round_trips_verified() -> None:
    """The real store serializes via `to_doc` on put and rehydrates via `from_doc` on
    get, so `verified` survives the KV round-trip (hermetic fake collection)."""
    collection = _FakeCollection()
    store = CouchbaseCandidateStore(
        LearningSettings(_env_file=None), cluster=_FakeCluster(collection)
    )
    await store.put(_env(verified=True))
    assert collection.docs["candidate::verified::0"]["verified"] is True
    assert (await store.get("candidate::verified::0")).verified is True
