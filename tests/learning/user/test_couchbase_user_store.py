"""`CouchbaseUserKnowledgeStore`'s keyspace guard and N1QL keyspace — Layer-1.

The REAL store, not the fake. That distinction is the whole reason this module exists:
`tests/learning/user/test_user_store.py` and `test_adversarial_boundaries.py` assert the
sibling-scope denial against `InMemoryUserKnowledgeStore`, whose `open_keyspace` is a
plain equality on an OPAQUE string. The fake has no notion of a bucket, a scope or a
collection, so `!=` fires for every non-granted string no matter how the grant is spelled
— including under the old bucket-name compare. Those tests therefore document the
boundary; they cannot fail because of it.

What actually has to hold lives here, in the only implementation that BUILDS a keyspace
out of configuration:

  - `keyspace()` is composed from bucket + scope + collection, so a sibling scope in the
    SAME bucket is a different string and the guard denies it (D17 — this is the one
    entity-bearing store, and in the shared `pcm_iwant` layout a bucket-name compare
    would admit `learning`.`audit`, `learning`.`candidates` and `sessions`.`sessions`);
  - `list_for_user` scans that SAME three-part keyspace, so it cannot read the sibling
    scopes' documents and hand them to `UserKnowledgeRecord.from_doc`.

One `keyspace()` feeds both, and both are asserted against it here — a store whose guard
and query ever disagreed about what it may touch would fail this module.

Hermetic: no cluster, no network.
"""

from __future__ import annotations

import pytest

from data_agent.learning.user.config import UserKnowledgeStoreConfig
from data_agent.learning.user.store import UserKnowledgeAccessError
from data_agent.runtime.couchbase_connect import COUCHBASE_AVAILABLE

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE, reason="Requires the 'couchbase' package for QueryOptions."
)


class _FakeQueryResult:
    """An empty N1QL result — these tests assert the STATEMENT, not the rows."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeCollection:
    pass


class _FakeBucket:
    def __init__(self, collection) -> None:
        self._collection = collection

    def scope(self, _name):
        return self

    def collection(self, _name):
        return self._collection

    def default_collection(self):
        return self._collection


class _FakeCluster:
    def __init__(self) -> None:
        self.queries: list[tuple[str, object]] = []

    def bucket(self, name):
        return _FakeBucket(_FakeCollection())

    def query(self, statement, options):
        self.queries.append((statement, options))
        return _FakeQueryResult()


def _store(**overrides):
    from data_agent.learning.user.couchbase_user_store import CouchbaseUserKnowledgeStore

    cluster = _FakeCluster()
    config = UserKnowledgeStoreConfig(_env_file=None, **overrides)
    return CouchbaseUserKnowledgeStore(config, cluster=cluster), cluster


_SHARED = {
    "user_knowledge_bucket": "pcm_iwant",
    "user_knowledge_scope": "user",
    "user_knowledge_collection": "knowledge",
}


# --- the grant is composed from config ---------------------------------------


def test_keyspace_is_composed_from_bucket_scope_and_collection():
    store, _ = _store(**_SHARED)
    assert store.keyspace() == "`pcm_iwant`.`user`.`knowledge`"


def test_default_keyspace_is_the_bucket_per_store_layout():
    """The shipped default reproduces the pre-scope deployment exactly."""
    store, _ = _store()
    assert store.keyspace() == "`user_knowledge`.`_default`.`_default`"


# --- the RBAC boundary, on the real store ------------------------------------


def test_open_keyspace_denies_every_sibling_scope_in_the_same_bucket():
    """The assertion the fake cannot make.

    Every keyspace below shares the granted BUCKET and differs only in scope and/or
    collection — the shared-bucket layout. A guard comparing bucket names would return the
    store for all of them, which is why the compare has to be on the composed keyspace.
    """
    store, _ = _store(**_SHARED)
    assert store.open_keyspace("`pcm_iwant`.`user`.`knowledge`") is store

    for sibling in (
        "`pcm_iwant`.`learning`.`audit`",
        "`pcm_iwant`.`learning`.`candidates`",
        "`pcm_iwant`.`learning`.`corpus`",
        "`pcm_iwant`.`sessions`.`sessions`",
        "`pcm_iwant`.`sessions`.`session_results`",
        # Same SCOPE, different collection — the narrowest miss there is.
        "`pcm_iwant`.`user`.`shadow`",
        "`pcm_iwant`.`user`.`_default`",
    ):
        with pytest.raises(UserKnowledgeAccessError):
            store.open_keyspace(sibling)


def test_open_keyspace_denies_the_bare_bucket_name():
    """The OLD grant spelling must no longer open the store.

    `open_bucket("pcm_iwant")` used to be the accepted call. If the bare bucket name still
    passed, the rename would be cosmetic and the guard would be back to admitting every
    sibling scope in the bucket.
    """
    store, _ = _store(**_SHARED)
    for bare in ("pcm_iwant", "`pcm_iwant`", "`pcm_iwant`.`user`"):
        with pytest.raises(UserKnowledgeAccessError):
            store.open_keyspace(bare)


def test_denial_message_names_both_keyspaces():
    store, _ = _store(**_SHARED)
    with pytest.raises(UserKnowledgeAccessError) as exc:
        store.open_keyspace("`pcm_iwant`.`learning`.`audit`")
    assert "`pcm_iwant`.`user`.`knowledge`" in str(exc.value)
    assert "`pcm_iwant`.`learning`.`audit`" in str(exc.value)


# --- the read scans the granted keyspace, nothing wider ----------------------


async def test_list_for_user_scans_the_configured_three_part_keyspace():
    """A one-part `FROM `pcm_iwant`` would read every sibling scope in the bucket."""
    store, cluster = _store(**_SHARED)

    await store.list_for_user("user-A", limit=10)

    statement, _ = cluster.queries[0]
    assert statement == (
        "SELECT r.* FROM `pcm_iwant`.`user`.`knowledge` r "
        "WHERE r.user_id = $user_id "
        "ORDER BY r.committed_at ASC LIMIT $limit"
    )


async def test_list_for_user_scans_exactly_the_keyspace_the_guard_protects():
    """ONE definition behind both — a guard that protected a keyspace the query did not
    read (or vice versa) would be a boundary in name only."""
    store, cluster = _store(**_SHARED)

    await store.list_for_user("user-A")

    statement, _ = cluster.queries[0]
    assert f"FROM {store.keyspace()} r " in statement
    assert store.open_keyspace(store.keyspace()) is store


async def test_default_config_scans_the_bucket_per_store_keyspace():
    """NO REGRESSION: with nothing configured the scan names the dedicated bucket's
    default scope + collection — which is what the previous one-part
    ``FROM `user_knowledge``` resolved to, since N1QL reads a one-part keyspace as the
    bucket's `_default`.`_default`."""
    store, cluster = _store()

    await store.list_for_user("user-A")

    statement, _ = cluster.queries[0]
    assert statement.startswith("SELECT r.* FROM `user_knowledge`.`_default`.`_default` r ")
