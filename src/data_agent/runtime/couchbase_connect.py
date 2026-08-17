"""How a Couchbase-backed store is BUILT and CONNECTED — the one home for both.

Two things live here, in dependency order:

  * `CouchbaseConnectGate` — the ONE place a store waits for its own connection,
    so no caller has to know the SDK's lifecycle (the bulk of this docstring).
  * `CouchbaseStoreBase` — the ONE place a store's `Cluster`/bucket/collection
    handles are constructed, along with the SDK import guard, the TTL
    normalisation and the `get_or_none` KV-read idiom. It builds ON the gate:
    construction is DEFERRED to the gate's first await (see below), which is
    what makes a store constructible outside a running event loop.

WHY this exists. `acouchbase.Cluster.__init__` *starts* the bootstrap but does
not finish it, and the SDK then refuses EVERY operation until someone awaits
`on_connect()`: both KV ops (`ClientAdapter.execute_collection_request`) and
N1QL (`AsyncClusterImpl.query`) call the SDK's own internal `_ensure_connected()`
and raise

    RuntimeError: Cannot perform operations without first establishing a connection.

A store's `__init__` is SYNCHRONOUS and therefore cannot await that, so before
this module the wait was the CALLER's job — and, being invisible, it was done
only by the callers a human happened to run interactively (three scripts each
reached into the private `store._cluster.on_connect()`), never by the daemons
(`run_learning_sweeper`, `run_learning_consumer`, `run_learning_scheduler`, the
inbox service), where the failure is just a log line in a background process.
The learning sweeper had consequently NEVER completed a cycle against live
Couchbase. A private attribute reached into from N call sites is the symptom of
a missing public step; this gate is that step, moved inside the store.

INVARIANTS this mixin establishes:

  1. No public coroutine on a Couchbase-backed store may touch a cluster,
     bucket, or collection handle before `await self._ensure_connected()`.
     Enforced by introspection, not by review:
     `tests/runtime/test_couchbase_connect_gate.py` enumerates every
     public coroutine of every store class and drives it against a double that
     reproduces the SDK's precondition — a new method that skips the gate fails
     the unit suite whether or not anyone runs its code path live.
  2. `_ensure_connected` is IDEMPOTENT and safe to call from concurrent tasks.
     Awaiting the SDK's `on_connect()` after connection is a cheap no-op, and
     concurrent first-callers all await the SAME underlying connect future, so
     they coalesce rather than opening a second connection. `_connected` is a
     fast path, not a mutex — no lock is needed and none is taken.
  3. A FAILED connect is never cached. `_connected` is set only after every
     handle reports connected, so a store built against a down cluster retries
     on the next call instead of being permanently poisoned.

CONSTRUCTION is gated too (2026-08-17). `Cluster.__init__` reaches for the running
loop (`ClientAdapter._get_loop` raises `RuntimeError('Event loop is not running.')`),
so a store built at module import — which is exactly what `uvicorn module:app` and
both UI launchers do — used to raise before it could serve anything. That was worked
around OUTSIDE the store, by two hand-written ~110-line lazy proxies in `scripts/`
that re-declared every `SessionStore` method, drifted from the Protocol twice, and
needed their own AST test to keep honest. `CouchbaseStoreBase` below folds the
deferral into the gate instead: `__init__` does NO I/O and touches NO loop, and the
FIRST `_ensure_connected()` builds the cluster and opens the handles.

The cost that deferral was once declined for is real and stands: a bad connection
string or credential now surfaces on the first request rather than at boot. That is
what `connect()` is for — a caller that wants boot-time failure awaits it once
(inside its loop) and gets the same attributable error the eager construction gave.
A failed lazy build is not cached (`_cluster` is assigned only on success), so the
retry posture matches invariant 3 below.
"""

from __future__ import annotations

import inspect
import logging
from datetime import timedelta
from typing import Any

try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import DocumentNotFoundException
    from couchbase.options import ClusterOptions, GetOptions

    COUCHBASE_AVAILABLE = True
except ImportError:  # pragma: no cover
    COUCHBASE_AVAILABLE = False

_logger = logging.getLogger(__name__)


