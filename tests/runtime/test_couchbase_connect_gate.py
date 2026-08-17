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

import ast
import inspect
from pathlib import Path
from typing import Any, Literal, get_args, get_origin
from unittest.mock import MagicMock

import pytest

from data_agent.learning.audit.couchbase_audit_store import (
    CouchbaseAuditStore,
)
from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus
from data_agent.learning.user.config import UserKnowledgeStoreConfig
from data_agent.learning.user.couchbase_user_store import CouchbaseUserKnowledgeStore
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.couchbase_connect import COUCHBASE_AVAILABLE
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


def _build(store_cls: type, cluster: Any = None) -> tuple[Any, _FakeCluster]:
    """Construct one store of each kind against a fresh fake cluster.

    Passing `cluster=None` builds the store the way PRODUCTION does — no injected
    handle graph, so its cluster is built lazily at the first connect.
    """
    cluster = _FakeCluster() if cluster is None else cluster
    if store_cls is CouchbaseSessionStore:
        return CouchbaseSessionStore(RuntimeSettings(_env_file=None), cluster=cluster), cluster
    if store_cls is CouchbaseUserKnowledgeStore:
        config = UserKnowledgeStoreConfig(_env_file=None)
        return CouchbaseUserKnowledgeStore(config, cluster=cluster), cluster
    return store_cls(LearningSettings(_env_file=None), cluster=cluster), cluster


def _build_unwired(store_cls: type) -> Any:
    """The same store with NO cluster injected — the production construction."""
    if store_cls is CouchbaseSessionStore:
        return CouchbaseSessionStore(RuntimeSettings(_env_file=None))
    if store_cls is CouchbaseUserKnowledgeStore:
        return CouchbaseUserKnowledgeStore(UserKnowledgeStoreConfig(_env_file=None))
    return store_cls(LearningSettings(_env_file=None))


_STORE_CLASSES = [
    CouchbaseSessionStore,
    CouchbaseAuditStore,
    CouchbaseCandidateStore,
    CouchbaseBlueprintCorpus,
    CouchbaseUserKnowledgeStore,
]

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "data_agent"

# The shared construction/connect machinery in `runtime/couchbase_connect.py` — not
# stores themselves, and the discriminator the source scan below uses to find stores.
_STORE_MACHINERY = {"CouchbaseStoreBase"}


