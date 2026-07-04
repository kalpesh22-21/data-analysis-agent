"""Layer-1 mapping + semantics for `CouchbaseBlueprintCorpus` (Wave 3b-(i), D48).

No infra: a fake async cluster/collection is injected so the store's doc mapping and
its D48 correctness posture are exercised hermetically. The ATOMICITY of the
increment (a real server-side counter with no lost updates) is proven separately by
the env-guarded Layer-2 live test — here we assert only that the store issues a
sub-document counter mutation (never a read-modify-write) and that both seed and
increment fail SOFT.
"""

from __future__ import annotations

import pytest

from data_agent.learning.config import LearningSettings
from data_agent.learning.dedup.corpus import CorpusArtifact
from data_agent.learning.dedup.couchbase_corpus import (
    COUCHBASE_AVAILABLE,
    CouchbaseBlueprintCorpus,
    _doc_id,
    _to_doc,
)

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE, reason="Requires the 'couchbase' package for exception types."
)


class _FakeGetResult:
    def __init__(self, doc: dict) -> None:
        self._doc = doc

    @property
    def content_as(self):
        return {dict: self._doc}


class _FakeCollection:
    """Records mutations; simulates KV get/insert/mutate_in with injectable errors."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.insert_calls: list[tuple[str, dict]] = []
        self.mutate_calls: list[tuple[str, list]] = []
        self.raise_not_found_on_mutate = False

    async def get(self, doc_id, options):
        from couchbase.exceptions import DocumentNotFoundException

        if doc_id not in self.docs:
            raise DocumentNotFoundException(f"missing {doc_id}")
        return _FakeGetResult(self.docs[doc_id])

    async def insert(self, doc_id, doc, options):
        from couchbase.exceptions import DocumentExistsException

        self.insert_calls.append((doc_id, doc))
        if doc_id in self.docs:
            raise DocumentExistsException(f"exists {doc_id}")
        self.docs[doc_id] = dict(doc)

    async def mutate_in(self, doc_id, specs):
        from couchbase.exceptions import DocumentNotFoundException

        self.mutate_calls.append((doc_id, specs))
        if self.raise_not_found_on_mutate or doc_id not in self.docs:
            raise DocumentNotFoundException(f"missing {doc_id}")
        self.docs[doc_id]["hit_count"] = int(self.docs[doc_id]["hit_count"]) + 1


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


def _corpus() -> tuple[CouchbaseBlueprintCorpus, _FakeCollection]:
    collection = _FakeCollection()
    settings = LearningSettings(_env_file=None)
    corpus = CouchbaseBlueprintCorpus(settings, cluster=_FakeCluster(collection))
    return corpus, collection


def _artifact(key: str = "sha256:abc") -> CorpusArtifact:
    return CorpusArtifact(
        id="candidate::1", canonical_key=key, intent="total earnings by dept",
        hit_count=1, uses_rules=("active_employee",),
    )


def test_doc_mapping_round_trips():
    art = _artifact()
    doc = _to_doc(art)
    assert doc == {
        "id": "candidate::1",
        "canonical_key": "sha256:abc",
        "intent": "total earnings by dept",
        "hit_count": 1,
        "uses_rules": ["active_employee"],
    }
    assert CorpusArtifact.from_doc(doc) == art


def test_doc_id_is_namespaced():
    assert _doc_id("sha256:abc") == "corpus::sha256:abc"


async def test_get_by_canonical_key_maps_doc():
    corpus, collection = _corpus()
    art = _artifact()
    collection.docs[_doc_id(art.canonical_key)] = _to_doc(art)
    got = await corpus.get_by_canonical_key(art.canonical_key)
    assert got == art


async def test_get_missing_returns_none():
    corpus, _ = _corpus()
    assert await corpus.get_by_canonical_key("sha256:nope") is None


async def test_seed_artifact_is_insert_wins_idempotent():
    corpus, collection = _corpus()
    art = _artifact()
    await corpus.seed_artifact(art)
    # A second seed at the same key is a tolerated no-op (insert → DocumentExists).
    await corpus.seed_artifact(CorpusArtifact.from_doc({**_to_doc(art), "hit_count": 99}))
    assert len(collection.insert_calls) == 2  # both attempted...
    assert collection.docs[_doc_id(art.canonical_key)]["hit_count"] == 1  # ...count NOT clobbered


async def test_increment_uses_subdocument_counter_not_rmw():
    corpus, collection = _corpus()
    art = _artifact()
    await corpus.seed_artifact(art)
    await corpus.increment_hit_count(art.canonical_key)
    # Exactly one mutate_in with a spec list — a server-side counter, never a
    # get-then-put read-modify-write (no get is issued by increment).
    assert len(collection.mutate_calls) == 1
    doc_id, specs = collection.mutate_calls[0]
    assert doc_id == _doc_id(art.canonical_key)
    assert isinstance(specs, list) and len(specs) == 1
    assert collection.docs[doc_id]["hit_count"] == 2


async def test_increment_on_vanished_key_is_fail_soft_noop():
    corpus, collection = _corpus()
    collection.raise_not_found_on_mutate = True
    # A hit on a vanished artifact must be a tolerated no-op, never a crash (D52).
    await corpus.increment_hit_count("sha256:gone")


async def test_hit_count_reader_role():
    corpus, collection = _corpus()
    art = _artifact()
    collection.docs[_doc_id(art.canonical_key)] = {**_to_doc(art), "hit_count": 4}
    assert await corpus.hit_count(art.canonical_key) == 4
    # Absent artifact ⇒ 0 (nothing has accrued).
    assert await corpus.hit_count("sha256:none") == 0