class CouchbaseConnectGate:
    """Mixin giving a Couchbase-backed store `_ensure_connected` / `connect` /
    `close`.

    The subclass (in practice `CouchbaseStoreBase`) registers its `Cluster` and
    bucket here via `_init_connect_gate` once they exist — at `__init__` when a
    ready cluster was injected, otherwise at the first `_ensure_connected()`.
    Everything asynchronous — the actual connect — is deferred to the first
    awaited method.
    """

    _connect_targets: tuple[Any, ...]
    _connected: bool

    def _init_connect_gate(self, cluster: Any, bucket: Any) -> None:
        """Register the handles whose connect must complete before any op.

        BOTH are registered, in this order, on purpose. `bucket.on_connect()`
        alone would be sufficient in the current SDK (the bucket-open request is
        chained onto the cluster connect future), but a cluster-level auth or
        endpoint failure then surfaces as a bucket-open error; awaiting the
        cluster first makes a bad credential read like a bad credential.
        """
        self._connect_targets = (cluster, bucket)
        self._connected = False

    async def _ensure_connected(self) -> None:
        """Block until this store's cluster and bucket are connected (invariant 1).

        Call this at the TOP of every public coroutine — including ones that only
        issue N1QL, which fail exactly the same way as KV ops do.

        A registered handle with no `on_connect`, or one whose `on_connect()`
        returns something that is not awaitable, is skipped: that is a unit-test
        double, not the SDK, and the unit suite must stay hermetic (and importable
        with no `couchbase` package at all). The gate exists to make the LIVE path
        correct; `tests/runtime/test_couchbase_connect_gate.py` supplies a
        double that DOES implement `on_connect` so the gate itself is still proven.

        That skip is fail-OPEN, so it is LOGGED (debug) with the handle's type. It is
        unreachable with the real SDK, and if it ever does fire in production the
        symptom lands somewhere else entirely — the next operation raising the SDK's
        'Cannot perform operations without first establishing a connection.' — with
        nothing at the skip site to find. One line naming the type is what makes that
        five minutes instead of the multi-caller hunt this module was written after.
        """
        if self._connected:
            return
        for target in self._connect_targets:
            on_connect = getattr(target, "on_connect", None)
            pending = on_connect() if on_connect is not None else None
            if inspect.isawaitable(pending):
                await pending
                continue
            _logger.debug(
                "connect gate SKIPPED handle %s.%s (%s) — assumed a test double, not "
                "the SDK. In production this means a real handle was never connected "
                "and the next operation on it will raise 'Cannot perform operations "
                "without first establishing a connection.'",
                type(target).__module__,
                type(target).__qualname__,
                "no on_connect attribute"
                if on_connect is None
                else "on_connect() returned a non-awaitable",
            )
        self._connected = True

    async def connect(self) -> None:
        """Establish the connection EAGERLY, before the first read.

        Optional — every public method gates itself, so nothing breaks if this is
        never called. It exists for callers that want a bad endpoint/credential to
        fail at startup with an attributable error rather than mid-work (e.g.
        `scripts/learning_trace.py`, which maps a connect failure to its own infra
        exit code). That used to require reaching into `store._cluster`.
        """
        await self._ensure_connected()

    async def close(self) -> None:
        """Release the cluster's resources (the public counterpart of `connect`).

        Idempotent from the store's side: closing resets `_connected`, so this
        store reports itself unconnected afterwards. The SDK does NOT support
        reconnecting a closed cluster (`_ensure_not_closed` raises), so a closed
        store is spent — `close` is for process shutdown, not for pooling.
        """
        self._connected = False
        if not self._connect_targets:
            # A lazily-built store that was never awaited has no cluster to
            # release — closing it is a no-op, not an error. (Registration and
            # construction happen together, so empty targets means "never built".)
            return
        cluster, _bucket = self._connect_targets
        closer = getattr(cluster, "close", None)
        if closer is None:
            return
        pending = closer()
        if inspect.isawaitable(pending):
            await pending


def couchbase_ttl(seconds: int, *, none_when_not_positive: bool = False) -> timedelta | None:
    """The `expiry=` value for a store's writes, from its configured seconds.

    Two semantics exist in this codebase and they are NOT interchangeable, so the
    choice is a parameter rather than a rule:

      * default — a bare `timedelta`, always applied (sessions, candidates, audit).
      * `none_when_not_positive=True` — a non-positive setting means "no expiry at
        all" and yields `None`, which the caller turns into options built WITHOUT
        `expiry=` (user knowledge, blueprint corpus). Passing `timedelta(0)` there
        would mean the opposite of what those stores intend for `0`, since the SDK
        reads a zero expiry as "clear the TTL".
    """
    if none_when_not_positive and seconds <= 0:
        return None
    return timedelta(seconds=seconds)