def _callee_name(func: ast.expr) -> str | None:
    """The bare name of whatever is being called (`Cluster` / `acouchbase.Cluster`)."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _base_names(node: ast.ClassDef) -> set[str]:
    return {_callee_name(base) for base in node.bases} - {None}  # type: ignore[operator]


def _couchbase_backed_classes() -> set[str]:
    """Every store class in the source tree that owns a Couchbase handle graph.

    A SOURCE scan, not an import-and-introspect one: the point is to find a store
    nobody registered, and a store nobody registered is a store nobody imported.

    Two signals, because the construction moved: a class that calls `Cluster(...)`
    itself, and a class that inherits `CouchbaseStoreBase` (which now builds the
    cluster on its behalf — that inheritance IS the store's declaration that it has
    the SDK's connect precondition to satisfy). The base itself is excluded: it is
    the shared machinery, not a store, and it is proven through all five of them.
    """
    found: set[str] = set()
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name in _STORE_MACHINERY:
                continue
            builds_cluster = any(
                isinstance(child, ast.Call) and _callee_name(child.func) == "Cluster"
                for child in ast.walk(node)
            )
            if builds_cluster or _base_names(node) & _STORE_MACHINERY:
                found.add(node.name)
    return found


def _public_coroutines(store_cls: type) -> list[str]:
    return sorted(
        name
        for name, member in inspect.getmembers(store_cls, inspect.iscoroutinefunction)
        if not name.startswith("_") and name not in _LIFECYCLE_METHODS
    )


def _synthetic_value(param: inspect.Parameter) -> Any:
    """One argument, derived from the parameter's own ANNOTATION.

    `MagicMock` satisfies every shape these methods do to their inputs before
    reaching a handle (attribute reads, `to_doc()`, `in`, `int()`, `list()`,
    f-string interpolation) — with one exception that a mock cannot fake: a
    parameter annotated as a `Literal` is a CLOSED ENUM, and the method may well
    validate it before touching a handle. `claim_finalization_block(kind=...)` does
    exactly that (an unrecognised kind would mint an unbounded allowance, so
    `finalization_block_key` raises), and a `MagicMock` there aborted the call
    before any Couchbase op and made this test report the connect defect it exists
    to find.

    So a `Literal` yields its FIRST member. That is still derivation from the
    signature — the thing this module insists on — one level deeper than before, and
    it generalises: any future closed-enum parameter is handled the day it is added.
    """
    annotation = param.annotation
    if get_origin(annotation) is Literal:
        return get_args(annotation)[0]
    return MagicMock()


def _synthesise_args(method: Any) -> tuple[list[Any], dict[str, Any]]:
    """Arguments from the SIGNATURE, never from a maintained table.

    A table of per-method arguments would be one more thing to forget, which is the
    bug under test. Parameters with defaults are left alone; the rest are built by
    `_synthetic_value`.

    `eval_str=True` because every module here uses `from __future__ import
    annotations`, so without it each annotation is the STRING `"FinalizationBlockKind"`
    and no `Literal` is ever recognised. It falls back to the unevaluated signature
    if a name cannot be resolved, which degrades to the old MagicMock-only behaviour
    rather than failing the whole parametrisation.
    """
    try:
        signature = inspect.signature(method, eval_str=True)
    except (NameError, TypeError):  # an annotation this module cannot resolve
        signature = inspect.signature(method)
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for name, param in signature.parameters.items():
        if name == "self" or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.default is not param.empty:
            continue
        if param.kind is param.KEYWORD_ONLY:
            kwargs[name] = _synthetic_value(param)
        else:
            args.append(_synthetic_value(param))
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


def test_every_store_in_the_source_tree_is_registered_here() -> None:
    """`_STORE_CLASSES` is hand-written, which is the same shape of hole this module
    argues against one granularity down.

    The method list is derived, so a new METHOD that skips the gate fails above. But a
    new CLASS that never inherits the gate at all is invisible to that enumeration —
    it simply is not in the list, and nothing notices. So the list is checked against
    the source tree: every class that owns a Couchbase handle graph — by constructing
    an `acouchbase` `Cluster` or by inheriting the base that constructs one for it —
    must be registered, which is precisely the population that has the SDK's connect
    precondition to satisfy.

    The old known limit (a class handed a cluster it never builds would slip past)
    closed when construction moved into `CouchbaseStoreBase`: such a class must
    inherit the base to get its handles, and the inheritance is what is detected.
    """
    registered = {cls.__name__ for cls in _STORE_CLASSES}
    discovered = _couchbase_backed_classes()
    assert discovered == registered, (
        "the set of Couchbase-backed stores in src/ no longer matches the set proven "
        f"here. Only in the source tree: {sorted(discovered - registered)}. Only in "
        f"_STORE_CLASSES: {sorted(registered - discovered)}. A new store must be added "
        "to _STORE_CLASSES (and inherit CouchbaseStoreBase) or every one of its "
        "methods ships unproven against the SDK's connect precondition."
    )


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
    await store.get_or_create_session("s1")
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
        await store.get_or_create_session("s1")

    cluster.connect_error = None
    doc = await store.get_or_create_session("s1")
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


# --------------------------------------------------------------------------
# CONSTRUCTION is deferred too (`CouchbaseStoreBase`)
#
# `Cluster.__init__` reaches for the RUNNING event loop, so a store built at module
# import — which is what `uvicorn scripts.run_ui_runtime:app` does — used to raise
# `RuntimeError('Event loop is not running.')` before it could serve anything. The
# workaround lived outside the store (two hand-written lazy `SessionStore` proxies in
# `scripts/`, which drifted from the Protocol twice and needed their own AST test).
# These cases pin the seam that replaced them. They are deliberately SYNCHRONOUS where
# the point is "no loop": a sync test function has no running loop, which is the exact
# condition the launchers construct under.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("store_cls", _STORE_CLASSES, ids=lambda cls: cls.__name__)
def test_construction_builds_no_cluster_and_needs_no_event_loop(
    store_cls: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`__init__` does NO I/O: it must not call `Cluster(...)` at all."""
    from data_agent.runtime import couchbase_connect

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            f"{store_cls.__name__}.__init__ constructed a Cluster. That reaches for the "
            "running event loop, so the store can no longer be built at module import "
            "and every launcher needs a lazy proxy again."
        )

    monkeypatch.setattr(couchbase_connect, "Cluster", _refuse)
    store = _build_unwired(store_cls)
    assert store._cluster is None
    assert store._connect_targets == ()


