"""The connect gate, proven by INTROSPECTION over every Couchbase-backed store.

The defect this locks down: `acouchbase` starts its bootstrap in `Cluster.__init__`
but refuses every operation — KV and N1QL alike — until `on_connect()` has been
awaited, and a store's sync `__init__` cannot await. That wait was therefore an
invisible CALLER obligation, and it was honoured only where a human had run the
code interactively and watched it blow up (three scripts reaching into the private
`store._cluster`). Both daemon entrypoints skipped it, so `run_learning_sweeper`
failed on its first read every cycle, forever, behind one log line.

A hand-written test per method would reproduce exactly that failure mode: it would
cover the methods someone thought of. So this module writes no method list. It
enumerates every PUBLIC COROUTINE of every store class with `inspect`, synthesises
arguments from the signature, and drives each one against a double that reproduces
the SDK's own precondition (`_NotConnectedError` from any handle touched before
`on_connect`). A method added tomorrow that forgets `await self._ensure_connected()`
fails here whether or not anyone ever runs its code path against live Couchbase.

Hermetic: no cluster, no network. Skipped only when the `couchbase` package itself
is unimportable, since the stores refuse to construct without its options types.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from data_agent.learning.audit.couchbase_audit_store import (
    COUCHBASE_AVAILABLE,
    CouchbaseAuditStore,
)
from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus
from data_agent.learning.user.config import UserKnowledgeStoreConfig
from data_agent.learning.user.couchbase_user_store import CouchbaseUserKnowledgeStore
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE,
    reason="Requires the 'couchbase' package (for its options/exception types only "
    "— no live cluster is used here, the whole handle graph is a double).",
)

# `connect`/`close` ARE the lifecycle; they are the one pair exempt from
# "must gate before touching a handle" (see `CouchbaseConnectGate`).
_LIFECYCLE_METHODS = frozenset({"connect", "close"})


class _NotConnectedError(RuntimeError):
    """What the SDK raises, verbatim, from `ClientAdapter._ensure_connected`."""

    def __init__(self) -> None:
        super().__init__("Cannot perform operations without first establishing a connection.")


class _FakeCluster:
    """A cluster/bucket/collection graph that enforces the SDK's connect precondition.

    Every handle op consults the SHARED `connected` flag on this object and raises
    `_NotConnectedError` until `on_connect()` has been awaited — so a store method
    that touches a handle without gating fails exactly the way the live sweeper did.
    `ops` records what was touched, which is what lets the enumeration below tell a
    genuinely-gated method apart from one that returned before touching anything.
    """

    def __init__(self) -> None:
        self.connected = False
        self.cluster_connects = 0
        self.bucket_connects = 0
        self.closes = 0
        self.connect_error: Exception | None = None
        self.ops: list[str] = []
        self.docs: dict[Any, Any] = {}
        self._bucket = _FakeBucket(self)

    async def on_connect(self) -> None:
        self.cluster_connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def close(self) -> None:
        self.closes += 1
        self.connected = False

    def bucket(self, _name: str) -> _FakeBucket:
        return self._bucket

    def query(self, statement: str, *_options: Any) -> _FakeQueryResult:
        self.ops.append("query")
        if not self.connected:
            raise _NotConnectedError()
        return _FakeQueryResult()

    def guard(self, op: str) -> None:
        self.ops.append(op)
        if not self.connected:
            raise _NotConnectedError()


class _FakeBucket:
    def __init__(self, cluster: _FakeCluster) -> None:
        self._cluster = cluster
        self._collection = _FakeCollection(cluster)

    async def on_connect(self) -> None:
        self._cluster.bucket_connects += 1

    def scope(self, _name: str) -> _FakeBucket:
        return self

    def collection(self, _name: str) -> _FakeCollection:
        return self._collection

    def default_collection(self) -> _FakeCollection:
        return self._collection


class _FakeQueryResult:
    """An empty N1QL result — `async for` over zero rows."""

    def __aiter__(self) -> _FakeQueryResult:
        return self

    async def __anext__(self) -> dict[str, Any]:
        raise StopAsyncIteration


class _FakeGetResult:
    def __init__(self, content: Any, cas: int) -> None:
        self.content_as = {dict: content}
        self.cas = cas


class _FakeCollection:
    """A dict-backed KV collection. Every op is gated on the cluster's connect flag."""

    def __init__(self, cluster: _FakeCluster) -> None:
        self._cluster = cluster

    async def get(self, key: Any, *_args: Any) -> _FakeGetResult:
        from couchbase.exceptions import DocumentNotFoundException

        self._cluster.guard("get")
        if key not in self._cluster.docs:
            raise DocumentNotFoundException(f"no doc {key!r}")
        return _FakeGetResult(self._cluster.docs[key], cas=1)

    async def upsert(self, key: Any, doc: Any, *_args: Any) -> _FakeGetResult:
        self._cluster.guard("upsert")
        self._cluster.docs[key] = doc
        return _FakeGetResult(doc, cas=2)

    async def insert(self, key: Any, doc: Any, *_args: Any) -> _FakeGetResult:
        self._cluster.guard("insert")
        self._cluster.docs[key] = doc
        return _FakeGetResult(doc, cas=2)

    async def replace(self, key: Any, doc: Any, *_args: Any) -> _FakeGetResult:
        self._cluster.guard("replace")
        self._cluster.docs[key] = doc
        return _FakeGetResult(doc, cas=3)

    async def mutate_in(self, _key: Any, _specs: Any, *_args: Any) -> _FakeGetResult:
        self._cluster.guard("mutate_in")
        return _FakeGetResult({}, cas=4)

    async def remove(self, key: Any, *_args: Any) -> None:
        self._cluster.guard("remove")
        self._cluster.docs.pop(key, None)