async def get_or_none(collection: Any, key: str) -> Any | None:
    """One KV read: the SDK `GetResult`, or `None` when the document is absent.

    "Missing is not exceptional" is the shape every store's read wants (a purged or
    TTL-expired document is a normal answer), so it is written once here instead of
    six times. Returns the RESULT, not the content: callers need `content_as[dict]`,
    and `CouchbaseSessionStore` also needs `result.cas`.

    Caller obligation: `await self._ensure_connected()` FIRST — this touches a handle
    (invariant 1). It is a free function, not a method, precisely so it cannot be
    mistaken for a gated entry point.
    """
    try:
        return await collection.get(key, GetOptions())
    except DocumentNotFoundException:
        return None


class CouchbaseStoreBase(CouchbaseConnectGate):
    """The construction half of a Couchbase-backed store: cluster, bucket, TTL.

    Five stores (sessions, candidates, audit, user knowledge, blueprint corpus)
    repeated the same `__init__` body — availability guard, `cluster or Cluster(...)`
    with a `PasswordAuthenticator`, bucket + collection derivation, `_init_connect_gate`,
    TTL — against five different settings objects. The differences are all DATA
    (credentials, bucket name, TTL semantics) except one: which collections the store
    opens, which is `_bind_collections`.

    The `cluster=` parameter every store exposes is a TEST SEAM (unit suites inject a
    fake handle graph), and that path stays EAGER: an injected cluster is already
    constructed, so its collections are derived in `__init__` exactly as before —
    a fake whose `collection()` is a one-shot `side_effect` still sees the same two
    calls, in the same order, at the same moment. Only the settings-derived cluster
    is deferred, because only it needs a running event loop.
    """

    # `_cluster` is None between `__init__` and the first connect on the lazy path.
    _cluster: Any
    _bucket_name: str
    _ttl: timedelta | None

    def _init_couchbase_store(
        self,
        *,
        cluster: Any,
        connection_string: str,
        username: str,
        password: str,
        bucket: str,
        ttl_seconds: int,
        ttl_none_when_not_positive: bool = False,
    ) -> None:
        """Called from a store's `__init__`, AFTER it has set the attributes its own
        `_bind_collections` reads (its settings object). Does no I/O when *cluster* is
        None: it only records what the first connect will need."""
        if not COUCHBASE_AVAILABLE:
            raise RuntimeError(
                "The 'couchbase' package is not installed. "
                f"Install it (see pyproject.toml) to use {type(self).__name__}."
            )
        self._bucket_name = bucket
        self._connection_string = connection_string
        self._username = username
        self._password = password
        self._ttl = couchbase_ttl(ttl_seconds, none_when_not_positive=ttl_none_when_not_positive)
        # Empty until the handles exist; `close()` reads this as "never built".
        self._connect_targets = ()
        self._connected = False
        self._cluster = cluster
        if cluster is not None:
            self._open_handles()

    def _bind_collections(self, bucket: Any) -> None:
        """Open the collections this store works in. Default: the bucket's default
        collection (KV-only stores). Overridden by stores with named scopes."""
        self._collection = bucket.default_collection()

    def _open_handles(self) -> None:
        """Derive bucket + collections from `self._cluster` and register the connect
        targets. Synchronous — the SDK's handle graph is sync to construct; only the
        connect is async."""
        bucket = self._cluster.bucket(self._bucket_name)
        self._bind_collections(bucket)
        self._init_connect_gate(self._cluster, bucket)

    async def _ensure_connected(self) -> None:
        """The gate, plus the lazy CONSTRUCTION step in front of it.

        No `await` separates the None-check from the assignment, so two concurrent
        first-callers cannot both build a cluster: the whole block runs to completion
        before the event loop can switch.

        `_cluster` ends up non-None only when the WHOLE build succeeded — cluster and
        handles. A partial build must not be kept: with `_cluster` set but
        `_connect_targets` still empty, the gate below would find nothing to await,
        declare itself connected, and every later call would `AttributeError` on a
        collection that was never bound, with `close()` leaking the cluster it could
        no longer see. So a failure anywhere in here unwinds to "never built" and the
        next call retries — the same posture invariant 3 gives a failed connect.
        """
        if self._cluster is None:
            self._cluster = Cluster(
                self._connection_string,
                ClusterOptions(PasswordAuthenticator(self._username, self._password)),
            )
            try:
                self._open_handles()
            except BaseException:
                self._cluster = None
                raise
        await super()._ensure_connected()