@pytest.mark.parametrize("store_cls", _STORE_CLASSES, ids=lambda cls: cls.__name__)
async def test_the_first_await_builds_the_cluster_exactly_once(
    store_cls: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred, not dropped: the first gated call constructs the cluster from the
    store's own settings, opens its handles, and connects — and later calls reuse it."""
    from data_agent.runtime import couchbase_connect

    cluster = _FakeCluster()
    built: list[str] = []

    def _factory(connection_string: str, _options: Any) -> _FakeCluster:
        built.append(connection_string)
        return cluster

    monkeypatch.setattr(couchbase_connect, "Cluster", _factory)
    store = _build_unwired(store_cls)

    await store.connect()
    assert built == ["couchbase://localhost"]
    assert cluster.cluster_connects == 1 and cluster.bucket_connects == 1

    await store.connect()
    assert built == ["couchbase://localhost"], "the cluster was rebuilt on a later call"


async def test_a_half_built_store_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cluster survived but opening its handles did not (a missing bucket/scope,
    or a transient SDK error): the store must unwind to "never built".

    Keeping it would be worse than the original failure — `_cluster` set with
    `_connect_targets` still empty makes the gate find nothing to await, mark itself
    connected, and then `AttributeError` on an unbound collection for the rest of the
    process, while `close()` silently leaks the cluster it can no longer see.
    """
    from data_agent.runtime import couchbase_connect

    cluster = _FakeCluster()
    monkeypatch.setattr(couchbase_connect, "Cluster", lambda *_a, **_k: cluster)
    store = _build_unwired(CouchbaseSessionStore)

    real_open_handles = store._open_handles
    failures: list[int] = []

    def _open_handles_failing_once() -> None:
        if not failures:
            failures.append(1)
            raise RuntimeError("bucket 'agent_sessions' does not exist")
        real_open_handles()

    monkeypatch.setattr(store, "_open_handles", _open_handles_failing_once)

    with pytest.raises(RuntimeError, match="does not exist"):
        await store.get_or_create_session("s1")
    assert store._cluster is None, "a half-built store was cached"
    assert store._connect_targets == ()
    assert store._connected is False

    # ... and the next call rebuilds from scratch and works.
    doc = await store.get_or_create_session("s1")
    assert doc.session_id == "s1"
    assert cluster.connected is True


async def test_a_failed_lazy_build_is_retried_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same posture as a failed connect (invariant 3): a store built while the endpoint
    is unreachable must recover on the next call, not be poisoned for the process."""
    from data_agent.runtime import couchbase_connect

    cluster = _FakeCluster()
    attempts: list[int] = []

    def _factory(_connection_string: str, _options: Any) -> _FakeCluster:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("bad connection string")
        return cluster

    monkeypatch.setattr(couchbase_connect, "Cluster", _factory)
    store = _build_unwired(CouchbaseSessionStore)

    with pytest.raises(RuntimeError, match="bad connection string"):
        await store.get_or_create_session("s1")

    doc = await store.get_or_create_session("s1")
    assert doc.session_id == "s1"
    assert len(attempts) == 2


async def test_closing_a_store_that_never_connected_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown must not resurrect (or trip over) a cluster that was never built — a
    launcher that starts and stops without serving a request closes exactly this store."""
    from data_agent.runtime import couchbase_connect

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("close() built a Cluster")

    monkeypatch.setattr(couchbase_connect, "Cluster", _refuse)
    store = _build_unwired(CouchbaseSessionStore)

    await store.close()
    assert store._connected is False


def test_an_injected_cluster_still_opens_its_handles_eagerly() -> None:
    """The `cluster=` seam keeps its ORIGINAL timing: handles are derived in `__init__`.

    Unit suites inject one-shot fakes (`collection.side_effect = [sessions, results]`)
    and read the collections back straight after constructing the store, so moving that
    derivation behind the first await would break them — and, less visibly, would change
    which object a test asserts against. Only the settings-derived cluster is deferred,
    because only it needs a running loop.
    """
    store, cluster = _build(CouchbaseSessionStore)
    assert store._cluster is cluster
    assert store._connect_targets == (cluster, cluster.bucket("any"))
    assert store._sessions is cluster.bucket("any").default_collection()