def _build(store_cls: type) -> tuple[Any, _FakeCluster]:
    """Construct one store of each kind against a fresh fake cluster."""
    cluster = _FakeCluster()
    if store_cls is CouchbaseSessionStore:
        return CouchbaseSessionStore(RuntimeSettings(_env_file=None), cluster=cluster), cluster
    if store_cls is CouchbaseUserKnowledgeStore:
        config = UserKnowledgeStoreConfig(_env_file=None)
        return CouchbaseUserKnowledgeStore(config, cluster=cluster), cluster
    return store_cls(LearningSettings(_env_file=None), cluster=cluster), cluster


_STORE_CLASSES = [
    CouchbaseSessionStore,
    CouchbaseAuditStore,
    CouchbaseCandidateStore,
    CouchbaseBlueprintCorpus,
    CouchbaseUserKnowledgeStore,
]


def _public_coroutines(store_cls: type) -> list[str]:
    return sorted(
        name
        for name, member in inspect.getmembers(store_cls, inspect.iscoroutinefunction)
        if not name.startswith("_") and name not in _LIFECYCLE_METHODS
    )


def _synthesise_args(method: Any) -> tuple[list[Any], dict[str, Any]]:
    """Arguments from the SIGNATURE, never from a maintained table.

    A table of per-method arguments would be one more thing to forget, which is the
    bug under test. `MagicMock` satisfies every shape these methods do to their
    inputs before reaching a handle (attribute reads, `to_doc()`, `in`, `int()`,
    `list()`, f-string interpolation); parameters with defaults are left alone.
    """
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(method).parameters.items():
        if name == "self" or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.default is not param.empty:
            continue
        if param.kind is param.KEYWORD_ONLY:
            kwargs[name] = MagicMock()
        else:
            args.append(MagicMock())
    return args, kwargs


@pytest.mark.parametrize(
    ("store_cls", "method_name"),
    [
        pytest.param(cls, name, id=f"{cls.__name__}.{name}")
        for cls in _STORE_CLASSES
        for name in _public_coroutines(cls)
    ],
)
async def test_every_public_coroutine_connects_before_touching_a_handle(
    store_cls: type, method_name: str
) -> None:
    """No caller has to know: each method establishes the connection itself."""
    store, cluster = _build(store_cls)
    method = getattr(store, method_name)
    args, kwargs = _synthesise_args(method)

    try:
        await method(*args, **kwargs)
    except _NotConnectedError as exc:  # the defect
        pytest.fail(
            f"{store_cls.__name__}.{method_name} touched a Couchbase handle before "
            f"awaiting `_ensure_connected()` — it will raise {exc!r} the first time it "
            "runs against a live cluster from a caller that did not connect for it."
        )
    except Exception:  # noqa: BLE001 - domain errors from synthetic args are irrelevant here
        pass

    assert cluster.ops, (
        f"{store_cls.__name__}.{method_name} reached no Couchbase handle at all, so this "
        "test proved nothing about it. The synthesised arguments are probably wrong for "
        "this signature — give it real ones rather than leaving it unproven."
    )
    assert cluster.cluster_connects >= 1 and cluster.bucket_connects >= 1


def test_the_enumeration_actually_covers_every_store() -> None:
    """Guards the guard: a store class whose surface stopped being enumerated (renamed
    methods, an empty class) would make the parametrisation above silently vacuous."""
    for store_cls in _STORE_CLASSES:
        assert _public_coroutines(store_cls), f"{store_cls.__name__} enumerated no methods"
    # Every store inherits the SAME gate — no per-store re-implementation to drift.
    from data_agent.runtime.couchbase_connect import CouchbaseConnectGate

    for store_cls in _STORE_CLASSES:
        assert issubclass(store_cls, CouchbaseConnectGate)


async def test_connect_is_awaited_once_however_many_calls_are_made() -> None:
    """Idempotent: the gate is on the hot path of every op, so it must not re-await
    the SDK once connected."""
    store, cluster = _build(CouchbaseSessionStore)
    await store.create_session("s1")
    await store.get_or_create_session("s1")
    await store.load_trail("s1")
    assert cluster.cluster_connects == 1
    assert cluster.bucket_connects == 1


async def test_a_failed_connect_is_not_cached_as_success() -> None:
    """A store built while the cluster is down must RETRY on the next call, not be
    poisoned for the life of the process (the daemons never rebuild their stores)."""
    store, cluster = _build(CouchbaseSessionStore)
    cluster.connect_error = RuntimeError("cluster is down")

    with pytest.raises(RuntimeError, match="cluster is down"):
        await store.create_session("s1")

    cluster.connect_error = None
    doc = await store.create_session("s1")
    assert doc.session_id == "s1"
    assert cluster.cluster_connects == 2


async def test_the_sweeper_read_needs_no_connect_from_its_caller() -> None:
    """The exact path that had never run: `run_learning_sweeper` builds the store and
    calls `scan_idle_sessions` directly. N1QL is not exempt from the SDK's precondition."""
    store, cluster = _build(CouchbaseSessionStore)
    rows = await store.scan_idle_sessions(
        statuses=["none"], last_activity_before="2026-01-01T00:00:00+00:00", limit=10
    )
    assert rows == []
    assert cluster.connected is True


async def test_close_releases_the_cluster_and_marks_the_store_unconnected() -> None:
    """The public counterpart of `connect` — so no caller reaches for `store._cluster`."""
    store, cluster = _build(CouchbaseSessionStore)
    await store.connect()
    assert cluster.cluster_connects == 1

    await store.close()
    assert cluster.closes == 1
    assert store._connected is False
